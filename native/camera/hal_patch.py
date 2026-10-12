#!/usr/bin/env python3
"""The edits that turn stock files into the camera / microphone overlay.

Used by extract_hal.sh; every edit is a small pure function so it can be read, tested and
re-run on its own. Standard library only.

    hal_patch.py lens-facing   <in.so>  <out.so>     report the camera as a front camera
    hal_patch.py camera-config <in.xml> <out.xml>    sensor orientation 270 degrees
    hal_patch.py audio-policy  <in.xml> <out.xml>    declare a 48 kHz stereo microphone
    hal_patch.py fps-property  <in.so>  <out.so>     hwcomposer reads ro.farm.redroid_fps
    hal_patch.py build-prop    <in>     <out> --fps N   add ro.farm.redroid_fps=N

Each command refuses to guess: when the input does not look the way the edit expects, it
stops with an explanation instead of writing a file that might half-work.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- lens facing
# camera.device@3.4-external-impl.so (32-bit x86) builds the camera's static metadata in
# ExternalCameraDevice::initDefaultCharsKeys(). AOSP source:
#
#     const uint8_t facing = ANDROID_LENS_FACING_EXTERNAL;      // 2
#     UPDATE(ANDROID_LENS_FACING, &facing, 1);                  // tag 0x00080005
#
# which compiles to
#
#     c6 44 24 XX 02      movb  $0x2, XX(%esp)       <- the value, patched to 00 (FRONT)
#     8d 44 24 XX         lea   XX(%esp), %eax
#     6a 01               push  $0x1
#     50                  push  %eax
#     68 05 00 08 00      push  $0x80005
#
# Apps that ask for "the front camera" skip a camera that calls itself external, so the
# immediate is changed from 2 (EXTERNAL) to 0 (FRONT). The site is found by its shape, not
# by a fixed offset, so other builds of the same source are handled too.
LENS_FACING_TAG_PUSH = bytes.fromhex("6805000800")
LENS_FACING_EXTERNAL = 2
LENS_FACING_FRONT = 0
_FACING_STORE = re.compile(rb"\xc6\x44\x24(.)([\x00-\x02])\x8d\x44\x24(.)\x6a\x01\x50$", re.DOTALL)


class PatchError(ValueError):
    pass


def patch_lens_facing(data: bytes) -> bytes:
    """Return the library with LENS_FACING changed from EXTERNAL to FRONT.

    Already patched input is returned unchanged. Raises PatchError when the instruction
    sequence is not found exactly once.
    """
    sites: list[tuple[int, int]] = []       # (offset of the immediate, its value)
    start = 0
    while True:
        at = data.find(LENS_FACING_TAG_PUSH, start)
        if at < 0:
            break
        start = at + 1
        m = _FACING_STORE.search(data[max(0, at - 12):at])
        if m and m.group(1) == m.group(3):
            sites.append((max(0, at - 12) + m.start(2), m.group(2)[0]))
    if len(sites) != 1:
        raise PatchError(
            f"expected exactly one place that stores LENS_FACING, found {len(sites)}. This is not the "
            "32-bit x86 camera.device@3.4-external-impl.so of an Android 13 build, or its compiler "
            "arranged the code differently; see native/camera/README.md, 'The front-camera patch'.")
    offset, value = sites[0]
    if value == LENS_FACING_FRONT:
        return data
    if value != LENS_FACING_EXTERNAL:
        raise PatchError(f"LENS_FACING is {value}, expected {LENS_FACING_EXTERNAL} (external); not touching it")
    return data[:offset] + bytes([LENS_FACING_FRONT]) + data[offset + 1:]


# ------------------------------------------------------------- camera config
ORIENTATION_COMMENT = "<!-- The farm's cameras act as a phone's front sensor -->"
_ORIENTATION = re.compile(r'<Orientation\s+degree="[^"]*"\s*/>')
_DEVICE_OPEN = re.compile(r"^([ \t]*)<Device>[ \t]*\r?\n", re.MULTILINE)


def patch_camera_config(xml: str, degrees: int = 270) -> str:
    """Set the sensor orientation in external_camera_config.xml.

    A phone's front sensor is mounted sideways (270 degrees) and apps rotate its picture
    accordingly. An external camera declares 0, so a portrait app would show the picture
    lying on its side. The feed stores its upright picture rotated to match.
    """
    element = f'<Orientation degree="{int(degrees)}"/>'
    if _ORIENTATION.search(xml):
        return _ORIENTATION.sub(element, xml, count=1)
    m = _DEVICE_OPEN.search(xml)
    if not m:
        raise PatchError("no <Device> element found: this is not an external_camera_config.xml")
    indent = m.group(1) + "    "
    return xml[:m.end()] + f"{indent}{ORIENTATION_COMMENT}\n{indent}{element}\n" + xml[m.end():]


# -------------------------------------------------------------- audio policy
_INPUT_PORT = re.compile(r'(<mixPort\s+name="primary input".*?</mixPort>)', re.DOTALL)
_INPUT_PROFILE = re.compile(r'samplingRates="[^"]*"(\s+)channelMasks="[^"]*"')


def patch_audio_policy(xml: str) -> str:
    """Declare the built-in microphone as 48 kHz stereo in
    primary_audio_policy_configuration.xml.

    The microphone shim only replaces input streams of exactly that format (it is what the
    feed delivers). Android opens the input in the format the policy file announces, so the
    two must agree or every recording falls back to the stub's silence.
    """
    block = _INPUT_PORT.search(xml)
    if not block:
        raise PatchError('no <mixPort name="primary input"> found in the audio policy file')
    patched, count = _INPUT_PROFILE.subn(
        r'samplingRates="48000"\1channelMasks="AUDIO_CHANNEL_IN_STEREO"', block.group(1))
    if count != 1:
        raise PatchError(f"expected one input profile in the primary input mixPort, found {count}")
    return xml[:block.start()] + patched + xml[block.end():]


# ------------------------------------------------------------- frame rate
FPS_PROP_STOCK = b"ro.boot.redroid_fps"
FPS_PROP_FARM = b"ro.farm.redroid_fps"
assert len(FPS_PROP_STOCK) == len(FPS_PROP_FARM)   # an in-place edit must not move anything


def patch_fps_property(data: bytes) -> bytes:
    """Make hwcomposer.redroid.so read its frame rate from ro.farm.redroid_fps.

    Stock redroid takes the rate from the boot argument androidboot.redroid_fps
    (ro.boot.redroid_fps), which is fixed when the container is created. With the property
    name swapped for one of the same length, the rate comes from /vendor/build.prop and can
    be changed by editing that file and restarting the Android.
    """
    if FPS_PROP_STOCK not in data and data.count(FPS_PROP_FARM) == 1:
        return data
    if data.count(FPS_PROP_STOCK) != 1:
        raise PatchError(f"expected the string {FPS_PROP_STOCK.decode()} exactly once in the hwcomposer "
                         f"module, found it {data.count(FPS_PROP_STOCK)} times")
    return data.replace(FPS_PROP_STOCK, FPS_PROP_FARM)


_FPS_LINE = re.compile(r"^ro\.farm\.redroid_fps=.*\n?", re.MULTILINE)


def patch_build_prop(text: str, fps: int) -> str:
    """Add (or replace) the frame-rate line the patched hwcomposer reads."""
    if not 1 <= int(fps) <= 240:
        raise PatchError(f"bad frame rate {fps}")
    text = _FPS_LINE.sub("", text)
    text = re.sub(r"# Display frame rate \(read by the patched hwcomposer.*\n", "", text)
    return (text.rstrip("\n") + "\n# Display frame rate (read by the patched hwcomposer instead of the boot argument)\n"
            f"ro.farm.redroid_fps={int(fps)}\n")


# ------------------------------------------------------------------- command
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply one camera-overlay edit to a file.")
    parser.add_argument("edit", choices=["lens-facing", "camera-config", "audio-policy", "fps-property", "build-prop"])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--fps", type=int, default=0, help="frame rate for build-prop")
    args = parser.parse_args(argv)
    try:
        if args.edit == "lens-facing":
            args.target.write_bytes(patch_lens_facing(args.source.read_bytes()))
        elif args.edit == "fps-property":
            args.target.write_bytes(patch_fps_property(args.source.read_bytes()))
        elif args.edit == "camera-config":
            args.target.write_text(patch_camera_config(args.source.read_text()))
        elif args.edit == "audio-policy":
            args.target.write_text(patch_audio_policy(args.source.read_text()))
        else:
            args.target.write_text(patch_build_prop(args.source.read_text(), args.fps))
    except (PatchError, OSError) as e:
        print(f"hal_patch: {args.edit}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
