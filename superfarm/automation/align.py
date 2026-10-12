"""Lining up the same step across several recordings.

Exact matching is too brittle for this. The same physical tap can be recorded with a
different element each time: the tree is read a fraction of a second before the tap,
and on a screen that has just changed it can still describe the screen before it.
Requiring every recording to agree exactly threw those steps away - that is how five
recorded steps once trained down to one.

So steps are aligned by *similarity* instead: same kind, same element identity, how
close the taps were, and their order (Needleman-Wunsch). Once aligned, the element
for a step is decided by majority vote across the recordings (see trainer.py), which
repairs a bad capture instead of discarding the step.
"""
from __future__ import annotations

from .store import Step

GAP = 0.30           # cost of leaving a step unmatched
FLOOR = 0.35         # below this, two steps are not the same step
NEAR = 260.0         # device pixels: taps this far apart score zero on position

_POINTED = ("tap", "long_press", "assert")


def identity(step: Step) -> tuple[str, str, str, str]:
    element = step.element or {}
    short_id = str(element.get("id") or str(element.get("resource_id") or "").rsplit("/", 1)[-1])
    return (short_id, str(element.get("text") or ""), str(element.get("desc") or ""),
            str(element.get("path") or ""))


def confident(step: Step) -> bool:
    """Whether the element recorded for this step can be trusted."""
    element = step.element or {}
    return bool(element) and not element.get("uncertain")


def _direction(step: Step) -> str:
    if not (step.device and step.to):
        return "?"
    dx, dy = step.to[0] - step.device[0], step.to[1] - step.device[1]
    if abs(dy) >= abs(dx):
        return "up" if dy < 0 else "down"
    return "left" if dx < 0 else "right"


def similarity(a: Step, b: Step) -> float:
    """How likely two recorded steps are the same step of the same task (0..1)."""
    if a.kind != b.kind:
        return 0.0

    if a.kind == "type":
        if a.dynamic or b.dynamic:
            return 1.0 if a.dynamic == b.dynamic else 0.2
        return 0.85                      # two literal typings, probably the same field
    if a.kind == "key":
        return 1.0 if (a.key or "").lower() == (b.key or "").lower() else 0.15
    if a.kind == "swipe":
        return 0.9 if _direction(a) == _direction(b) else 0.35
    if a.kind == "wait":
        return 0.8
    if a.kind == "launch":
        return 1.0 if a.package == b.package else 0.1
    if a.kind not in _POINTED:
        return 0.5

    rid_a, text_a, desc_a, path_a = identity(a)
    rid_b, text_b, desc_b, path_b = identity(b)
    by_identity = 0.0
    if rid_a and rid_a == rid_b:
        by_identity = 1.0
    elif text_a and text_a == text_b:
        by_identity = 0.95
    elif desc_a and desc_a == desc_b:
        by_identity = 0.9
    elif path_a and path_a == path_b:
        by_identity = 0.85               # same place in the hierarchy
    elif (rid_a and rid_b) or (text_a and text_b):
        by_identity = 0.2                # both identified, and they disagree

    by_position = 0.0
    if a.device and b.device:
        distance = max(abs(a.device[0] - b.device[0]), abs(a.device[1] - b.device[1]))
        by_position = max(0.0, 1.0 - distance / NEAR)

    # Position alone is weaker evidence than a matching id, but it is enough to
    # recognise a step whose element was captured wrongly in one recording.
    return max(by_identity, by_position * 0.8, by_identity * 0.5 + by_position * 0.5)


def align(reference: list[Step], other: list[Step]) -> list[int | None]:
    """Needleman-Wunsch: for each step of `reference`, the matching index in `other`,
    or None when that recording did not do it."""
    rows, cols = len(reference), len(other)
    sim = [[similarity(reference[i], other[j]) for j in range(cols)] for i in range(rows)]
    score = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        score[i][cols] = score[i + 1][cols] - GAP
    for j in range(cols - 1, -1, -1):
        score[rows][j] = score[rows][j + 1] - GAP
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            score[i][j] = max(score[i + 1][j + 1] + (sim[i][j] - FLOOR),
                              score[i + 1][j] - GAP, score[i][j + 1] - GAP)

    mapping: list[int | None] = [None] * rows
    i = j = 0
    while i < rows and j < cols:
        value = sim[i][j]
        if score[i][j] == score[i + 1][j + 1] + (value - FLOOR) and value >= FLOOR:
            mapping[i] = j
            i += 1
            j += 1
        elif score[i][j] == score[i + 1][j] - GAP:
            i += 1
        else:
            j += 1
    return mapping


def alignment_score(reference: list[Step], other: list[Step]) -> float:
    matched = sum(1 for index in align(reference, other) if index is not None)
    return matched / max(len(reference), len(other), 1)


def choose_reference(sequences: list[list[Step]]) -> int:
    """The recording that agrees best with all the others: the one to build on.

    Picking the longest would import every one-off detour; picking the first is
    arbitrary. The best-agreeing recording is the most representative run.
    """
    if len(sequences) <= 1:
        return 0
    best, best_score = 0, -1.0
    for index, candidate in enumerate(sequences):
        total = sum(alignment_score(candidate, other)
                    for position, other in enumerate(sequences) if position != index)
        if total > best_score:
            best, best_score = index, total
    return best


def columns(sequences: list[list[Step]]) -> tuple[int, list[list[Step]]]:
    """Group the recordings' steps into one column per step of the task.

    Steps the template recording does not have are not thrown away: they are
    collected at the point they occurred, grouped with the matching steps from the
    other recordings, and inserted as columns of their own. Otherwise a step that
    most recordings made would vanish simply because the template missed it.
    """
    if not sequences:
        return 0, []
    reference_index = choose_reference(sequences)
    reference = sequences[reference_index]
    anchored: list[list[Step]] = [[step] for step in reference]
    extras: dict[int, list[tuple[int, Step]]] = {}

    for index, other in enumerate(sequences):
        if index == reference_index:
            continue
        mapping = align(reference, other)
        matched = {in_other: in_reference for in_reference, in_other in enumerate(mapping)
                   if in_other is not None}
        last = -1
        for position, step in enumerate(other):
            if position in matched:
                last = matched[position]
                anchored[last].append(step)
            else:
                extras.setdefault(last + 1, []).append((index, step))

    inserted = {slot: _cluster(entries) for slot, entries in extras.items()}
    out: list[list[Step]] = []
    for position in range(len(anchored) + 1):
        out.extend(inserted.get(position, []))
        if position < len(anchored):
            out.append(anchored[position])
    return reference_index, out


def _cluster(entries: list[tuple[int, Step]]) -> list[list[Step]]:
    """Group the same extra step from different recordings into one column."""
    clusters: list[list[tuple[int, Step]]] = []
    for recording_index, step in entries:
        for cluster in clusters:
            if any(recording_index == other_index for other_index, _ in cluster):
                continue                      # one step per recording per column
            if any(similarity(step, other) >= FLOOR for _, other in cluster):
                cluster.append((recording_index, step))
                break
        else:
            clusters.append([(recording_index, step)])
    return [[step for _, step in cluster] for cluster in clusters]
