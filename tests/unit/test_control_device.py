"""Device state: validation, the info probe, the clipboard binder call, restarts."""
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

from superfarm.control import device as dev_mod
from superfarm.control.device import Device
from superfarm.core.adb import AdbError
from superfarm.core.tasks import TaskManager


def load_support():
    name = "sf_control_support"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_control_support.py"))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


S = load_support()

# Captured from an Android 13 container (property list trimmed).
INFO = """@@size
Physical size: 480x854
Override size: 720x1280
@@density
Physical density: 160
Override density: 320
@@android_id
7e034ae01dae55c2
@@locales
de-DE
@@uptime
8688.14 242915.30
@@df
Filesystem     1K-blocks     Used Available Use% Mounted on
overlay        187743392 72278856 105854912  41% /data
@@cgroup
753807360
1610612736
@@meminfo
MemTotal:       32038784 kB
MemAvailable:   20879448 kB
@@battery
Current Battery Service state:
  AC powered: true
  USB powered: false
  status: 2
  present: false
  level: 77
  scale: 100
  technology:
@@proxy
10.0.0.9:3128
@@night
Night mode: yes
@@font
1.15
@@anim
0
@@props
[ro.product.model]: [redroid13_x86_64]
[ro.product.manufacturer]: [redroid]
[ro.build.version.release]: [13]
[ro.build.version.sdk]: [33]
[ro.product.cpu.abilist]: [x86_64,arm64-v8a,x86]
[ro.boottime.init]: [8666790662420]
[ro.dalvik.vm.native.bridge]: [libnb.so]
[persist.sys.timezone]: [Europe/Berlin]
[ro.build.fingerprint]: [redroid/redroid_x86_64/redroid_x86_64:13/TQ3A/eng:userdebug/test-keys]
[some.odd]: [value with ] bracket]
"""

# `service call clipboard 4 ...` on Android 13 after setting the clip to "hello clip".
CLIP_REPLY = """Result: Parcel(
  0x00000000: 00000000 00000001 00000001 ffffffff '................'
  0x00000010: 00000001 0000000a 00650074 00740078 '........t.e.x.t.'
  0x00000020: 0070002f 0061006c 006e0069 00000000 '/.p.l.a.i.n.....'
  0x00000030: ffffffff 2dbbb2e3 000001a1 00000000 '.......-........'
  0x00000040: 00000001 00000000 00000000 00000001 '................'
  0x00000050: 00000001 0000000a 6c6c6568 6c63206f '........hello cl'
  0x00000060: 00007069 ffffffff 00000000 00000000 'ip..............'
  0x00000070: 00000000 00000000                   '........        ')
"""


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(dev_mod.time, "sleep", lambda s: None)
    mgr, tasks = S.FakeManager(ids=(1, 2)), TaskManager(workers=4)
    yield mgr, tasks, Device(mgr, tasks)
    tasks.shutdown()


def done(tasks, task):
    tasks.wait(task, timeout=20)
    assert task.state == "done", (task.state, task.error)
    return task


# ----------------------------------------------------------------------------- validation

def test_locale_timezone_geo_proxy_url_validation():
    assert dev_mod.check_locale("en_US") == "en-US" and dev_mod.check_locale("sr-Latn-RS") == "sr-Latn-RS" and dev_mod.check_locale("fil") == "fil"
    for bad in ("", "english", "en-us", "EN-US", "en-US; reboot", "en\n-US", "$(id)"):
        with pytest.raises(ValueError):
            dev_mod.check_locale(bad)
    assert dev_mod.check_timezone("Europe/Berlin") == "Europe/Berlin" and dev_mod.check_timezone(" UTC ") == "UTC"
    for bad in ("Mars/Olympus", "Europe/Berlin; id", "../etc/passwd", "", "Europe/\nBerlin"):   # unknown zones silently become GMT on Android
        with pytest.raises(ValueError):
            dev_mod.check_timezone(bad)
    assert dev_mod.check_geo("52.52", 13.405) == (52.52, 13.405)
    for lat, lon in ((91, 0), (0, 181), (float("nan"), 0), (0, float("inf"))):
        with pytest.raises(ValueError):
            dev_mod.check_geo(lat, lon)
    assert dev_mod.check_proxy("proxy.example.com:03128") == "proxy.example.com:3128" and dev_mod.check_proxy("") == "" and dev_mod.check_proxy(None) == ""
    for bad in ("http://host:80", "host", "host:0", "host:70000", "user:pw@host:80", "ho st:80", "host:80; id", "-x:80"):
        with pytest.raises(ValueError):
            dev_mod.check_proxy(bad)
    assert dev_mod.check_url("https://example.com/a?b=1&c=$(x)") and dev_mod.check_url("market://details?id=org.example")
    for bad in ("example.com", "https://exa mple.com", "", "javascript:\nalert(1)", "://x"):
        with pytest.raises(ValueError):
            dev_mod.check_url(bad)


