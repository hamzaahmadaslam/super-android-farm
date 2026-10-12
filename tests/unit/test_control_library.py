"""The app library: taking uploads apart safely, describing them, choosing splits per device."""
import importlib.util
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from superfarm.config import Config
from superfarm.control.library import AppLibrary, DeviceTraits, LibraryError, check_app_id, select_splits
from superfarm.paths import get_paths


def load_support():
    name = "sf_control_support"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_control_support.py"))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


S = load_support()
PKG = "org.example.game"


@pytest.fixture
def lib(tmp_path):
    return AppLibrary(get_paths(tmp_path / "home").ensure(), Config())


def base(vc=10, **kw):
    return S.make_apk(None, S.manifest(PKG, vc, "1.0", label="Game", permissions=("android.permission.CAMERA",), **kw))


def split(name, vc=10, pkg=PKG, abis=(), **kw):
    return S.make_apk(None, S.manifest(pkg, vc, split=name, **kw), abis=abis)


def bundle_entries():
    return {
        "base.apk": base(),
        "config.arm64_v8a.apk": split("config.arm64_v8a", abis=("arm64-v8a",)),
        "config.x86_64.apk": split("config.x86_64", abis=("x86_64",)),
        "config.mdpi.apk": split("config.mdpi"), "config.xhdpi.apk": split("config.xhdpi"),
        "config.xxhdpi.apk": split("config.xxhdpi"),
        "config.en.apk": split("config.en"), "config.de.apk": split("config.de"), "config.fr.apk": split("config.fr"),
        f"Android/obb/{PKG}/main.10.{PKG}.obb": b"OBBDATA" * 100,
        "manifest.json": json.dumps({"package_name": PKG, "name": "Hint Name", "version_code": "10"}).encode(),
        "icon.png": b"\x89PNG",
    }


def files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# ----------------------------------------------------------------------------- adding

def test_single_apk_is_stored_and_indexed(lib, tmp_path):
    src = tmp_path / "My App (1).apk"
    S.make_apk(src, S.manifest(PKG, 10, "1.0", label="Game", permissions=("android.permission.CAMERA",)), abis=("x86_64",))
    meta = lib.add("My App (1).apk", src)
    assert meta["id"] == f"{PKG}-10" and meta["format"] == "apk" and meta["original"] == "My_App_1_.apk"
    assert (meta["package"], meta["version_code"], meta["version_name"], meta["label"]) == (PKG, 10, "1.0", "Game")
    assert meta["abis"] == ["x86_64"] and meta["permissions"] == ["android.permission.CAMERA"] and meta["splits"] == []
    assert meta["files"] == [{"file": "base.apk", "split": "", "kind": "base", "value": "", "for": "", "size": src.stat().st_size, "abis": ["x86_64"]}]
    assert len(meta["sha256"]) == 64 and src.exists()                    # the upload is copied, not consumed
    assert lib.file(meta["id"], "base.apk").read_bytes() == src.read_bytes()
    assert lib.get(meta["id"]) == meta and lib.list() == [meta] and lib.find(PKG) == [meta]
    assert json.loads((lib.root / "index.json").read_text())["apps"][meta["id"]]["package"] == PKG
    assert not [p for p in lib.root.iterdir() if p.name.startswith(".staging")]


def test_move_consumes_the_upload(lib, tmp_path):
    src = tmp_path / "a.apk"
    src.write_bytes(base())
    lib.add("a.apk", src, move=True)
    assert not src.exists()


