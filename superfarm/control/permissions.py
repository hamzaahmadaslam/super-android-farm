"""The permissions an app has to be *given*, as opposed to the ones it merely declares.

Android sorts permissions into kinds. Two of them need an action on the device:

    dangerous   runtime permissions: a dialog the first time the app needs them
                (`pm grant` / `pm revoke`).
    special     switches in Settings, backed by an app-op rather than a grant
                (`appops set <package> <op> allow`).

Fleet automation stalls on the first permission dialog nobody is there to tap, so the
farm grants up front. `pm grant` refuses anything that is not a runtime permission, and
the device does not offer a cheap "list what is dangerous" query, hence this catalogue.
Each entry records the API level that introduced it (and the last one that honours it)
so an old Android is not asked for a permission it has never heard of.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ANDROID_PREFIX = "android.permission."
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*\Z")


@dataclass(frozen=True)
class Permission:
    name: str
    group: str
    label: str
    min_sdk: int = 1
    max_sdk: int = 10_000


def _p(short: str, group: str, label: str, min_sdk: int = 1, max_sdk: int = 10_000, prefix: str = ANDROID_PREFIX) -> Permission:
    return Permission(prefix + short, group, label, min_sdk, max_sdk)


_DANGEROUS = (
    _p("READ_CALENDAR", "calendar", "Read calendar events"),
    _p("WRITE_CALENDAR", "calendar", "Add or change calendar events"),
    _p("READ_CALL_LOG", "call_log", "Read the call log"),
    _p("WRITE_CALL_LOG", "call_log", "Change the call log"),
    _p("PROCESS_OUTGOING_CALLS", "call_log", "See and redirect outgoing calls", max_sdk=28),
    _p("CAMERA", "camera", "Take pictures and record video"),
    _p("READ_CONTACTS", "contacts", "Read contacts"),
    _p("WRITE_CONTACTS", "contacts", "Change contacts"),
    _p("GET_ACCOUNTS", "contacts", "Find accounts on the device"),
    _p("ACCESS_FINE_LOCATION", "location", "Precise location"),
    _p("ACCESS_COARSE_LOCATION", "location", "Approximate location"),
    _p("ACCESS_BACKGROUND_LOCATION", "location", "Location while in the background", min_sdk=29),
    _p("ACCESS_MEDIA_LOCATION", "storage", "Read locations stored in photos", min_sdk=29),
    _p("RECORD_AUDIO", "microphone", "Record audio"),
    _p("READ_PHONE_STATE", "phone", "Read phone status and identity"),
    _p("READ_PHONE_NUMBERS", "phone", "Read the device's phone numbers", min_sdk=26),
    _p("CALL_PHONE", "phone", "Place phone calls"),
    _p("ANSWER_PHONE_CALLS", "phone", "Answer phone calls", min_sdk=26),
    _p("ADD_VOICEMAIL", "phone", "Add voicemail", prefix="com.android.voicemail.permission."),
    _p("USE_SIP", "phone", "Make and receive SIP calls"),
    _p("ACCEPT_HANDOVER", "phone", "Continue a call from another app", min_sdk=28),
    _p("BODY_SENSORS", "sensors", "Read body sensors"),
    _p("BODY_SENSORS_BACKGROUND", "sensors", "Read body sensors in the background", min_sdk=33),
    _p("ACTIVITY_RECOGNITION", "activity", "Recognise physical activity", min_sdk=29),
    _p("SEND_SMS", "sms", "Send SMS messages"),
    _p("RECEIVE_SMS", "sms", "Receive SMS messages"),
    _p("READ_SMS", "sms", "Read SMS messages"),
    _p("RECEIVE_WAP_PUSH", "sms", "Receive WAP push messages"),
    _p("RECEIVE_MMS", "sms", "Receive MMS messages"),
    _p("READ_EXTERNAL_STORAGE", "storage", "Read shared storage"),
    _p("WRITE_EXTERNAL_STORAGE", "storage", "Write shared storage"),
    _p("READ_MEDIA_IMAGES", "storage", "Read photos", min_sdk=33),
    _p("READ_MEDIA_VIDEO", "storage", "Read videos", min_sdk=33),
    _p("READ_MEDIA_AUDIO", "storage", "Read music and audio", min_sdk=33),
    _p("READ_MEDIA_VISUAL_USER_SELECTED", "storage", "Read the photos and videos the user picked", min_sdk=34),
    _p("BLUETOOTH_SCAN", "nearby", "Find Bluetooth devices", min_sdk=31),
    _p("BLUETOOTH_CONNECT", "nearby", "Connect to Bluetooth devices", min_sdk=31),
    _p("BLUETOOTH_ADVERTISE", "nearby", "Advertise to Bluetooth devices", min_sdk=31),
    _p("UWB_RANGING", "nearby", "Measure distance to nearby devices", min_sdk=31),
    _p("NEARBY_WIFI_DEVICES", "nearby", "Find and connect to Wi-Fi devices", min_sdk=33),
    _p("POST_NOTIFICATIONS", "notifications", "Show notifications", min_sdk=33),
)

DANGEROUS: dict[str, Permission] = {p.name: p for p in _DANGEROUS}

# Permission -> the app-op that actually decides it. `pm grant` rejects these; the switch
# the user would flip in Settings is an app-op mode.
SPECIAL: dict[str, str] = {
    ANDROID_PREFIX + "SYSTEM_ALERT_WINDOW": "SYSTEM_ALERT_WINDOW",
    ANDROID_PREFIX + "WRITE_SETTINGS": "WRITE_SETTINGS",
    ANDROID_PREFIX + "MANAGE_EXTERNAL_STORAGE": "MANAGE_EXTERNAL_STORAGE",
    ANDROID_PREFIX + "REQUEST_INSTALL_PACKAGES": "REQUEST_INSTALL_PACKAGES",
    ANDROID_PREFIX + "PACKAGE_USAGE_STATS": "GET_USAGE_STATS",
    ANDROID_PREFIX + "SCHEDULE_EXACT_ALARM": "SCHEDULE_EXACT_ALARM",
}

ALL = "all"   # the word callers use for "every dangerous permission the app requests"


def normalize(name: str) -> str:
    """Accept `CAMERA` as well as `android.permission.CAMERA`; reject anything that is not a
    plain permission name (it ends up as an argument of a device command)."""
    name = str(name).strip()
    if not name or len(name) > 200 or not _NAME.match(name):
        raise ValueError(f"bad permission name {name!r}")
    if "." not in name:
        name = ANDROID_PREFIX + name.upper()
    return name


def is_dangerous(name: str, sdk: int | None = None) -> bool:
    p = DANGEROUS.get(name)
    return bool(p) and (sdk is None or p.min_sdk <= sdk <= p.max_sdk)


def is_special(name: str) -> bool:
    return name in SPECIAL


def dangerous_of(requested: list[str], sdk: int | None = None) -> list[str]:
    """The runtime permissions among those an app requests, in the app's own order.
    With `sdk`, permissions the device's Android version does not know are left out."""
    seen: set[str] = set()
    out = []
    for name in requested:
        if name not in seen and is_dangerous(name, sdk):
            seen.add(name)
            out.append(name)
    return out


def special_of(requested: list[str]) -> list[str]:
    return [n for n in dict.fromkeys(requested) if n in SPECIAL]


def catalogue() -> list[dict]:
    """For display: every permission the farm can grant, grouped as Settings groups them."""
    rows = [{"name": p.name, "group": p.group, "label": p.label, "kind": "dangerous",
             "min_sdk": p.min_sdk, "max_sdk": None if p.max_sdk >= 10_000 else p.max_sdk} for p in _DANGEROUS]
    rows += [{"name": n, "group": "special", "label": f"App-op {op}", "kind": "special", "min_sdk": 1, "max_sdk": None}
             for n, op in SPECIAL.items()]
    return rows
