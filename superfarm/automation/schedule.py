"""Recurring runs: "every 30 minutes" or cron-like "m h dom mon dow".

Deliberately small. The daemon calls tick() about once a minute; tick() starts what
is due and never anything else:

  * a schedule never overlaps itself - if its previous run is still going when it
    comes due again, that occurrence is skipped (and counted), not queued;
  * occurrences missed while the daemon was down are not replayed - one run starts
    if it is overdue, and the next time is computed from now;
  * schedules are stored on disk; the variables kept with one may not be secrets.

Cron fields are minute hour day-of-month month day-of-week, each `*`, a number, a
range `a-b`, a list `a,b`, or a step `*/n` / `a-b/n`. Day-of-week is 0-6 with 0 =
Sunday (7 is also Sunday). As in cron, when both day fields are restricted a day
matching either one counts. Times are the host's local time.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..util.files import locked, read_json, write_json
from .values import check_name, looks_secret

MIN_INTERVAL = 30                    # seconds; anything faster is a loop, not a schedule
MAX_INTERVAL = 366 * 86_400
_ID = re.compile(r"^[a-z0-9]{6,32}$")
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]         # 0 = Sunday
    any_day: bool                    # day-of-month was `*`
    any_weekday: bool                # day-of-week was `*`

    def day_matches(self, moment: datetime) -> bool:
        weekday = (moment.weekday() + 1) % 7            # Python: Monday = 0; cron: Sunday = 0
        by_day, by_weekday = moment.day in self.days, weekday in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return by_weekday
        if self.any_weekday:
            return by_day
        return by_day or by_weekday                     # both restricted: either one, as cron does


def _parse_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        match = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", part)
        if not match:
            raise ValueError(f"cron {name} field {text!r}: expected *, a number, a-b, a list, or a step like */5")
        base, step = match.group(1), int(match.group(2) or 1)
        if step < 1:
            raise ValueError(f"cron {name} field {text!r}: a step must be at least 1")
        if base == "*":
            first, last = low, high
        elif "-" in base:
            first, last = (int(v) for v in base.split("-"))
        else:
            first = int(base)
            last = high if match.group(2) else first
        if not (low <= first <= high and low <= last <= high and first <= last):
            raise ValueError(f"cron {name} field {text!r}: values must be within {low}-{high}")
        values.update(range(first, last + 1, step))
    return frozenset(values)


def parse_cron(expression: str) -> Cron:
    parts = str(expression).split()
    if len(parts) != 5:
        raise ValueError("a cron schedule has five fields: minute hour day-of-month month day-of-week "
                         "(for example `30 3 * * *` = every day at 03:30)")
    sets = [_parse_field(part, name, low, high) for part, (name, low, high) in zip(parts, _FIELDS, strict=True)]
    weekdays = frozenset(0 if d == 7 else d for d in sets[4])
    cron = Cron(sets[0], sets[1], sets[2], sets[3], weekdays, parts[2] == "*", parts[4] == "*")
    if cron.any_weekday and not any(_days_in(month) >= min(cron.days) for month in cron.months):
        raise ValueError(f"cron {expression!r} can never happen (no such day in those months)")
    return cron


def _days_in(month: int) -> int:
    return 29 if month == 2 else 30 if month in (4, 6, 9, 11) else 31


def cron_next(cron: Cron, after: datetime) -> datetime:
    """The first matching minute strictly after `after`."""
    moment = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = moment + timedelta(days=366 * 8)        # Feb 29 on a given weekday can be years away
    while moment < limit:
        if moment.month not in cron.months:
            year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
            moment = moment.replace(year=year, month=month, day=1, hour=0, minute=0)
        elif not cron.day_matches(moment):
            moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
        elif moment.hour not in cron.hours:
            moment = (moment + timedelta(hours=1)).replace(minute=0)
        elif moment.minute not in cron.minutes:
            moment += timedelta(minutes=1)
        else:
            return moment
    raise ValueError("this cron schedule never comes due")


@dataclass
class Schedule:
    id: str
    name: str
    kind: str                      # program | flow
    target: str                    # program or flow name
    selector: str                  # which instances
    every: int = 0                 # seconds; 0 when `cron` is used
    cron: str = ""
    variables: dict = field(default_factory=dict)
    dry_run: bool = False
    enabled: bool = True
    created: float = 0.0
    next_run: float = 0.0
    last_run: float = 0.0
    last_task: str = ""
    last_result: str = ""
    runs: int = 0
    skipped: int = 0

    def next_after(self, now: float) -> float:
        if self.cron:
            return cron_next(parse_cron(self.cron), datetime.fromtimestamp(now)).timestamp()
        return now + self.every


class Schedules:
    def __init__(self, path: Path, start: Callable[[Schedule], Any], *, is_active: Callable[[str], bool],
                 forbidden_words: Iterable[str] = ()):
        """`start(schedule)` begins a run and returns something with an `.id` (a Task);
        `is_active(task_id)` says whether that run is still going."""
        self.path = Path(path)
        self._start = start
        self._is_active = is_active
        self.forbidden_words = list(forbidden_words)
        self._lock = threading.RLock()

    # -- storage -----------------------------------------------------------
    def _load(self) -> list[Schedule]:
        data = read_json(self.path, [])
        known = {f.name for f in fields(Schedule)}
        out = []
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict) and row.get("id"):
                try:
                    out.append(Schedule(**{k: v for k, v in row.items() if k in known}))
                except TypeError:
                    continue
        return out

    def _save(self, schedules: list[Schedule]) -> None:
        write_json(self.path, [asdict(s) for s in schedules], mode=0o600)

    def _validate(self, schedule: Schedule) -> Schedule:
        if not schedule.name.strip() or len(schedule.name) > 80:
            raise ValueError("a schedule needs a name (up to 80 characters)")
        if schedule.kind not in ("program", "flow"):
            raise ValueError("kind must be `program` or `flow`")
        if not schedule.target or not schedule.selector:
            raise ValueError("a schedule needs a target (program or flow name) and a selector (which instances)")
        if bool(schedule.every) == bool(schedule.cron):
            raise ValueError("give either `every` (seconds) or `cron` (m h dom mon dow), not both")
        if schedule.cron:
            parse_cron(schedule.cron)
        elif not MIN_INTERVAL <= int(schedule.every) <= MAX_INTERVAL:
            raise ValueError(f"`every` must be between {MIN_INTERVAL} seconds and a year")
        if not isinstance(schedule.variables, dict):
            raise ValueError("variables must be a mapping")
        for key in schedule.variables:
            check_name(str(key))
            if looks_secret(str(key), self.forbidden_words):
                raise ValueError(f"the variable {key!r} looks like a secret; schedules are stored on disk and "
                                 "cannot carry one")
        schedule.variables = {str(k): str(v) for k, v in schedule.variables.items()}
        schedule.every = int(schedule.every or 0)
        return schedule

    # -- editing -----------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            return [asdict(s) for s in self._load()]

    def get(self, schedule_id: str) -> Schedule:
        for schedule in self._load():
            if schedule.id == schedule_id:
                return schedule
        raise KeyError(f"no schedule {schedule_id!r}")

    def add(self, *, name: str, kind: str, target: str, selector: str, every: int = 0, cron: str = "",
            variables: dict | None = None, dry_run: bool = False, enabled: bool = True,
            now: float | None = None) -> Schedule:
        now = time.time() if now is None else now
        schedule = self._validate(Schedule(id=uuid.uuid4().hex[:12], name=name.strip(), kind=kind, target=target,
                                           selector=selector, every=int(every or 0), cron=cron.strip(),
                                           variables=dict(variables or {}), dry_run=bool(dry_run),
                                           enabled=bool(enabled), created=now))
        schedule.next_run = schedule.next_after(now)
        with self._lock, locked(self.path):
            schedules = self._load()
            schedules.append(schedule)
            self._save(schedules)
        return schedule

    def update(self, schedule_id: str, patch: dict, now: float | None = None) -> Schedule:
        allowed = {"name", "kind", "target", "selector", "every", "cron", "variables", "dry_run", "enabled"}
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))}; editable: {', '.join(sorted(allowed))}")
        now = time.time() if now is None else now
        with self._lock, locked(self.path):
            schedules = self._load()
            for index, schedule in enumerate(schedules):
                if schedule.id == schedule_id:
                    changed = Schedule(**{**asdict(schedule), **patch})
                    if "every" in patch and patch["every"]:
                        changed.cron = "" if "cron" not in patch else changed.cron
                    if "cron" in patch and patch["cron"]:
                        changed.every = 0 if "every" not in patch else changed.every
                    changed = self._validate(changed)
                    changed.next_run = changed.next_after(now)
                    schedules[index] = changed
                    self._save(schedules)
                    return changed
        raise KeyError(f"no schedule {schedule_id!r}")

    def remove(self, schedule_id: str) -> bool:
        if not _ID.match(schedule_id or ""):
            return False
        with self._lock, locked(self.path):
            schedules = self._load()
            kept = [s for s in schedules if s.id != schedule_id]
            if len(kept) == len(schedules):
                return False
            self._save(kept)
            return True

    # -- the clock ---------------------------------------------------------
    def tick(self, now: float | None = None) -> list[str]:
        """Start every schedule that is due. Returns the ids that were started."""
        now = time.time() if now is None else now
        started: list[str] = []
        with self._lock, locked(self.path):
            schedules = self._load()
            changed = False
            for schedule in schedules:
                if not schedule.enabled:
                    continue
                if not schedule.next_run:
                    schedule.next_run, changed = schedule.next_after(now), True
                    continue
                if now < schedule.next_run:
                    continue
                changed = True
                # Compute the next time from now, not from the missed slot: a daemon
                # that was down for a day must not fire a day's worth of runs.
                try:
                    schedule.next_run = schedule.next_after(now)
                except ValueError as e:
                    schedule.enabled, schedule.last_result = False, f"disabled: {e}"
                    continue
                if schedule.last_task and self._is_active(schedule.last_task):
                    schedule.skipped += 1
                    schedule.last_result = "skipped: the previous run was still going"
                    continue
                try:
                    task = self._start(schedule)
                except Exception as e:  # noqa: BLE001 - one broken schedule must not stop the others
                    schedule.last_result = f"could not start: {e}"[:300]
                    continue
                schedule.last_task = str(getattr(task, "id", "") or "")
                schedule.last_run = now
                schedule.runs += 1
                schedule.last_result = "started"
                started.append(schedule.id)
            if changed:
                self._save(schedules)
        return started