# ----------------------------------------------------------------------------- info

def test_parse_info():
    i = dev_mod.parse_info(INFO)
    assert (i["model"], i["manufacturer"], i["android"], i["sdk"]) == ("redroid13_x86_64", "redroid", "13", 33)
    assert i["abis"] == ["x86_64", "arm64-v8a", "x86"] and i["native_bridge"] and i["timezone"] == "Europe/Berlin"
    assert (i["width"], i["height"], i["density"]) == (720, 1280, 320)            # overrides win: they are what apps see
    assert i["android_id"] == "7e034ae01dae55c2" and i["locale"] == "de-DE"
    assert i["uptime_seconds"] == 21                                                # host uptime minus the container's start
    assert i["storage"] == {"total_mb": 183343, "used_mb": 70584, "free_mb": 103373}
    assert i["memory"] == {"used_mb": 718, "limit_mb": 1536}                        # the container's cgroup, not the host's RAM
    assert i["battery"] == {"level": 77, "status": "charging", "plugged": True, "present": False}
    assert i["proxy"] == "10.0.0.9:3128" and i["dark_mode"] and i["font_scale"] == 1.15 and not i["animations"]


def test_parse_info_survives_missing_sections():
    i = dev_mod.parse_info("@@size\n@@df\n@@cgroup\nmax\n@@meminfo\nMemTotal: 2048000 kB\nMemAvailable: 1024000 kB\n@@proxy\n:0\n@@font\nnull\n")
    assert (i["width"], i["density"], i["uptime_seconds"], i["storage"]) == (0, 0, None, None)
    assert i["memory"] == {"used_mb": 1000, "limit_mb": 2000} and i["proxy"] == "" and i["font_scale"] == 1.0 and i["animations"]
    assert i["battery"]["level"] is None and dev_mod.parse_info("")["model"] == ""


def test_info_screenshot_and_logcat(env):
    mgr, _, device = env
    dev = mgr.devices[1].on(r'^echo "@@size"', INFO)
    info = device.info(1)
    assert info["model"] == "redroid13_x86_64" and info["foreground"] == {"package": "com.android.launcher3", "activity": "com.android.launcher3.Launcher"}
    assert device.screenshot(1).startswith(b"\x89PNG")
    dev.on(r"^logcat ", "--------- beginning of main\nl1 keep\nl2 drop\nl3 keep\nl4 keep\n").on(r"^pidof org\.example\.app$", "4321 4400")
    assert device.logcat(1, 2, grep="keep") == "l3 keep\nl4 keep"
    assert dev.argv() == ["logcat", "-d", "-v", "threadtime", "-t", "1000", "*:V"]   # filtered: read deeper, trim here
    device.logcat(1, 50)
    assert dev.argv() == ["logcat", "-d", "-v", "threadtime", "-t", "50", "*:V"]
    device.logcat(1, 10, priority="w", tags=["ActivityManager", "My Tag".replace(" ", "_")], package="org.example.app")
    assert dev.argv() == ["logcat", "-d", "-v", "threadtime", "-t", "1000", "--pid=4321", "ActivityManager:W", "My_Tag:W", "*:S"]
    assert device.logcat(1, 10, package="org.not.running") == ""
    device.logcat(1, 10 ** 9)
    assert dev.argv()[5] == str(dev_mod.MAX_LOGCAT_LINES)
    for kwargs in ({"priority": "X"}, {"tags": ["a b"]}, {"tags": ["x:V"]}, {"package": "a;b"}):
        with pytest.raises(ValueError):
            device.logcat(1, 10, **kwargs)


