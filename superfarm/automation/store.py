"""On-disk shape of the automation area.

    automation/
      datasets/<slug>/dataset.json            one goal: name, description, package
      datasets/<slug>/recordings/<rid>/       recording.json + shots/*.png
      programs/<slug>/program.json            what training produced (see program.py)
      flows/<slug>.yaml                       hand-written flows (see flows.py)
      profiles/<package>.yaml                 named selectors per app (see profiles.py)
      values/                                 shared values, per-instance values, pools
      memory/fixes.jsonl                      recoveries worth keeping
      runs/                                   screenshots taken by runs
      schedules.json                          recurring runs

A dataset is one goal. It holds several recordings of that goal, made on any
device. Training turns them into one program.

Every name that arrives from the panel or the CLI becomes a slug before it touches a
path, and every path is checked to stay under its root: a recording id or dataset
name is user input, and this tree sits next to VPN profiles and secrets.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..util.files import atomic_write, inside, read_json, write_json

_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_RID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")
_PACKAGE_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

STEP_KINDS = ("tap", "long_press", "swipe", "type", "key", "launch", "wait", "assert")


def slugify(name: str) -> str:
    """Turn a display name into a folder name. Raises when nothing usable is left."""
    slug = _SLUG_CHARS.sub("-", (name or "").strip().lower()).strip("-")[:64].strip("-")
    if not slug:
        raise ValueError("a name needs at least one letter or digit")
    return slug


def check_slug(slug: str) -> str:
    """Accept only a plain folder name. Slugs come back from clients, so they are
    validated again rather than trusted because this module produced them once."""
    if not isinstance(slug, str) or not _SLUG_OK.match(slug):
        raise ValueError(f"bad name {slug!r}: use lower-case letters, digits and dashes")
    return slug


def check_package(package: str) -> str:
    """Package names reach `am start` and file names; keep them to what Android allows."""
    if not isinstance(package, str) or not _PACKAGE_OK.match(package) or len(package) > 200:
        raise ValueError(f"bad package name {package!r}: expected something like com.example.app")
    return package


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------- steps
@dataclass
class Step:
    """One thing that happened during a recording."""
    id: str = ""
    kind: str = "tap"                  # one of STEP_KINDS
    at: float = 0.0                    # seconds from the start of the session
    device: list | None = None         # [x, y] in device pixels
    to: list | None = None             # swipe end, device pixels
    text: str | None = None            # literal text that was typed
    dynamic: str | None = None         # name of a value typed instead of literal text
    key: str | None = None             # back | enter | home ...
    package: str | None = None         # launch: the app that was started
    seconds: float = 0.0               # wait / long press duration
    shot: str | None = None            # picture of the tapped area, relative to the recording
    shot_box: list | None = None       # [l, t, r, b] in device pixels
    element: dict | None = None        # the UI element under the tap, if there was one
    candidates: list = field(default_factory=list)   # other named elements under the same point
    activity: str = ""                 # foreground activity when it happened
    note: str = ""

    def describe(self) -> str:
        element = self.element or {}
        label = element.get("text") or element.get("desc") or element.get("id") or ""
        if self.kind in ("tap", "long_press"):
            verb = "tap" if self.kind == "tap" else "long-press"
            return f"{verb} {label!r}" if label else f"{verb} at {tuple(self.device or ())}"
        if self.kind == "type":
            return f"type {{{self.dynamic}}}" if self.dynamic else f"type {self.text!r}"
        if self.kind == "key":
            return f"press {self.key}"
        if self.kind == "swipe":
            return f"swipe {tuple(self.device or ())} -> {tuple(self.to or ())}"
        if self.kind == "wait":
            return f"wait {self.seconds:.1f}s"
        if self.kind == "launch":
            return f"launch {self.package}"
        if self.kind == "assert":
            return f"expect {label!r}" if label else "expect an element"
        return self.kind

    @classmethod
    def from_dict(cls, data: dict) -> Step:
        """Tolerant load: a file written by a newer version may carry fields this one
        does not know, and that must not make a whole dataset unreadable."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", 0.0, [], {})}


