"""Builders and fakes shared by the control tests (not tests themselves).

    build_axml / manifest / make_apk / make_bundle   tiny but well-formed Android packages,
        written independently of the parser under test (own attribute-id table, own writer)
    make_arsc        a two-configuration resources.arsc holding one string
    sign_apk         APK Signature Scheme v2 with a throwaway key (needs the openssl CLI);
                     only the live tests install anything
    FakeAdb / FakeManager   stand-ins for a device and for the instance manager

Other test modules load this file by path (see `load_support` in each), so it works
whatever import mode pytest runs in.
"""
from __future__ import annotations

import hashlib
import io
import re
import shlex
import struct
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from superfarm.config import Config
from superfarm.core.adb import AdbError
from superfarm.core.models import InstanceSpec, parse_selector, resolve
from superfarm.util.proc import Result

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ATTR_IDS = {
    "label": 0x01010001, "name": 0x01010003, "hasCode": 0x0101000C, "debuggable": 0x0101000F,
    "minSdkVersion": 0x0101020C, "versionCode": 0x0101021B, "versionName": 0x0101021C,
    "targetSdkVersion": 0x01010270, "isFeatureSplit": 0x0101055B, "versionCodeMajor": 0x01010576,
    "isSplitRequired": 0x01010591, "exported": 0x01010010,
}


@dataclass(frozen=True)
class Ref:
    id: int


@dataclass
class El:
    tag: str
    attrs: dict = field(default_factory=dict)       # "android:name" or plain "package" -> str | int | bool | Ref
    children: list = field(default_factory=list)


# --------------------------------------------------------------------------- AXML writer

def _pool(strings: list[str], utf8: bool) -> bytes:
    blobs = []
    for s in strings:
        if utf8:
            raw = s.encode("utf-8")
            def n(v):  # one or two length bytes
                return bytes([v]) if v < 0x80 else bytes([0x80 | (v >> 8), v & 0xFF])
            blobs.append(n(len(s)) + n(len(raw)) + raw + b"\x00")
        else:
            blobs.append(struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\x00\x00")
    offsets, pos = [], 0
    for b in blobs:
        offsets.append(pos)
        pos += len(b)
    data = b"".join(blobs)
    data += b"\x00" * (-len(data) % 4)
    header_size = 28
    strings_start = header_size + 4 * len(strings)
    body = struct.pack(f"<{len(offsets)}I", *offsets) + data
    return struct.pack("<HHIIIIII", 0x0001, header_size, header_size + len(body), len(strings), 0,
                       0x100 if utf8 else 0, strings_start, 0) + body


def build_axml(root: El, *, utf8: bool = False, blank_names: bool = False) -> bytes:
    """Serialise an element tree as Android binary XML. blank_names empties the names of
    framework attributes in the string pool, as shrinkers do (only the resource ids remain)."""
    framework: list[str] = []

    def collect(el: El) -> None:
        for key in el.attrs:
            if key.startswith("android:") and key[8:] not in framework:
                framework.append(key[8:])
        for c in el.children:
            collect(c)

    collect(root)
    framework.sort(key=lambda a: ATTR_IDS[a])
    strings: list[str] = ["" if blank_names else a for a in framework]
    index: dict[str, int] = {}

    def sid(s: str) -> int:
        if s not in index:
            index[s] = len(strings)
            strings.append(s)
        return index[s]

    body = b""
    ns_prefix, ns_uri = sid("android"), sid(ANDROID_NS)
    body += struct.pack("<HHIIIII", 0x0100, 16, 24, 1, 0xFFFFFFFF, ns_prefix, ns_uri)

    def emit(el: El) -> bytes:
        keys = sorted(el.attrs, key=lambda k: (0, ATTR_IDS[k[8:]]) if k.startswith("android:") else (1, 0))
        attrs = b""
        for key in keys:
            value = el.attrs[key]
            is_fw = key.startswith("android:")
            name_idx = framework.index(key[8:]) if is_fw else sid(key)
            raw = 0xFFFFFFFF
            if isinstance(value, bool):
                dtype, data = 0x12, 0xFFFFFFFF if value else 0
            elif isinstance(value, Ref):
                dtype, data = 0x01, value.id
            elif isinstance(value, int):
                dtype, data = 0x10, value & 0xFFFFFFFF
            else:
                raw = data = sid(str(value))
                dtype = 0x03
            attrs += struct.pack("<IIIHBBI", ns_uri if is_fw else 0xFFFFFFFF, name_idx, raw, 8, 0, dtype, data)
        name = sid(el.tag)
        out = struct.pack("<HHIIIIIHHHHHH", 0x0102, 16, 36 + len(attrs), 1, 0xFFFFFFFF, 0xFFFFFFFF, name,
                          20, 20, len(keys), 0, 0, 0) + attrs
        for c in el.children:
            out += emit(c)
        return out + struct.pack("<HHIIIII", 0x0103, 16, 24, 1, 0xFFFFFFFF, 0xFFFFFFFF, name)

    tree = emit(root)
    body += tree + struct.pack("<HHIIIII", 0x0101, 16, 24, 1, 0xFFFFFFFF, ns_prefix, ns_uri)
    resmap = struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(framework)) + struct.pack(
        f"<{len(framework)}I", *(ATTR_IDS[a] for a in framework))
    payload = _pool(strings, utf8) + resmap + body
    return struct.pack("<HHI", 0x0003, 8, 8 + len(payload)) + payload


