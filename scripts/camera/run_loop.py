#!/usr/bin/env python3
"""Keep one command running: start it again whenever it exits.

    run_loop.py [--delay SECONDS] [--give-up N] -- command [args...]

Started by superfarm.media.camera around each feed's ffmpeg. Standard library only.

* A live ingest (`ffmpeg -listen 1 -i rtmp://...`) serves exactly one publisher and exits
  when that publisher stops streaming. It has to listen again, for as long as the feed is
  wanted: --give-up 0.
* A file feed normally never exits (`-stream_loop -1`). If it does, one restart usually
  cures it (a reader was replaced). If it keeps dying within seconds the file is broken or
  missing: with --give-up N the loop stops after N short runs in a row and exits 1, so the
  feed shows up as failed, with ffmpeg's own message in the log, instead of spinning.

SIGTERM / SIGINT stop the command and end the loop with exit status 0. The command runs in
this process's group, so a signal sent to the group reaches it as well.
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time

SHORT_RUN = 5.0     # a run shorter than this counts as a failure to start


class Loop:
    def __init__(self, argv: list[str], delay: float, give_up: int):
        self.argv, self.delay, self.give_up = argv, delay, give_up
        self.child: subprocess.Popen | None = None
        self.stopping = False

    def stop(self, *_: object) -> None:
        self.stopping = True
        child = self.child
        if child is not None and child.poll() is None:
            child.terminate()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def run(self) -> int:
        short_runs = 0
        while not self.stopping:
            began = time.monotonic()
            try:
                self.child = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL)
            except OSError as e:
                print(f"run_loop: cannot start {self.argv[0]}: {e}", file=sys.stderr, flush=True)
                return 127
            code = self._wait()
            if self.stopping:
                break
            lasted = time.monotonic() - began
            short_runs = short_runs + 1 if lasted < SHORT_RUN else 0
            if self.give_up and short_runs >= self.give_up:
                print(f"run_loop: {self.argv[0]} exited {code} within {SHORT_RUN:.0f} s, "
                      f"{short_runs} times in a row; giving up", file=sys.stderr, flush=True)
                return 1
            self._sleep(self.delay)
        return 0

    def _wait(self) -> int:
        child = self.child
        assert child is not None
        while True:
            try:
                return child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                if self.stopping:
                    return self._finish(child)

    @staticmethod
    def _finish(child: subprocess.Popen) -> int:
        try:
            return child.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            child.kill()
            return child.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--delay", type=float, default=1.0, help="pause before each restart (default 1 s)")
    parser.add_argument("--give-up", type=int, default=0,
                        help="exit 1 after this many short runs in a row (0 = never give up)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- command and its arguments")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given")
    loop = Loop(command, max(0.0, args.delay), max(0, args.give_up))
    signal.signal(signal.SIGTERM, loop.stop)
    signal.signal(signal.SIGINT, loop.stop)
    return loop.run()


if __name__ == "__main__":
    sys.exit(main())
