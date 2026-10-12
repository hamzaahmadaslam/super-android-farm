"""What a screen looked like, and what got a stuck run past it.

Two things live here because one is keyed by the other:

  * small picture helpers (difference hash, patch similarity) built on Pillow and
    numpy only, so the farm needs no computer-vision library;
  * the fix memory: recoveries that worked, keyed by the app's package and the
    screen's difference hash, replayed next time without asking a model again.

    automation/memory/fixes.jsonl     one JSON object per line

The same obstacles turn up on every device of a farm - the same permission sheet,
the same "rate limited" dialog - so a fix learned on one device is found by all.
"""
from __future__ import annotations

import io
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import numpy as np
from PIL import Image

from ..util.files import atomic_write, locked

STORE = "fixes.jsonl"
SAME_SCREEN_BITS = 10        # hash bits that may differ and still be "the same screen"
PATCH_MATCH = 0.55           # similarity a saved picture needs before coordinates are trusted
MAX_ENTRIES = 2000           # the file is read whole; keep it small enough for that


# ----------------------------------------------------------------- pictures
def to_image(data: bytes | Image.Image) -> Image.Image:
    """PNG/JPEG bytes (a screenshot) or an image -> RGB image."""
    if isinstance(data, Image.Image):
        return data.convert("RGB")
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


