"""What an Android tells apps about itself, and how that identity is put in place.

Two moments matter:

    boot        `ro.*` properties can only be given on the container's command line; they are
                read-only once Android runs. boot_props() turns an identity into those arguments.
    after boot  android_id, time zone, locale and mock location live in /data or in running
                services and are set through ADB by apply_post_boot().

An identity is generated once, when the instance is created, and then kept: apps tie accounts
to it, so it has to survive every rebuild of the container.
"""
from __future__ import annotations

import os
import re
import secrets
import string
from functools import lru_cache
from pathlib import Path
from collections.abc import Callable, Sequence

from ..config import Config
from ..paths import PACKAGE_DIR
from ..util.files import read_yaml
from .adb import Adb, AdbError
from .models import DeviceIdentity, Resolved, new_android_id

DEVICES_FILE = PACKAGE_DIR / "data" / "devices.yaml"

_PRODUCT_KEYS = ("brand", "manufacturer", "model", "device", "name")
# Properties that may be given at boot. Anything else is refused: the value ends up on a
# container command line, and a typo there fails silently inside Android.
_BOOT_KEY = re.compile(r"^(ro|androidboot)\.[A-Za-z0-9_.\-]{1,90}$")
_BOOT_VALUE = re.compile(r"^[\x21-\x7e]{1,255}$")         # printable ASCII and, above all, no spaces (see boot_props)
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")
_TIMEZONE = re.compile(r"^[A-Za-z0-9_+\-]{1,32}(/[A-Za-z0-9_+\-]{1,32}){0,2}$")
_ANDROID_ID = re.compile(r"^[0-9a-f]{16}$")

# Runs a command as root inside the Android container and returns its output. ADB's shell is
# an unprivileged user on these images, and a few settings (mock location) need root.
RootExec = Callable[[Sequence[str]], str]
# Looks up where an Android's traffic leaves the internet: {"timezone": ..., "lat": ..., "lon": ...}.
Locator = Callable[[Resolved], "dict | None"]


# --------------------------------------------------------------------------- presets

@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, dict]:
    raw = read_yaml(Path(path), {}) or {}
    out: dict[str, dict] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,40}", str(key)):
            raise ValueError(f"{path}: bad device preset {key!r}")
        for field in _PRODUCT_KEYS:
            value = str(entry.get(field, ""))
            if not _BOOT_VALUE.match(value):
                raise ValueError(f"{path}: preset {key!r} needs `{field}` without spaces (got {value!r})")
        screen = entry.get("screen") or {}
        out[str(key)] = {
            "label": str(entry.get("label") or key),
            **{f: str(entry[f]) for f in _PRODUCT_KEYS},
            "screen": {k: int(screen[k]) for k in ("width", "height", "dpi") if k in screen},
        }
    return out


def profiles(path: Path | None = None) -> dict[str, dict]:
    """Every device preset: name -> {label, brand, manufacturer, model, device, name, screen}."""
    return {k: {**v, "screen": dict(v["screen"])} for k, v in _load(str(path or DEVICES_FILE)).items()}


def profile_screen(profile: str, *, width: int = 480) -> dict | None:
    """A preset's screen scaled to `width` pixels: same aspect ratio, same width in dp.

    Rendering a phone's native 1080-1440 pixel panel costs several times the CPU/GPU of the
    farm's default size and nobody looks at the extra pixels. Scaling the density with the
    width keeps the dp width, so apps still choose the phone's layout."""
    screen = profiles().get(profile, {}).get("screen") or {}
    if not {"width", "height", "dpi"} <= set(screen):
        return None
    scale = width / screen["width"]
    height = int(round(screen["height"] * scale / 2) * 2)      # even: video encoders reject odd sizes
    return {"width": int(width), "height": max(320, min(3840, height)),
            "dpi": max(80, min(640, int(round(screen["dpi"] * scale))))}


# -------------------------------------------------------------------------- identity

def _serial(brand: str) -> str:
    """A serial shaped like the brand's real ones, so it does not stand out in a device list."""
    upper = string.ascii_uppercase + string.digits
    if brand.lower() == "samsung":
        return "R" + "".join(secrets.choice(upper) for _ in range(10))
    if brand.lower() in ("xiaomi", "redmi", "poco"):
        return secrets.token_hex(4)
    return "".join(secrets.choice(upper) for _ in range(12))


