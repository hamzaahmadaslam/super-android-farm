"""Screen fingerprints, picture comparison and the fix memory."""
from __future__ import annotations

import io
import threading

from PIL import Image, ImageDraw

from superfarm.automation.memory import (
    PATCH_MATCH,
    FixMemory,
    crop,
    crop_box,
    dhash,
    hamming,
    load_image,
    patch_similarity,
    save_png,
    scale_box,
    to_image,
)


def picture(seed: int, size=(240, 427)) -> Image.Image:
    """A deterministic 'screen': a few rectangles and lines that depend on the seed."""
    image = Image.new("RGB", size, (20 + seed * 7 % 60, 20, 40))
    draw = ImageDraw.Draw(image)
    for i in range(6):
        x = (seed * 37 + i * 53) % (size[0] - 60)
        y = (seed * 91 + i * 71) % (size[1] - 60)
        draw.rectangle([x, y, x + 50, y + 40], fill=((i * 40 + seed * 13) % 255, (i * 70) % 255, 200))
        draw.line([0, y, size[0], y + i * 3], fill=(250, 250, 250), width=2)
    return image


def test_dhash_tells_screens_apart_but_tolerates_small_changes():
    a, b = picture(1), picture(5)
    assert dhash(a) == dhash(a.copy()) and 0 <= dhash(a) < 2 ** 64
    assert hamming(dhash(a), dhash(b)) > 10
    ticking = a.copy()
    ImageDraw.Draw(ticking).rectangle([200, 2, 236, 12], fill=(255, 255, 255))     # a clock in the status bar
    assert hamming(dhash(a), dhash(ticking)) <= 10
    assert hamming(0b1011, 0b0010) == 2


def test_patch_similarity():
    a = picture(2)
    box = [40, 60, 220, 240]
    patch = crop(a, box)
    assert patch_similarity(patch, crop(a, box)) > 0.99
    assert patch_similarity(patch, crop(picture(9), box)) < PATCH_MATCH
    brighter = patch.point(lambda v: min(255, int(v * 1.1) + 5))
    assert patch_similarity(patch, brighter) > 0.9                     # lighting is not a different screen
    assert patch_similarity(patch, patch.resize((90, 90))) > 0.9       # nor is another resolution


def test_flat_patches_are_compared_by_brightness():
    white, grey, black = (Image.new("RGB", (64, 64), c) for c in ((255, 255, 255), (250, 250, 250), (0, 0, 0)))
    assert patch_similarity(white, grey) == 1.0
    assert patch_similarity(white, black) == 0.0
    assert patch_similarity(white, crop(picture(3), [0, 0, 64, 64])) == 0.0
    assert patch_similarity(Image.new("RGB", (4, 4)), Image.new("RGB", (4, 4))) == 0.0     # too small to judge


def test_crop_box_stays_on_screen():
    assert crop_box((240, 400), 180, (480, 854)) == [150, 310, 330, 490]
    assert crop_box((5, 5), 180, (480, 854)) == [0, 0, 95, 95]
    assert crop_box((479, 853), 180, (480, 854)) == [389, 763, 480, 854]
    box = crop_box((9999, -50), 180, (480, 854))
    assert 0 <= box[0] < box[2] <= 480 and 0 <= box[1] < box[3] <= 854
    assert crop(picture(1), [500, 500, 600, 600]).size == (1, 1)


def test_scale_box_follows_the_screen_size():
    assert scale_box([10, 20, 110, 220], [480, 854], (960, 1708)) == [20, 40, 220, 440]
    assert scale_box([10, 20, 110, 220], [480, 854], (480, 854)) == [10, 20, 110, 220]
    assert scale_box([10, 20, 110, 220], None, (960, 1708)) == [10, 20, 110, 220]


def test_png_round_trip(tmp_path):
    path = save_png(picture(4), tmp_path / "shots" / "a.png")
    assert load_image(path).size == (240, 427)
    assert load_image(tmp_path / "missing.png") is None
    (tmp_path / "broken.png").write_bytes(b"not a picture")
    assert load_image(tmp_path / "broken.png") is None
    buffer = io.BytesIO()
    picture(4).save(buffer, format="PNG")
    assert to_image(buffer.getvalue()).mode == "RGB"