def test_xapk_with_splits_and_obb(lib, tmp_path):
    meta = lib.add("game.xapk", S.make_bundle(tmp_path / "game.xapk", bundle_entries()))
    assert meta["format"] == "xapk" and meta["label"] == "Game"            # the APK's own label beats the bundle's hint
    kinds = {f["file"]: (f["kind"], f["value"]) for f in meta["files"]}
    assert kinds == {
        "base.apk": ("base", ""), "split_config.arm64_v8a.apk": ("abi", "arm64-v8a"), "split_config.x86_64.apk": ("abi", "x86_64"),
        "split_config.mdpi.apk": ("density", "mdpi"), "split_config.xhdpi.apk": ("density", "xhdpi"),
        "split_config.xxhdpi.apk": ("density", "xxhdpi"), "split_config.en.apk": ("locale", "en"),
        "split_config.de.apk": ("locale", "de"), "split_config.fr.apk": ("locale", "fr")}
    assert meta["files"][0]["kind"] == "base" and meta["abis"] == ["arm64-v8a", "x86_64"]
    assert meta["obb"] == [{"file": f"main.10.{PKG}.obb", "size": 700}]
    assert lib.file(meta["id"], f"obb/main.10.{PKG}.obb").read_bytes() == b"OBBDATA" * 100
    stored = files_under(lib.dir(meta["id"]))
    assert stored == set(kinds) | {f"obb/main.10.{PKG}.obb"}                # icon and manifest.json are not kept
    assert meta["size"] == sum(lib.file(meta["id"], f).stat().st_size for f in stored)


def test_bundle_hint_is_used_for_the_label_only_when_the_apk_has_none(lib, tmp_path):
    entries = bundle_entries()
    entries["base.apk"] = S.make_apk(None, S.manifest(PKG, 10, label=S.Ref(0x7F0B0001)))   # label needs a table that is absent
    assert lib.add("g.xapk", S.make_bundle(tmp_path / "g.xapk", entries))["label"] == "Hint Name"
    entries.pop("manifest.json")
    entries["info.json"] = json.dumps({"app_name": "Mirror Name"}).encode()
    assert lib.add("g.apkm", S.make_bundle(tmp_path / "g.apkm", entries))["label"] == "Mirror Name"
    entries.pop("info.json")
    assert lib.add("g.apks", S.make_bundle(tmp_path / "g.apks", entries))["label"] == PKG


def test_apks_from_bundletool_ignores_standalone_apks(lib, tmp_path):
    # A standalone APK is a complete app for old devices; mixed in with the splits it would
    # be a second "base" and the install would be rejected.
    standalone = S.make_apk(None, S.manifest(PKG, 10, "1.0", label="Game"), abis=("arm64-v8a",), extra={"pad": b"x" * 5000})
    meta = lib.add("game.apks", S.make_bundle(tmp_path / "game.apks", {
        "toc.pb": b"\x0a\x00", "standalones/standalone-arm64_v8a_xxhdpi.apk": standalone,
        "splits/base-master.apk": base(), "splits/base-arm64_v8a.apk": split("config.arm64_v8a", abis=("arm64-v8a",)),
        "splits/base-en.apk": split("config.en")}))
    assert [f["file"] for f in meta["files"]] == ["base.apk", "split_config.arm64_v8a.apk", "split_config.en.apk"]
    assert lib.file(meta["id"], "base.apk").read_bytes() == base()
    assert files_under(lib.dir(meta["id"])) == {"base.apk", "split_config.arm64_v8a.apk", "split_config.en.apk"}


def test_foreign_and_broken_pieces_are_dropped_with_a_warning(lib, tmp_path):
    meta = lib.add("g.xapk", S.make_bundle(tmp_path / "g.xapk", {
        "base.apk": base(), "other.apk": split("config.en", pkg="org.other.app"), "old.apk": split("config.de", vc=9),
        "broken.apk": b"not a zip at all", "config.fr.apk": split("config.fr")}))
    assert [f["file"] for f in meta["files"]] == ["base.apk", "split_config.fr.apk"]
    assert len(meta["warnings"]) == 3 and any("org.other.app" in w for w in meta["warnings"]) and any("broken.apk" in w for w in meta["warnings"])
    assert files_under(lib.dir(meta["id"])) == {"base.apk", "split_config.fr.apk"}