@dataclass
class Recording:
    id: str
    created: str = ""
    device: str = ""                   # adb serial it was recorded on
    instance: int | None = None        # farm instance id, when known
    package: str = ""
    screen: list = field(default_factory=list)       # device [width, height]
    steps: list = field(default_factory=list)
    seconds: float = 0.0
    note: str = ""

    @property
    def taps(self) -> int:
        return sum(1 for s in self.steps if s.kind in ("tap", "long_press"))

    def to_dict(self) -> dict:
        return {"id": self.id, "created": self.created or now(), "device": self.device,
                "instance": self.instance, "package": self.package, "screen": self.screen,
                "seconds": self.seconds, "note": self.note,
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict, fallback_id: str = "") -> Recording:
        return cls(id=str(data.get("id") or fallback_id), created=data.get("created", ""),
                   device=data.get("device", ""), instance=data.get("instance"),
                   package=data.get("package", ""), screen=data.get("screen") or [],
                   seconds=float(data.get("seconds") or 0.0), note=data.get("note", ""),
                   steps=[Step.from_dict(s) for s in data.get("steps") or [] if isinstance(s, dict)])


# ------------------------------------------------------------------ dataset
class Dataset:
    """One goal: its description and every recording made of it."""

    def __init__(self, root: Path, slug: str):
        self.root = Path(root)
        self.slug = check_slug(slug)
        self.dir = inside(self.root, self.root / self.slug)
        self.meta_path = self.dir / "dataset.json"
        self.meta: dict = read_json(self.meta_path, {}) or {}

    # -- lifecycle ---------------------------------------------------------
    def exists(self) -> bool:
        return self.meta_path.is_file()

    def save(self) -> None:
        self.meta["updated"] = now()
        write_json(self.meta_path, self.meta)

    @property
    def name(self) -> str:
        return self.meta.get("name", self.slug)

    @property
    def description(self) -> str:
        return self.meta.get("description", "")

    @property
    def package(self) -> str:
        return self.meta.get("package", "")

    def update(self, *, name: str | None = None, description: str | None = None,
               package: str | None = None) -> None:
        if name is not None and name.strip():
            self.meta["name"] = name.strip()[:120]
        if description is not None:
            self.meta["description"] = description.strip()[:4000]
        if package is not None:
            self.meta["package"] = check_package(package) if package else ""
        self.save()

    # -- recordings --------------------------------------------------------
    @property
    def recordings_dir(self) -> Path:
        return self.dir / "recordings"

    def recording_ids(self) -> list[str]:
        if not self.recordings_dir.is_dir():
            return []
        return sorted(p.parent.name for p in self.recordings_dir.glob("*/recording.json"))

    def new_recording_id(self) -> str:
        # The number keeps them in order; the clock keeps a deleted-then-recreated
        # number from colliding with pictures of the recording it replaces.
        number = 1
        for rid in self.recording_ids():
            m = re.match(r"r(\d+)", rid)
            if m:
                number = max(number, int(m.group(1)) + 1)
        return f"r{number:03d}-{time.strftime('%H%M%S')}"

    def recording_dir(self, rid: str) -> Path:
        if not isinstance(rid, str) or not _RID_OK.match(rid):
            raise ValueError(f"bad recording id {rid!r}")
        return inside(self.recordings_dir, self.recordings_dir / rid)

    def load_recording(self, rid: str) -> Recording | None:
        data = read_json(self.recording_dir(rid) / "recording.json", None)
        if not isinstance(data, dict):
            return None
        return Recording.from_dict(data, fallback_id=rid)

    def recordings(self) -> list[Recording]:
        out = []
        for rid in self.recording_ids():
            recording = self.load_recording(rid)
            if recording is not None:
                out.append(recording)
        return out

    def save_recording(self, recording: Recording) -> Path:
        path = write_json(self.recording_dir(recording.id) / "recording.json", recording.to_dict())
        self.save()
        return path

    def delete_recording(self, rid: str) -> bool:
        target = self.recording_dir(rid)
        if not target.is_dir():
            return False
        shutil.rmtree(target, ignore_errors=True)
        self.save()
        return True

    def shot_path(self, rid: str, relative: str) -> Path:
        """A picture inside one recording, refusing anything that climbs out of it."""
        base = self.recording_dir(rid)
        return inside(base, base / relative)

    # -- summaries for the panel -------------------------------------------
    def summary(self, programs_dir: Path | None = None) -> dict:
        recordings = self.recordings()
        trained = bool(programs_dir and (Path(programs_dir) / self.slug / "program.json").is_file())
        return {"slug": self.slug, "name": self.name, "description": self.description,
                "package": self.package, "created": self.meta.get("created", ""),
                "updated": self.meta.get("updated", ""), "recordings": len(recordings),
                "steps": sum(len(r.steps) for r in recordings), "trained": trained}

    def recording_summaries(self) -> list[dict]:
        return [{"id": r.id, "created": r.created, "device": r.device, "instance": r.instance,
                 "steps": len(r.steps), "taps": r.taps, "seconds": round(r.seconds, 1), "note": r.note,
                 "dynamic": sorted({s.dynamic for s in r.steps if s.dynamic}),
                 "preview": [s.describe() for s in r.steps[:6]]} for r in self.recordings()]


