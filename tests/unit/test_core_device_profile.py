"""Device presets, identity generation, boot properties and post-boot identity."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import FakeAdb  # noqa: E402

from superfarm.config import Config  # noqa: E402
from superfarm.core import device_profile as dp  # noqa: E402
from superfarm.core.models import DeviceIdentity, InstanceSpec, resolve  # noqa: E402


def resolved(identity: DeviceIdentity):
    return resolve(InstanceSpec(id=3, device=identity), Config())


@pytest.fixture
def adb():
    FakeAdb.registry = {}
    return FakeAdb("127.0.0.1:17003")


# ------------------------------------------------------------------------ presets

def test_presets_are_plentiful_complete_and_free_of_spaces():
    presets = dp.profiles()
    assert len(presets) >= 20
    for name, p in presets.items():
        assert re.fullmatch(r"[a-z0-9][a-z0-9\-]*", name)
        for key in ("brand", "manufacturer", "model", "device", "name"):
            assert p[key] and not re.search(r"\s", p[key]), (name, key)
        assert p["label"]
        s = p["screen"]
        assert 720 <= s["width"] <= 1600 and s["height"] > s["width"] and 240 <= s["dpi"] <= 640, name
    assert len({p["model"] for p in presets.values()}) == len(presets)


def test_profiles_returns_copies():
    dp.profiles()["samsung-s23"]["model"] = "changed"
    dp.profiles()["samsung-s23"]["screen"]["width"] = 1
    assert dp.profiles()["samsung-s23"]["model"] == "SM-S911B"
    assert dp.profiles()["samsung-s23"]["screen"]["width"] == 1080


def test_a_preset_with_a_space_is_rejected_when_loaded(tmp_path):
    bad = tmp_path / "devices.yaml"
    bad.write_text("pixel-7:\n  brand: google\n  manufacturer: Google\n  model: Pixel 7\n  device: panther\n  name: panther\n")
    with pytest.raises(ValueError, match="without spaces"):
        dp.profiles(bad)


def test_profile_screen_scales_keeping_shape_and_dp_width():
    s = dp.profile_screen("samsung-s23", width=480)          # native 1080x2340 @ 480 dpi
    assert s == {"width": 480, "height": 1040, "dpi": 213}
    assert s["height"] % 2 == 0
    assert abs(1080 / 480 * 160 - 480 / s["dpi"] * 160) < 2            # ~360 dp wide, like the phone
    assert dp.profile_screen("nope") is None


# ----------------------------------------------------------------------- identity

def test_new_identity_from_a_named_preset():
    ident = dp.new_identity(Config(), "samsung-s23")
    assert ident.profile == "samsung-s23"
    assert ident.props == {"ro.product.brand": "samsung", "ro.product.manufacturer": "samsung",
                           "ro.product.model": "SM-S911B", "ro.product.device": "dm1q", "ro.product.name": "dm1qxxx"}
    assert re.fullmatch(r"[0-9a-f]{16}", ident.android_id)
    assert re.fullmatch(r"R[A-Z0-9]{10}", ident.serial)
    assert ident.locale is None and ident.timezone is None and ident.geo is None


def test_random_profile_draws_a_known_preset_and_differs_between_androids():
    idents = [dp.new_identity(Config()) for _ in range(12)]          # config default is `random`
    assert all(i.profile in dp.profiles() for i in idents)
    assert len({i.android_id for i in idents}) == 12
    assert len({i.serial for i in idents}) == 12


def test_profile_none_keeps_the_image_identity():
    ident = dp.new_identity(Config(), "none")
    assert ident.profile == "none" and ident.props == {} and ident.serial == ""
    assert dp.boot_props(ident) == []


def test_android_id_randomisation_can_be_switched_off():
    cfg = Config.model_validate({"device": {"randomize_android_id": False, "profile": "none"}})
    assert dp.new_identity(cfg).android_id == ""


def test_config_extra_props_are_added_and_validated():
    cfg = Config.model_validate({"device": {"extra_props": {"ro.build.tags": "release-keys"}}})
    assert dp.new_identity(cfg, "xiaomi-13").props["ro.build.tags"] == "release-keys"
    cfg = Config.model_validate({"device": {"extra_props": {"ro.product.model": "Pixel 7"}}})
    with pytest.raises(ValueError, match="without spaces"):
        dp.new_identity(cfg, "xiaomi-13")


def test_unknown_profile_names_the_choices():
    with pytest.raises(ValueError, match="samsung-s23"):
        dp.new_identity(Config(), "iphone")


def test_boot_props_are_sorted_and_include_serial_and_locale():
    ident = DeviceIdentity(serial="ABC123", locale="pt-BR", props={"ro.product.model": "M", "ro.product.brand": "b"})
    assert dp.boot_props(ident) == ["androidboot.serialno=ABC123", "ro.product.brand=b", "ro.product.locale=pt-BR",
                                    "ro.product.model=M"]


@pytest.mark.parametrize("props", [{"persist.sys.x": "1"}, {"ro.x": "a b"}, {"ro.x": ""}, {"ro.x y": "1"},
                                   {"ro.x": "tab\there"}, {"debug.x": "1"}])
def test_boot_props_refuse_what_cannot_be_passed_at_boot(props):
    with pytest.raises(ValueError):
        dp.boot_props(DeviceIdentity(props=props))


def test_bad_locale_is_refused():
    with pytest.raises(ValueError, match="locale"):
        dp.boot_props(DeviceIdentity(locale="english please"))


def test_geo_parsing():
    assert dp.parse_geo("52.52, 13.405") == (52.52, 13.405)
    assert dp.parse_geo("-33.86,151.2") == (-33.86, 151.2)
    for bad in ("", "52.52", "91,0", "0,181", "a,b", "1,2,3", "1;2"):
        with pytest.raises(ValueError):
            dp.parse_geo(bad)


def test_validate_identity_checks_timezone_and_geo():
    dp.validate_identity(DeviceIdentity(timezone="Europe/Berlin", geo="1,2"))
    with pytest.raises(ValueError, match="time zone"):
        dp.validate_identity(DeviceIdentity(timezone="Berlin; reboot"))
    with pytest.raises(ValueError):
        dp.validate_identity(DeviceIdentity(geo="north"))


@pytest.mark.parametrize("field", ["timezone", "geo"])
def test_there_is_no_auto_for_timezone_or_location(field):
    """Nothing in the farm can tell where an Android's traffic leaves the internet, so a
    value that promises to derive the zone or position from it is refused everywhere."""
    with pytest.raises(ValueError):
        dp.validate_identity(DeviceIdentity(**{field: "auto"}))
    with pytest.raises(ValueError):
        Config.model_validate({"device": {field: "auto"}})
    assert "auto" not in Config.model_fields["device"].annotation.model_fields[field].description.lower()


def test_farm_wide_timezone_and_location_are_checked_when_the_config_is_loaded():
    cfg = Config.model_validate({"device": {"timezone": " Europe/Berlin ", "geo": "52.52,13.405"}})
    assert (cfg.device.timezone, cfg.device.geo) == ("Europe/Berlin", "52.52,13.405")
    assert Config().device.timezone == "" and Config().device.geo == ""
    for bad in ({"timezone": "Berlin; reboot"}, {"geo": "north"}, {"geo": "91,0"}):
        with pytest.raises(ValueError):
            Config.model_validate({"device": bad})


def test_effective_identity_fills_farm_defaults_but_keeps_pinned_values():
    cfg = Config.model_validate({"device": {"locale": "fr-FR", "timezone": "Europe/Paris", "geo": "48.85,2.35"}})
    filled = dp.effective_identity(DeviceIdentity(), cfg)
    assert (filled.locale, filled.timezone, filled.geo) == ("fr-FR", "Europe/Paris", "48.85,2.35")
    pinned = dp.effective_identity(DeviceIdentity(locale="ja-JP", timezone="Asia/Tokyo", geo="35.6,139.7"), cfg)
    assert (pinned.locale, pinned.timezone, pinned.geo) == ("ja-JP", "Asia/Tokyo", "35.6,139.7")


def test_host_timezone_is_read_from_etc(tmp_path):
    (tmp_path / "etc").mkdir()
    assert dp.host_timezone(tmp_path) == ""
    os.symlink("/usr/share/zoneinfo/America/Sao_Paulo", tmp_path / "etc/localtime")
    assert dp.host_timezone(tmp_path) == "America/Sao_Paulo"
    (tmp_path / "etc/timezone").write_text("Europe/Rome\n")
    assert dp.host_timezone(tmp_path) == "Europe/Rome"


# ---------------------------------------------------------------------- post-boot

def test_post_boot_sets_android_id_and_timezone_once(adb):
    ident = DeviceIdentity(android_id="0123456789abcdef", timezone="Europe/Berlin", locale="en-US")
    out = dp.apply_post_boot(adb, resolved(ident))
    assert out["applied"] == ["android_id", "timezone"] and out["warnings"] == []
    assert adb.dev["settings"]["secure/android_id"] == "0123456789abcdef"
    assert adb.dev["props"]["persist.sys.timezone"] == "Europe/Berlin"
    again = dp.apply_post_boot(adb, resolved(ident))
    assert again["applied"] == []


def test_post_boot_timezone_falls_back_to_the_property(adb):
    adb.shell = lambda command, **kw: ""                       # an Android whose alarm service has no shell command
    dp.apply_post_boot(adb, resolved(DeviceIdentity(timezone="Asia/Tokyo")))
    assert adb.dev["props"]["persist.sys.timezone"] == "Asia/Tokyo"


def test_post_boot_locale_is_saved_for_the_next_restart(adb):
    out = dp.apply_post_boot(adb, resolved(DeviceIdentity(locale="de-DE")))
    assert adb.dev["props"]["persist.sys.locale"] == "de-DE"
    assert "locale" in out["applied"] and any("restart" in w for w in out["warnings"])
    assert dp.apply_post_boot(adb, resolved(DeviceIdentity(locale="de-DE")))["applied"] == []


def test_post_boot_location_needs_root_and_uses_it(adb):
    ident = DeviceIdentity(geo="52.52,13.405")
    without = dp.apply_post_boot(adb, resolved(ident))
    assert any("root" in w for w in without["warnings"])
    calls = []
    out = dp.apply_post_boot(adb, resolved(ident), root_exec=lambda argv: calls.append(list(argv)) or "")
    assert "location" in out["applied"]
    assert calls[0] == ["cmd", "appops", "set", "--uid", "0", "android:mock_location", "allow"]
    assert ["cmd", "location", "providers", "set-test-provider-location", "gps", "--location",
            "52.520000,13.405000", "--accuracy", "5"] in calls


def test_post_boot_refuses_a_zone_that_is_not_a_zone(adb):
    out = dp.apply_post_boot(adb, resolved(DeviceIdentity(timezone="auto")))
    assert out["applied"] == [] and "bad time zone" in out["warnings"][0]
    assert adb.dev["props"]["persist.sys.timezone"] == "GMT" and not any("set-timezone" in str(c) for c in adb.dev["shell"])


def test_one_failing_item_does_not_stop_the_others(adb):
    def broken(namespace, key):
        raise dp.AdbError("device offline")
    adb.setting_get = broken
    out = dp.apply_post_boot(adb, resolved(DeviceIdentity(android_id="0123456789abcdef", timezone="Europe/Rome")))
    assert out["applied"] == ["timezone"] and "android_id" in out["warnings"][0]
