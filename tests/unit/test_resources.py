"""Finding the shipped deploy/, scripts/ and native/ folders in a checkout and in an installed wheel."""
import re
from pathlib import Path

import pytest

from superfarm import resources
from superfarm.paths import PACKAGE_DIR

CHECKOUT = PACKAGE_DIR.parent


@pytest.fixture(autouse=True)
def fresh_cache():
    resources.resource_root.cache_clear()
    yield
    resources.resource_root.cache_clear()


def test_source_checkout_layout_is_found():
    assert resources.resource_root() == CHECKOUT
    for parts in (("deploy", "docker", "net", "Dockerfile"), ("deploy", "docker", "android", "Dockerfile"),
                  ("deploy", "systemd", "superfarm.service"), ("deploy", "systemd", "superfarm-host.service"),
                  ("scripts", "camera", "run_loop.py"), ("scripts", "host", "sign-module.sh"),
                  ("native", "venus", "build.sh"), ("native", "v4l2loopback", "build.sh")):
        assert resources.resource_path(*parts).is_file(), parts
    assert resources.resource_path("deploy", "docker", "net").is_dir()


def fake_install(tmp_path, monkeypatch, *, with_data=True):
    """A virtual environment as pip lays it out: <prefix>/lib/pythonX.Y/site-packages/superfarm."""
    prefix = tmp_path / "venv"
    package = prefix / "lib" / "python3.12" / "site-packages" / "superfarm"
    package.mkdir(parents=True)
    if with_data:
        (prefix / "share" / "superfarm-resources" / "deploy" / "systemd").mkdir(parents=True)
        (prefix / "share" / "superfarm-resources" / "deploy" / "systemd" / "superfarm.service").write_text("[Unit]\n")
    monkeypatch.setattr(resources, "PACKAGE_DIR", package)
    return prefix


def test_installed_wheel_layout_is_found(tmp_path, monkeypatch):
    prefix = fake_install(tmp_path, monkeypatch)
    assert resources.resource_root() == prefix / "share" / "superfarm-resources"
    assert resources.resource_path("deploy", "systemd", "superfarm.service").read_text() == "[Unit]\n"
    # A file that is not shipped is still a path, so the caller's own error can name it.
    assert not resources.resource_path("scripts", "camera", "nope.py").exists()


def test_other_packages_folders_in_site_packages_are_not_mistaken_for_ours(tmp_path, monkeypatch):
    prefix = fake_install(tmp_path, monkeypatch)
    (prefix / "lib" / "python3.12" / "site-packages" / "deploy").mkdir()     # someone else's top-level package
    assert resources.resource_root() == prefix / "share" / "superfarm-resources"


def test_broken_install_falls_back_to_the_package_parent(tmp_path, monkeypatch):
    prefix = fake_install(tmp_path, monkeypatch, with_data=False)
    monkeypatch.setattr(resources.sysconfig, "get_path", lambda name: str(tmp_path / "elsewhere"))
    assert resources.resource_root() == prefix / "lib" / "python3.12" / "site-packages"


def test_interpreter_prefix_is_the_last_resort(tmp_path, monkeypatch):
    fake_install(tmp_path, monkeypatch, with_data=False)
    other = tmp_path / "prefix2"
    (other / "share" / "superfarm-resources" / "deploy" / "systemd").mkdir(parents=True)
    monkeypatch.setattr(resources.sysconfig, "get_path", lambda name: str(other))
    assert resources.resource_root() == other / "share" / "superfarm-resources"


@pytest.mark.parametrize("parts", [(), ("superfarm",), ("etc", "passwd"), ("deploy", ".."), ("deploy", "a/b"),
                                   ("deploy", ""), ("scripts", "..", "..", "x")])
def test_only_shipped_folders_and_plain_names(parts):
    with pytest.raises(ValueError):
        resources.resource_path(*parts)


def test_every_shipped_folder_is_listed_as_wheel_data():
    """data-files are not recursive: a new folder under deploy/, scripts/ or native/ that is
    missing from pyproject.toml would silently be absent from an installed wheel."""
    text = (CHECKOUT / "pyproject.toml").read_text()
    section = text.split("[tool.setuptools.data-files]", 1)[1].split("\n[", 1)[0]
    listed = dict(re.findall(r'^"share/superfarm-resources/([^"]+)"\s*=\s*\["([^"]+)/\*"\]\s*$', section, re.M))
    assert listed and all(target == source for target, source in listed.items())
    on_disk = set()
    for top in resources.TOP_LEVEL:
        for path in (CHECKOUT / top).rglob("*"):
            rel = path.relative_to(CHECKOUT)
            # Build output folders of the native scripts (.gitignore: native/**/out, native/**/work).
            if path.is_file() and not {"__pycache__", "out", "work"} & set(rel.parts):
                on_disk.add(str(rel.parent))
    assert on_disk == set(listed)
    assert str(resources.INSTALLED_SUBDIR) == "share/superfarm-resources"


def test_wheel_data_files_have_no_subfolder_only_entries():
    # A folder that holds only folders needs no entry; one that holds files and is missing is caught above.
    assert not [p for p in (CHECKOUT / "deploy").iterdir() if p.is_file()]
    assert not [p for p in map(Path, ("scripts", "native")) if [f for f in (CHECKOUT / p).iterdir() if f.is_file()]]
