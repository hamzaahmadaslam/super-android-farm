"""Read and change the state of the Androids themselves: identity-adjacent settings
(language, time zone, location), display and input conveniences, and a few probes.

Every setter exists twice: a plain function that takes an Adb handle (so the instance
manager's post-boot step and tests can call it on one device), and a Device method that
runs it across a selector as a Task.

What was verified on a container Android 13 and why each approach was chosen:

    locale     `persist.sys.locale` + `settings system_locales`, then a framework restart.
               Android has no shell command that applies a system locale live; the restart
               (`am restart`, root) takes about 7 s and keeps the container and ADB up.
    timezone   `cmd alarm set-timezone <Olson id>`: live, no restart, survives reboot.
    location   test providers: allow the `android:mock_location` app-op for the shell,
               then `cmd location providers add-test-provider / set-test-provider-location`
               for gps and network. Live, but held in memory by the framework: it must be
               set again after every (re)start. Needs Android 12+ (older shells have no
               such command).
    clipboard  there is no clipboard shell command, so the clip is written with a raw
               binder call (`service call clipboard`). The argument layout follows
               Android's ClipData and is version-specific: verified on Android 13,
               used unverified on 12 and 14+, always confirmed by reading the clip back.
    soft reboot  `sys.boot_completed` stays 1 across a framework restart, so readiness is
               judged by a *new* system_server answering and an activity being resumed.
"""
from __future__ import annotations

import functools
import re
import struct
import time
import zoneinfo
from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..core.adb import Adb, AdbError
from ..core.tasks import Task, TaskManager
from .apps import check_package
from .shell import fleet, reach, root_sh, selector_text, sh

if TYPE_CHECKING:
    from ..core.instances import InstanceManager

MAX_CLIPBOARD_BYTES = 4096
MAX_LOGCAT_LINES = 5000
_SETTINGS_FLUSH_SECONDS = 2.5
_LOCALE = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|\d{3}))?\Z")
_TIMEZONE = re.compile(r"^[A-Za-z0-9_+\-]{1,32}(/[A-Za-z0-9_+\-]{1,32}){0,2}\Z")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*\Z")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]{0,31}:[^\s\x00-\x1f\x7f]{1,4000}\Z")
_TAG = re.compile(r"^[^\s:*]{1,80}\Z")
_PRIORITIES = ("V", "D", "I", "W", "E", "F")
_BATTERY_STATUS = {1: "unknown", 2: "charging", 3: "discharging", 4: "not charging", 5: "full"}
_PROPS = {
    "model": "ro.product.model", "manufacturer": "ro.product.manufacturer", "brand": "ro.product.brand",
    "device": "ro.product.device", "android": "ro.build.version.release", "sdk": "ro.build.version.sdk",
    "abis": "ro.product.cpu.abilist", "fingerprint": "ro.build.fingerprint", "serial": "ro.serialno",
    "timezone": "persist.sys.timezone", "native_bridge": "ro.dalvik.vm.native.bridge", "renderer": "ro.hardware.egl",
}
_INFO_PROBE = (
    'echo "@@size"; wm size; echo "@@density"; wm density; echo "@@android_id"; settings get secure android_id; '
    'echo "@@locales"; settings get system system_locales; echo "@@uptime"; cat /proc/uptime; '
    'echo "@@df"; df -k /data; echo "@@cgroup"; cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max 2>/dev/null; '
    'echo "@@meminfo"; grep -E "^(MemTotal|MemAvailable):" /proc/meminfo; echo "@@battery"; dumpsys battery; '
    'echo "@@proxy"; settings get global http_proxy; echo "@@night"; cmd uimode night; '
    'echo "@@font"; settings get system font_scale; echo "@@anim"; settings get global animator_duration_scale; '
    'echo "@@props"; getprop'
)


# --------------------------------------------------------------------------- validation

def check_locale(tag: str) -> str:
    tag = str(tag).strip().replace("_", "-")
    if not _LOCALE.match(tag):
        raise ValueError(f"bad locale {tag[:40]!r}: use a language tag such as en-US, de-DE or sr-Latn-RS")
    return tag


def check_timezone(name: str) -> str:
    """Android silently falls back to GMT when handed a zone it does not know, so the
    name is checked against the host's zone database before it is sent."""
    name = str(name).strip()
    known = _known_timezones()
    if not _TIMEZONE.match(name) or (known and name not in known):
        raise ValueError(f"unknown time zone {name[:60]!r}: use an Olson id such as Europe/Berlin")
    return name