# ----------------------------------------------------------------------------- setters

def test_timezone_set_and_fallbacks(env):
    mgr, tasks, device = env
    dev = mgr.devices[1].on(r"^cmd alarm set-timezone", lambda line: dev.props.__setitem__("persist.sys.timezone", line.split()[-1]) or "")
    assert done(tasks, device.set_timezone("1", "Asia/Tokyo")).results[1]["detail"] == "time zone Asia/Tokyo"
    old = mgr.devices[2].on(r"^cmd alarm", (255, "", "Unknown command: set-timezone")).on(r"^id -u$", "2000").on(
        r"^su 0 setprop persist\.sys\.timezone", lambda line: old.props.__setitem__("persist.sys.timezone", line.split()[-1]) or "")
    assert "applies at the next restart" in done(tasks, device.set_timezone("2", "Asia/Tokyo")).results[2]["detail"]
    stubborn = S.FakeAdb()                       # the command "succeeds" but the zone did not change
    with pytest.raises(AdbError, match="did not accept"):
        dev_mod.set_timezone(stubborn, "Asia/Tokyo")
    with pytest.raises(ValueError):
        device.set_timezone("all", "Not/AZone")


def test_geo_sets_test_providers_and_explains_old_androids(env):
    mgr, tasks, device = env
    task = done(tasks, device.set_geo("1", 52.52, 13.405, accuracy=5))
    assert task.results[1]["detail"] == "location 52.520000,13.405000 (gps + network test providers)"
    dev = mgr.devices[1]
    assert dev.ran(r"^appops set com\.android\.shell android:mock_location allow$") and dev.ran(r"^appops set root android:mock_location allow$")
    assert dev.ran(r"^cmd location set-location-enabled true$")
    assert dev.ran(r"set-test-provider-location") == [
        "cmd location providers set-test-provider-location gps --location 52.520000,13.405000 --accuracy 5",
        "cmd location providers set-test-provider-location network --location 52.520000,13.405000 --accuracy 5"]
    legacy = mgr.devices[2].on(r"^cmd location providers", (255, "", "Unknown command: providers"))
    legacy.props["ro.build.version.sdk"] = "30"
    assert "need Android 12 or newer" in done(tasks, device.set_geo("2", 1, 2)).results[2]["detail"]
    cleared = done(tasks, device.set_geo("1", None))
    assert cleared.results[1]["detail"] == "mock location removed" and len(dev.ran("remove-test-provider")) == 2
    for args in ((95, 0), (10, None)):
        with pytest.raises(ValueError):
            device.set_geo("1", *args)


