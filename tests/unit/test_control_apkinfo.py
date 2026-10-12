"""Reading APKs without Android tools: the binary-XML and resource-table readers."""
import importlib.util
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from superfarm.control import apkinfo
from superfarm.control.apkinfo import ApkError, ResRef, classify_split, parse_apk, parse_axml, parse_badging, resolve_string


def load_support():
    name = "sf_control_support"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_control_support.py"))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


S = load_support()
PERMS = ("android.permission.CAMERA", "android.permission.INTERNET", "com.example.permission.CUSTOM")


@pytest.mark.parametrize("variant", [{}, {"utf8": True}, {"blank_names": True}, {"utf8": True, "blank_names": True}])
def test_reads_a_base_apk_in_every_encoding(tmp_path, variant):
    # blank_names: shrinkers empty the attribute names; only the framework resource ids remain.
    apk = tmp_path / "app.apk"
    S.make_apk(apk, S.manifest("org.example.app", 42, "4.2-beta", permissions=PERMS, min_sdk=23, target_sdk=34, **variant),
               abis=("x86_64", "arm64-v8a"))
    info = parse_apk(apk, use_aapt=False)
    assert (info.package, info.version_code, info.version_name) == ("org.example.app", 42, "4.2-beta")
    assert (info.min_sdk, info.target_sdk, info.label) == (23, 34, "Example")
    assert info.permissions == list(PERMS)
    assert info.abis == ["arm64-v8a", "x86_64"]            # canonical order, not zip order
    assert info.launcher == "org.example.app/org.example.app.Main"
    assert info.is_base and not info.debuggable and info.size == apk.stat().st_size and info.source == "axml"


def test_non_ascii_strings_survive_both_pool_encodings(tmp_path):
    for utf8 in (False, True):
        S.make_apk(tmp_path / "a.apk", S.manifest(label="Über Äpp 应用 " + "x" * 200, utf8=utf8))
        assert parse_apk(tmp_path / "a.apk", use_aapt=False).label == "Über Äpp 应用 " + "x" * 200


def test_split_apk_fields(tmp_path):
    S.make_apk(tmp_path / "s.apk", S.manifest(split="feature_cam.config.arm64_v8a", config_for="feature_cam"), abis=("arm64-v8a",))
    info = parse_apk(tmp_path / "s.apk", use_aapt=False)
    assert (info.split, info.config_for_split, info.is_base, info.is_feature_split) == ("feature_cam.config.arm64_v8a", "feature_cam", False, False)
    S.make_apk(tmp_path / "f.apk", S.manifest(split="feature_cam", feature=True))
    assert parse_apk(tmp_path / "f.apk", use_aapt=False).is_feature_split


def test_long_version_code_and_debuggable(tmp_path):
    S.make_apk(tmp_path / "a.apk", S.manifest(version_code=5, version_major=2, debuggable=True))
    info = parse_apk(tmp_path / "a.apk", use_aapt=False)
    assert info.version_code == (2 << 32) | 5 and info.debuggable


def test_label_reference_is_resolved_from_the_resource_table_preferring_the_default(tmp_path):
    res_id = 0x7F0B0003
    arsc = S.make_arsc(res_id, "Default Name", "Nom Français")
    assert resolve_string(arsc, res_id) == "Default Name"
    assert resolve_string(arsc, res_id + 1) is None and resolve_string(arsc, 0x01040000) is None
    assert resolve_string(b"garbage", res_id) is None and resolve_string(arsc[:60], res_id) is None
    S.make_apk(tmp_path / "a.apk", S.manifest(label=S.Ref(res_id)), extra={"resources.arsc": arsc})
    assert parse_apk(tmp_path / "a.apk", use_aapt=False).label == "Default Name"
    S.make_apk(tmp_path / "b.apk", S.manifest(label=S.Ref(res_id)))       # no table: unknown, not a crash
    assert parse_apk(tmp_path / "b.apk", use_aapt=False).label == ""


def test_axml_tree_shape_and_typed_values():
    root = parse_axml(S.build_axml(S.El("manifest", {"package": "a.b", "android:versionCode": -3, "android:label": S.Ref(0x7F010001)},
                                        [S.El("application", {"android:debuggable": False}, [S.El("activity", {"android:name": ".X"})])])))
    assert root.tag == "manifest" and root.attrs == {"package": "a.b", "versionCode": -3, "label": ResRef(0x7F010001)}
    assert str(root.attrs["label"]) == "@0x7f010001"
    app = root.find("application")
    assert app.attrs == {"debuggable": False} and [c.tag for c in app.children] == ["activity"] and root.find("nope") is None


def test_every_truncation_is_a_clean_error_never_a_crash_or_a_hang():
    data = S.manifest(permissions=PERMS)
    for n in range(0, len(data)):
        try:
            parse_axml(data[:n])
        except ApkError:
            pass