def test_same_version_replaces_and_versions_coexist(lib, tmp_path):
    (tmp_path / "v10.apk").write_bytes(base(10))
    (tmp_path / "v11.apk").write_bytes(base(11))
    lib.add("v10.apk", tmp_path / "v10.apk")
    lib.add("g.xapk", S.make_bundle(tmp_path / "g.xapk", bundle_entries()))     # same package + version: replaces
    lib.add("v11.apk", tmp_path / "v11.apk")
    assert [m["id"] for m in lib.find(PKG)] == [f"{PKG}-11", f"{PKG}-10"]
    assert lib.get(f"{PKG}-10")["format"] == "xapk" and len(lib.list()) == 2
    assert not [p for p in lib.root.iterdir() if p.name.startswith((".staging", ".trash"))]
    lib.remove(f"{PKG}-10")
    assert [m["id"] for m in lib.list()] == [f"{PKG}-11"] and not (lib.root / f"{PKG}-10").exists()
    with pytest.raises(KeyError):
        lib.get(f"{PKG}-10")
    with pytest.raises(KeyError):
        lib.remove(f"{PKG}-10")


def test_reindex_rebuilds_from_folders(lib, tmp_path):
    meta = lib.add("g.xapk", S.make_bundle(tmp_path / "g.xapk", bundle_entries()))
    (lib.root / "index.json").unlink()
    assert lib.list() == []
    (lib.root / "junk-folder").mkdir()
    again = lib.reindex()
    assert [m["id"] for m in again] == [meta["id"]]
    assert again[0]["files"] == meta["files"] and again[0]["obb"] == meta["obb"] and again[0]["sha256"] == meta["sha256"]


# ----------------------------------------------------------------------------- hostile uploads

@pytest.mark.parametrize("evil", ["../evil.apk", "../../outside/evil.apk", "/abs/evil.apk", "a/../../evil.apk", "dir\\..\\evil.apk"])
def test_zip_slip_is_refused_and_nothing_is_written_outside(lib, tmp_path, evil):
    before = files_under(tmp_path)
    entries = {"base.apk": base(), evil: split("config.en")}
    bundle = S.make_bundle(tmp_path / "evil.xapk", entries)
    with pytest.raises(LibraryError, match="leave the archive"):
        lib.add("evil.xapk", bundle)
    assert files_under(tmp_path) - before == {"evil.xapk"}                 # no stray file, no half-added app
    assert lib.list() == [] and not [p for p in lib.root.iterdir() if p.name != "index.json.lock"]


def test_symlink_members_are_refused(lib, tmp_path):
    bundle = tmp_path / "link.xapk"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("base.apk", base())
        link = zipfile.ZipInfo("config.en.apk")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "/etc/passwd")
    with pytest.raises(LibraryError, match="symbolic link"):
        lib.add("link.xapk", bundle)
    assert lib.list() == []


def test_names_inside_the_archive_never_choose_the_location(lib, tmp_path):
    meta = lib.add("g.xapk", S.make_bundle(tmp_path / "g.xapk", {
        "deep/er/base.apk": base(), "x/y/z/config.en.apk": split("config.en"), "obb dir/we ird$(id).obb": b"o"}))
    folder = lib.dir(meta["id"])
    assert files_under(folder) == {"base.apk", "split_config.en.apk", "obb/we_ird_id_.obb"}


def test_zip_bomb_is_stopped_by_the_size_cap(tmp_path):
    lib = AppLibrary(get_paths(tmp_path / "home").ensure(), Config.model_validate({"control": {"max_app_mb": 16}}))
    bomb = tmp_path / "bomb.xapk"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("base.apk", base())
        for i in range(3):
            zf.writestr(f"pad{i}.obb", b"\x00" * (7 * 1024 * 1024))        # 21 MB of zeros in a few KB
    assert bomb.stat().st_size < 200_000
    with pytest.raises(LibraryError, match="more than 16 MB"):
        lib.add("bomb.xapk", bomb)
    assert lib.list() == [] and not [p for p in lib.root.iterdir() if p.is_dir()]


