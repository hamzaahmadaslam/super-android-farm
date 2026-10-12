"""Read what an APK says about itself, without Android build tools.

An APK is a zip. Its `AndroidManifest.xml` is not text but Android's binary XML ("AXML"),
and names such as the app label usually point into `resources.arsc`, a second binary
table. Requiring `aapt` on every farm host just to show a package name and version
would be a heavy dependency for a small job, so this module carries two compact readers:

    parse_axml()      binary XML -> a small element tree
    resolve_string()  one string resource out of resources.arsc

Both treat their input as hostile (uploads come from anywhere): every offset is checked
against the buffer, sizes are capped, and loops always advance. If `aapt2`/`aapt` happens
to be installed it is used as a second opinion for the few fields the readers could not
work out (never the other way round, so results do not depend on the host).
"""
from __future__ import annotations

import re
import struct
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from collections.abc import Iterator

from ..util.proc import CommandError, run, which

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARSC_BYTES = 256 * 1024 * 1024
MAX_ZIP_ENTRIES = 200_000
MAX_DEPTH = 64
MAX_ELEMENTS = 200_000

# Chunk types (frameworks/base ResourceTypes.h).
_RES_STRING_POOL = 0x0001
_RES_TABLE = 0x0002
_RES_XML = 0x0003
_XML_START_ELEMENT = 0x0102
_XML_END_ELEMENT = 0x0103
_XML_RESOURCE_MAP = 0x0180
_TABLE_PACKAGE = 0x0200
_TABLE_TYPE = 0x0201

_TYPE_REFERENCE, _TYPE_STRING, _TYPE_FLOAT = 0x01, 0x03, 0x04
_TYPE_INT_DEC, _TYPE_INT_HEX, _TYPE_INT_BOOL = 0x10, 0x11, 0x12
_NO_ENTRY = 0xFFFFFFFF

# Framework attribute ids. Shrinkers and obfuscators blank the attribute *names* in the
# string pool because Android itself only looks at these ids; so must we.
_ATTR_IDS = {
    0x01010001: "label", 0x01010002: "icon", 0x01010003: "name", 0x0101000C: "hasCode",
    0x0101000E: "enabled", 0x0101000F: "debuggable", 0x01010010: "exported", 0x01010024: "value",
    0x01010202: "targetActivity", 0x0101020C: "minSdkVersion", 0x0101021B: "versionCode",
    0x0101021C: "versionName", 0x01010270: "targetSdkVersion", 0x01010271: "maxSdkVersion",
    0x01010272: "testOnly", 0x0101028E: "required", 0x010104EA: "extractNativeLibs",
    0x0101055B: "isFeatureSplit", 0x01010576: "versionCodeMajor", 0x01010591: "isSplitRequired",
}

ABIS = ("arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86", "mips64", "mips", "riscv64")
# Split names cannot contain `-`, so bundle tools write `arm64_v8a`; `x86_64` keeps its own underscore.
_ABI_TOKENS = {a.replace("-", "_"): a for a in ABIS}
DENSITIES = {"ldpi": 120, "mdpi": 160, "tvdpi": 213, "hdpi": 240, "xhdpi": 320, "xxhdpi": 480, "xxxhdpi": 640}
_DENSITY_WORDS = {*DENSITIES, "nodpi", "anydpi"}
_LOCALE = re.compile(r"^[a-z]{2,3}([_-]r?[A-Z]{2}|[_-][A-Z][a-z]{3}|[_-]\d{3})*\Z")
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*\Z")


class ApkError(ValueError):
    """The file is not an APK this module can read; the message says what is wrong."""


@dataclass(frozen=True)
class ResRef:
    """A value that points at a resource (`@0x7f100020`) instead of holding the data."""
    id: int

    def __str__(self) -> str:
        return f"@0x{self.id:08x}"


@dataclass
class Element:
    tag: str
    attrs: dict[str, object] = field(default_factory=dict)
    children: list[Element] = field(default_factory=list)

    def find_all(self, tag: str) -> Iterator[Element]:
        return (c for c in self.children if c.tag == tag)

    def find(self, tag: str) -> Element | None:
        return next(self.find_all(tag), None)


def valid_package(name: str) -> bool:
    return bool(name) and len(name) <= 255 and bool(_PACKAGE.match(name))


# --------------------------------------------------------------------------- binary plumbing

def _u16(data: bytes, off: int) -> int:
    if off < 0 or off + 2 > len(data):
        raise ApkError("truncated binary XML")
    return struct.unpack_from("<H", data, off)[0]