def new_identity(config: Config, profile: str | None = None) -> DeviceIdentity:
    """Draw a fresh identity. `profile` is a preset name, `random` or `none`; None follows
    device.profile from the config."""
    wanted = (profile if profile is not None else config.device.profile) or "none"
    known = profiles()
    android_id = new_android_id() if config.device.randomize_android_id else ""
    extra = {str(k): str(v) for k, v in config.device.extra_props.items()}
    if wanted == "none":
        identity = DeviceIdentity(profile="none", android_id=android_id, props=extra)
    else:
        if wanted == "random":
            if not known:
                raise ValueError(f"no device presets found in {DEVICES_FILE}")
            wanted = secrets.choice(sorted(known))
        if wanted not in known:
            raise ValueError(f"unknown device profile {wanted!r}; choose one of: random, none, {', '.join(sorted(known))}")
        preset = known[wanted]
        props = {f"ro.product.{f}": preset[f] for f in _PRODUCT_KEYS}
        identity = DeviceIdentity(profile=wanted, android_id=android_id, serial=_serial(preset["brand"]),
                                  props={**props, **extra})
    boot_props(identity)      # fail now, with the reason, rather than at the first start
    return identity


def host_timezone(root: Path = Path("/")) -> str:
    """The host's Olson zone name, or "" when it cannot be told."""
    try:
        name = (root / "etc/timezone").read_text(encoding="utf-8").strip()
        if _TIMEZONE.match(name):
            return name
    except OSError:
        pass
    try:
        target = os.readlink(root / "etc/localtime")
    except OSError:
        return ""
    _, sep, name = target.partition("zoneinfo/")
    return name if sep and _TIMEZONE.match(name) else ""


def effective_identity(identity: DeviceIdentity, config: Config) -> DeviceIdentity:
    """Fill what the instance leaves open (locale, time zone, location) from the farm defaults.

    The stored identity keeps None for these so a changed farm default reaches every Android
    that did not pin its own value."""
    locale = identity.locale if identity.locale is not None else config.device.locale
    timezone = identity.timezone if identity.timezone is not None else config.device.timezone
    geo = identity.geo if identity.geo is not None else config.device.geo
    return identity.model_copy(update={"locale": locale or None, "timezone": timezone or host_timezone() or None,
                                       "geo": geo or None})


def boot_props(identity: DeviceIdentity) -> list[str]:
    """`key=value` boot arguments for an identity, sorted so the container plan is stable.

    Raises ValueError for anything that cannot be passed this way. The container's init
    splits its command line on whitespace (verified: `ro.product.model=Pixel 7` boots as
    model "Pixel"), so a value with a space would silently corrupt the identity."""
    out: dict[str, str] = {}
    for key, value in identity.props.items():
        key, value = str(key), str(value)
        if not _BOOT_KEY.match(key):
            raise ValueError(f"device property {key!r} cannot be set at boot: only ro.* and androidboot.* names are accepted")
        if not _BOOT_VALUE.match(value):
            raise ValueError(f"device property {key}={value!r} is not usable: values must be 1-255 printable ASCII characters "
                             "without spaces (Android splits its boot arguments on spaces)")
        out[key] = value
    if identity.serial:
        out["androidboot.serialno"] = identity.serial          # becomes ro.serialno and ro.boot.serialno
    if identity.locale:
        if not _LOCALE.match(identity.locale):
            raise ValueError(f"bad locale {identity.locale!r}: expected a tag like en-US or pt-BR")
        # Default language of a fresh /data. An Android that already chose a language keeps
        # it in persist.sys.locale, which apply_post_boot() brings in line.
        out.setdefault("ro.product.locale", identity.locale)
    return [f"{k}={v}" for k, v in sorted(out.items())]


def parse_geo(value: str) -> tuple[float, float]:
    """`lat,lon` -> floats, range-checked."""
    m = re.fullmatch(r"\s*(-?\d{1,3}(?:\.\d{1,8})?)\s*,\s*(-?\d{1,3}(?:\.\d{1,8})?)\s*", str(value))
    if not m:
        raise ValueError(f"bad location {value!r}: expected `latitude,longitude`, e.g. 52.52,13.405")
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"location {value!r} is outside -90..90, -180..180")
    return lat, lon


def validate_identity(identity: DeviceIdentity) -> None:
    """Everything apply_post_boot() and boot_props() would reject, checked up front."""
    boot_props(identity)
    if identity.timezone and identity.timezone != "auto" and not _TIMEZONE.match(identity.timezone):
        raise ValueError(f"bad time zone {identity.timezone!r}: expected an Olson name like Europe/Berlin")
    if identity.geo and identity.geo != "auto":
        parse_geo(identity.geo)