def test_too_many_members_and_oversized_uploads(lib, tmp_path, monkeypatch):
    many = tmp_path / "many.xapk"
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(60):
            zf.writestr(f"f{i}.txt", b"")
    monkeypatch.setattr("superfarm.control.library.MAX_MEMBERS", 50)
    with pytest.raises(LibraryError, match="entries"):
        lib.add("many.xapk", many)
    monkeypatch.setattr(lib, "_limit_bytes", lambda: 10)
    (tmp_path / "a.apk").write_bytes(base())
    with pytest.raises(LibraryError, match="limit"):
        lib.add("a.apk", tmp_path / "a.apk")


def test_rejections_with_helpful_messages(lib, tmp_path):
    (tmp_path / "a.apk").write_bytes(base())
    with pytest.raises(LibraryError, match="unsupported file type"):
        lib.add("app.zip", tmp_path / "a.apk")
    (tmp_path / "text.apk").write_text("not a zip")
    with pytest.raises(LibraryError, match="not a zip archive"):
        lib.add("text.apk", tmp_path / "text.apk")
    (tmp_path / "split.apk").write_bytes(split("config.en"))
    with pytest.raises(LibraryError, match="only the split 'config.en'"):
        lib.add("split.apk", tmp_path / "split.apk")
    with pytest.raises(LibraryError, match="no base APK"):
        lib.add("s.xapk", S.make_bundle(tmp_path / "s.xapk", {"config.en.apk": split("config.en")}))
    with pytest.raises(LibraryError, match="no base APK"):
        lib.add("e.xapk", S.make_bundle(tmp_path / "e.xapk", {"readme.txt": b"hi"}))
    (tmp_path / "link.apk").symlink_to(tmp_path / "a.apk")
    with pytest.raises(LibraryError, match="not a regular file"):
        lib.add("link.apk", tmp_path / "link.apk")
    assert lib.list() == []


def test_a_single_apk_misnamed_as_a_bundle_is_still_one_apk(lib, tmp_path):
    (tmp_path / "a.xapk").write_bytes(base())
    assert lib.add("a.xapk", tmp_path / "a.xapk")["format"] == "apk"


@pytest.mark.parametrize("bad", ["../etc", "a/b-1", "x-1/../../y-2", "", "nodash", "pkg-", "-5", "a b-1", "pkg-1\n"])
def test_app_ids_cannot_escape_the_library(lib, bad):
    with pytest.raises(LibraryError):
        check_app_id(bad)
    for call in (lib.get, lib.dir, lib.remove, lambda i: lib.file(i, "base.apk")):
        with pytest.raises(LibraryError):
            call(bad)


def test_file_lookup_stays_inside_the_app_folder(lib, tmp_path):
    (tmp_path / "a.apk").write_bytes(base())
    meta = lib.add("a.apk", tmp_path / "a.apk")
    with pytest.raises(ValueError):
        lib.file(meta["id"], "../index.json")
    with pytest.raises(LibraryError, match="missing"):
        lib.file(meta["id"], "split_nope.apk")


# ----------------------------------------------------------------------------- split selection

X86 = DeviceTraits(abis=["x86_64", "arm64-v8a", "x86", "armeabi-v7a"], density=160, locales=["de-DE"], sdk=33, native_bridge=True)


@pytest.fixture
def meta(lib, tmp_path):
    return lib.add("game.xapk", S.make_bundle(tmp_path / "game.xapk", bundle_entries()))


def test_selects_first_matching_abi_nearest_density_and_device_language(meta):
    sel = select_splits(meta, X86)
    assert sel.files == ["base.apk", "split_config.x86_64.apk", "split_config.mdpi.apk", "split_config.de.apk"]
    assert sel.abi == "x86_64" and sel.warnings == []
    arm_phone = DeviceTraits(abis=["arm64-v8a", "armeabi-v7a"], density=420, locales=["fr-FR", "en-US"], sdk=33)
    assert select_splits(meta, arm_phone).files == ["base.apk", "split_config.arm64_v8a.apk", "split_config.xxhdpi.apk",
                                                    "split_config.en.apk", "split_config.fr.apk"]
    dense = DeviceTraits(abis=["x86_64"], density=640, locales=["ja-JP"], sdk=33)
    assert select_splits(meta, dense).files == ["base.apk", "split_config.x86_64.apk", "split_config.xxhdpi.apk"]   # densest there is; no ja split
    assert len(select_splits(meta, X86, all_locales=True).files) == 6


