"""Training: several recordings of one goal become one program.

It is not model training. A handful of demonstrations cannot tune anything, and what
is needed is something that runs: so this is program synthesis, in two passes, in
this order, because the second one is allowed to be wrong.

1. **Consensus (deterministic).** The recordings are aligned step by step (align.py)
   rather than matched exactly, because the same tap can be captured with a
   different element in different recordings. The recording that agrees most with
   the others is the template; the rest are aligned onto it. A step is kept when at
   least half the recordings made it, its element is decided by majority vote, and
   its coordinates are the median - one bad capture is outvoted instead of throwing
   the step away. This pass alone produces a runnable program.

2. **A model (optional enrichment).** It is given the goal and the consensus and
   writes what each step is *for*. It can only annotate: it never adds, reorders or
   invents a step. If no model is configured or reachable the program is just as
   valid, only less well described.

Training says plainly what it left out and why. Those warnings are the product: a
step that silently vanished is how a program "works" in the test and fails on the
fleet.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Iterable

from .align import columns, confident
from .llm import LlmClient, LlmError, annotate, clean_annotation
from .program import POINTED, Program, ProgramStep
from .selectors import label
from .store import Dataset, Recording, Step, now
from .values import looks_secret

MAX_ALTERNATIVES = 4
SPREAD_WARNING = 90          # device pixels between recordings before a blind tap is flagged


@dataclass
class Consensus:
    steps: list[ProgramStep]
    warnings: list[str]
    reference: str = ""                                  # id of the template recording
    shots: dict[str, tuple[str, str]] = field(default_factory=dict)   # step id -> (recording id, relative path)


# ------------------------------------------------------------------ merging
def _median_point(points: Iterable[list | None]) -> list[int] | None:
    usable = [p for p in points if p and len(p) == 2]
    if not usable:
        return None
    return [int(statistics.median(p[0] for p in usable)), int(statistics.median(p[1] for p in usable))]


def _spread(points: Iterable[list | None]) -> int:
    """How far apart the recordings put the same step, in device pixels."""
    usable = [p for p in points if p and len(p) == 2]
    if len(usable) < 2:
        return 0
    return int(max(max(abs(a[0] - b[0]), abs(a[1] - b[1])) for a in usable for b in usable))


def _selector_key(element: dict) -> tuple[str, str, str, str, str]:
    # Recordings made before elements carried a short id only have the
    # package-qualified resource id: its last segment is the same thing.
    short_id = str(element.get("id") or str(element.get("resource_id") or "").rsplit("/", 1)[-1])
    return (short_id, str(element.get("text") or ""), str(element.get("desc") or ""),
            str(element.get("cls") or ""), str(element.get("path") or ""))


def _vote_selector(steps: list[Step]) -> tuple[dict, list[dict], str]:
    """Decide the element for a step by majority vote across the recordings.

    One bad capture - the tree read a moment too early, describing the previous
    screen - is outvoted instead of throwing the step away. Captures flagged
    uncertain only count if nothing better exists.
    """
    trusted = [s for s in steps if confident(s)] or [s for s in steps if s.element]
    names = ("id", "text", "desc", "cls", "path")
    counts: dict[tuple, int] = {}
    for step in trusted:
        key = _selector_key(step.element or {})
        if any(key[:3]) or key[4]:              # an id, text, description, or a path
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return {}, [], ""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    candidates = [{k: v for k, v in zip(names, key, strict=True) if v} for key, _ in ordered[:MAX_ALTERNATIVES]]
    top_votes, voters = ordered[0][1], len(trusted)

    # A winner that most recordings did not agree on is not evidence. Committing to
    # it would send playback hunting for an element that is not on this screen; the
    # median coordinates are the better first guess, with every element any
    # recording saw here offered as a candidate to try first.
    if top_votes * 2 > voters:
        selector, alternatives = candidates[0], candidates[1:]
        disagreement = ""
        if len(candidates) > 1:
            disagreement = f"{top_votes} of {voters} recordings agreed on {label(candidates[0])!r} here"
    else:
        selector, alternatives = {}, candidates
        disagreement = (f"no element had majority support ({top_votes} of {voters} for {label(candidates[0])!r}) - "
                        f"this step will try {len(candidates)} candidate(s), then fall back to the recorded "
                        "position. Re-record this dataset if the app's screens have changed.")
    return selector, alternatives, disagreement


def _neighbours(steps: list[Step], taken: list[dict]) -> list[dict]:
    """Other named elements the recordings saw under the same point, most common
    first, that the vote did not already produce."""
    counts: dict[tuple, int] = {}
    for step in steps:
        for candidate in step.candidates or []:
            if isinstance(candidate, dict):
                key = _selector_key(candidate)
                if any(key[:3]):
                    counts[key] = counts.get(key, 0) + 1
    names = ("id", "text", "desc", "cls", "path")
    known = {_selector_key(t) for t in taken}
    out = []
    for key, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if key not in known:
            out.append({k: v for k, v in zip(names, key, strict=True) if v})
    return out


def build_consensus(recordings: list[Recording], keep_ratio: float = 0.5) -> Consensus:
    """Align every recording, then keep the steps enough of them agree on."""
    sequences = [recording.steps for recording in recordings]
    owner = {id(step): recording.id for recording in recordings for step in recording.steps}
    warnings: list[str] = []
    reference_index, grouped = columns(sequences)
    total = len(sequences)
    reference = recordings[reference_index].id if recordings else ""
    if total > 1:
        warnings.append(f"built from {reference} (the recording that agreed most with the others), "
                        "with the rest aligned onto it")
    elif total == 1:
        warnings.append("trained from a single recording: nothing could be cross-checked. "
                        "Record the same goal at least three times for a real consensus.")
    needed = max(1, math.ceil(total * keep_ratio))

    steps: list[ProgramStep] = []
    shots: dict[str, tuple[str, str]] = {}
    left_out: list[str] = []
    for examples in grouped:
        if len(examples) < needed:
            left_out.append(f"{examples[0].describe()} ({len(examples)} of {total})")
            continue
        first = examples[0]
        selector, alternatives, disagreement = ({}, [], "")
        if first.kind in POINTED or first.kind == "type":
            selector, alternatives, disagreement = _vote_selector(examples)
            extra = _neighbours(examples, ([selector] if selector else []) + alternatives)
            alternatives = (alternatives + extra)[:MAX_ALTERNATIVES]
        step = ProgramStep(
            id=f"p{len(steps):03d}", kind=first.kind, selector=selector, alternatives=alternatives,
            device=_median_point(s.device for s in examples), to=_median_point(s.to for s in examples),
            text=first.text, dynamic=next((s.dynamic for s in examples if s.dynamic), None),
            key=first.key, package=first.package,
            seconds=float(statistics.median(s.seconds for s in examples)) if first.kind in ("wait", "long_press") else 0.0,
            expect_activity=first.activity or "", seen_in=len(examples), optional=len(examples) < total)
        shot_source = next((s for s in examples if s.shot and s.shot_box), None)
        if shot_source is not None:
            shots[step.id] = (owner.get(id(shot_source), ""), shot_source.shot or "")
            step.shot_box = list(shot_source.shot_box or [])
        if disagreement:
            warnings.append(f"{step.id}: {disagreement}")
        if not step.selector and step.alternatives:
            step.intent = "unclear which element this is"
        spread = _spread(s.device for s in examples)
        if step.kind in POINTED and spread > SPREAD_WARNING and not step.has_selector():
            warnings.append(f"{step.id}: the recordings tapped up to {spread}px apart here with nothing to "
                            "identify the spot - check this step")
        steps.append(step)

    if left_out:
        shown = "; ".join(left_out[:6]) + (f"; and {len(left_out) - 6} more" if len(left_out) > 6 else "")
        warnings.append(f"{len(left_out)} step(s) appeared in fewer than {needed} of the {total} recordings and "
                        f"were left out: {shown}")
    if not steps:
        warnings.append("nothing could be derived from these recordings - they may be of different tasks")
    blind = [s for s in steps if s.kind in ("tap", "long_press") and not s.has_selector()]
    if blind:
        warnings.append(f"{len(blind)} tap(s) have no id or text and can only be found by position "
                        f"({', '.join(s.id for s in blind[:8])}) - they are the most likely to fail on a "
                        "different device")
    optional = [s for s in steps if s.optional]
    if optional:
        warnings.append(f"{len(optional)} step(s) were not in every recording and will be skipped when their "
                        f"element is not there ({', '.join(s.id for s in optional[:8])})")
    packages = {r.package for r in recordings if r.package}
    if len(packages) > 1:
        warnings.append("the recordings were made in different apps (" + ", ".join(sorted(packages))
                        + ") - one dataset should hold one goal in one app")
    return Consensus(steps=steps, warnings=warnings, reference=reference, shots=shots)


# ------------------------------------------------------------- annotation
def describe_for_model(steps: list[ProgramStep], total_recordings: int, forbidden_words: Iterable[str]) -> list[dict]:
    """The consensus as a model sees it. Typed literals that could be secrets, and
    anything named like one, are described but not shown."""
    words = list(forbidden_words)
    out = []
    for step in steps:
        entry: dict = {"id": step.id, "action": step.kind, "seen_in": f"{step.seen_in}/{total_recordings} recordings"}
        field_name = " ".join(str(step.selector.get(k) or "") for k in ("id", "desc"))
        secret_field = looks_secret(field_name, words)
        if step.selector:
            entry["element"] = {k: v for k, v in step.selector.items() if k != "path"}
        elif step.device:
            entry["position_only"] = step.device
        if step.dynamic:
            entry["types"] = ("a secret value" if looks_secret(step.dynamic, words)
                              else f"the value named {step.dynamic}")
        elif step.kind == "type":
            entry["types"] = "text (withheld)" if secret_field else (step.text or "")[:60]
        if step.key:
            entry["key"] = step.key
        if step.package:
            entry["app"] = step.package
        if step.expect_activity:
            entry["screen"] = step.expect_activity.split("/")[-1][:60]
        out.append(entry)
    return out


def apply_annotation(program: Program, client: LlmClient | None, total_recordings: int,
                     forbidden_words: Iterable[str], log: Callable[[str], None] = lambda _m: None) -> None:
    """Ask the model to describe the steps. Never changes what the program does."""
    if client is None or not program.steps:
        return
    payload = {"goal": program.goal or "(not written down)", "recordings_used": total_recordings,
               "steps": describe_for_model(program.steps, total_recordings, forbidden_words)}
    try:
        reply = clean_annotation(annotate(client, payload), [s.id for s in program.steps])
    except LlmError as e:
        program.warnings.append(f"the steps are not described: {e}")
        return
    program.model = client.model
    by_id = {step.id: step for step in program.steps}
    for step_id, entry in reply["steps"].items():
        step = by_id[step_id]
        if entry.get("intent"):
            step.intent = entry["intent"]
        if entry.get("fragile"):
            program.notes.append(f"{step.id} looks fragile: {step.label()}")
        if entry.get("wait_after"):
            step.wait_after = entry["wait_after"]
    if reply["summary"]:
        program.notes.insert(0, reply["summary"])
    program.notes.extend(reply["notes"])
    log(f"the model described {len(reply['steps'])} of {len(program.steps)} step(s)")


# ------------------------------------------------------------------ train
def train(dataset: Dataset, programs_root: Path, *, client: LlmClient | None = None,
          forbidden_words: Iterable[str] = (), log: Callable[[str], None] = lambda _m: None) -> Program:
    """Turn every recording of a dataset into its program and save it."""
    recordings = [r for r in dataset.recordings() if r.steps]
    if not recordings:
        raise ValueError(f"the dataset {dataset.name!r} has no recordings yet: record the goal first "
                         "(three or more recordings give a real consensus)")
    log(f"training on {len(recordings)} recording(s) of {dataset.name!r}")
    consensus = build_consensus(recordings)
    if 1 < len(recordings) < 3:
        consensus.warnings.append(f"trained from {len(recordings)} recordings: with fewer than three, one bad "
                                  "recording cannot be outvoted")
    package = dataset.package or next((r.package for r in recordings if r.package), "")
    program = Program(slug=dataset.slug, name=dataset.name, goal=dataset.description, package=package,
                      created=now(), from_recordings=[r.id for r in recordings],
                      screen=next((r.screen for r in recordings if r.screen), []),
                      steps=consensus.steps, warnings=consensus.warnings)
    log(f"consensus: {len(program.steps)} step(s), {sum(1 for s in program.steps if s.optional)} seen in only "
        "some recordings")
    for warning in program.warnings:
        log(f"! {warning}")
    apply_annotation(program, client, len(recordings), forbidden_words, log)

    # Replace, never merge: pictures of steps that no longer exist must not survive.
    Program.delete(programs_root, program.slug)
    program.save(programs_root)
    _copy_shots(dataset, program, consensus.shots)
    program.save(programs_root)
    return program


def _copy_shots(dataset: Dataset, program: Program, shots: dict[str, tuple[str, str]]) -> None:
    """Keep the pictures next to the program, so playback does not depend on the
    dataset staying around."""
    assert program.dir is not None
    for step in program.steps:
        rid, relative = shots.get(step.id, ("", ""))
        step.shot = None
        if not rid or not relative:
            step.shot_box = None
            continue
        try:
            source = dataset.shot_path(rid, relative)
            data = source.read_bytes()
        except (OSError, ValueError):
            step.shot_box = None
            continue
        target = program.dir / "shots" / f"{step.id}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        step.shot = f"shots/{target.name}"
