"""Control: apps, files, device settings and shell commands for any app on any Android.

    AppLibrary   the farm's store of installable apps (APK and split bundles)
    Apps         install / ensure / uninstall / launch / stop / permissions / inventory
    Files        push, pull, list, add to the gallery
    Device       info, locale, time zone, location, proxy, clipboard, reboot, logcat, ...
    Shell        run a command on a selector, bounded in time and output

Everything that touches more than one Android returns a core.tasks.Task and treats each
device independently.
"""
from .apps import Apps
from .device import Device
from .files import Files
from .library import AppLibrary, LibraryError
from .shell import Refused, Shell

__all__ = ["AppLibrary", "Apps", "Device", "Files", "LibraryError", "Refused", "Shell"]
