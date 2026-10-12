"""The catalogue of permissions the farm grants."""
import pytest

from superfarm.control import permissions as perms


def test_catalogue_is_consistent():
    rows = perms.catalogue()
    names = [r["name"] for r in rows]
    assert len(names) == len(set(names)) and len(perms.DANGEROUS) >= 35
    assert all(r["label"] and r["group"] and r["kind"] in ("dangerous", "special") for r in rows)
    assert not set(perms.DANGEROUS) & set(perms.SPECIAL)
    assert "android.permission.INTERNET" not in perms.DANGEROUS          # normal permissions are never "granted"
    assert "com.android.voicemail.permission.ADD_VOICEMAIL" in perms.DANGEROUS


def test_normalize():
    assert perms.normalize("camera") == perms.normalize(" android.permission.CAMERA ") == "android.permission.CAMERA"
    assert perms.normalize("com.example.permission.X") == "com.example.permission.X"
    for bad in ("", "a b", "CAMERA;id", "$(x)", "-g", "x" * 300, "a/b", "CAM\nERA"):
        with pytest.raises(ValueError):
            perms.normalize(bad)


def test_dangerous_of_respects_the_device_android_version():
    requested = ["android.permission.INTERNET", "android.permission.CAMERA", "android.permission.POST_NOTIFICATIONS",
                 "android.permission.CAMERA", "android.permission.PROCESS_OUTGOING_CALLS", "android.permission.BLUETOOTH_CONNECT",
                 "android.permission.SYSTEM_ALERT_WINDOW"]
    assert perms.dangerous_of(requested) == ["android.permission.CAMERA", "android.permission.POST_NOTIFICATIONS",
                                             "android.permission.PROCESS_OUTGOING_CALLS", "android.permission.BLUETOOTH_CONNECT"]
    assert perms.dangerous_of(requested, sdk=28) == ["android.permission.CAMERA", "android.permission.PROCESS_OUTGOING_CALLS"]
    assert perms.dangerous_of(requested, sdk=33) == ["android.permission.CAMERA", "android.permission.POST_NOTIFICATIONS",
                                                     "android.permission.BLUETOOTH_CONNECT"]
    assert perms.special_of(requested) == ["android.permission.SYSTEM_ALERT_WINDOW"]
    assert perms.is_special("android.permission.WRITE_SETTINGS") and not perms.is_dangerous("android.permission.WRITE_SETTINGS")
