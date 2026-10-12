"""Camera, microphone and sound.

    camera.Camera     virtual front camera + microphone fed from a video file or an RTMP stream
    audio.Listener    listen to one Android on the host's speakers
    feeds             the pieces both are built from (helper processes, ffmpeg commands, files)

Imports are lazy so that `import superfarm.media` stays cheap for callers that only need
one of the two.
"""
from __future__ import annotations

__all__ = ["Camera", "Listener", "host_device_commands", "host_udev_rule"]


def __getattr__(name: str):
    if name in ("Camera", "host_device_commands", "host_udev_rule"):
        from . import camera
        return getattr(camera, name)
    if name == "Listener":
        from .audio import Listener
        return Listener
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