# --------------------------------------------------------------- fix memory
ACTIONS = [{"type": "tap", "selector": {"id": "close"}, "x": 10, "y": 10}]


def test_a_fix_is_found_again_by_app_and_screen(tmp_path):
    memory = FixMemory(tmp_path)
    screen_hash = dhash(picture(1))
    entry = memory.remember(package="com.example.app", dhash=screen_hash, actions=ACTIONS, analysis="closed a dialog")
    assert entry is not None and entry.hits == 1

    again = FixMemory(tmp_path)                                         # another process, same file
    assert again.lookup(package="com.example.app", dhash=screen_hash).id == entry.id
    assert again.lookup(package="com.example.app", dhash=screen_hash ^ 0b111).id == entry.id   # nearly the same
    assert again.lookup(package="com.example.app", dhash=dhash(picture(7))) is None
    # the same-looking dialog in another app is not the same situation
    assert again.lookup(package="com.other.app", dhash=screen_hash) is None
    assert again.lookup(package="", dhash=screen_hash) is None


def test_the_closest_screen_wins(tmp_path):
    memory = FixMemory(tmp_path)
    far = memory.remember(package="p.q", dhash=0b1111, actions=ACTIONS)
    near = memory.remember(package="p.q", dhash=0b0001, actions=ACTIONS)
    assert memory.lookup(package="p.q", dhash=0b0000).id == near.id != far.id


def test_fixes_that_keep_failing_stop_being_used(tmp_path):
    memory = FixMemory(tmp_path)
    entry = memory.remember(package="p.q", dhash=5, actions=ACTIONS)
    for _ in range(3):
        memory.bump(entry.id, False)
    stored = memory.entries()[0]
    assert stored.hits == 1 and stored.misses == 3 and stored.distrusted
    assert memory.lookup(package="p.q", dhash=5) is None
    assert memory.examples("p.q") == []
    assert memory.prune() == 1 and memory.entries() == []


def test_working_fixes_gain_confidence_and_become_examples(tmp_path):
    memory = FixMemory(tmp_path)
    entry = memory.remember(package="p.q", dhash=5, actions=ACTIONS, analysis="closed it")
    memory.bump(entry.id, True)
    assert memory.entries()[0].hits == 2
    example = memory.examples("p.q")[0]
    assert example["what_worked"] == ACTIONS and example["times_it_worked"] == 2
    assert memory.stats() == {"entries": 1, "packages": ["p.q"], "by_source": {"model": 1}, "total_uses": 2}
    assert memory.forget(entry.id) and not memory.forget(entry.id)


def test_disabled_memory_does_nothing(tmp_path):
    memory = FixMemory(tmp_path, enabled=False)
    assert memory.remember(package="p.q", dhash=5, actions=ACTIONS) is None
    assert memory.lookup(package="p.q", dhash=5) is None and not (tmp_path / "fixes.jsonl").exists()
    assert FixMemory(tmp_path).remember(package="p.q", dhash=5, actions=[]) is None      # nothing to remember


def test_a_damaged_line_does_not_cost_the_rest(tmp_path):
    memory = FixMemory(tmp_path)
    memory.remember(package="p.q", dhash=5, actions=ACTIONS)
    with open(tmp_path / "fixes.jsonl", "a") as f:
        f.write("{half a line\n\n")
        f.write('{"id": "x", "unknown_field": 1}\n')
    assert len(memory.entries()) == 1
    assert memory.remember(package="p.q", dhash=9, actions=ACTIONS) is not None
    assert len(memory.entries()) == 2


def test_many_devices_can_learn_at_once(tmp_path):
    memory = FixMemory(tmp_path)

    def learn(n: int) -> None:
        for i in range(8):
            memory.remember(package="p.q", dhash=n * 100 + i, actions=ACTIONS)

    threads = [threading.Thread(target=learn, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entries = memory.entries()
    assert len(entries) == 48 and len({e.id for e in entries}) == 48