def _u32(data: bytes, off: int) -> int:
    if off < 0 or off + 4 > len(data):
        raise ApkError("truncated binary XML")
    return struct.unpack_from("<I", data, off)[0]


def _chunks(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int, int]]:
    """(type, offset, header size, size) of each chunk in data[start:end]. A chunk that
    claims to be shorter than its own header would loop forever; that is an error."""
    off = start
    while off + 8 <= end:
        ctype, hsize, size = _u16(data, off), _u16(data, off + 2), _u32(data, off + 4)
        if hsize < 8 or size < hsize or off + size > end:
            raise ApkError(f"corrupt chunk at offset {off}")
        yield ctype, off, hsize, size
        off += size


class _StringPool:
    """Strings are decoded on demand: a resource table can hold 100k of them and we
    usually want one."""

    def __init__(self, data: bytes, off: int, size: int):
        self.data, self.end = data, off + size
        self.count = _u32(data, off + 8)
        flags = _u32(data, off + 16)
        self.utf8 = bool(flags & 0x100)
        self.strings_start = off + _u32(data, off + 20)
        self.offsets_at = off + _u16(data, off + 2)
        if self.offsets_at + 4 * self.count > self.end or self.strings_start > self.end:
            raise ApkError("corrupt string pool")

    def get(self, index: int) -> str:
        if index == _NO_ENTRY or index >= self.count:
            return ""
        data, pos = self.data, self.strings_start + _u32(self.data, self.offsets_at + 4 * index)
        if pos >= self.end:
            raise ApkError("string outside its pool")
        if self.utf8:
            # Two lengths, each one or two bytes: characters (unused here), then bytes.
            pos += 2 if data[pos] & 0x80 else 1
            n = data[pos]
            if n & 0x80:
                n, pos = ((n & 0x7F) << 8) | data[pos + 1], pos + 2
            else:
                pos += 1
            return data[pos:min(pos + n, self.end)].decode("utf-8", "replace")
        n = _u16(data, pos)
        if n & 0x8000:
            n, pos = ((n & 0x7FFF) << 16) | _u16(data, pos + 2), pos + 4
        else:
            pos += 2
        return data[pos:min(pos + 2 * n, self.end)].decode("utf-16-le", "replace")


def _typed(pool: _StringPool, raw: int, dtype: int, value: int) -> object:
    if dtype == _TYPE_STRING:
        return pool.get(raw if raw != _NO_ENTRY else value)
    if dtype == _TYPE_REFERENCE:
        return ResRef(value)
    if dtype == _TYPE_INT_BOOL:
        return value != 0
    if dtype in (_TYPE_INT_DEC, _TYPE_INT_HEX):
        return value - (1 << 32) if dtype == _TYPE_INT_DEC and value & 0x80000000 else value
    if dtype == _TYPE_FLOAT:
        return struct.unpack("<f", struct.pack("<I", value))[0]
    return value


def parse_axml(data: bytes) -> Element:
    """Binary XML -> element tree. Namespaces are dropped (`android:name` -> `name`):
    a manifest has no two attributes that differ only by namespace."""
    try:
        return _parse_axml(data)
    except (IndexError, struct.error):
        raise ApkError("truncated binary XML") from None


def _parse_axml(data: bytes) -> Element:
    if len(data) < 8 or _u16(data, 0) != _RES_XML:
        raise ApkError("not a binary AndroidManifest.xml (a plain-text manifest means this is not a built APK)")
    total = min(_u32(data, 4), len(data))
    pool: _StringPool | None = None
    res_ids: list[int] = []
    root: Element | None = None
    stack: list[Element] = []
    count = 0
    for ctype, off, hsize, size in _chunks(data, _u16(data, 2), total):
        if ctype == _RES_STRING_POOL and pool is None:
            pool = _StringPool(data, off, size)
        elif ctype == _XML_RESOURCE_MAP:
            res_ids = list(struct.unpack_from(f"<{(size - hsize) // 4}I", data, off + hsize))
        elif ctype == _XML_START_ELEMENT:
            if pool is None:
                raise ApkError("binary XML has no string pool")
            count += 1
            if count > MAX_ELEMENTS or len(stack) >= MAX_DEPTH:
                raise ApkError("manifest is too large or too deeply nested")
            ext = off + hsize
            attr_start, attr_size, attr_count = _u16(data, ext + 8), _u16(data, ext + 10), _u16(data, ext + 12)
            if attr_size < 20 or ext + attr_start + attr_count * attr_size > off + size:
                raise ApkError("corrupt element attributes")
            el = Element(pool.get(_u32(data, ext + 4)))
            for i in range(attr_count):
                a = ext + attr_start + i * attr_size
                name_idx = _u32(data, a + 4)
                rid = res_ids[name_idx] if name_idx < len(res_ids) else 0
                name = _ATTR_IDS.get(rid) or pool.get(name_idx)
                if name:
                    el.attrs[name] = _typed(pool, _u32(data, a + 8), data[a + 15], _u32(data, a + 16))
            if stack:
                stack[-1].children.append(el)
            elif root is None:
                root = el
            stack.append(el)
        elif ctype == _XML_END_ELEMENT and stack:
            stack.pop()
    if root is None:
        raise ApkError("binary XML has no root element")
    return root