@functools.lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    try:
        return frozenset(zoneinfo.available_timezones())
    except Exception:  # noqa: BLE001 - a host without a zone database only loses the extra check
        return frozenset()


def check_geo(lat: float, lon: float) -> tuple[float, float]:
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):     # also false for NaN
        raise ValueError(f"bad coordinates {lat},{lon}: latitude is -90..90, longitude -180..180")
    return lat, lon


def check_proxy(value: str) -> str:
    """`host:port`, or "" to switch the proxy off."""
    value = str(value or "").strip()
    if not value:
        return ""
    host, _, port = value.rpartition(":")
    if not _HOST.match(host) or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"bad proxy {value[:80]!r}: expected host:port (no scheme, no credentials)")
    return f"{host}:{int(port)}"


def check_url(url: str) -> str:
    url = str(url).strip()
    if not _URL.match(url):
        raise ValueError(f"bad URL {url[:80]!r}: it needs a scheme (https://, market://, an app's own) and no spaces")
    return url


# --------------------------------------------------------------------------- info

def parse_info(text: str) -> dict:
    """Output of the fixed probe in info() -> a flat description of the device."""
    sections: dict[str, list[str]] = {}
    name = ""
    for line in text.splitlines():
        if line.startswith("@@"):
            name = line[2:].strip()
            sections[name] = []
        elif name:
            sections[name].append(line)

    def first(section: str) -> str:
        value = next((ln.strip() for ln in sections.get(section, []) if ln.strip()), "")
        return "" if value == "null" else value

    props = dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]$", "\n".join(sections.get("props", [])), re.M))
    out: dict = {key: props.get(prop, "") for key, prop in _PROPS.items()}
    out["sdk"] = int(out["sdk"]) if out["sdk"].isdigit() else 0
    out["abis"] = [a for a in out["abis"].split(",") if a]
    out["native_bridge"] = out["native_bridge"] not in ("", "0")
    out["locale"] = first("locales") or props.get("persist.sys.locale") or props.get("ro.product.locale", "")
    out["android_id"] = first("android_id")

    size = re.search(r"(\d+)x(\d+)", " ".join(sections.get("size", [])[-1:]))   # an override, when set, is printed last
    out["width"], out["height"] = (int(size.group(1)), int(size.group(2))) if size else (0, 0)
    densities = re.findall(r"density:\s*(\d+)", " ".join(sections.get("density", [])))
    out["density"] = int(densities[-1]) if densities else 0

    # /proc/uptime is the *host's* uptime inside a container; the Android's own age is that
    # minus the moment (on the same clock) its init started.
    since_boot = re.match(r"([\d.]+)", first("uptime"))
    init_ns = props.get("ro.boottime.init", "")
    age = float(since_boot.group(1)) - int(init_ns) / 1e9 if since_boot and init_ns.isdigit() else -1
    out["uptime_seconds"] = round(age) if age >= 0 else None

    df = [ln.split() for ln in sections.get("df", []) if ln.strip() and not ln.startswith("Filesystem")]
    out["storage"] = ({"total_mb": int(df[0][1]) // 1024, "used_mb": int(df[0][2]) // 1024, "free_mb": int(df[0][3]) // 1024}
                      if df and len(df[0]) >= 4 and all(c.isdigit() for c in df[0][1:4]) else None)

    # The container's own memory accounting; /proc/meminfo would show the whole host.
    cgroup = [ln.strip() for ln in sections.get("cgroup", []) if ln.strip()]
    meminfo = dict(re.findall(r"^(\w+):\s+(\d+)", "\n".join(sections.get("meminfo", [])), re.M))
    if cgroup and cgroup[0].isdigit():
        limit = int(cgroup[1]) >> 20 if len(cgroup) > 1 and cgroup[1].isdigit() else None
        out["memory"] = {"used_mb": int(cgroup[0]) >> 20, "limit_mb": limit}
    elif "MemTotal" in meminfo:
        total, avail = int(meminfo["MemTotal"]) // 1024, int(meminfo.get("MemAvailable", 0)) // 1024
        out["memory"] = {"used_mb": total - avail, "limit_mb": total}
    else:
        out["memory"] = None

    battery = dict(re.findall(r"^\s*([A-Za-z ]+):\s*(\S+)\s*$", "\n".join(sections.get("battery", [])), re.M))
    status = battery.get("status", "")
    out["battery"] = {"level": int(battery["level"]) if battery.get("level", "").isdigit() else None,
                      "status": _BATTERY_STATUS.get(int(status), "unknown") if status.isdigit() else "unknown",
                      "plugged": battery.get("AC powered") == "true" or battery.get("USB powered") == "true",
                      "present": battery.get("present") == "true"}
    proxy = first("proxy")
    out["proxy"] = "" if proxy in ("", ":0") else proxy
    out["dark_mode"] = "yes" in first("night").lower()
    font = first("font")
    out["font_scale"] = float(font) if re.fullmatch(r"\d+(\.\d+)?", font) else 1.0
    out["animations"] = first("anim") not in ("0", "0.0")
    return out


def info(adb: Adb) -> dict:
    """Model, Android version, screen, identity, uptime, storage, memory, battery (which a
    container fakes), and the app in the foreground. Two ADB round trips."""
    out = parse_info(adb.shell(_INFO_PROBE, timeout=30, check=False))
    package, activity = adb.current_app()
    out["foreground"] = {"package": package, "activity": activity}
    return out


# --------------------------------------------------------------------------- restart

def soft_restart(adb: Adb, *, timeout: float = 120) -> float:
    """Restart the Android framework (zygote, system_server, all apps) without touching the
    container: the network namespace, the VPN and ADB stay up. Returns the seconds it took."""
    # Android writes changed settings to disk up to 2 s after the change. This restart
    # kills the framework outright, and a setting changed just before it (the locale, for
    # one) was observed to be lost: let pending writes land first.
    time.sleep(_SETTINGS_FLUSH_SECONDS)
    before = adb.shell("pidof system_server", timeout=10, check=False).strip()
    started = time.monotonic()
    out = root_sh(adb, ["am", "restart"], timeout=30)
    # Success looks like a failure here: the command's own connection dies with the
    # framework ("Broken pipe"). Only a permission refusal means nothing happened.
    if "SecurityException" in out.text or "not found" in out.stderr:
        raise AdbError(f"framework restart refused: {out.last} (it needs root: an image where `adb shell` is root or `su` exists)")
    deadline = started + timeout
    while time.monotonic() < deadline:
        time.sleep(1.0)
        try:
            now = adb.shell("pidof system_server", timeout=10, check=False).strip()
            if now and now != before and sh(adb, "pm path android", timeout=10).stdout.startswith("package:") \
                    and adb.current_app()[0]:
                return round(time.monotonic() - started, 1)
        except AdbError:
            continue    # commands time out while the framework is coming up; that is the wait
    raise AdbError(f"framework did not come back within {timeout:g}s after a soft restart")


# --------------------------------------------------------------------------- setters (one device)

def set_locale(adb: Adb, locale: str, *, restart: bool = True) -> str:
    tag = check_locale(locale)
    adb.setting_put("system", "system_locales", tag)
    sh(adb, ["setprop", "persist.sys.locale", tag], timeout=10)
    if adb.getprop("persist.sys.locale") != tag:      # some builds only let root write persist.*
        root_sh(adb, ["setprop", "persist.sys.locale", tag], timeout=10)
    if adb.getprop("persist.sys.locale") != tag:
        raise AdbError(f"could not set persist.sys.locale to {tag} (the property is read-only on this image)")
    if not restart:
        return f"locale {tag} saved; it applies at the next restart"
    took = soft_restart(adb)
    # First line is e.g. `config: de-rDE-ldltr-...` (or `b+sr+Latn+RS-...` for script tags).
    active = (sh(adb, "am get-config", timeout=15).stdout.splitlines() or [""])[0].replace("config:", "").strip()
    if tag.split("-")[0] not in re.split(r"[-+]", active)[:2]:
        raise AdbError(f"locale {tag} was saved but the running configuration is {active[:40] or 'unreadable'}")
    return f"locale {tag} active (framework restarted in {took:g}s)"


def set_timezone(adb: Adb, timezone: str) -> str:
    zone = check_timezone(timezone)
    out = sh(adb, ["cmd", "alarm", "set-timezone", zone], timeout=15)
    if adb.getprop("persist.sys.timezone") == zone:
        return f"time zone {zone}"
    if out.ok:
        raise AdbError(f"Android did not accept the time zone {zone} (its zone database may be older than the host's)")
    # Android before 12 has no such command; the property alone applies after a restart.
    root_sh(adb, ["setprop", "persist.sys.timezone", zone], timeout=10)
    if adb.getprop("persist.sys.timezone") != zone:
        raise AdbError(f"could not set the time zone to {zone}: {out.last}")
    return f"time zone {zone} saved; it applies at the next restart"


def set_geo(adb: Adb, lat: float, lon: float, *, accuracy: float = 10.0) -> str:
    """Make the Android report a fixed location (see the module notes for the method and
    its limits). Call again after every restart of the Android."""
    lat, lon = check_geo(lat, lon)
    if not 0 < float(accuracy) <= 10000:
        raise ValueError("accuracy is in metres, between 0 and 10000")
    # The shell runs as `com.android.shell` on some images and as root on others; each
    # needs the app-op under its own name, and granting the absent one is a harmless error.
    for who in ("com.android.shell", "root"):
        sh(adb, ["appops", "set", who, "android:mock_location", "allow"], timeout=15)
    sh(adb, ["cmd", "location", "set-location-enabled", "true"], timeout=15)
    where = f"{lat:.6f},{lon:.6f}"
    for provider in ("gps", "network"):
        sh(adb, ["cmd", "location", "providers", "add-test-provider", provider], timeout=15)   # "already exists" is fine
        enabled = sh(adb, ["cmd", "location", "providers", "set-test-provider-enabled", provider, "true"], timeout=15)
        placed = sh(adb, ["cmd", "location", "providers", "set-test-provider-location", provider,
                          "--location", where, "--accuracy", f"{float(accuracy):g}"], timeout=15)
        if not (enabled.ok and placed.ok):
            sdk = adb.getprop("ro.build.version.sdk")
            why = "mock locations from the shell need Android 12 or newer" if sdk.isdigit() and int(sdk) < 31 else placed.last
            raise AdbError(f"could not set a mock location: {why}")
    return f"location {where} (gps + network test providers)"


def clear_geo(adb: Adb) -> str:
    for provider in ("gps", "network"):
        sh(adb, ["cmd", "location", "providers", "remove-test-provider", provider], timeout=15)
    return "mock location removed"


def set_proxy(adb: Adb, proxy: str) -> str:
    """Android's global HTTP proxy. Apps that open raw sockets ignore it; it is a
    convenience for HTTP(S) clients, not a tunnel."""
    value = check_proxy(proxy)
    # `:0` is how Android spells "no proxy"; deleting the key leaves a cached proxy active
    # until the next reboot.
    adb.setting_put("global", "http_proxy", value or ":0")
    if adb.setting_get("global", "http_proxy") != (value or ":0"):
        raise AdbError("the proxy setting did not stick")
    return f"proxy {value}" if value else "proxy off"


def set_animations(adb: Adb, enabled: bool) -> str:
    for key in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
        adb.setting_put("global", key, "1" if enabled else "0")
    return "animations on" if enabled else "animations off"


def set_font_scale(adb: Adb, scale: float) -> str:
    scale = float(scale)
    if not 0.5 <= scale <= 2.0:
        raise ValueError("font scale must be between 0.5 and 2.0 (1.0 is the default)")
    adb.setting_put("system", "font_scale", f"{scale:.2f}")
    return f"font scale {scale:.2f}"


def set_dark_mode(adb: Adb, enabled: bool) -> str:
    out = sh(adb, ["cmd", "uimode", "night", "yes" if enabled else "no"], timeout=15)
    if not out.ok or "Night mode" not in out.stdout:
        raise AdbError(f"could not switch dark mode: {out.last}")
    return "dark mode on" if enabled else "dark mode off"


def wake(adb: Adb) -> str:
    """Screen on, lock screen dismissed, and kept on (a sleeping screen stops automation
    and shows black in the live view)."""
    adb.key("WAKEUP")
    sh(adb, "wm dismiss-keyguard", timeout=15)
    sh(adb, "svc power stayon true", timeout=15)
    return "awake"


def set_battery(adb: Adb, level: int | None = None, charging: bool | None = None) -> str:
    """Fake the battery state apps see (containers have no battery). level=None and
    charging=None hands the values back to the system."""
    if level is None and charging is None:
        sh(adb, "dumpsys battery reset", timeout=15)
        return "battery state reset"
    if level is not None:
        if not 0 <= int(level) <= 100:
            raise ValueError("battery level is 0-100")
        sh(adb, ["dumpsys", "battery", "set", "level", str(int(level))], timeout=15)
    if charging is not None:
        sh(adb, ["dumpsys", "battery", "set", "ac", "1" if charging else "0"], timeout=15)
        sh(adb, ["dumpsys", "battery", "set", "status", "2" if charging else "3"], timeout=15)
    return "battery " + ", ".join(p for p in (f"{level}%" if level is not None else "",
                                              ("charging" if charging else "discharging") if charging is not None else "") if p)


def open_url(adb: Adb, url: str, package: str | None = None) -> str:
    argv = ["am", "start", "-a", "android.intent.action.VIEW", "-d", check_url(url)]
    if package:
        argv += ["-p", check_package(package)]
    out = sh(adb, argv, timeout=30)
    if not out.ok or "Error" in out.text:
        raise AdbError(f"nothing on the device opens this link: {out.last}")
    return "opened"


def logcat(adb: Adb, lines: int = 200, *, priority: str | None = None, tags: Iterable[str] | None = None,
           package: str | None = None, grep: str | None = None) -> str:
    """The last `lines` log lines, optionally narrowed by minimum priority (V D I W E F),
    by tags, by the processes of one package, and by a plain substring."""
    lines = max(1, min(int(lines), MAX_LOGCAT_LINES))
    level = (priority or "V").upper()
    if level not in _PRIORITIES:
        raise ValueError(f"bad log priority {priority!r}: one of {' '.join(_PRIORITIES)}")
    tag_list = list(tags or [])
    for tag in tag_list:
        if not _TAG.match(tag):
            raise ValueError(f"bad log tag {tag[:40]!r}")
    # `-t N` counts lines *before* tag and priority filters are applied (those run in the
    # logcat client), so a filtered request reads a deeper tail and is trimmed here.
    filtered = bool(tag_list) or level != "V" or bool(grep)
    depth = min(MAX_LOGCAT_LINES * 4, max(lines * 25, 1000)) if filtered else lines
    argv = ["logcat", "-d", "-v", "threadtime", "-t", str(depth)]
    if package:
        pids = adb.shell(["pidof", check_package(package)], timeout=10, check=False).split()
        if not pids or not pids[0].isdigit():
            return ""       # not running: it has no log lines to show
        argv.append(f"--pid={pids[0]}")
    argv += [f"{t}:{level}" for t in tag_list] + (["*:S"] if tag_list else [f"*:{level}"])
    kept = [ln for ln in adb.shell(argv, timeout=30, check=False).splitlines()
            if not ln.startswith("--------- beginning of") and (not grep or grep in ln)]
    return "\n".join(kept[-lines:])


# --------------------------------------------------------------------------- clipboard

def _string8_words(text: bytes) -> list[int]:
    """A Parcel String8 as 32-bit words: byte length, then the bytes, NUL-terminated and
    padded to a word boundary."""
    raw = text + b"\x00"
    raw += b"\x00" * (-len(raw) % 4)
    return [len(text), *(w for (w,) in struct.iter_unpack("<i", raw))]


def clipboard_set_args(text: str, sdk: int, package: str = "com.android.shell") -> list[str]:
    """Arguments after `service call clipboard 1` that spell IClipboard.setPrimaryClip
    with one plain-text item. `service call` can only write ints and UTF-16 strings, so
    the UTF-8 text is packed into ints."""

    def ints(*values: int) -> list[str]:
        return [part for v in values for part in ("i32", str(v))]

    args = ints(1,          # a ClipData follows
                1, -1,      # label: plain CharSequence, null
                1)          # one MIME type:
    args += ["s16", "text/plain"]
    args += ints(-1)        # no extras
    args += ["i64", "0"]    # timestamp (the service stamps its own)
    args += ints(0, 1, 0,   # not styled, classification "not complete", empty confidence bundle
                 0,         # no icon
                 1,         # one item:
                 1, *_string8_words(text.encode("utf-8")),     # its text
                 -1, 0, 0, 0, 0)                               # no html, intent, uri, activity info, text links
    args += ["s16", package]
    if sdk >= 34:
        args += ints(-1)    # attribution tag: null (parameter added in Android 14)
    args += ints(0)         # user 0
    if sdk >= 34:
        args += ints(0)     # default device id (Android 14)
    return args


def parse_parcel(text: str) -> bytes:
    """The hex dump `service call` prints -> the reply's bytes."""
    words: list[int] = []
    for line in text.splitlines():
        line = line.split("'", 1)[0].replace("Result: Parcel(", "")
        line = re.sub(r"^\s*0x[0-9a-fA-F]{8}:", "", line)
        words += [int(w, 16) for w in re.findall(r"\b[0-9a-fA-F]{8}\b", line)]
    return struct.pack(f"<{len(words)}I", *words)


def clip_text(reply: bytes) -> str | None:
    """The first item's text from a getPrimaryClip reply, or None (no clip, or a layout
    this reader does not know)."""
    pos = 0

    def i32() -> int:
        nonlocal pos
        (v,) = struct.unpack_from("<i", reply, pos)
        pos += 4
        return v

    def skip_bundle() -> None:
        nonlocal pos
        length = i32()
        if length > 0:
            pos += 4 + length

    def string8() -> str | None:
        nonlocal pos
        n = i32()
        if n < 0:
            return None
        raw = reply[pos:pos + n]
        pos += (n + 4) & ~3
        return raw.decode("utf-8", "replace")

    try:
        if i32() != 0 or i32() != 1:        # exception code, "ClipData present"
            return None
        i32()
        string8()                           # label
        for _ in range(i32()):              # MIME types (UTF-16)
            n = i32()
            if n >= 0:
                pos += (2 * (n + 1) + 3) & ~3
        skip_bundle()                       # extras
        pos += 8                            # timestamp
        i32()
        i32()                               # styled, classification
        skip_bundle()                       # confidences
        if i32() != 0 or i32() < 1:         # icon (not handled), item count
            return None
        i32()
        return string8()
    except struct.error:
        return None


def get_clipboard(adb: Adb) -> str | None:
    sdk = adb.getprop("ro.build.version.sdk")
    extra = ["i32", "-1"] if sdk.isdigit() and int(sdk) >= 34 else []
    tail = ["i32", "0"] if sdk.isdigit() and int(sdk) >= 34 else []
    out = sh(adb, ["service", "call", "clipboard", "4", "s16", "com.android.shell", *extra, "i32", "0", *tail], timeout=15)
    return clip_text(parse_parcel(out.stdout)) if out.ok else None


def set_clipboard(adb: Adb, text: str) -> str:
    text = str(text)
    if not text or len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES or "\x00" in text:
        raise ValueError(f"clipboard text must be 1-{MAX_CLIPBOARD_BYTES} bytes of UTF-8 without NUL")
    sdk_text = adb.getprop("ro.build.version.sdk")
    sdk = int(sdk_text) if sdk_text.isdigit() else 0
    if sdk < 31:
        raise AdbError("setting the clipboard from outside needs Android 12 or newer "
                       "(older versions use a different binder layout that is not implemented)")
    out = sh(adb, ["service", "call", "clipboard", "1", *clipboard_set_args(text, sdk)], timeout=20)
    if not out.ok:
        raise AdbError(f"clipboard call failed: {out.last}")
    if get_clipboard(adb) != text:
        raise AdbError(f"the clipboard did not take the text on this Android (API {sdk}); the binder layout "
                       "is verified for API 33 only")
    return f"clipboard set ({len(text)} characters)"


# --------------------------------------------------------------------------- the fleet API

class Device:
    def __init__(self, mgr: InstanceManager, tasks: TaskManager):
        self.mgr, self.tasks = mgr, tasks

    def _fleet(self, kind: str, title: str, selector: str | int | Iterable[int], fn, *, heavy: bool = False) -> Task:
        return fleet(self.mgr, self.tasks, kind, title, selector, lambda _id, adb, _t: fn(adb), heavy=heavy)

    # reads (one device, immediate)
    def info(self, id: int) -> dict:
        return info(reach(self.mgr, id))

    def screenshot(self, id: int) -> bytes:
        """The screen as PNG bytes."""
        return reach(self.mgr, id).screencap_png()

    def logcat(self, id: int, lines: int = 200, *, priority: str | None = None, tags: Iterable[str] | None = None,
               package: str | None = None, grep: str | None = None) -> str:
        return logcat(reach(self.mgr, id), lines, priority=priority, tags=tags, package=package, grep=grep)

    def clipboard(self, id: int) -> str | None:
        return get_clipboard(reach(self.mgr, id))

    # changes (fleet, as Tasks)
    def set_locale(self, selector: str | int | Iterable[int], locale: str, *, restart: bool = True) -> Task:
        tag = check_locale(locale)
        return self._fleet("device-locale", f"Set locale {tag}", selector, lambda adb: set_locale(adb, tag, restart=restart),
                           heavy=restart)

    def set_timezone(self, selector: str | int | Iterable[int], timezone: str) -> Task:
        zone = check_timezone(timezone)
        return self._fleet("device-timezone", f"Set time zone {zone}", selector, lambda adb: set_timezone(adb, zone))

    def set_geo(self, selector: str | int | Iterable[int], lat: float | None, lon: float | None = None, *,
                accuracy: float = 10.0) -> Task:
        """lat=None removes the mock location."""
        if lat is None:
            return self._fleet("device-geo", "Remove mock location", selector, clear_geo)
        lat, lon = check_geo(lat, lon if lon is not None else float("nan"))
        return self._fleet("device-geo", f"Set location {lat:.4f},{lon:.4f}", selector,
                           lambda adb: set_geo(adb, lat, lon, accuracy=accuracy))

    def set_proxy(self, selector: str | int | Iterable[int], proxy: str) -> Task:
        value = check_proxy(proxy)
        return self._fleet("device-proxy", f"Set proxy {value or 'off'}", selector, lambda adb: set_proxy(adb, value))

    def set_clipboard(self, selector: str | int | Iterable[int], text: str) -> Task:
        if not text or len(str(text).encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            raise ValueError(f"clipboard text must be 1-{MAX_CLIPBOARD_BYTES} bytes of UTF-8")
        # The title never carries the text: clipboards hold passwords.
        return self._fleet("device-clipboard", "Set clipboard", selector, lambda adb: set_clipboard(adb, text))

    def set_animations(self, selector: str | int | Iterable[int], enabled: bool) -> Task:
        return self._fleet("device-animations", f"Animations {'on' if enabled else 'off'}", selector,
                           lambda adb: set_animations(adb, enabled))

    def set_font_scale(self, selector: str | int | Iterable[int], scale: float) -> Task:
        if not 0.5 <= float(scale) <= 2.0:
            raise ValueError("font scale must be between 0.5 and 2.0 (1.0 is the default)")
        return self._fleet("device-font", f"Font scale {float(scale):.2f}", selector, lambda adb: set_font_scale(adb, scale))

    def set_dark_mode(self, selector: str | int | Iterable[int], enabled: bool) -> Task:
        return self._fleet("device-dark", f"Dark mode {'on' if enabled else 'off'}", selector,
                           lambda adb: set_dark_mode(adb, enabled))

    def set_battery(self, selector: str | int | Iterable[int], level: int | None = None, charging: bool | None = None) -> Task:
        if level is not None and not 0 <= int(level) <= 100:
            raise ValueError("battery level is 0-100")
        return self._fleet("device-battery", "Set battery state", selector, lambda adb: set_battery(adb, level, charging))

    def wake(self, selector: str | int | Iterable[int]) -> Task:
        return self._fleet("device-wake", "Wake and unlock", selector, wake)

    def open_url(self, selector: str | int | Iterable[int], url: str, *, package: str | None = None) -> Task:
        link = check_url(url)
        if package:
            check_package(package)
        return self._fleet("device-url", f"Open {link[:60]}", selector, lambda adb: open_url(adb, link, package))

    def reboot(self, selector: str | int | Iterable[int], *, mode: str = "soft", force: bool = False) -> Task:
        """soft: restart the Android framework inside the running container (seconds; the
        network and VPN are untouched). container: restart the instance through the
        instance manager, which restarts the network sidecar with it and honours the
        farm's start concurrency and capacity rules (`force` is passed on to it)."""
        if mode == "container":
            return self.mgr.bulk("restart", selector_text(selector), **({"force": True} if force else {}))
        if mode != "soft":
            raise ValueError("reboot mode is `soft` or `container`")
        timeout = float(getattr(getattr(self.mgr.config, "performance", None), "boot_timeout", 180))
        return self._fleet("device-reboot", "Soft reboot", selector,
                           lambda adb: f"framework restarted in {soft_restart(adb, timeout=timeout):g}s", heavy=True)
