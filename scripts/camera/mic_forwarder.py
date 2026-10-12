#!/usr/bin/env python3
"""Forward one Android's microphone sound from its source FIFO into its microphone FIFO.

    mic_forwarder.py --source <audio fifo> --target <mic fifo> [--mute-marker <file>]

Started by superfarm.media.camera, one small process per Android. Standard library only,
so it runs with any Python the daemon runs with.

Reads 48 kHz stereo s16le PCM from the source FIFO (written by the feed's ffmpeg) and
writes it to the target FIFO, which is bind-mounted into the container as /dev/farm_mic
where the audio HAL shim reads it.

Why this exists instead of letting ffmpeg write the microphone FIFO directly:

* Android reads the microphone only while an app is recording. A plain writer would block
  the moment the pipe is full, and because the picture and the sound of a feed come from
  the same ffmpeg, the camera of every Android on that feed would freeze with it. Writes
  here never block: when the pipe is full the block is dropped.
* Sources deliver sound in bursts; Android needs it evenly. Blocks go out at a steady
  20 ms pace, after a small cushion has gathered.
* A muted Android (marker file present) simply gets nothing, which Android records as
  silence. Checking a marker twice a second means muting needs no restart of anything.

Between blocks the process sleeps; it costs a fraction of a percent of one core.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import select
import signal
import sys
import time

FRAME = 4               # bytes per sample frame: stereo, 16 bit
BLOCK = 3840            # 20 ms at 48 kHz; at most PIPE_BUF (4096), so every write is all-or-nothing
TICK = 0.02             # one block per tick
RESUME = 3 * BLOCK      # cushion gathered before sending (re)starts
MAX_PENDING = 25 * BLOCK    # half a second: beyond this the source runs ahead of real time
PIPE_BYTES = 32768      # small pipe = little stale sound when an app starts recording
F_SETPIPE_SZ = 1031     # fcntl constant, not exported by every Python build
TARGET_CHECK = 2.0      # seconds between checks that the target FIFO is still the same file
MUTE_CHECK = 0.5


class Pacer:
    """The timing logic, free of I/O so it can be tested with a fake clock.

    feed() takes whatever the source delivered; due() hands back one block when it is time
    to send one, else None.
    """

    def __init__(self) -> None:
        self.pending = bytearray()
        self.primed = False
        self.next_tick = 0.0

    def feed(self, data: bytes) -> None:
        self.pending += data
        if len(self.pending) > MAX_PENDING:
            # Skip forward to stay live, on a frame boundary so left and right never swap.
            drop = (len(self.pending) - RESUME) // FRAME * FRAME
            del self.pending[:drop]

    def wait(self, now: float) -> float | None:
        """How long the caller may sleep: until the next block is due, or (None) until
        sound arrives when there is none to send."""
        return max(0.0, self.next_tick - now) if self.primed else None

    def due(self, now: float) -> bytes | None:
        if not self.primed:
            if len(self.pending) < RESUME:
                return None
            self.primed, self.next_tick = True, now
        if now < self.next_tick:
            return None
        if len(self.pending) < BLOCK:
            self.primed = False         # ran dry: gather a cushion again before resuming
            return None
        block = bytes(self.pending[:BLOCK])
        del self.pending[:BLOCK]
        # Advancing from the previous tick (not from `now`) keeps the long-run rate exact:
        # a late wake-up is followed by a slightly shorter wait, never by lost time.
        self.next_tick += TICK
        return block


def open_target(path: str) -> int | None:
    try:
        # Read-write: opening succeeds without a reader, and Android never sees end-of-file
        # when this process restarts.
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        fcntl.fcntl(fd, F_SETPIPE_SZ, PIPE_BYTES)
    except OSError:
        pass
    return fd


def same_file(fd: int, path: str) -> bool:
    """False once the FIFO at `path` is no longer the one we hold open."""
    try:
        there, here = os.stat(path), os.fstat(fd)
    except OSError:
        return False
    return (there.st_dev, there.st_ino) == (here.st_dev, here.st_ino)


def forward(source_path: str, target_path: str, mute_marker: str | None) -> None:
    # Read-write on the source too: a feed that stops is then not end-of-file, and the next
    # feed finds a reader already waiting.
    source = os.open(source_path, os.O_RDWR)
    target: int | None = None
    check_at = mute_at = 0.0
    muted = False
    pacer = Pacer()
    while True:
        if select.select([source], [], [], pacer.wait(time.monotonic()))[0]:
            pacer.feed(os.read(source, 65536))
        now = time.monotonic()
        if now >= check_at:
            check_at = now + TARGET_CHECK
            if target is not None and not same_file(target, target_path):
                os.close(target)
                target = None
            if target is None:
                target = open_target(target_path)
        block = pacer.due(now)
        if block is None:
            continue
        if mute_marker and now >= mute_at:
            mute_at = now + MUTE_CHECK
            muted = os.path.exists(mute_marker)
        if target is None or muted:
            continue
        try:
            os.write(target, block)
        except BlockingIOError:
            pass                        # nobody is recording: drop
        except OSError:
            os.close(target)
            target, check_at = None, 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="FIFO the feed writes 48 kHz stereo s16le PCM to")
    parser.add_argument("--target", required=True, help="FIFO mounted into the Android as /dev/farm_mic")
    parser.add_argument("--mute-marker", default="", help="while this file exists, nothing is forwarded")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        forward(args.source, args.target, args.mute_marker or None)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"mic_forwarder: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