# --------------------------------------------------------------------------- resources.arsc

def _config_rank(data: bytes, off: int) -> int:
    """How good a resource configuration is for a label shown in an English panel:
    0 the default (no qualifiers), 1 English, 2 anything else."""
    size = _u32(data, off)
    body = data[off + 4:off + max(size, 4)]
    if not any(body):
        return 0
    return 1 if size >= 12 and data[off + 8:off + 10] == b"en" else 2


def _table_entry(data: bytes, off: int, hsize: int, size: int, entry: int) -> tuple[int, int] | None:
    """(data type, value) of one simple entry in a ResTable_type chunk, or None."""
    flags, entry_count, entries_start = data[off + 9], _u32(data, off + 12), _u32(data, off + 16)
    table = off + hsize
    if flags & 0x01:      # sparse: sorted (index, offset/4) pairs
        pos = None
        for i in range(min(entry_count, (off + size - table) // 4)):
            if _u16(data, table + 4 * i) == entry:
                pos = _u16(data, table + 4 * i + 2) * 4
                break
        if pos is None:
            return None
    elif flags & 0x02:    # 16-bit offsets, in units of 4 bytes
        if entry >= entry_count:
            return None
        pos = _u16(data, table + 2 * entry)
        if pos == 0xFFFF:
            return None
        pos *= 4
    else:
        if entry >= entry_count:
            return None
        pos = _u32(data, table + 4 * entry)
        if pos == _NO_ENTRY:
            return None
    e = off + entries_start + pos
    if e + 8 > off + size:
        return None
    eflags = _u16(data, e + 2)
    if eflags & 0x0008:   # compact entry: type in the high byte of flags, value where the key would be
        return eflags >> 8, _u32(data, e + 4)
    if eflags & 0x0001 or e + 16 > off + size:   # a map (style, plurals...), not a single value
        return None
    return data[e + 11], _u32(data, e + 12)


def resolve_string(arsc: bytes, res_id: int, _depth: int = 0) -> str | None:
    """Look one string resource up in resources.arsc, preferring the default
    configuration. Returns None when the id is not a string in this table."""
    if _depth > 8 or len(arsc) < 12 or _u16(arsc, 0) != _RES_TABLE:
        return None
    package_id, type_id, entry = res_id >> 24, (res_id >> 16) & 0xFF, res_id & 0xFFFF
    pool: _StringPool | None = None
    best: tuple[int, int, int] | None = None     # (rank, data type, value)
    try:
        for ctype, off, hsize, size in _chunks(arsc, _u16(arsc, 2), min(_u32(arsc, 4), len(arsc))):
            if ctype == _RES_STRING_POOL and pool is None:
                pool = _StringPool(arsc, off, size)
            elif ctype == _TABLE_PACKAGE and _u32(arsc, off + 8) == package_id:
                for sub, soff, shsize, ssize in _chunks(arsc, off + hsize, off + size):
                    if sub != _TABLE_TYPE or arsc[soff + 8] != type_id:
                        continue
                    found = _table_entry(arsc, soff, shsize, ssize, entry)
                    if found is None:
                        continue
                    rank = _config_rank(arsc, soff + 20)
                    if best is None or rank < best[0]:
                        best = (rank, *found)
                    if rank == 0:
                        break
        if best is None or pool is None:
            return None
        _, dtype, value = best
        if dtype == _TYPE_STRING:
            return pool.get(value)
        if dtype == _TYPE_REFERENCE and value != res_id:
            return resolve_string(arsc, value, _depth + 1)
    except (ApkError, IndexError, struct.error):
        return None
    return None


# --------------------------------------------------------------------------- APK level

@dataclass
class ApkInfo:
    package: str
    version_code: int = 0
    version_name: str = ""
    label: str = ""
    min_sdk: int = 1
    target_sdk: int = 0
    abis: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    split: str = ""                 # "" for a base APK, else the split's name
    config_for_split: str = ""      # the feature split a config split belongs to ("" = base)
    is_feature_split: bool = False
    split_required: bool = False    # base refuses to install without its splits
    debuggable: bool = False
    launcher: str = ""              # `package/activity` started by the launcher icon, if any
    size: int = 0
    source: str = "axml"            # which reader produced this: axml | aapt

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_base(self) -> bool:
        return not self.split


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return default


def _component(package: str, name: str) -> str:
    if not name:
        return ""
    full = package + name if name.startswith(".") else name if "." in name else f"{package}.{name}"
    return f"{package}/{full}"


def _launcher(package: str, application: Element | None) -> str:
    if application is None:
        return ""
    for act in application.children:
        if act.tag not in ("activity", "activity-alias") or act.attrs.get("enabled") is False:
            continue
        for f in act.find_all("intent-filter"):
            actions = {a.attrs.get("name") for a in f.find_all("action")}
            cats = {c.attrs.get("name") for c in f.find_all("category")}
            if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in cats:
                return _component(package, str(act.attrs.get("name", "")))
    return ""


def info_from_manifest(root: Element) -> tuple[ApkInfo, object]:
    """ApkInfo from a parsed manifest, plus the raw label (a str, a ResRef or None)."""
    if root.tag != "manifest":
        raise ApkError(f"manifest root element is <{root.tag}>, expected <manifest>")
    package = str(root.attrs.get("package", ""))
    if not valid_package(package):
        raise ApkError(f"manifest has no valid package name ({package[:80]!r})")
    info = ApkInfo(package=package)
    info.version_code = (_int(root.attrs.get("versionCodeMajor")) << 32) | (_int(root.attrs.get("versionCode")) & 0xFFFFFFFF)
    vn = root.attrs.get("versionName")
    info.version_name = "" if vn is None or isinstance(vn, ResRef) else str(vn)
    info.split = str(root.attrs.get("split") or "")
    info.config_for_split = str(root.attrs.get("configForSplit") or "")
    info.is_feature_split = root.attrs.get("isFeatureSplit") is True
    info.split_required = root.attrs.get("isSplitRequired") is True
    sdk = root.find("uses-sdk")
    if sdk is not None:
        info.min_sdk = _int(sdk.attrs.get("minSdkVersion"), 1) or 1
        info.target_sdk = _int(sdk.attrs.get("targetSdkVersion"), info.min_sdk)
    perms: list[str] = []
    for tag in ("uses-permission", "uses-permission-sdk-23"):
        for el in root.find_all(tag):
            name = el.attrs.get("name")
            if isinstance(name, str) and name and name not in perms:
                perms.append(name)
    info.permissions = perms
    app = root.find("application")
    if app is not None:
        info.debuggable = app.attrs.get("debuggable") is True
    info.launcher = _launcher(package, app)
    return info, (app.attrs.get("label") if app is not None else None)


def classify_split(split: str, *, is_feature: bool = False, abis: list[str] | None = None) -> tuple[str, str]:
    """What a split is for: (`base`|`abi`|`density`|`locale`|`feature`|`other`, value).

    Config splits are named `config.<qualifier>` (or `<feature>.config.<qualifier>`) by
    every bundle tool; the qualifier says which devices need the split."""
    if not split:
        return "base", ""
    if is_feature:
        return "feature", split
    _, sep, token = split.rpartition("config.")
    if not sep and not split.startswith("config"):
        return "feature", split
    token = token or split
    if token in _ABI_TOKENS:
        return "abi", _ABI_TOKENS[token]
    if token in _DENSITY_WORDS:
        return "density", token
    if _LOCALE.match(token):
        parts = re.split(r"[_-]", token)
        return "locale", "-".join([parts[0], *(p[1:] if p.startswith("r") and len(p) == 3 else p for p in parts[1:])])
    if abis and len(abis) == 1:
        return "abi", abis[0]
    return "other", token


def _native_abis(names: list[str]) -> list[str]:
    found = {m.group(1) for n in names if (m := re.match(r"^lib/([A-Za-z0-9_-]+)/[^/]+\.so$", n))}
    return [a for a in ABIS if a in found] + sorted(found - set(ABIS))


def _aapt_badging(path: Path) -> dict | None:
    """`aapt2 dump badging` as a dict, or None when no tool is installed or it fails."""
    tool = which("aapt2") or which("aapt")
    if not tool:
        return None
    try:
        out = run([tool, "dump", "badging", str(path)], timeout=30).stdout
    except CommandError:
        return None
    return parse_badging(out)


def parse_badging(text: str) -> dict:
    out: dict = {"permissions": [], "abis": []}
    for line in text.splitlines():
        if line.startswith("package:"):
            for key, dest in (("name", "package"), ("versionCode", "version_code"), ("versionName", "version_name")):
                m = re.search(rf"\b{key}='([^']*)'", line)
                if m:
                    out[dest] = m.group(1)
        elif line.startswith(("sdkVersion:", "minSdkVersion:")):
            out["min_sdk"] = line.split(":", 1)[1].strip().strip("'")
        elif line.startswith("targetSdkVersion:"):
            out["target_sdk"] = line.split(":", 1)[1].strip().strip("'")
        elif line.startswith("application-label:"):
            out["label"] = line.split(":", 1)[1].strip().strip("'")
        elif line.startswith("uses-permission:"):
            m = re.search(r"name='([^']*)'", line)
            if m and m.group(1) not in out["permissions"]:
                out["permissions"].append(m.group(1))
        elif line.startswith("native-code:"):
            out["abis"] = re.findall(r"'([^']*)'", line)
        elif line.startswith("launchable-activity:"):
            m = re.search(r"name='([^']*)'", line)
            if m:
                out["launcher_activity"] = m.group(1)
    return out


def _from_badging(b: dict, size: int) -> ApkInfo:
    package = str(b.get("package", ""))
    if not valid_package(package):
        raise ApkError("could not read a package name from this APK")
    info = ApkInfo(package=package, version_code=_int(b.get("version_code")), version_name=str(b.get("version_name", "")),
                   label=str(b.get("label", "")), min_sdk=_int(b.get("min_sdk"), 1) or 1,
                   target_sdk=_int(b.get("target_sdk")), abis=list(b.get("abis", [])),
                   permissions=list(b.get("permissions", [])), size=size, source="aapt")
    if b.get("launcher_activity"):
        info.launcher = _component(package, b["launcher_activity"])
    return info


def _read_member(zf: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    try:
        member = zf.getinfo(name)
    except KeyError:
        return None
    if member.file_size > limit:
        raise ApkError(f"{name} is {member.file_size} bytes, more than the {limit} this reader accepts")
    with zf.open(member) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ApkError(f"{name} unpacks to more than it declares")
    return data


def parse_apk(path: Path | str, *, use_aapt: bool = True) -> ApkInfo:
    """Describe one APK file. Raises ApkError (with the reason) for anything that is not
    a readable APK."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if len(names) > MAX_ZIP_ENTRIES:
                raise ApkError(f"APK has {len(names)} entries, more than {MAX_ZIP_ENTRIES}")
            manifest = _read_member(zf, "AndroidManifest.xml", MAX_MANIFEST_BYTES)
            if manifest is None:
                raise ApkError("no AndroidManifest.xml inside: this is not an APK (an app *bundle* such as "
                               ".xapk/.apks/.apkm holds APKs and must be added as that type)")
            abis = _native_abis(names)
            try:
                info, label = info_from_manifest(parse_axml(manifest))
            except (ApkError, IndexError, struct.error) as e:
                badging = _aapt_badging(path) if use_aapt else None
                if not badging:
                    raise ApkError(f"cannot read AndroidManifest.xml: {e}") from None
                info, label = _from_badging(badging, size), None
            info.abis, info.size = info.abis or abis, size
            if isinstance(label, str):
                info.label = label
            elif isinstance(label, ResRef):
                arsc = _read_member(zf, "resources.arsc", MAX_ARSC_BYTES)
                info.label = (resolve_string(arsc, label.id) if arsc else None) or ""
    except zipfile.BadZipFile:
        raise ApkError("not a zip archive, so not an APK") from None
    except (OSError, RuntimeError, NotImplementedError) as e:   # unreadable, encrypted or exotic compression
        raise ApkError(f"cannot read APK: {e}") from None
    if not info.label and info.is_base and use_aapt and info.source == "axml":
        badging = _aapt_badging(path)
        if badging and badging.get("label"):
            info.label = str(badging["label"])
    return info