# -------------------------------------------------------------------- store
class AutomationStore:
    """Everything automation keeps on disk, under paths.automation."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    @property
    def datasets_dir(self) -> Path: return self.root / "datasets"
    @property
    def programs_dir(self) -> Path: return self.root / "programs"
    @property
    def flows_dir(self) -> Path: return self.root / "flows"
    @property
    def profiles_dir(self) -> Path: return self.root / "profiles"
    @property
    def values_dir(self) -> Path: return self.root / "values"
    @property
    def memory_dir(self) -> Path: return self.root / "memory"
    @property
    def runs_dir(self) -> Path: return self.root / "runs"
    @property
    def schedules_file(self) -> Path: return self.root / "schedules.json"

    # -- datasets ----------------------------------------------------------
    def create_dataset(self, name: str, description: str = "", package: str = "") -> Dataset:
        slug = slugify(name)
        with self._lock:
            dataset = Dataset(self.datasets_dir, slug)
            if dataset.exists():
                raise FileExistsError(f"a dataset called {name!r} already exists (folder {slug})")
            dataset.meta = {"name": name.strip()[:120] or slug, "slug": slug,
                            "description": (description or "").strip()[:4000],
                            "package": check_package(package) if package else "",
                            "created": now(), "updated": now()}
            dataset.recordings_dir.mkdir(parents=True, exist_ok=True)
            write_json(dataset.meta_path, dataset.meta)
        return dataset

    def datasets(self) -> list[Dataset]:
        if not self.datasets_dir.is_dir():
            return []
        out = []
        for path in sorted(self.datasets_dir.glob("*/dataset.json")):
            try:
                dataset = Dataset(self.datasets_dir, path.parent.name)
            except ValueError:
                continue                 # a folder someone put there by hand; not ours
            if dataset.meta:
                out.append(dataset)
        return out

    def dataset(self, slug: str) -> Dataset:
        dataset = Dataset(self.datasets_dir, slug)
        if not dataset.exists():
            raise KeyError(f"no dataset {slug!r}")
        return dataset

    def delete_dataset(self, slug: str) -> bool:
        with self._lock:
            dataset = Dataset(self.datasets_dir, slug)
            if not dataset.exists():
                return False
            shutil.rmtree(dataset.dir, ignore_errors=True)
            return True

    # -- flows (text only; flows.py owns the meaning) -----------------------
    def flow_path(self, name: str) -> Path:
        return inside(self.flows_dir, self.flows_dir / f"{check_slug(name)}.yaml")

    def flow_names(self) -> list[str]:
        if not self.flows_dir.is_dir():
            return []
        return sorted(p.stem for p in self.flows_dir.glob("*.yaml") if _SLUG_OK.match(p.stem))

    def read_flow(self, name: str) -> str:
        path = self.flow_path(name)
        if not path.is_file():
            raise KeyError(f"no flow {name!r}")
        return path.read_text(encoding="utf-8")

    def write_flow(self, name: str, text: str) -> Path:
        return atomic_write(self.flow_path(name), text)

    def delete_flow(self, name: str) -> bool:
        path = self.flow_path(name)
        if not path.is_file():
            return False
        path.unlink()
        return True

    # -- run artefacts -----------------------------------------------------
    def run_dir(self, run_id: str, instance_id: int) -> Path:
        """Where one device of one run keeps its screenshots."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(run_id))[:60] or "run"
        target = inside(self.runs_dir, self.runs_dir / safe / str(int(instance_id)))
        target.mkdir(parents=True, exist_ok=True)
        return target