def test_proxy_animations_font_dark_battery_wake_url(env):
    mgr, tasks, device = env
    dev = mgr.devices[1].on(r"^cmd uimode night", "Night mode: yes")
    assert done(tasks, device.set_proxy("1", "10.0.0.9:3128")).results[1]["detail"] == "proxy 10.0.0.9:3128"
    assert dev.settings[("global", "http_proxy")] == "10.0.0.9:3128"
    assert done(tasks, device.set_proxy("1", "")).results[1]["detail"] == "proxy off" and dev.settings[("global", "http_proxy")] == ":0"
    done(tasks, device.set_animations("1", False))
    assert {dev.settings[("global", k)] for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")} == {"0"}
    assert done(tasks, device.set_font_scale("1", 1.3)).results[1]["detail"] == "font scale 1.30" and dev.settings[("system", "font_scale")] == "1.30"
    assert done(tasks, device.set_dark_mode("1", True)).results[1]["detail"] == "dark mode on" and dev.argv() == ["cmd", "uimode", "night", "yes"]
    assert done(tasks, device.set_battery("1", 55, False)).results[1]["detail"] == "battery 55%, discharging"
    assert dev.ran(r"^dumpsys battery set level 55$") and dev.ran(r"^dumpsys battery set status 3$")
    assert done(tasks, device.set_battery("1")).results[1]["detail"] == "battery state reset"
    assert done(tasks, device.wake("1")).results[1]["detail"] == "awake" and dev.keys == ["WAKEUP"] and dev.ran("dismiss-keyguard")
    assert done(tasks, device.open_url("1", "https://example.com/?a=1&b=$(x)", package="org.example.app")).results[1]["ok"]
    assert dev.argv() == ["am", "start", "-a", "android.intent.action.VIEW", "-d", "https://example.com/?a=1&b=$(x)", "-p", "org.example.app"]
    dev.on(r"^am start", (0, "Starting: Intent", "Error: Activity not started, unable to resolve Intent"))
    assert "nothing on the device opens this link" in done(tasks, device.open_url("1", "odd://x")).results[1]["detail"]
    for call in (lambda: device.set_font_scale("1", 5), lambda: device.set_battery("1", 101), lambda: device.set_proxy("1", "nohost"),
                 lambda: device.open_url("1", "no scheme")):
        with pytest.raises(ValueError):
            call()


# ----------------------------------------------------------------------------- clipboard

def emulate_get(args: list[str]) -> str:
    """Turn `service call clipboard 1 <args>` back into the dump a later getPrimaryClip prints."""
    words, i = [0, ], 0
    payload: list[int] = []
    while i < len(args):
        kind, value = args[i], args[i + 1]
        i += 2
        if kind == "i32":
            payload.append(int(value) & 0xFFFFFFFF)
        elif kind == "i64":
            payload += [int(value) & 0xFFFFFFFF, int(value) >> 32]
        elif kind == "s16":
            raw = value.encode("utf-16-le") + b"\x00\x00"
            raw += b"\x00" * (-len(raw) % 4)
            payload += [len(value), *struct.unpack(f"<{len(raw) // 4}I", raw)]
    words += payload
    lines = ["Result: Parcel("]
    for n in range(0, len(words), 4):
        lines.append(f"  0x{4 * n:08x}: " + " ".join(f"{w:08x}" for w in words[n:n + 4]) + " '....'")
    return "\n".join(lines) + ")"


def test_clip_reply_from_a_real_device_is_decoded():
    assert dev_mod.clip_text(dev_mod.parse_parcel(CLIP_REPLY)) == "hello clip"
    assert dev_mod.clip_text(dev_mod.parse_parcel("Result: Parcel(00000000 00000000   '........')")) is None    # empty clipboard
    assert dev_mod.clip_text(b"") is None and dev_mod.clip_text(b"\x00" * 7) is None
    assert dev_mod.parse_parcel("Result: Parcel(00000001 0000000a   '..face..')") == struct.pack("<II", 1, 10)


@pytest.mark.parametrize("text", ["hi", "abc", "abcd", "héllo wörld ✓ 123", "line1\nline2 'quoted' $(x)", "x" * 1000])
def test_clipboard_arguments_round_trip_through_the_reply_decoder(text):
    args = dev_mod.clipboard_set_args(text, 33)
    assert args[-4:] == ["s16", "com.android.shell", "i32", "0"] and all(k in ("i32", "i64", "s16") for k in args[::2])
    assert dev_mod.clip_text(dev_mod.parse_parcel(emulate_get(args))) == text
    assert dev_mod.clipboard_set_args(text, 34)[-8:] == ["s16", "com.android.shell", "i32", "-1", "i32", "0", "i32", "0"]


def test_set_clipboard_verifies_by_reading_back(env):
    mgr, tasks, device = env
    store: dict = {}
    dev = mgr.devices[1].on(r"^service call clipboard 4 ", lambda line: store.get("reply", "Result: Parcel(00000000 00000000 '....')")).on(
        r"^service call clipboard 1 ", lambda line: store.__setitem__("reply", emulate_get(S.shlex.split(line)[4:])) or "Result: Parcel(00000000 '....')")
    task = done(tasks, device.set_clipboard("all", "pässword ✓"))
    assert task.title == "Set clipboard" and "pässword" not in str(task.to_dict()["title"])        # clipboards hold secrets
    assert task.results[1]["detail"] == "clipboard set (10 characters)" and device.clipboard(1) == "pässword ✓"
    assert not task.results[2]["ok"] and "did not take the text" in task.results[2]["detail"]     # device 2 swallowed the call
    dev.props["ro.build.version.sdk"] = "30"
    assert "needs Android 12 or newer" in done(tasks, device.set_clipboard("1", "x")).results[1]["detail"]
    for bad in ("", "x" * 5000):
        with pytest.raises(ValueError):
            device.set_clipboard("1", bad)


# ----------------------------------------------------------------------------- restart / locale

def restartable(dev, pids=("100", "", "200")):
    state = {"n": 0}

    def pidof(_line):
        state["n"] += 1
        return pids[min(state["n"] - 1, len(pids) - 1)]

    return dev.on(r"^pidof system_server$", pidof).on(r"^id -u$", "2000").on(
        r"^su 0 am restart$", (224, "Restart the system...\ncmd: Failure calling service activity: Broken pipe (32)", "")).on(
        r"^pm path android$", "package:/system/framework/framework-res.apk")


def test_soft_reboot_waits_for_a_new_framework(env):
    mgr, tasks, device = env
    restartable(mgr.devices[1])
    refused = mgr.devices[2].on(r"^pidof", "100").on(r"^id -u$", "2000").on(
        r"^su 0 am restart$", (255, "", "java.lang.SecurityException: Requires permission android.permission.SHUTDOWN"))
    task = done(tasks, device.reboot("all"))
    assert task.results[1]["ok"] and task.results[1]["detail"].startswith("framework restarted in")
    assert not task.results[2]["ok"] and "needs root" in task.results[2]["detail"] and refused.ran("am restart")


def test_soft_reboot_times_out_when_the_framework_stays_the_same(env, monkeypatch):
    mgr, _, _ = env
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(dev_mod.time, "monotonic", lambda: next(clock))
    with pytest.raises(AdbError, match="did not come back within 30s"):
        dev_mod.soft_restart(restartable(mgr.devices[1], pids=("100",)), timeout=30)


def test_container_reboot_goes_through_the_instance_manager(env):
    mgr, _, device = env
    assert device.reboot([2, 1], mode="container") == ("bulk", "restart", "2,1") and mgr.bulk_calls == [("restart", "2,1")]
    device.reboot("all", mode="container", force=True)
    assert mgr.bulk_calls[-1] == ("restart", "all", "force")
    with pytest.raises(ValueError):
        device.reboot("all", mode="hard")


def test_locale_is_saved_then_applied_by_a_restart(env):
    mgr, tasks, device = env
    dev = restartable(mgr.devices[1]).on(r"^setprop persist\.sys\.locale", lambda line: dev.props.__setitem__("persist.sys.locale", line.split()[-1]) or "").on(
        r"^am get-config$", "config: fr-rFR-ldltr-sw480dp\nabi: x86_64")
    task = done(tasks, device.set_locale("1", "fr_FR"))
    assert task.results[1]["detail"].startswith("locale fr-FR active (framework restarted in") and dev.settings[("system", "system_locales")] == "fr-FR"
    lazy = mgr.devices[2].on(r"^setprop persist\.sys\.locale", lambda line: lazy.props.__setitem__("persist.sys.locale", line.split()[-1]) or "")
    assert done(tasks, device.set_locale("2", "ja-JP", restart=False)).results[2]["detail"] == "locale ja-JP saved; it applies at the next restart"
    assert not lazy.ran("am restart")
    wrong = restartable(S.FakeAdb()).on(r"^am get-config$", "config: en-rUS-ldltr")
    wrong.on(r"^setprop persist\.sys\.locale", lambda line: wrong.props.__setitem__("persist.sys.locale", line.split()[-1]) or "")
    with pytest.raises(AdbError, match="saved but the running configuration is en-rUS"):
        dev_mod.set_locale(wrong, "fr-FR")
    readonly = S.FakeAdb().on(r"^id -u$", "0")
    with pytest.raises(AdbError, match="read-only"):
        dev_mod.set_locale(readonly, "fr-FR")
