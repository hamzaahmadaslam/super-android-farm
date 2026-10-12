#!/usr/bin/env python3
"""Make a test clip for the virtual camera and microphone.

    make_test_clip.py [output.mp4] [--seconds N]

Default output: test_bars_beeps.mp4 in the current folder; put it in the farm's media
folder to play it. Needs ffmpeg. Standard library only.

The clip is built to answer the questions a camera test actually has:

* Is the picture mirrored?  A solid red stripe runs down the LEFT edge (cyan is on the
  right) and a clock with a frame counter runs in the top-left corner. Red on the right
  means the picture arrives mirrored; media.camera.mirror decides whether the farm
  flips it (on by default, because apps mirror a front camera).
* Is it upright and uncropped?  Portrait 9:16, so it fills a phone screen.
* Does sound arrive?  Short beeps that step through 400, 800, 1200 and 1600 Hz, three
  per second. Do NOT test with a steady tone: voice apps run noise suppression, which
  removes a stationary tone completely - a steady 440 Hz test signal reached a viewer as
  pure silence, while these beeps (and speech, and music) came through.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

WIDTH, HEIGHT, FPS = 720, 1280, 15
BEEPS_PER_SECOND = 3
PITCHES = (400, 800, 1200, 1600)


def beep_expression() -> str:
    """Audio as an ffmpeg expression: pitch steps every beep, each beep fills 60% of its slot."""
    step = PITCHES[1] - PITCHES[0]
    pitch = f"({PITCHES[0]}+{step}*mod(floor({BEEPS_PER_SECOND}*t),{len(PITCHES)}))"
    return f"0.5*sin(2*PI*{pitch}*t)*lt(mod({BEEPS_PER_SECOND}*t,1),0.6)"


def command(output: str, seconds: int, ffmpeg: str = "ffmpeg") -> list[str]:
    video = (f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS},"
             f"drawbox=x=0:y=0:w={WIDTH // 10}:h=ih:color=red:t=fill")
    # Single quotes keep the commas of the expression away from the filter-graph parser.
    audio = f"aevalsrc=exprs='{beep_expression()}':s=48000:c=stereo"
    return [ffmpeg, "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
            "-t", str(int(seconds)), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "128k", "-shortest", output]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("output", nargs="?", default="test_bars_beeps.mp4")
    parser.add_argument("--seconds", type=int, default=20, help="length of the clip (it is played in a loop)")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    if not shutil.which("ffmpeg"):
        print("make_test_clip: ffmpeg is not installed", file=sys.stderr)
        return 1
    try:
        subprocess.run(command(args.output, args.seconds), check=True, timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"make_test_clip: ffmpeg failed: {e}", file=sys.stderr)
        return 1
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