def manifest(package: str = "org.example.app", version_code: int = 7, version_name: str = "1.2.3", *,
             split: str | None = None, config_for: str | None = None, feature: bool = False,
             label: object = "Example", permissions: tuple[str, ...] = (), min_sdk: int = 21, target_sdk: int = 33,
             launcher: str | None = ".Main", debuggable: bool = False, version_major: int | None = None,
             utf8: bool = False, blank_names: bool = False, has_code: bool | None = None) -> bytes:
    root = El("manifest", {"android:versionCode": version_code, "package": package})
    if version_major is not None:
        root.attrs["android:versionCodeMajor"] = version_major
    if split is None:
        root.attrs["android:versionName"] = version_name
        root.children.append(El("uses-sdk", {"android:minSdkVersion": min_sdk, "android:targetSdkVersion": target_sdk}))
        for p in permissions:
            root.children.append(El("uses-permission", {"android:name": p}))
        app = El("application", {"android:label": label})
        if debuggable:
            app.attrs["android:debuggable"] = True
        if has_code is not None:
            app.attrs["android:hasCode"] = has_code
        if launcher:
            app.children.append(El("activity", {"android:name": launcher, "android:exported": True}, [
                El("intent-filter", {}, [El("action", {"android:name": "android.intent.action.MAIN"}),
                                         El("category", {"android:name": "android.intent.category.LAUNCHER"})])]))
        root.children.append(app)
    else:
        root.attrs["split"] = split
        if config_for is not None:
            root.attrs["configForSplit"] = config_for
        if feature:
            root.attrs["android:isFeatureSplit"] = True
        root.children.append(El("application", {"android:hasCode": False}))
    return build_axml(root, utf8=utf8, blank_names=blank_names)


