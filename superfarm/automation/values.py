"""Values that automation types: shared, per-instance, and pools.

Three kinds, because they answer three different questions:

  * **shared** - one value per name, the same on every device, and reading it never
    consumes it: a search term, a message. It stays until someone changes it.
  * **per-instance** - one value per name *per Android*: a nickname, a city.
  * **pool** - a list of lines where every use takes one line for good: invite
    codes, one-time links. A value from a pool must never be used twice, so the
    line is written to a used-journal *before* it is removed from the pool. A crash
    between the two can lose that one value; it can never hand it out again.

Names that look like secrets (password, pin, otp, ...) are refused by every writer
here: this folder is plain files, and a credential in it would end up in backups
and model prompts. Such values are passed when a run is started and live only in
that run's memory.

    automation/values/shared.json
    automation/values/instances/<id>.json
    automation/values/pools/<name>.txt           one value per line (0600)
    automation/values/pools/<name>.used.jsonl    the used-journal (0600)
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from collections.abc import Iterable, Mapping

from ..core.models import check_id
from ..util.files import atomic_write, inside, locked, read_json, write_json

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TOKENS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+[A-Za-z]*")
MAX_VALUE = 10_000
JOURNAL_TAIL = 64 * 1024


class PoolEmpty(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"the pool {name!r} is empty: add lines to it before running again")
        self.pool = name


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"bad value name {name!r}: use letters, digits and underscores, starting with a letter")
    return name


def looks_secret(name: str, words: Iterable[str]) -> bool:
    """Whether a value's name suggests a credential.

    Short words are matched as whole parts of the name (`pin`, `user_pin`, `pinCode`)
    so that `shipping` or `discard` are not caught; long ones anywhere in it
    (`userpassword`).
    """
    lowered = (name or "").lower()
    tokens = {t.lower() for t in _TOKENS.findall(name or "")} | set(re.split(r"[^a-z0-9]+", lowered))
    for word in words:
        word = word.lower().strip()
        if not word:
            continue
        if word in tokens or (len(word) >= 6 and word in lowered):
            return True
    return False


class Values:
    def __init__(self, root: Path, forbidden_words: Iterable[str] = ()):
        self.root = Path(root)
        self.forbidden_words = [w for w in forbidden_words if w]
        self._lock = threading.RLock()

    # -- guards ------------------------------------------------------------
    def is_secret(self, name: str) -> bool:
        return looks_secret(name, self.forbidden_words)

    def _storable(self, name: str, what: str = "value") -> str:
        check_name(name)
        if self.is_secret(name):
            raise ValueError(
                f"{name!r} looks like a secret, and secrets are never stored as a {what}. Pass it when you "
                "start the run instead (run variables stay in memory only), or pick a name that does not "
                "contain: " + ", ".join(self.forbidden_words))
        return name

    @staticmethod
    def _value(value: object) -> str:
        text = str(value)
        if len(text) > MAX_VALUE:
            raise ValueError(f"value too long ({len(text)} characters, limit {MAX_VALUE})")
        return text

    # -- shared ------------------------------------------------------------
    @property
    def _shared_file(self) -> Path:
        return self.root / "shared.json"

    def shared(self) -> dict[str, str]:
        data = read_json(self._shared_file, {})
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def get_shared(self, name: str) -> str | None:
        """Read a shared value. Reading never consumes it."""
        return self.shared().get(name)

    def set_shared(self, name: str, value: object) -> None:
        self._storable(name, "shared value")
        with self._lock, locked(self._shared_file):
            data = self.shared()
            data[name] = self._value(value)
            write_json(self._shared_file, data, mode=0o600)

    def remove_shared(self, name: str) -> bool:
        with self._lock, locked(self._shared_file):
            data = self.shared()
            if name not in data:
                return False
            del data[name]
            write_json(self._shared_file, data, mode=0o600)
            return True

    # -- per instance ------------------------------------------------------
    def _instance_file(self, instance_id: int) -> Path:
        return self.root / "instances" / f"{check_id(instance_id)}.json"

    def instance(self, instance_id: int) -> dict[str, str]:
        data = read_json(self._instance_file(instance_id), {})
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def get_instance(self, instance_id: int, name: str) -> str | None:
        return self.instance(instance_id).get(name)

    def set_instance(self, instance_id: int, name: str, value: object) -> None:
        self._storable(name, "per-instance value")
        path = self._instance_file(instance_id)
        with self._lock, locked(path):
            data = self.instance(instance_id)
            data[name] = self._value(value)
            write_json(path, data, mode=0o600)

    def remove_instance(self, instance_id: int, name: str) -> bool:
        path = self._instance_file(instance_id)
        with self._lock, locked(path):
            data = self.instance(instance_id)
            if name not in data:
                return False
            del data[name]
            write_json(path, data, mode=0o600)
            return True

    # -- pools -------------------------------------------------------------
    @property
    def _pools_dir(self) -> Path:
        return self.root / "pools"

    def _pool_file(self, name: str) -> Path:
        return inside(self._pools_dir, self._pools_dir / f"{check_name(name)}.txt")

    def _journal_file(self, name: str) -> Path:
        return inside(self._pools_dir, self._pools_dir / f"{check_name(name)}.used.jsonl")

    def _pool_lines(self, name: str) -> list[str]:
        try:
            text = self._pool_file(name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [line for line in (raw.strip("\r") for raw in text.split("\n")) if line.strip()]

    def _write_pool(self, name: str, lines: list[str]) -> None:
        atomic_write(self._pool_file(name), "".join(line + "\n" for line in lines), mode=0o600)

    def _journal(self, name: str, entry: dict) -> None:
        """Append one line to the used-journal and force it to disk before returning:
        the journal is the proof a value was handed out, so it must outlive a crash."""
        path = self._journal_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _journal_tail(self, name: str) -> list[dict]:
        path = self._journal_file(name)
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - JOURNAL_TAIL))
                raw = f.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return []
        out = []
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue             # the first line of a tail may be cut in half
            if isinstance(entry, dict):
                out.append(entry)
        return out

    def _finish_interrupted_take(self, name: str, lines: list[str]) -> list[str]:
        """Repair after a crash between "journalled" and "removed from the pool".

        The last journal entry is a take with no `done` marker after it. If the pool
        still starts with that value, the removal never happened: remove it now. If
        the pool starts with something else the removal did happen and only the
        marker is missing. (With two identical lines in a row this drops one extra
        value - losing a value is allowed, reusing one is not.)
        """
        tail = self._journal_tail(name)
        if not tail or tail[-1].get("done") or "value" not in tail[-1]:
            return lines
        pending = tail[-1]
        if lines and lines[0] == pending["value"]:
            lines = lines[1:]
            self._write_pool(name, lines)
        self._journal(name, {"n": pending.get("n"), "done": True, "recovered": True})
        return lines

    def pool_take(self, name: str, *, instance_id: int | None = None, run: str = "") -> str:
        """Take one line out of a pool for good."""
        pool = self._pool_file(name)
        with self._lock, locked(pool):
            lines = self._finish_interrupted_take(name, self._pool_lines(name))
            if not lines:
                raise PoolEmpty(name)
            value = lines[0]
            sequence = int(time.time() * 1000)
            # Order matters. Journal first: from here on the value counts as used,
            # whatever happens next.
            self._journal(name, {"n": sequence, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "value": value,
                                 "instance": instance_id, "run": run})
            self._write_pool(name, lines[1:])
            self._journal(name, {"n": sequence, "done": True})
            return value

    def pool_add(self, name: str, lines: Iterable[str]) -> int:
        """Append lines to a pool (created when missing). Returns how many were added."""
        self._storable(name, "pool")
        fresh = []
        for line in lines:
            text = self._value(line).strip()
            if "\n" in text or "\r" in text:
                raise ValueError("a pool value must be a single line")
            if text:
                fresh.append(text)
        pool = self._pool_file(name)
        with self._lock, locked(pool):
            current = self._finish_interrupted_take(name, self._pool_lines(name))
            self._write_pool(name, current + fresh)
        return len(fresh)

    def pool_count(self, name: str) -> int:
        return len(self._pool_lines(name))

    def pool_used(self, name: str, limit: int = 50) -> list[dict]:
        """The most recent takes (newest last), for the panel."""
        taken = [e for e in self._journal_tail(name) if "value" in e]
        return taken[-max(1, limit):]

    def pool_delete(self, name: str) -> bool:
        pool = self._pool_file(name)
        with self._lock, locked(pool):
            existed = pool.exists()
            for path in (pool, self._journal_file(name)):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            return existed

    def pools(self) -> list[dict]:
        if not self._pools_dir.is_dir():
            return []
        out = []
        for path in sorted(self._pools_dir.glob("*.txt")):
            if _NAME.match(path.stem):
                out.append({"name": path.stem, "remaining": self.pool_count(path.stem),
                            "used": sum(1 for e in self._journal_tail(path.stem) if "value" in e)})
        return out


class Resolver:
    """What `{name}` means for one device during one run.

    Looked up in this order: variables of the run (flow defaults, then what the
    operator passed, then anything a `set` step captured), the built-ins
    `instance_id` and `serial`, `pool.<name>` (one line taken from that pool, the
    same line for the rest of this run), the device's per-instance values, and the
    shared values.
    """

    def __init__(self, values: Values | None = None, *, instance_id: int | None = None, serial: str = "",
                 variables: Mapping[str, object] | None = None, run_id: str = "", consume: bool = True):
        self.values = values
        self.consume = consume          # False in a practice run: pools are left untouched
        self.instance_id = instance_id
        self.serial = serial
        self.run_id = run_id
        self.vars: dict[str, str] = {str(k): str(v) for k, v in (variables or {}).items()}
        self._taken: dict[str, str] = {}
        self._secrets: set[str] = set()
        for name, value in self.vars.items():
            self._note_secret(name, value)

    def _note_secret(self, name: str, value: str) -> None:
        if value and len(value) >= 3 and self.is_secret(name):
            self._secrets.add(value)

    def is_secret(self, name: str) -> bool:
        return bool(self.values and self.values.is_secret(name))

    def get(self, name: str) -> str | None:
        if name in self.vars:
            return self.vars[name]
        if name == "instance_id":
            return None if self.instance_id is None else str(self.instance_id)
        if name == "serial":
            return self.serial or None
        if name.startswith("pool."):
            pool = name[5:]
            if pool not in self._taken:
                self._taken[pool] = self.take(pool)
            return self._taken[pool]
        if self.values is None:
            return None
        if self.instance_id is not None:
            value = self.values.get_instance(self.instance_id, name)
            if value is not None:
                return value
        return self.values.get_shared(name)

    def set(self, name: str, value: object) -> None:
        """Set a variable for the rest of this run. Never written to disk."""
        check_name(name)
        self.vars[name] = str(value)
        self._note_secret(name, self.vars[name])

    def take(self, pool: str) -> str:
        """Take a fresh line from a pool (always consumes one, except in a practice run)."""
        check_name(pool)
        if self.values is None:
            raise PoolEmpty(pool)
        if not self.consume:
            if self.values.pool_count(pool) == 0:
                raise PoolEmpty(pool)
            return f"<a line from pool {pool}>"
        return self.values.pool_take(pool, instance_id=self.instance_id, run=self.run_id)

    def mark_secret(self, value: str) -> None:
        if value and len(value) >= 3:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        """Remove secret values from a line about to be logged or shown."""
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text
