"""The operator's Yes/No: a queue of approval requests kept by the daemon.

A request is created when an AI assistant asks for something that changes the farm and the
policy says "ask". It is `pending` until the operator answers in the panel or the terminal:

    pending --approve--> approved      the action runs
            --deny-----> denied        the assistant is told, with the operator's reason
            --(time)---> expired       nobody answered within ai.approval_timeout_seconds,
                                       or the assistant stopped waiting

A decision is final. Saying the same thing twice is harmless; saying the opposite afterwards
is refused, because the action may already have run.

Every change is appended to logs/approvals.jsonl (owner-only), so the history survives a
restart. A restart never decides anything: whatever was still pending is expired, because
the caller that was waiting for the answer is gone.

"Approve and remember" covers the same tool on the same Androids (or fewer) for the rest of
that assistant session. It is kept in memory only, never for destructive tools, and is gone
when the session ends, after REMEMBER_HOURS, or when the daemon restarts.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("superfarm.ai")

PENDING, APPROVED, DENIED, EXPIRED = "pending", "approved", "denied", "expired"
STATES = (PENDING, APPROVED, DENIED, EXPIRED)
KEEP = 500                    # requests kept (file and memory); the oldest decided ones go first
REMEMBER_HOURS = 8
MAX_SESSIONS = 200
_ID = re.compile(r"^[a-f0-9]{8}$")
_SESSION = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")


class Conflict(RuntimeError):
    """The request was already decided the other way, or has expired."""


@dataclass
class Approval:
    id: str
    created: float
    expires: float
    caller: str                   # who asks, e.g. "mcp:desktop-assistant"
    session: str
    tool: str
    klass: str                    # change | destructive
    summary: str                  # one line written by the farm from the validated arguments
    arguments: dict               # as sent, with secret-named values hidden
    targets: list[int]            # the Androids the action applies to, resolved when asked
    can_remember: bool = False
    state: str = PENDING
    decided_by: str = ""
    decided_at: float = 0.0
    reason: str = ""
    remembered: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Grant:
    tool: str
    scope: frozenset[int] | None      # None: the tool names no Androids
    since: float = field(default_factory=time.time)


def check_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError("bad approval id")
    return value


def check_session(value: str) -> str:
    if not isinstance(value, str) or not _SESSION.match(value):
        raise ValueError("bad session id: 1 to 80 letters, digits or _ . : -")
    return value


class Approvals:
    def __init__(self, path: Path, *, timeout_seconds: float = 300, keep: int = KEEP,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.timeout_seconds = float(timeout_seconds)
        self.keep = max(10, int(keep))
        self._clock = clock
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._items: dict[str, Approval] = {}
        self._grants: dict[str, list[_Grant]] = {}
        self._lines = 0
        self._load()

    def reload_config(self, config) -> None:
        """Requests already waiting keep the deadline they were created with."""
        self.timeout_seconds = float(config.ai.approval_timeout_seconds)

    # ------------------------------------------------------------------ disk
    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("approval history could not be read (%s); starting empty", e)
            return
        known = set(Approval.__dataclass_fields__)
        for line in text.splitlines():
            try:
                data = json.loads(line)
                item = Approval(**{k: v for k, v in data.items() if k in known})
                if item.state not in STATES:
                    raise ValueError(item.state)
            except (ValueError, TypeError):
                continue                    # a torn last line after a crash: skip it
            self._items[item.id] = item         # a later line of the same request replaces it in place
        now = self._clock()
        for item in self._items.values():
            if item.state == PENDING:
                # Whoever was waiting for this answer went away with the old daemon. Leaving
                # it pending would invite an approval that nothing acts on.
                item.state, item.decided_by, item.decided_at = EXPIRED, "daemon restart", now
                item.reason = "the daemon restarted before anybody answered"
        self._rewrite()

    def _rewrite(self) -> None:
        """Write the kept requests afresh (start-up, and when the file has grown)."""
        for old in [i for i, a in self._items.items() if a.state != PENDING][:max(0, len(self._items) - self.keep)]:
            del self._items[old]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_name(self.path.name + ".part")
            with open(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as out:
                for item in self._items.values():
                    out.write(json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(partial, self.path)
            self._lines = len(self._items)
        except OSError as e:
            log.error("approval history could not be written: %s", e)

    def _save(self, item: Approval) -> None:
        if self._lines >= 4 * self.keep or len(self._items) > self.keep + 50:
            self._rewrite()
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as out:
                out.write(json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
            self._lines += 1
        except OSError as e:
            # The decision still holds in memory; the operator must be able to find out the
            # history stopped being written.
            log.error("approval history could not be written: %s", e)

    # ------------------------------------------------------------- internals
    def _sweep(self) -> None:
        now = self._clock()
        for item in self._items.values():
            if item.state == PENDING and item.expires <= now:
                self._decide(item, EXPIRED, "timeout", "nobody answered in time")

    def _decide(self, item: Approval, state: str, by: str, reason: str = "") -> None:
        item.state, item.decided_by, item.decided_at, item.reason = state, by[:80], self._clock(), reason[:500]
        self._save(item)
        self._changed.notify_all()

    def _find(self, approval_id: str) -> Approval:
        item = self._items.get(check_id(approval_id))
        if item is None:
            raise KeyError(f"no approval request {approval_id}")
        return item

    # ---------------------------------------------------------------- public
    def create(self, *, tool: str, klass: str, summary: str, arguments: dict, targets: Iterable[int],
               caller: str, session: str, can_remember: bool = False) -> Approval:
        with self._lock:
            self._sweep()
            now = self._clock()
            approval_id = secrets.token_hex(4)
            while approval_id in self._items:
                approval_id = secrets.token_hex(4)
            item = Approval(id=approval_id, created=now, expires=now + self.timeout_seconds, caller=caller[:80],
                            session=check_session(session), tool=tool, klass=klass, summary=summary[:500],
                            arguments=arguments, targets=sorted({int(i) for i in targets}),
                            can_remember=bool(can_remember))
            self._items[item.id] = item
            self._save(item)
            self._changed.notify_all()
            return item

    def get(self, approval_id: str) -> Approval:
        with self._lock:
            self._sweep()
            return self._find(approval_id)

    def list(self, state: str | None = None, limit: int = 100) -> list[Approval]:
        """Newest first."""
        if state is not None and state not in STATES:
            raise ValueError(f"unknown state {state!r}; use one of: {', '.join(STATES)}")
        with self._lock:
            self._sweep()
            items = [a for a in reversed(self._items.values()) if state is None or a.state == state]
        return items[:max(1, int(limit))]

    def approve(self, approval_id: str, *, by: str, remember: bool = False) -> Approval:
        with self._lock:
            self._sweep()
            item = self._find(approval_id)
            if item.state == APPROVED:
                return item                       # said twice: still yes
            if item.state != PENDING:
                raise Conflict(f"this request can no longer be approved: it was already {self._outcome(item)}")
            item.remembered = bool(remember and item.can_remember)
            if item.remembered:
                self._remember(item)
            self._decide(item, APPROVED, by)
            return item

    def deny(self, approval_id: str, *, by: str, reason: str = "") -> Approval:
        with self._lock:
            self._sweep()
            item = self._find(approval_id)
            if item.state == DENIED:
                return item
            if item.state != PENDING:
                raise Conflict(f"this request can no longer be denied: it was already {self._outcome(item)}")
            self._decide(item, DENIED, by, reason)
            return item

    def cancel(self, approval_id: str, *, by: str = "assistant") -> Approval:
        """The caller stopped waiting: a pending request expires now, so nobody approves an
        action that would run with nobody receiving its result. Decided requests are left alone."""
        with self._lock:
            self._sweep()
            item = self._find(approval_id)
            if item.state == PENDING:
                self._decide(item, EXPIRED, by, "the assistant stopped waiting for the answer")
            return item

    def wait(self, approval_id: str, timeout: float) -> Approval:
        """Block until the request is decided or `timeout` seconds passed; returns it either way."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._changed:
            while True:
                self._sweep()
                item = self._find(approval_id)
                left = deadline - time.monotonic()
                if item.state != PENDING or left <= 0:
                    return item
                self._changed.wait(min(left, max(0.05, item.expires - self._clock()), 1.0))

    @staticmethod
    def _outcome(item: Approval) -> str:
        if item.state == EXPIRED:
            return f"expired ({item.reason or 'nobody answered in time'})"
        return f"{item.state} by {item.decided_by or 'someone'}"

    # -------------------------------------------------------------- remember
    def _remember(self, item: Approval) -> None:
        grants = self._grants.pop(item.session, [])
        grants.append(_Grant(item.tool, frozenset(item.targets) if item.targets else None, self._clock()))
        self._grants[item.session] = grants          # most recently used session last
        while len(self._grants) > MAX_SESSIONS:
            del self._grants[next(iter(self._grants))]

    def remembered(self, session: str, tool: str, targets: Iterable[int]) -> bool:
        """Did the operator already say "yes, and remember" to this tool on these Androids
        (or on a larger set containing them) in this session?"""
        wanted = frozenset(int(i) for i in targets)
        horizon = self._clock() - REMEMBER_HOURS * 3600
        with self._lock:
            grants = [g for g in self._grants.get(session, []) if g.since > horizon]
            if session in self._grants:
                self._grants[session] = grants
            for grant in grants:
                if grant.tool != tool:
                    continue
                if grant.scope is None:
                    if not wanted:
                        return True
                elif wanted and wanted <= grant.scope:
                    return True
        return False

    def end_session(self, session: str) -> int:
        """Forget what was remembered for a session, and expire what it still has pending."""
        with self._lock:
            self._sweep()
            forgotten = len(self._grants.pop(session, []))
            for item in self._items.values():
                if item.session == session and item.state == PENDING:
                    self._decide(item, EXPIRED, "assistant", "the assistant's session ended")
            return forgotten