def make_apk(path: Path | None = None, manifest_bytes: bytes | None = None, *, abis: tuple[str, ...] = (),
             extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("AndroidManifest.xml", manifest_bytes if manifest_bytes is not None else manifest())
        for abi in abis:
            zf.writestr(f"lib/{abi}/libnative.so", b"\x7fELF" + abi.encode())
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    data = buf.getvalue()
    if path is not None:
        Path(path).write_bytes(data)
    return data


def make_bundle(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return Path(path)


def make_arsc(res_id: int, default: str, french: str) -> bytes:
    """A resource table with one string resource in two configurations. The French one
    comes first in the file, so a reader that takes the first match gets the wrong answer."""
    package_id, type_id, entry = res_id >> 24, (res_id >> 16) & 0xFF, res_id & 0xFFFF
    global_pool = _pool([french, default], utf8=True)
    type_pool = _pool(["attr"] * (type_id - 1) + ["string"], utf8=False)
    key_pool = _pool(["app_name"], utf8=False)

    def type_chunk(language: bytes, string_index: int) -> bytes:
        config = struct.pack("<IHH2s2s", 64, 0, 0, language, b"\x00\x00").ljust(64, b"\x00")
        offsets = struct.pack(f"<{entry + 1}I", *([0xFFFFFFFF] * entry + [0]))
        value = struct.pack("<HHI", 8, 0, 0) + struct.pack("<HBBI", 8, 0, 0x03, string_index)
        header_size = 20 + len(config)
        return struct.pack("<HHIBBHII", 0x0201, header_size, header_size + len(offsets) + len(value), type_id, 0, 0,
                           entry + 1, header_size + len(offsets)) + config + offsets + value

    spec = struct.pack("<HHIBBHI", 0x0202, 16, 16 + 4 * (entry + 1), type_id, 0, 0, entry + 1) + b"\x00" * 4 * (entry + 1)
    children = type_pool + key_pool + spec + type_chunk(b"fr", 0) + type_chunk(b"\x00\x00", 1)
    name = "org.example.app".encode("utf-16-le").ljust(256, b"\x00")
    package = struct.pack("<HHII", 0x0200, 288, 288 + len(children), package_id) + name + struct.pack(
        "<IIIII", 288, 0, 288 + len(type_pool), 0, 0) + children
    return struct.pack("<HHII", 0x0002, 12, 12 + len(global_pool) + len(package), 1) + global_pool + package


# --------------------------------------------------------------------------- APK signing (v2)

def _lp(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def new_signing_key(folder: Path) -> tuple[Path, Path]:
    key, cert = folder / "key.pem", folder / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "30", "-subj", "/CN=superfarm-test"], check=True, capture_output=True, timeout=60)
    return key, cert


def sign_apk(unsigned: bytes, key: Path, cert: Path) -> bytes:
    """APK Signature Scheme v2 (RSA PKCS#1 v1.5, SHA-256), enough for Android 7+ to install."""
    eocd = unsigned.rfind(b"PK\x05\x06")
    cd_size, cd_offset = struct.unpack_from("<II", unsigned, eocd + 12)
    sections = [unsigned[:cd_offset], unsigned[cd_offset:cd_offset + cd_size], unsigned[eocd:]]
    chunks = [s[i:i + 1048576] for s in sections for i in range(0, len(s), 1048576)]
    digests = b"".join(hashlib.sha256(b"\xa5" + struct.pack("<I", len(c)) + c).digest() for c in chunks)
    top = hashlib.sha256(b"\x5a" + struct.pack("<I", len(chunks)) + digests).digest()
    cert_der = subprocess.run(["openssl", "x509", "-in", str(cert), "-outform", "DER"], check=True, capture_output=True, timeout=30).stdout
    pub_der = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"], check=True, capture_output=True, timeout=30).stdout
    signed_data = _lp(_lp(struct.pack("<I", 0x0103) + _lp(top))) + _lp(_lp(cert_der)) + _lp(b"")
    signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=signed_data, check=True,
                               capture_output=True, timeout=30).stdout
    signer = _lp(signed_data) + _lp(_lp(struct.pack("<I", 0x0103) + _lp(signature))) + _lp(pub_der)
    value = _lp(_lp(signer))
    pair = struct.pack("<QI", 4 + len(value), 0x7109871A) + value
    size = len(pair) + 8 + 16
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"
    new_eocd = bytearray(unsigned[eocd:])
    struct.pack_into("<I", new_eocd, 16, cd_offset + len(block))
    return sections[0] + block + sections[1] + bytes(new_eocd)


# --------------------------------------------------------------------------- fakes

