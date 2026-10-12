"""A trained program: what playback runs.

It is derived from the recordings of one dataset, but it is deliberately not a replay
of any single one. Every step carries

  * a selector (id / text / description / path) voted from the elements that were
    actually touched, which is what lets it work on another device,
  * the alternatives the other recordings saw at that step,
  * the device coordinates, as a fallback when no selector matches,
  * a picture of the area, to check before coordinates are trusted,
  * whether the step appeared in every recording or only in some.

    automation/programs/<slug>/program.json
    automation/programs/<slug>/shots/<step id>.png
"""
from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..util.files import inside, read_json, write_json
from .store import check_slug

POINTED = ("tap", "long_press", "assert")


@dataclass
class ProgramStep:
    id: str
    kind: str                          # tap | long_press | swipe | type | key | launch | wait | assert
    intent: str = ""                   # plain words, written during training
    selector: dict = field(default_factory=dict)       # id / text / desc / cls / path
    alternatives: list = field(default_factory=list)
    device: list | None = None         # [x, y] fallback, device pixels
    to: list | None = None
    shot: str | None = None            # relative to the program folder
    shot_box: list | None = None
    text: str | None = None
    dynamic: str | None = None         # a named value, resolved per device at run time
    key: str | None = None
    package: str | None = None
    seconds: float = 0.0
    optional: bool = False             # not present in every recording
    wait_after: float = 0.0            # 0 = automation.settle_seconds
    expect_activity: str = ""
    seen_in: int = 0                   # how many recordings had this step

    def label(self) -> str:
        if self.intent:
            return self.intent
        name = (self.selector.get("text") or self.selector.get("desc") or self.selector.get("id")
                or self.selector.get("resource_id") or "")
        if self.kind in ("tap", "long_press"):
            verb = "tap" if self.kind == "tap" else "long-press"
            return f"{verb} {name}" if name else f"{verb} {tuple(self.device or ())}"
        if self.kind == "type":
            return f"type the {self.dynamic} value" if self.dynamic else f"type {self.text!r}"
        if self.kind == "key":
            return f"press {self.key}"
        if self.kind == "launch":
            return f"launch {self.package}"
        if self.kind == "assert":
            return f"expect {name}" if name else "expect an element"
        if self.kind == "wait":
            return f"wait {self.seconds:.1f}s"
        return self.kind

    def has_selector(self) -> bool:
        return bool(self.selector.get("id") or self.selector.get("resource_id")
                    or self.selector.get("text") or self.selector.get("desc"))

    def candidates(self) -> list[dict]:
        return ([self.selector] if self.selector else []) + [a for a in self.alternatives if a]

    @classmethod
    def from_dict(cls, data: dict) -> ProgramStep:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Program:
    slug: str
    name: str = ""
    goal: str = ""
    package: str = ""
    created: str = ""
    model: str = ""
    from_recordings: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    screen: list = field(default_factory=list)       # [width, height] the recordings were made on
    dir: Path | None = None

    # -- io ----------------------------------------------------------------
    @staticmethod
    def folder(root: Path, slug: str) -> Path:
        root = Path(root)
        return inside(root, root / check_slug(slug))

    @classmethod
    def load(cls, root: Path, slug: str) -> Program | None:
        folder = cls.folder(root, slug)
        data = read_json(folder / "program.json", None)
        if not isinstance(data, dict):
            return None
        program = cls(slug=slug, name=data.get("name", ""), goal=data.get("goal", ""),
                      package=data.get("package", ""), created=data.get("created", ""),
                      model=data.get("model", ""), from_recordings=data.get("from_recordings") or [],
                      notes=data.get("notes") or [], warnings=data.get("warnings") or [],
                      screen=data.get("screen") or [],
                      steps=[ProgramStep.from_dict(s) for s in data.get("steps") or [] if isinstance(s, dict)])
        program.dir = folder
        return program

    @classmethod
    def list(cls, root: Path) -> list[Program]:
        root = Path(root)
        if not root.is_dir():
            return []
        out = []
        for path in sorted(root.glob("*/program.json")):
            try:
                program = cls.load(root, path.parent.name)
            except ValueError:
                continue
            if program is not None:
                out.append(program)
        return out

    @classmethod
    def delete(cls, root: Path, slug: str) -> bool:
        """Remove a trained program's folder. Only a plain folder name directly under
        the programs root is accepted, so a crafted name cannot reach anything else."""
        try:
            folder = cls.folder(root, slug)
        except ValueError:
            return False
        if folder.parent != Path(root).resolve() or not (folder / "program.json").is_file():
            return False
        shutil.rmtree(folder)
        return True

    def save(self, root: Path) -> Path:
        folder = self.folder(root, self.slug)
        self.dir = folder
        payload = {"slug": self.slug, "name": self.name, "goal": self.goal, "package": self.package,
                   "created": self.created, "model": self.model, "from_recordings": self.from_recordings,
                   "screen": self.screen, "notes": self.notes, "warnings": self.warnings,
                   "steps": [{k: v for k, v in asdict(step).items() if v not in (None, "", [], {}, 0.0, False, 0)
                              or k in ("id", "kind")} for step in self.steps]}
        return write_json(folder / "program.json", payload)

    def shot_path(self, relative: str) -> Path | None:
        if not self.dir or not relative:
            return None
        try:
            return inside(self.dir, self.dir / relative)
        except ValueError:
            return None

    # -- for the panel -----------------------------------------------------
    def summary(self) -> dict:
        return {"slug": self.slug, "name": self.name, "goal": self.goal, "package": self.package,
                "created": self.created, "model": self.model, "steps": len(self.steps),
                "optional": sum(1 for s in self.steps if s.optional),
                "with_selector": sum(1 for s in self.steps if s.has_selector()),
                "dynamic": sorted({s.dynamic for s in self.steps if s.dynamic}),
                "from_recordings": self.from_recordings, "warnings": self.warnings, "notes": self.notes}

    def outline(self) -> list[dict]:
        return [{"id": s.id, "kind": s.kind, "label": s.label(), "selector": s.selector,
                 "optional": s.optional, "dynamic": s.dynamic, "seen_in": s.seen_in, "shot": s.shot,
                 "has_selector": s.has_selector()} for s in self.steps]
