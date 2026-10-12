"""Small, careful file helpers: atomic writes, owner-only secrets, safe names."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from collections.abc import Iterator

import yaml

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, default: str = "file") -> str:
    """Reduce any client-supplied name to a plain file name (no directories, no dots-only)."""
    base = _SAFE_NAME.sub("_", os.path.basename(name or "")).strip("._")
    return base[:120] or default


def inside(root: Path, candidate: Path) -> Path:
    """Resolve candidate and insist it stays under root. Raises ValueError otherwise."""
    root_r, cand_r = root.resolve(), candidate.resolve()
    if cand_r != root_r and root_r not in cand_r.parents:
        raise ValueError(f"path escapes {root}")
    return cand_r


def atomic_write(path: Path, data: str | bytes, mode: int = 0o644) -> Path:
    """Write through a temp file + rename so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_secret(path: Path, data: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    return atomic_write(path, data, mode=0o600)


def read_yaml(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return default
    return default if data is None else data


def write_yaml(path: Path, data: Any, mode: int = 0o644) -> Path:
    return atomic_write(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True), mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any, mode: int = 0o644) -> Path:
    return atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False), mode)


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive advisory lock, for read-modify-write of shared state files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
