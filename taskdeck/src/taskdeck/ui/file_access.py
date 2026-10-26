"""Android's "All files access" (MANAGE_EXTERNAL_STORAGE), needed to read archives and write
extracted files anywhere on the phone. Uses the flet-permission-handler plugin (in the APK).
On a desktop there is nothing to ask for."""

from __future__ import annotations

import importlib.util

from taskdeck.shared.files import is_android


def supported() -> bool:
    """False on an APK built before the permission plugin was added."""
    if not is_android():
        return True
    return importlib.util.find_spec("flet_permission_handler") is not None


async def granted() -> bool:
    if not is_android():
        return True
    if not supported():
        return False
    from flet_permission_handler import Permission, PermissionHandler, PermissionStatus

    status = await PermissionHandler().get_status(Permission.MANAGE_EXTERNAL_STORAGE)
    return status == PermissionStatus.GRANTED


async def request() -> bool:
    """Open Android's "All files access" page for TaskDeck; True once it is granted."""
    if not is_android():
        return True
    if not supported():
        return False
    from flet_permission_handler import Permission, PermissionHandler, PermissionStatus

    status = await PermissionHandler().request(Permission.MANAGE_EXTERNAL_STORAGE)
    return status == PermissionStatus.GRANTED


EXPLAIN = ("TaskDeck needs access to your files to open archives and save what's in them. "
           "Android will show a switch: “Allow access to manage all files”.")
NEEDS_APK = "File access needs the latest APK. Install it from the TaskDeck release page."