def test_arm_only_app(lib, tmp_path):
    entries = {k: v for k, v in bundle_entries().items() if "x86_64" not in k}
    meta = lib.add("arm.xapk", S.make_bundle(tmp_path / "arm.xapk", entries))
    assert select_splits(meta, X86).files[1] == "split_config.arm64_v8a.apk"          # translated: fine with a bridge
    no_bridge_listed = DeviceTraits(abis=["x86_64", "arm64-v8a"], density=160, locales=["en-US"], sdk=33, native_bridge=False)
    assert "no native bridge" in select_splits(meta, no_bridge_listed).warnings[0]
    pure_x86 = DeviceTraits(abis=["x86_64", "x86"], density=160, locales=["en-US"], sdk=33, native_bridge=False)
    with pytest.raises(LibraryError, match="ARM-only.*native bridge"):
        select_splits(meta, pure_x86)


def test_monolithic_apk_abi_and_sdk_checks(lib, tmp_path):
    S.make_apk(tmp_path / "a.apk", S.manifest(PKG, 3, min_sdk=34), abis=("armeabi-v7a",))
    meta = lib.add("a.apk", tmp_path / "a.apk")
    with pytest.raises(LibraryError, match="needs Android API 34"):
        select_splits(meta, X86)
    newer = DeviceTraits(abis=["x86_64", "x86"], density=160, locales=["en-US"], sdk=34)
    with pytest.raises(LibraryError, match="armeabi-v7a"):
        select_splits(meta, newer)
    S.make_apk(tmp_path / "java.apk", S.manifest(PKG, 4))                   # no native code: runs anywhere
    assert select_splits(lib.add("java.apk", tmp_path / "java.apk"), newer).files == ["base.apk"]


def test_feature_modules_and_their_config_splits(lib, tmp_path):
    meta = lib.add("f.apks", S.make_bundle(tmp_path / "f.apks", {
        "splits/base-master.apk": base(), "splits/base-x86_64.apk": split("config.x86_64", abis=("x86_64",)),
        "splits/cam-master.apk": split("cam", feature=True),
        "splits/cam-x86_64.apk": split("cam.config.x86_64", config_for="cam", abis=("x86_64",)),
        "splits/cam-arm64_v8a.apk": split("cam.config.arm64_v8a", config_for="cam", abis=("arm64-v8a",)),
        "splits/base-other.apk": split("config.weird_token")}))
    files = select_splits(meta, X86).files
    assert files[:2] == ["base.apk", "split_cam.apk"]                       # base first, then feature modules
    assert set(files[2:]) == {"split_config.x86_64.apk", "split_config.weird_token.apk", "split_cam.config.x86_64.apk"}
    only_arm_v7 = DeviceTraits(abis=["armeabi-v7a"], density=160, locales=["en"], sdk=33)
    with pytest.raises(LibraryError, match="has native code for arm64-v8a, x86_64 but the device runs armeabi-v7a"):
        select_splits(meta, only_arm_v7)
    # The app as a whole fits the device, but one module has no piece for its CPU.
    meta["files"] = [f for f in meta["files"] if f["file"] != "split_cam.config.x86_64.apk"]
    x86_only = DeviceTraits(abis=["x86_64", "x86"], density=160, locales=["en"], sdk=33)
    with pytest.raises(LibraryError, match="no cam split for this device's CPU"):
        select_splits(meta, x86_only)


def test_selection_needs_a_base():
    with pytest.raises(LibraryError, match="no base APK"):
        select_splits({"id": "x-1", "package": "x", "files": []}, X86)
