"""Android images: when one is pulled, built, rebuilt or simply used."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import FakeDocker  # noqa: E402

from superfarm.config import Config  # noqa: E402
from superfarm.core import images as im  # noqa: E402
from superfarm.core.images import ImageError, Images  # noqa: E402
from superfarm.core.tasks import Task  # noqa: E402
from superfarm.paths import Paths  # noqa: E402

BASE = "example/android:13"
TAG = "superfarm/android:13"


@pytest.fixture
def env(tmp_path):
    docker = FakeDocker()
    config = Config.model_validate({"android": {"images": {"13": {"base": BASE}}}})
    paths = Paths(tmp_path / "home").ensure()
    return docker, Images(docker, config, paths), paths


def overlay(paths, rel: str, text: str = "x", mode: int = 0o644):
    f = paths.overlays / "android" / "13" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    os.chmod(f, mode)
    return f


def instructions(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_shipped_dockerfiles_match_the_built_in_copies():
    for name, builtin in (("Dockerfile", im.ANDROID_DOCKERFILE), ("Dockerfile.tools", im.TOOLS_DOCKERFILE)):
        shipped = (im.DOCKERFILE_DIR / name).read_text()
        assert instructions(shipped) == instructions(builtin), name
    assert "COPY overlay/ /" in im.ANDROID_DOCKERFILE and "ARG BASE" in im.ANDROID_DOCKERFILE


def test_ensure_pulls_the_base_and_builds_once(env):
    docker, images, _ = env
    assert images.ensure("13") == TAG
    assert docker.names_called("pull") == [BASE]
    builds = [c for c in docker.calls if c[0] == "build"]
    assert len(builds) == 1 and builds[0][1] == TAG
    assert builds[0][2] == {"BASE": BASE, "BASE_ID": f"sha256:{BASE}", "OVERLAY_HASH": "none"}
    assert images.ensure("13") == TAG                     # second call: nothing to do
    assert len([c for c in docker.calls if c[0] in ("build", "pull")]) == 2


def test_overlay_change_triggers_a_rebuild(env):
    docker, images, paths = env
    images.ensure("13")
    overlay(paths, "vendor/lib64/hw/camera.test.so", "v1")
    assert images.overlay_files("13") == ["vendor/lib64/hw/camera.test.so"]
    first = images.overlay_hash("13")
    assert first != "none"
    images.ensure("13")
    assert len([c for c in docker.calls if c[0] == "build"]) == 2
    images.ensure("13")
    assert len([c for c in docker.calls if c[0] == "build"]) == 2          # unchanged overlay: no build
    overlay(paths, "vendor/lib64/hw/camera.test.so", "version two", 0o755)
    assert images.overlay_hash("13") != first
    images.ensure("13")
    assert len([c for c in docker.calls if c[0] == "build"]) == 3


def test_new_base_image_triggers_a_rebuild(env):
    docker, images, _ = env
    images.ensure("13")
    docker.images[BASE]["id"] = "sha256:newer-base"
    images.ensure("13")
    assert len([c for c in docker.calls if c[0] == "build"]) == 2


def test_without_pull_a_missing_base_is_an_error_that_says_what_to_run(env):
    docker, images, _ = env
    with pytest.raises(ImageError, match=f"docker pull {BASE}"):
        images.ensure("13", pull=False)
    assert docker.names_called("pull") == []


def test_failed_pull_and_failed_build_explain_themselves(env):
    docker, images, _ = env
    docker.pull_fails = True
    with pytest.raises(ImageError, match="could not pull"):
        images.ensure("13")
    docker.pull_fails, docker.build_fails = False, True
    with pytest.raises(ImageError, match="building .* failed"):
        images.ensure("13")


def test_custom_tag_is_used_as_is_and_never_built(tmp_path):
    docker = FakeDocker()
    docker.images["local/mine:1"] = {"id": "x", "labels": {}}
    config = Config.model_validate({"android": {"images": {"13": {"base": BASE, "tag": "local/mine:1"}}}})
    images = Images(docker, config, Paths(tmp_path).ensure())
    assert images.ensure("13") == "local/mine:1"
    assert not [c for c in docker.calls if c[0] in ("build", "pull")]
    with pytest.raises(ImageError, match="ready-made image"):
        images.build("13")


def test_unknown_or_malformed_versions_are_refused(env):
    _, images, _ = env
    with pytest.raises(ImageError, match="no image configured"):
        images.ensure("99")
    with pytest.raises(ImageError, match="bad Android version"):
        images.overlay_dir("../../etc")
    with pytest.raises(ImageError, match="bad image reference"):
        images.ensure_ref("--privileged")


def test_build_context_holds_dockerfile_and_overlay_and_is_cleaned_up(env, monkeypatch):
    docker, images, paths = env
    overlay(paths, "system/bin/tool", "#!/system/bin/sh\n", 0o755)
    os.chmod(paths.overlays / "android" / "13" / "system", 0o775)
    os.symlink("tool", paths.overlays / "android" / "13" / "system/bin/alias")
    os.mkfifo(paths.overlays / "android" / "13" / "system/pipe")
    seen = {}

    def build(tag, context, **kw):
        seen["dockerfile"] = open(os.path.join(context, "Dockerfile")).read()
        seen["tool_mode"] = os.stat(os.path.join(context, "overlay/system/bin/tool")).st_mode & 0o777
        seen["dir_mode"] = os.stat(os.path.join(context, "overlay/system")).st_mode & 0o777
        seen["link"] = os.readlink(os.path.join(context, "overlay/system/bin/alias"))
        seen["fifo"] = os.path.exists(os.path.join(context, "overlay/system/pipe"))
        seen["context"] = context
        docker.images[tag] = {"id": "built", "labels": {}}

    monkeypatch.setattr(docker, "build", build)
    task = Task(id="t", kind="k", title="t")
    assert images.build("13", task) == TAG
    assert "COPY overlay/ /" in seen["dockerfile"]
    assert seen["tool_mode"] == 0o755 and seen["dir_mode"] == 0o755
    assert seen["link"] == "tool" and seen["fifo"] is False
    assert "skipped 1 special file" in task.message
    assert not os.path.exists(seen["context"])


def test_status_reports_each_version(env):
    docker, images, paths = env
    row = images.status()[0]
    assert row["version"] == "13" and row["base"] == BASE and row["tag"] == TAG
    assert row["built"] is False and row["base_present"] is False and row["overlays"] == []
    images.ensure("13")
    overlay(paths, "vendor/etc/file.xml")
    row = images.status()[0]
    assert row["built"] and row["base_present"] and row["overlays"] == ["vendor/etc/file.xml"]
    assert row["stale"] and row["stale_reason"] == "overlay changed"


def test_status_orders_versions_numerically(tmp_path):
    config = Config()
    images = Images(FakeDocker(), config, Paths(tmp_path / "nowhere"))
    assert [r["version"] for r in images.status()] == ["9", "10", "11", "12", "13", "14", "15"]


def test_helper_prefers_an_image_already_present_for_simple_work(env):
    docker, images, _ = env
    assert images.helper() == "alpine:latest"                      # no build on an Android's start path
    assert not [c for c in docker.calls if c[0] == "build"]
    assert images.helper(gnu_tar=True) == im.TOOLS_TAG             # backups need GNU tar: build the tools image
    assert [c[1] for c in docker.calls if c[0] == "build"] == [im.TOOLS_TAG]
    assert images.helper() == im.TOOLS_TAG                         # once built it serves both


def test_helper_build_failure_says_how_to_fix_it(env):
    docker, images, _ = env
    docker.build_fails = True
    with pytest.raises(ImageError, match="needs the internet once"):
        images.helper(gnu_tar=True)
