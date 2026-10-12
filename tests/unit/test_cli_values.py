"""`key=value` parsing for `sf set` and `sf config set`: typed by the model, not guessed from the text."""
import pytest

from superfarm.cli import values
from superfarm.cli.commands.instances import spec_patch
from superfarm.cli.client import CliError
from superfarm.config import Config
from superfarm.core.models import InstanceSpec


def patch(*assignments: str) -> dict:
    return values.parse_assignments(InstanceSpec, list(assignments))


def test_plain_fields_take_the_type_of_the_model():
    assert patch("fps=30") == {"fps": 30}
    assert patch("autostart=true") == {"autostart": True}
    assert patch("autostart=off") == {"autostart": False}
    assert patch("resources.cpus=2.5") == {"resources": {"cpus": 2.5}}


def test_text_fields_stay_text_even_when_they_look_like_numbers():
    # A YAML-style guess would turn these into ints and the model would reject them.
    assert patch("name=13") == {"name": "13"}
    assert patch("android_version=12") == {"android_version": "12"}
    assert patch("notes=true") == {"notes": "true"}
    assert patch("name=a = b") == {"name": "a = b"}             # only the first = separates


def test_nested_keys_become_a_nested_patch_and_merge():
    assert patch("network.mode=vpn", "network.vpn_profile=nl.ovpn", "resources.memory_mb=2048", "fps=25") == {
        "network": {"mode": "vpn", "vpn_profile": "nl.ovpn"}, "resources": {"memory_mb": 2048}, "fps": 25}


def test_lists_are_comma_separated():
    assert patch("tags=eu, shop ,test") == {"tags": ["eu", "shop", "test"]}
    assert patch("tags=") == {"tags": []}
    assert patch("tags=[a, b]") == {"tags": ["a", "b"]}
    assert patch("extra_boot_args=androidboot.x=1,ro.y=2") == {"extra_boot_args": ["androidboot.x=1", "ro.y=2"]}


def test_dict_entries_keep_dots_in_the_entry_name():
    assert patch("extra_props.ro.product.model=Pixel 7") == {"extra_props": {"ro.product.model": "Pixel 7"}}
    assert patch("device.props.ro.serialno=ABC") == {"device": {"props": {"ro.serialno": "ABC"}}}


@pytest.mark.parametrize("empty", ["", "  ", "{}"])
def test_an_empty_value_empties_a_whole_map(empty):
    # A patch merges maps entry by entry, so `{}` would change nothing: null is what removes.
    assert patch(f"extra_props={empty}") == {"extra_props": None}
    assert patch(f"device.props={empty}") == {"device": {"props": None}}
    assert patch(f"extra_props.ro.x={empty}") == {"extra_props": {"ro.x": empty}}      # one entry: a value like any other
    assert patch("fps=30", f"extra_props={empty}") == {"fps": 30, "extra_props": None}


def test_a_map_cannot_be_emptied_and_filled_in_one_command():
    # One patch cannot say "remove everything, then add": the entries would simply be merged in.
    for order in (("extra_props=", "extra_props.ro.x=1"), ("extra_props.ro.x=1", "extra_props="),
                  ("device.props=", "device.props.ro.x=1")):
        with pytest.raises(values.KeyValueError, match="in two commands"):
            patch(*order)
    assert patch("extra_props={ro.x: 1}") == {"extra_props": {"ro.x": 1}}
    assert patch("fps=default", "fps=30") == {"fps": 30}                                  # the last one still wins elsewhere


def test_the_server_empties_the_map_for_that_patch(tmp_path):
    from superfarm.core.instances import _apply_patch
    saved = {"extra_props": {"ro.a": "1", "ro.b": "2"}, "fps": 30}
    assert _apply_patch(saved, patch("extra_props=")) == {"fps": 30}                     # gone: the model's default is {}
    assert _apply_patch(saved, {"extra_props": {}}) == saved                              # which is why {} is not sent
    assert InstanceSpec.model_validate({"id": 1, **_apply_patch(saved, patch("extra_props="))}).extra_props == {}