def dhash(image: Image.Image, hash_size: int = 8) -> int:
    """Difference hash: a 64-bit 'which screen is this' signature that survives
    small changes (a clock ticking, a blinking cursor) but not a different screen."""
    small = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
    pixels = np.asarray(small, dtype=np.int16)
    bits = (pixels[:, 1:] > pixels[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def crop_box(center: tuple[int, int], size: int, screen: tuple[int, int]) -> list[int]:
    """A square of `size` pixels around a point, clipped to the screen: [l, t, r, b]."""
    half = max(1, size // 2)
    width, height = screen
    left = max(0, min(int(center[0]) - half, max(0, width - 1)))
    top = max(0, min(int(center[1]) - half, max(0, height - 1)))
    return [left, top, max(left + 1, min(width, int(center[0]) + half)),
            max(top + 1, min(height, int(center[1]) + half))]


def crop(image: Image.Image, box: list[int] | tuple[int, int, int, int]) -> Image.Image:
    left, top, right, bottom = (int(v) for v in box)
    left, top = max(0, left), max(0, top)
    right, bottom = min(image.width, right), min(image.height, bottom)
    if right <= left or bottom <= top:
        return Image.new("RGB", (1, 1))
    return image.crop((left, top, right, bottom))


def scale_box(box: list[int], recorded: list[int] | tuple[int, int] | None,
              live: tuple[int, int]) -> list[int]:
    """Move a box recorded on one screen size onto another (devices in a farm may
    not all share one resolution)."""
    if not recorded or len(recorded) != 2 or not all(recorded) or tuple(recorded) == tuple(live):
        return [int(v) for v in box]
    sx, sy = live[0] / recorded[0], live[1] / recorded[1]
    return [int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy)]


def patch_similarity(a: Image.Image, b: Image.Image) -> float:
    """How alike two pictures of the same area are: 1.0 identical, about 0 unrelated.

    Zero-mean normalised correlation of the grey values. Two flat patches correlate
    with nothing (no detail to compare), so they are judged on brightness alone.
    """
    width, height = min(a.width, b.width), min(a.height, b.height)
    if width < 8 or height < 8:
        return 0.0
    size = (min(width, 96), min(height, 96))
    x = np.asarray(a.convert("L").resize(size, Image.Resampling.BILINEAR), dtype=np.float64)
    y = np.asarray(b.convert("L").resize(size, Image.Resampling.BILINEAR), dtype=np.float64)
    xd, yd = x - x.mean(), y - y.mean()
    sx, sy = float(np.sqrt((xd * xd).sum())), float(np.sqrt((yd * yd).sum()))
    flat = 2.0 * np.sqrt(x.size)                 # a standard deviation of two grey levels
    if sx < flat and sy < flat:
        return 1.0 if abs(float(x.mean()) - float(y.mean())) < 12 else 0.0
    if sx < flat or sy < flat:
        return 0.0
    return float((xd * yd).sum() / (sx * sy))


def save_png(image: Image.Image, path: Path) -> Path:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return atomic_write(path, buffer.getvalue())


def load_image(path: Path) -> Image.Image | None:
    try:
        with Image.open(path) as image:
            return image.convert("RGB")
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------- fix memory
@dataclass
class Remembered:
    id: str
    created: str
    package: str                   # app the screen belonged to
    dhash: int                     # what the screen looked like
    actions: list                  # bound actions that cleared it (see runtime.bind_actions)
    reason: str = "step_blocked"
    source: str = "model"          # model | human
    analysis: str = ""
    program: str = ""
    step_id: str = ""
    hits: int = 0
    misses: int = 0
    tags: list = field(default_factory=list)

    @property
    def score(self) -> float:
        """Confidence, from how it has performed since it was learned."""
        total = self.hits + self.misses
        return 1.0 if total == 0 else self.hits / total

    @property
    def distrusted(self) -> bool:
        return self.hits + self.misses >= 3 and self.score < 0.34

    def summary(self) -> dict:
        """The compact form shown to a model as an example of what worked here."""
        return {"when": f"{self.package} / {self.reason}", "what_worked": self.actions,
                "why": self.analysis[:200], "learned_from": self.source, "times_it_worked": self.hits}


class FixMemory:
    """Recoveries that worked, shared by every run on every device.

    One file, rewritten whole under a lock: several device threads (and possibly a
    second farm process) learn at the same time, and an interleaved append would
    corrupt a line.
    """

    def __init__(self, directory: Path, enabled: bool = True):
        self.dir = Path(directory)
        self.enabled = enabled
        self.path = self.dir / STORE
        self._lock = threading.RLock()

    # -- io ----------------------------------------------------------------
    def _load(self) -> list[Remembered]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        known = {f.name for f in fields(Remembered)}
        out: list[Remembered] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                out.append(Remembered(**{k: v for k, v in data.items() if k in known}))
            except (json.JSONDecodeError, TypeError):
                continue          # one damaged line must not cost every other fix
        return out

    def _write(self, entries: list[Remembered]) -> None:
        atomic_write(self.path, "".join(json.dumps(asdict(e), ensure_ascii=False) + "\n" for e in entries))

    def entries(self) -> list[Remembered]:
        with self._lock:
            return self._load()

    # -- writing -----------------------------------------------------------
    def remember(self, *, package: str, dhash: int, actions: list, reason: str = "step_blocked",
                 source: str = "model", analysis: str = "", program: str = "",
                 step_id: str = "") -> Remembered | None:
        if not self.enabled or not actions:
            return None
        with self._lock, locked(self.path):
            entries = self._load()
            entry = Remembered(id=f"m{uuid.uuid4().hex[:12]}", created=time.strftime("%Y-%m-%dT%H:%M:%S"),
                               package=package or "", dhash=int(dhash), actions=list(actions), reason=reason,
                               source=source, analysis=(analysis or "")[:300], program=program,
                               step_id=step_id, hits=1)
            entries.append(entry)
            # Distrusted fixes go first, then the least used, when the file would grow without bound.
            if len(entries) > MAX_ENTRIES:
                entries.sort(key=lambda e: (e.distrusted, -e.hits))
                entries = entries[:MAX_ENTRIES]
            self._write(entries)
        return entry

    def bump(self, entry_id: str, worked: bool) -> None:
        if not self.enabled:
            return
        with self._lock, locked(self.path):
            entries = self._load()
            for entry in entries:
                if entry.id == entry_id:
                    if worked:
                        entry.hits += 1
                    else:
                        entry.misses += 1
                    self._write(entries)
                    return

    def forget(self, entry_id: str) -> bool:
        with self._lock, locked(self.path):
            entries = self._load()
            kept = [e for e in entries if e.id != entry_id]
            if len(kept) == len(entries):
                return False
            self._write(kept)
            return True

    def prune(self) -> int:
        """Drop entries that keep failing."""
        with self._lock, locked(self.path):
            entries = self._load()
            kept = [e for e in entries if not e.distrusted]
            if len(kept) != len(entries):
                self._write(kept)
            return len(entries) - len(kept)

    # -- reading -----------------------------------------------------------
    def lookup(self, *, package: str, dhash: int, reason: str = "step_blocked",
               distance: int = SAME_SCREEN_BITS) -> Remembered | None:
        """The best fix for this app and this screen, or None.

        The package must match exactly: two apps can show near-identical dialogs,
        and a fix that taps "OK" in one may confirm a purchase in the other.
        """
        if not self.enabled:
            return None
        best: tuple[int, Remembered] | None = None
        for entry in self.entries():
            if entry.reason != reason or entry.package != (package or "") or entry.distrusted:
                continue
            gap = hamming(entry.dhash, dhash)
            if gap <= distance and (best is None or gap < best[0]):
                best = (gap, entry)
        return best[1] if best else None

    def examples(self, package: str, limit: int = 3) -> list[dict]:
        """Fixes that worked in this app before, as hints for a model."""
        if not self.enabled:
            return []
        usable = [e for e in self.entries() if e.package == package and not e.distrusted and e.actions]
        return [e.summary() for e in sorted(usable, key=lambda e: -e.hits)[:limit]]

    def stats(self) -> dict:
        entries = self.entries()
        return {"entries": len(entries), "packages": sorted({e.package for e in entries if e.package}),
                "by_source": {s: sum(1 for e in entries if e.source == s) for s in {e.source for e in entries}},
                "total_uses": sum(e.hits for e in entries)}