def test_corrupt_chunks_are_rejected():
    data = bytearray(S.manifest())
    with pytest.raises(ApkError, match="not a binary"):
        parse_axml(b"<?xml version='1.0'?><manifest/>")
    zero = bytearray(data)
    struct.pack_into("<I", zero, 12, 0)             # first chunk claims size 0: would loop forever
    with pytest.raises(ApkError, match="corrupt chunk"):
        parse_axml(bytes(zero))
    huge = bytearray(data)
    struct.pack_into("<I", huge, 16, 0x7FFFFFFF)    # string count far beyond the buffer
    with pytest.raises(ApkError):
        parse_axml(bytes(huge))
    flipped = bytearray(data)
    for i in range(40, len(flipped), 13):           # scattered damage must never escape as another exception type
        flipped[i] ^= 0xFF
    try:
        parse_axml(bytes(flipped))
    except ApkError:
        pass


def test_nesting_depth_is_bounded():
    el = S.El("leaf")
    for _ in range(apkinfo.MAX_DEPTH + 5):
        el = S.El("n", {}, [el])
    with pytest.raises(ApkError, match="nested"):
        parse_axml(S.build_axml(el))


def test_files_that_are_not_apks(tmp_path):
    (tmp_path / "text.apk").write_text("hello")
    with pytest.raises(ApkError, match="not a zip"):
        parse_apk(tmp_path / "text.apk")
    with zipfile.ZipFile(tmp_path / "bundle.apk", "w") as zf:
        zf.writestr("base.apk", b"x")
    with pytest.raises(ApkError, match="no AndroidManifest.xml"):
        parse_apk(tmp_path / "bundle.apk")
    S.make_apk(tmp_path / "bad.apk", b"\x03\x00\x08\x00\x10\x00\x00\x00junkjunk")
    with pytest.raises(ApkError, match="cannot read AndroidManifest.xml"):
        parse_apk(tmp_path / "bad.apk", use_aapt=False)
    S.make_apk(tmp_path / "nopkg.apk", S.build_axml(S.El("manifest", {"package": "bad name; rm -rf"})))
    with pytest.raises(ApkError, match="package name"):
        parse_apk(tmp_path / "nopkg.apk", use_aapt=False)
    with pytest.raises(ApkError, match="cannot read APK"):
        parse_apk(tmp_path / "missing.apk")


def test_oversized_manifest_is_refused_before_it_is_unpacked(tmp_path, monkeypatch):
    S.make_apk(tmp_path / "a.apk", S.manifest())
    monkeypatch.setattr(apkinfo, "MAX_MANIFEST_BYTES", 100)
    with pytest.raises(ApkError, match="more than"):
        parse_apk(tmp_path / "a.apk", use_aapt=False)


@pytest.mark.parametrize("split,kind,value", [
    ("", "base", ""), ("config.arm64_v8a", "abi", "arm64-v8a"), ("config.x86", "abi", "x86"),
    ("config.x86_64", "abi", "x86_64"), ("config.xxhdpi", "density", "xxhdpi"),
    ("config.en", "locale", "en"), ("config.pt_BR", "locale", "pt-BR"), ("config.zh_rTW", "locale", "zh-TW"),
    ("maps.config.armeabi_v7a", "abi", "armeabi-v7a"), ("maps.config.de", "locale", "de"),
    ("maps", "feature", "maps"), ("config.nodpi", "density", "nodpi"),
])
def test_classify_split(split, kind, value):
    assert classify_split(split) == (kind, value)


def test_classify_split_uses_native_code_when_the_name_says_nothing():
    assert classify_split("config.weird_token", abis=["x86_64"]) == ("abi", "x86_64")
    assert classify_split("config.weird_token") == ("other", "weird_token")
    assert classify_split("anything", is_feature=True) == ("feature", "anything")


def test_valid_package():
    assert apkinfo.valid_package("com.example.app_1") and apkinfo.valid_package("android")
    for bad in ("", "1abc.def", "a..b", "a b", "a;b", "a/b", "../x", "a.b-c", "x" * 300):
        assert not apkinfo.valid_package(bad)


def test_parse_badging_fallback_format():
    b = parse_badging("package: name='org.example.app' versionCode='12' versionName='1.2' platformBuildVersionName='13'\n"
                      "sdkVersion:'21'\ntargetSdkVersion:'33'\nuses-permission: name='android.permission.CAMERA'\n"
                      "application-label:'Example App'\nlaunchable-activity: name='org.example.app.Main'  label='' icon=''\n"
                      "native-code: 'arm64-v8a' 'x86_64'\n")
    info = apkinfo._from_badging(b, 10)
    assert (info.package, info.version_code, info.label, info.min_sdk, info.target_sdk) == ("org.example.app", 12, "Example App", 21, 33)
    assert info.abis == ["arm64-v8a", "x86_64"] and info.permissions == ["android.permission.CAMERA"]
    assert info.launcher == "org.example.app/org.example.app.Main" and info.source == "aapt"