@pytest.mark.parametrize("word", ["default", "", "DEFAULT"])
def test_optional_fields_can_go_back_to_the_farm_default(word):
    assert patch(f"gpu_mode={word}") == {"gpu_mode": None}
    assert patch(f"fps={word}") == {"fps": None}
    assert patch(f"network.mode={word}") == {"network": {"mode": None}}


def test_none_is_a_value_not_a_reset():
    # `none` is a real network mode (no network at all) and a real device profile.
    assert patch("network.mode=none") == {"network": {"mode": "none"}}
    assert patch("device.profile=none") == {"device": {"profile": "none"}}
    assert patch("network.vpn_profile=none") == {"network": {"vpn_profile": "none"}}


def test_choices_are_checked_with_the_valid_values_named():
    with pytest.raises(values.KeyValueError, match="software.*igpu.*nvidia"):
        patch("gpu_mode=fast")
    assert patch("gpu_mode=igpu") == {"gpu_mode": "igpu"}
    assert patch("network.mode=gateway") == {"network": {"mode": "gateway"}}


@pytest.mark.parametrize("bad, message", [
    ("fps", "expected key=value"),
    ("=5", "expected key=value"),
    ("fps=fast", "whole number"),
    ("resources.cpus=many", "a number"),
    ("autostart=maybe", "true or false"),
    ("nope=1", "no field 'nope'"),
    ("network.nope=1", "network has no field 'nope'"),
    ("fps.x=1", "single value"),
    ("network=vpn", "group of settings"),
    ("a..b=1", "bad key"),
])
def test_bad_assignments_explain_themselves(bad, message):
    with pytest.raises(values.KeyValueError, match=message):
        patch(bad)


def test_every_parsed_patch_is_accepted_by_the_model():
    made = patch("name=shop", "tags=eu,a", "fps=30", "gpu_mode=dgpu", "network.mode=proxy", "network.proxy=10.0.0.1:3128",
                 "resources.cpus=3", "extra_props.ro.x=1", "device.locale=de-DE", "camera=true")
    spec = InstanceSpec.model_validate({"id": 1, **made})
    assert spec.network.proxy == "10.0.0.1:3128" and spec.tags == ["eu", "a"] and spec.camera is True


def test_spec_patch_refuses_read_only_keys_and_lists_settable_ones():
    with pytest.raises(CliError, match="id cannot be changed"):
        spec_patch(["id=5"])
    with pytest.raises(CliError, match="created cannot be changed"):
        spec_patch(["created=1"])
    with pytest.raises(CliError) as e:
        spec_patch(["bogus=1"])
    assert "settable keys" in str(e.value) and "network.mode" in str(e.value) and e.value.code == 2


def test_keys_of_lists_every_leaf():
    keys = values.keys_of(InstanceSpec)
    assert {"fps", "tags", "network.mode", "resources.cpus", "device.locale", "extra_props.<name>"} <= set(keys)
    assert "network" not in keys


def test_config_keys_resolve_through_nested_groups_and_named_entries():
    path, annotation, optional = values.locate(Config, "network.vpn.selection")
    assert path == ["network", "vpn", "selection"] and optional is False
    assert values.coerce(annotation, "unique") == "unique"
    path, annotation, _ = values.locate(Config, "network.gateways.eu.profile")
    assert path == ["network", "gateways", "eu", "profile"] and annotation is str
    path, annotation, _ = values.locate(Config, "android.images.13.tag")
    assert path == ["android", "images", "13", "tag"]
    path, annotation, _ = values.locate(Config, "network.gateways.eu")
    assert values.coerce(annotation, "{profile: nl.ovpn}") == {"profile": "nl.ovpn"}
    path, annotation, _ = values.locate(Config, "network.dns")
    assert values.coerce(annotation, "9.9.9.9,1.1.1.1") == ["9.9.9.9", "1.1.1.1"]


def test_nest_and_merge():
    assert values.nest(["a", "b"], 1) == {"a": {"b": 1}}
    assert values.merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 5}, "d": 1}) == {"a": {"b": 5, "c": 2}, "d": 1}
