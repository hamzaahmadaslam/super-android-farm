"""Distribution detection and package naming."""
import pytest

from superfarm.host import distro

UBUNTU = 'PRETTY_NAME="Ubuntu 26.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="26.04"\nID=ubuntu\nID_LIKE=debian\n'
FEDORA = 'NAME="Fedora Linux"\nVERSION_ID=42\nID=fedora\nPRETTY_NAME="Fedora Linux 42 (Workstation Edition)"\n'
ROCKY = 'NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.5"\n'
ARCH = 'NAME="Arch Linux"\nID=arch\nPRETTY_NAME="Arch Linux"\n'
MANJARO = 'NAME="Manjaro Linux"\nID=manjaro\nID_LIKE=arch\n'
TUMBLEWEED = 'NAME="openSUSE Tumbleweed"\nID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n'
MINT = 'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'


def make(tmp_path, text, kernel="6.8.0-45-generic", where="etc/os-release"):
    f = tmp_path / where
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return distro.detect(tmp_path, kernel=kernel)


@pytest.mark.parametrize("text,family,manager", [
    (UBUNTU, "debian", "apt"), (MINT, "debian", "apt"), (FEDORA, "fedora", "dnf"), (ROCKY, "fedora", "dnf"),
    (ARCH, "arch", "pacman"), (MANJARO, "arch", "pacman"), (TUMBLEWEED, "suse", "zypper"),
])
def test_family_and_package_manager(tmp_path, text, family, manager):
    d = make(tmp_path, text)
    assert (d.family, d.pkg_manager, d.supported) == (family, manager, True)


def test_os_release_parsing_handles_quotes_comments_and_junk():
    data = distro.parse_os_release('# comment\nID="ubuntu"\nNAME=\'A B\'\nBROKEN\nEMPTY=\nX="unterminated\n')
    assert data["ID"] == "ubuntu" and data["NAME"] == "A B" and data["EMPTY"] == "" and "BROKEN" not in data


def test_falls_back_to_usr_lib_and_to_unknown(tmp_path):
    assert make(tmp_path, ARCH, where="usr/lib/os-release").family == "arch"
    empty = distro.detect(tmp_path / "nothing-here", kernel="6.1.0")
    assert empty.family == "unknown" and not empty.supported and empty.pkg_manager == ""
    assert "package manager" in empty.install_command("docker")


def test_unknown_derivative_is_placed_by_id_like(tmp_path):
    d = make(tmp_path, 'ID=somethingnew\nID_LIKE="ubuntu debian"\nNAME=New\n')
    assert d.family == "debian"


def test_debian_names_and_kernel_headers_follow_the_running_kernel(tmp_path):
    d = make(tmp_path, UBUNTU, kernel="7.0.0-38-generic")
    ps = d.packages("docker", "adb", "python", "kernel-headers", "build-tools")
    assert ps.names == ("docker.io", "adb", "python3", "python3-venv", "python3-dev",
                        "linux-headers-7.0.0-38-generic", "build-essential")
    assert d.install_argv(ps.names)[:4] == ["apt-get", "install", "-y", "--no-install-recommends"]
    assert d.refresh_argv() == ["apt-get", "update"]
    assert d.install_command("mokutil", "dkms") == "sudo apt-get install -y --no-install-recommends mokutil dkms"


def test_scrcpy_missing_on_fedora_gives_a_hint_not_a_package(tmp_path):
    d = make(tmp_path, FEDORA)
    ps = d.packages("scrcpy", "ffmpeg")
    assert "scrcpy" not in ps.names and ps.names == ("ffmpeg-free",)
    assert "github.com/Genymobile/scrcpy" in ps.hints["scrcpy"]
    cmd = d.install_command("scrcpy")
    assert not cmd.startswith("sudo ") and "releases" in cmd


def test_rhel_family_explains_docker_adb_and_ffmpeg(tmp_path):
    d = make(tmp_path, ROCKY, kernel="5.14.0-503.el9.x86_64")
    assert d.rhel_like and d.family == "fedora"
    ps = d.packages("docker", "adb", "ffmpeg", "zstd", "kernel-headers")
    assert ps.names == ("zstd", "kernel-devel-5.14.0-503.el9.x86_64")
    assert "docker-ce" in ps.hints["docker"] and "EPEL" in ps.hints["adb"] and "RPM Fusion" in ps.hints["ffmpeg"]
    assert not make(tmp_path, FEDORA).rhel_like


@pytest.mark.parametrize("text,kernel,expected", [
    (ARCH, "6.12.1-arch1-1", "linux-headers"), (ARCH, "6.6.63-1-lts", "linux-lts-headers"),
    (ARCH, "6.12.1-zen1-1-zen", "linux-zen-headers"), (MANJARO, "6.6.54-2-MANJARO", "linux66-headers"),
    (TUMBLEWEED, "6.11.8-1-default", "kernel-default-devel"), (TUMBLEWEED, "6.4.0-150600.23-preempt", "kernel-preempt-devel"),
])
def test_kernel_header_package_matches_kernel_flavour(tmp_path, text, kernel, expected):
    assert make(tmp_path, text, kernel=kernel).packages("kernel-headers").names == (expected,)


def test_every_key_resolves_on_every_family_without_error(tmp_path):
    for text in (UBUNTU, FEDORA, ROCKY, ARCH, TUMBLEWEED):
        d = make(tmp_path, text)
        for key in distro._PACKAGES:
            ps = d.packages(key)
            assert ps.names or key in ps.hints, (d.id, key)
            if ps.names:
                assert d.install_argv(ps.names)[0] in ("apt-get", "dnf", "pacman", "zypper")


def test_bad_inputs_are_refused(tmp_path):
    d = make(tmp_path, UBUNTU)
    with pytest.raises(KeyError):
        d.packages("no-such-thing")
    with pytest.raises(ValueError):
        d.install_argv(["good", "bad; rm -rf /"])
    assert d.install_argv([]) == []
    with pytest.raises(ValueError):
        distro.Distro().install_argv(["x"])