class FakeAdb:
    """A device that answers from a rule list. Rules are (regex, response); the first
    regex that matches the command line wins. A response is a string (stdout), a tuple
    (exit code, stdout, stderr) or a callable taking the line."""

    binary = "adb"

    def __init__(self, serial: str = "fake:1", rules: list | None = None, online: bool = True):
        self.serial, self.online = serial, online
        self.rules: list = list(rules or [])
        self.calls: list[str] = []
        self.installs: list[tuple[list[str], dict]] = []
        self.pushed: list[tuple[str, str]] = []
        self.pulled: list[tuple[str, str]] = []
        self.keys: list[str] = []
        self.props: dict[str, str] = {"ro.build.version.sdk": "33"}
        self.settings: dict[tuple[str, str], str] = {}
        self.install_error = ""
        self.connects = self.disconnects = 0
        self.foreground = ("com.android.launcher3", "com.android.launcher3.Launcher")

    def on(self, pattern: str, response) -> FakeAdb:
        self.rules.insert(0, (pattern, response))
        return self

    def argv(self, index: int = -1) -> list[str]:
        return shlex.split(self.calls[index])

    def ran(self, pattern: str) -> list[str]:
        return [c for c in self.calls if re.search(pattern, c)]

    def _respond(self, line: str) -> tuple[int, str, str]:
        self.calls.append(line)
        if not self.online:
            return 1, "", "error: device offline"
        for pattern, response in self.rules:
            if re.search(pattern, line):
                if callable(response):
                    response = response(line)
                return (0, response, "") if isinstance(response, str) else response
        return 0, "", ""

    # the Adb surface the control modules use
    def state(self) -> str:
        return "device" if self.online else "offline"

    def connect(self, attempts: int = 5, delay: float = 1.0) -> bool:
        self.connects += 1
        return self.online

    def disconnect(self) -> None:
        self.disconnects += 1

    def raw(self, argv, *, timeout: float = 30, check: bool = True, binary: bool = False, input=None) -> Result:
        code, out, err = self._respond(argv[1] if argv[0] == "shell" else " ".join(argv))
        if check and code:
            raise AdbError(f"{self.serial}: adb {argv[0]} failed: {err or out}")
        return Result(code, out, err)

    def shell(self, command, *, timeout: float = 30, check: bool = True) -> str:
        line = command if isinstance(command, str) else " ".join(shlex.quote(str(a)) for a in command)
        return self.raw(["shell", line], timeout=timeout, check=check).stdout.strip()

    def getprop(self, name: str) -> str:
        return self.props.get(name, "")

    def setprop(self, name: str, value: str) -> None:
        self.props[name] = value

    def setting_put(self, namespace: str, key: str, value: str) -> None:
        self.settings[(namespace, key)] = value

    def setting_get(self, namespace: str, key: str) -> str:
        return self.settings.get((namespace, key), "null")

    def install(self, apks, *, replace=True, grant=True, downgrade=False, timeout=900) -> None:
        self.installs.append(([Path(a).name for a in apks], {"replace": replace, "grant": grant, "downgrade": downgrade}))
        if self.install_error or not self.online:
            raise AdbError(f"{self.serial}: install failed: {self.install_error or 'device offline'}")

    def push(self, local: str, remote: str, timeout: float = 600) -> None:
        if not self.online:
            raise AdbError("device offline")
        self.pushed.append((local, remote))

    def pull(self, remote: str, local: str, timeout: float = 600) -> None:
        self.pulled.append((remote, local))
        Path(local).write_bytes(b"pulled:" + remote.encode())

    def key(self, name) -> None:
        self.keys.append(str(name))

    def screencap_png(self, timeout: float = 20) -> bytes:
        return b"\x89PNG fake"

    def current_app(self) -> tuple[str, str]:
        return self.foreground


class FakeManager:
    """The slice of the instance manager the control modules rely on."""

    def __init__(self, ids=(1, 2, 3), config: Config | None = None):
        self.config = config or Config()
        self.specs = {i: InstanceSpec(id=i) for i in ids}
        self.devices = {i: FakeAdb(f"fake:{i}") for i in ids}
        self.bulk_calls: list[tuple[str, str]] = []

    def adb(self, instance_id: int) -> FakeAdb:
        return self.devices[instance_id]

    def select(self, selector: str) -> list[int]:
        return parse_selector(selector, self.specs, running=set(self.specs))

    def resolved(self, instance_id: int):
        return resolve(self.specs[instance_id], self.config)

    def bulk(self, action: str, selector: str, *, force: bool = False):
        self.bulk_calls.append((action, selector) + (("force",) if force else ()))
        return ("bulk", action, selector)