# ------------------------------------------------------------------------- post-boot

def _is_set(value: str) -> bool:
    return value not in ("", "null")


def apply_post_boot(adb: Adb, r: Resolved, *, root_exec: RootExec | None = None,
                    locate: Locator | None = None) -> dict:
    """Bring a booted Android's mutable identity in line with `r.device`.

    Idempotent and cheap when nothing changed (reads before it writes). One failing item
    never stops the others; the returned dict lists what was `applied` and any `warnings`.
    `root_exec` and `locate` are optional helpers supplied by the instance manager."""
    identity = r.device
    applied: list[str] = []
    warnings: list[str] = []

    def attempt(what: str, fn: Callable[[], bool]) -> None:
        try:
            if fn():
                applied.append(what)
        except (AdbError, ValueError, RuntimeError) as e:
            warnings.append(f"{what}: {e}")

    def android_id() -> bool:
        if not _ANDROID_ID.match(identity.android_id):
            return False
        if adb.setting_get("secure", "android_id") == identity.android_id:
            return False
        # Since Android 8 each app is handed its own id derived from a key stored in /data,
        # so two Androids with separate data volumes never share one anyway. This value is
        # what the shell, older apps and device listings read.
        adb.setting_put("secure", "android_id", identity.android_id)
        return True

    where: dict | None = None

    def located() -> dict:
        nonlocal where
        if where is None:
            where = (locate(r) if locate else None) or {}
        return where

    def timezone() -> bool:
        zone = identity.timezone or ""
        if zone == "auto":
            zone = str(located().get("timezone") or "")
            if not zone:
                raise RuntimeError("the exit address could not be located; set device.timezone to a zone name")
        if not zone:
            return False
        if not _TIMEZONE.match(zone):
            raise ValueError(f"bad time zone {zone!r}")
        if adb.getprop("persist.sys.timezone") == zone:
            return False
        # The alarm service updates running apps too; the bare property is the fallback
        # for releases whose alarm service has no shell command.
        adb.shell(["cmd", "alarm", "set-timezone", zone], timeout=15, check=False)
        if adb.getprop("persist.sys.timezone") != zone:
            adb.setprop("persist.sys.timezone", zone)
        return True

    def locale() -> bool:
        tag = identity.locale or ""
        if not tag:
            return False
        if not _LOCALE.match(tag):
            raise ValueError(f"bad locale {tag!r}")
        saved = adb.getprop("persist.sys.locale")
        if saved == tag or (not saved and adb.getprop("ro.product.locale") == tag):
            return False
        adb.setprop("persist.sys.locale", tag)
        warnings.append(f"locale {tag} is saved and takes effect when this Android next restarts")
        return True

    def geo() -> bool:
        spot = identity.geo or ""
        if not spot:
            return False
        if spot == "auto":
            here = located()
            if here.get("lat") is None or here.get("lon") is None:
                raise RuntimeError("the exit address could not be located; set device.geo to `lat,lon`")
            spot = f"{here['lat']},{here['lon']}"
        lat, lon = parse_geo(spot)
        if root_exec is None:
            raise RuntimeError("mock location needs root inside the Android, which is not available here")
        set_mock_location(root_exec, lat, lon)
        return True

    attempt("android_id", android_id)
    attempt("timezone", timezone)
    attempt("locale", locale)
    attempt("location", geo)
    return {"applied": applied, "warnings": warnings}


MOCK_PROVIDERS = ("gps", "network")


def set_mock_location(root_exec: RootExec, lat: float, lon: float) -> None:
    """Make the location services report a fixed position.

    Android only accepts test providers from a caller holding the mock-location app-op; the
    shell command runs as the `android` package under the caller's uid, so that op is granted
    to root first. The position has to be set again after every boot: test providers live in
    the running location service, not in /data."""
    root_exec(["cmd", "appops", "set", "--uid", "0", "android:mock_location", "allow"])
    where = f"{lat:.6f},{lon:.6f}"
    for provider in MOCK_PROVIDERS:
        root_exec(["cmd", "location", "providers", "add-test-provider", provider])
        root_exec(["cmd", "location", "providers", "set-test-provider-enabled", provider, "true"])
        root_exec(["cmd", "location", "providers", "set-test-provider-location", provider,
                   "--location", where, "--accuracy", "5"])
