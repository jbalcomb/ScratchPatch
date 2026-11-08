"""TaskDeck's camera for receipts (a pushed screen), modelled on Instacart's receipt camera.

* A live camera view, with ✕ (asks before throwing photos away) and a flash switch (off, auto, on).
* The photos taken so far in a strip along the top; tap one to see it large, retake it or delete it.
* Receipt edge guides: shaded bands down both sides, "Left receipt edge" / "Right receipt edge", so the
  receipt fills the width (bigger text reads better).
* An overlap guide: after the first photo, the bottom of the last photo shows faded at the top of the view.
  Lining it up gives each photo of a long receipt the overlap the reading needs to join them.
* A shutter, and ✓ to finish the receipt: the photos go to the job, in order.

Photos are kept in the app's own storage (``files.camera_dir()``), never the gallery, and the job deletes
them once they are sent (Receipt capture); ones left behind (a receipt never sent) are cleared after a week.

The camera is flet-camera, a plugin built into the APK: an app installed before it was added says so
(``camera_supported``) instead of opening. Permission is asked through flet-permission-handler, as the
plugin's documentation recommends.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import flet as ft

from taskdeck.shared import files, photos


if TYPE_CHECKING:
    from .app import TaskDeckApp

KEEP_UNSENT_DAYS = 7
GUIDE_SHARE = 0.10   # each edge band, of the view's width
OVERLAP_SHARE = 0.18  # the faded strip of the last photo, of the view's height
NEEDS_APK = ("The camera needs the newest TaskDeck APK (the camera plugin is built into the app). Install it "
             "from Settings → App updates. Until then, use Add… to pick photos you took with the camera app.")
CAMERA_BLOCKED = ("The camera is blocked for TaskDeck. Open TaskDeck's app info → Permissions → Camera → "
                  "“Allow only while using the app”.")
FLASH = [("off", ft.Icons.FLASH_OFF, "Flash off"), ("auto", ft.Icons.FLASH_AUTO, "Flash auto"),
         ("always", ft.Icons.FLASH_ON, "Flash on")]


def camera_supported() -> bool:
    """False on an APK built before the camera plugin was added (a live update can't add it)."""
    return importlib.util.find_spec("flet_camera") is not None


def clear_old_photos(now: float | None = None) -> int:
    """Delete camera photos older than KEEP_UNSENT_DAYS (never sent). -> how many."""
    cutoff = (time.time() if now is None else now) - KEEP_UNSENT_DAYS * 86400
    n = 0
    for p in files.camera_dir().glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


def _flet_camera(width: float, height: float):
    import flet_camera as fc

    return fc.Camera(width=width, height=height, preview_enabled=True)


class CameraCapture:
    def __init__(self, app: "TaskDeckApp", title: str, on_done: Callable[[list[Path]], None],
                 guides: str = "receipt", make_camera: Callable[[float, float], Any] = _flet_camera):
        self.app, self.title, self.on_done, self.guides = app, title, on_done, guides
        self.photos: list[Path] = []
        self.retake_at: int | None = None
        self.flash = 0
        self.busy = False
        self.ready = False
        self.width = float(getattr(app.page, "width", None) or 412)
        self.height = round(self.width * 4 / 3)  # a phone camera's 3:4 portrait frame
        self.camera = make_camera(self.width, self.height)
        self.message = ft.Text("Starting the camera…", color=ft.Colors.WHITE, text_align=ft.TextAlign.CENTER)
        self.strip = ft.Row(spacing=8, scroll=ft.ScrollMode.AUTO, height=84)
        self.flash_button = ft.IconButton(FLASH[0][1], tooltip=FLASH[0][2], on_click=self.toggle_flash)
        self.count = ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT, width=88)
        self.shutter = ft.Container(
            ft.Container(width=58, height=58, border_radius=29, bgcolor=ft.Colors.PRIMARY),
            width=78, height=78, border_radius=39, border=ft.Border.all(4, ft.Colors.PRIMARY),
            alignment=ft.Alignment.CENTER, padding=6, on_click=self.shoot, tooltip="Take a photo")
        self.done_button = ft.IconButton(ft.Icons.CHECK, tooltip="Done: this receipt is complete", disabled=True,
                                         icon_size=28, on_click=lambda e: self.done(),
                                         style=ft.ButtonStyle(bgcolor=ft.Colors.SECONDARY_CONTAINER))
        self.preview = ft.Container(self.camera, width=self.width, height=self.height, bgcolor=ft.Colors.BLACK)
        self.view = ft.View(
            route="/camera", padding=0,
            controls=[ft.SafeArea(ft.Column([
                ft.Row([ft.IconButton(ft.Icons.CLOSE, tooltip="Close", on_click=lambda e: self.close()),
                        ft.Text(title, size=16, weight=ft.FontWeight.W_600, expand=True), self.flash_button],
                       height=52),
                ft.Container(self.strip, padding=ft.Padding.only(left=12, right=12)),
                self.preview,
                ft.Row([self.count, self.shutter, ft.Container(self.done_button, width=88,
                                                               alignment=ft.Alignment.CENTER)],
                       alignment=ft.MainAxisAlignment.SPACE_EVENLY, height=110),
            ], spacing=0, expand=True), expand=True)],
        )
        self._render()

    @classmethod
    def open(cls, app: "TaskDeckApp", on_done: Callable[[list[Path]], None], title: str = "Receipt photos",
             guides: str = "receipt") -> "CameraCapture | None":
        """Push the camera screen and start the camera; on an app without the plugin, say so instead."""
        if not camera_supported():
            app.page.show_dialog(ft.AlertDialog(title=ft.Text("Camera"), content=ft.Text(NEEDS_APK),
                                                actions=[ft.TextButton("OK", on_click=lambda e: app.page.pop_dialog())]))
            return None
        clear_old_photos()
        screen = cls(app, title, on_done, guides)
        app.push(screen)
        app.page.run_task(screen.start)
        return screen

    # ---- starting the camera -------------------------------------------------------------------

    async def start(self) -> None:
        problem = await self._permission()
        if problem:
            self._fail(problem)
            return
        try:
            import flet_camera as fc

            cameras = await self.camera.get_available_cameras()
            if not cameras:
                self._fail("No camera found on this phone.")
                return
            back = next((c for c in cameras if c.lens_direction == fc.CameraLensDirection.BACK), cameras[0])
            await self.camera.initialize(back, fc.ResolutionPreset.VERY_HIGH, enable_audio=False)
            try:
                await self.camera.lock_capture_orientation(ft.DeviceOrientation.PORTRAIT_UP)
            except Exception:  # not every device supports it; photos still carry their orientation
                pass
        except Exception as exc:
            self._fail(f"The camera didn't start: {exc}")
            return
        self.ready = True
        await self._set_flash()
        self._render()

    async def _permission(self) -> str:
        """"" when the camera may be used; otherwise what to tell the user."""
        if not files.is_android():
            return ""
        try:
            from flet_permission_handler import Permission, PermissionHandler, PermissionStatus
        except ImportError:
            return NEEDS_APK
        handler = PermissionHandler()
        if await handler.get_status(Permission.CAMERA) == PermissionStatus.GRANTED:
            return ""
        status = await handler.request(Permission.CAMERA)
        if status == PermissionStatus.GRANTED:
            await asyncio.sleep(0.8)  # let Android finish returning to TaskDeck before the camera opens
            return ""
        if status == PermissionStatus.PERMANENTLY_DENIED:
            return CAMERA_BLOCKED
        return "TaskDeck needs the camera to photograph receipts. Close this and tap Take photos again."

    def _fail(self, text: str) -> None:
        self.message.value = text
        self.ready = False
        self._render()

    # ---- taking photos ---------------------------------------------------------------------------

    async def shoot(self, e=None) -> None:
        if self.busy or not self.ready:
            return
        self.busy = True
        try:
            data = await self.camera.take_picture()
            path = files.camera_dir() / f"receipt-{time.strftime('%Y%m%d-%H%M%S')}-{len(self.photos) + 1}.jpg"
            n = 2
            while path.exists():
                path = path.with_name(f"{path.stem}-{n}.jpg")
                n += 1
            path.write_bytes(bytes(data))
            if self.retake_at is not None and self.retake_at < len(self.photos):
                self.photos[self.retake_at].unlink(missing_ok=True)
                self.photos[self.retake_at] = path
            else:
                self.photos.append(path)
            self.retake_at = None
        except Exception as exc:
            self.message.value = f"That photo didn't work: {exc}"
        finally:
            self.busy = False
        self._render()

    async def toggle_flash(self, e=None) -> None:
        self.flash = (self.flash + 1) % len(FLASH)
        await self._set_flash()
        self._render()

    async def _set_flash(self) -> None:
        if not self.ready:
            return
        try:
            import flet_camera as fc

            await self.camera.set_flash_mode(fc.FlashMode(FLASH[self.flash][0]))
        except Exception:
            pass

    # ---- the photos taken ------------------------------------------------------------------------

    def show_photo(self, i: int) -> None:
        """A photo large, with Retake (the next shot takes its place) and Delete."""
        page = self.app.page
        path = self.photos[i]

        def act(what: str):
            def run(e=None):
                page.pop_dialog()
                if what == "delete":
                    self.photos.pop(i).unlink(missing_ok=True)
                    self.retake_at = None
                elif what == "retake":
                    self.retake_at = i
                self._render()
            return run

        try:
            data = path.read_bytes()
        except OSError:
            return
        page.show_dialog(ft.AlertDialog(
            title=ft.Text(f"Photo {i + 1} of {len(self.photos)}", size=14),
            content=ft.InteractiveViewer(ft.Image(src=data, fit=ft.BoxFit.CONTAIN), min_scale=1, max_scale=6),
            actions=[ft.TextButton("Delete", icon=ft.Icons.DELETE_OUTLINE, on_click=act("delete")),
                     ft.TextButton("Retake", icon=ft.Icons.REPLAY, on_click=act("retake")),
                     ft.TextButton("Close", on_click=act("close"))],
            inset_padding=8))

    def done(self) -> None:
        taken = list(self.photos)
        self.app.back()
        if taken:
            self.on_done(taken)

    def close(self) -> None:
        if not self.photos:
            self.app.back()
            return
        page = self.app.page

        def discard(e=None):
            page.pop_dialog()
            for p in self.photos:
                p.unlink(missing_ok=True)
            self.photos = []
            self.app.back()

        n = len(self.photos)
        page.show_dialog(ft.AlertDialog(
            title=ft.Text(f"Throw away {n} photo{'s' * (n != 1)}?"),
            content=ft.Text("They haven't been sent. ✓ keeps them for the receipt instead."),
            actions=[ft.TextButton("Keep taking photos", on_click=lambda e: page.pop_dialog()),
                     ft.TextButton("Throw away", on_click=discard)]))

    # ---- drawing ---------------------------------------------------------------------------------

    def _render(self) -> None:
        n = len(self.photos)
        self.strip.controls = [self._thumb(i, p) for i, p in enumerate(self.photos)]
        self.count.value = ("Retaking photo " + str(self.retake_at + 1)) if self.retake_at is not None else \
            (f"{n} photo{'s' * (n != 1)}" if n else "")
        self.done_button.disabled = n == 0
        self.flash_button.icon, self.flash_button.tooltip = FLASH[self.flash][1], FLASH[self.flash][2]
        self.camera.content = self._overlay()
        self.preview.content = self.camera
        try:
            self.app.page.update()
        except Exception:  # not on screen yet
            pass

    def _thumb(self, i: int, path: Path) -> ft.Control:
        try:
            data = path.read_bytes()
        except OSError:
            data = None
        selected = self.retake_at == i
        return ft.Container(
            ft.Image(src=data, width=60, height=80, fit=ft.BoxFit.COVER, cache_width=180) if data else
            ft.Icon(ft.Icons.BROKEN_IMAGE_OUTLINED),
            width=60, height=80, border_radius=6, clip_behavior=ft.ClipBehavior.HARD_EDGE,
            border=ft.Border.all(3, ft.Colors.PRIMARY) if selected else None,
            on_click=lambda e, i=i: self.show_photo(i), tooltip=f"Photo {i + 1}: tap to see it, retake or delete")

    def _overlay(self) -> ft.Control:
        w, h = self.width, self.height
        layers: list[ft.Control] = []
        guide = self._overlap_guide()
        if guide is not None:
            layers.append(guide)
        if self.guides == "receipt":
            band = round(w * GUIDE_SHARE)
            for side, turns, label in (("left", 3, "Left receipt edge"), ("right", 1, "Right receipt edge")):
                layers.append(ft.Container(
                    ft.RotatedBox(quarter_turns=turns, content=ft.Text(label, color=ft.Colors.WHITE, size=15,
                                                                        weight=ft.FontWeight.W_500)),
                    width=band, top=0, bottom=0, alignment=ft.Alignment.CENTER,
                    bgcolor=ft.Colors.with_opacity(0.45, ft.Colors.BLACK), **{side: 0}))
        if not self.ready:
            layers.append(ft.Container(self.message, left=0, right=0, top=0, bottom=0, alignment=ft.Alignment.CENTER,
                                       padding=32))
        return ft.Stack(layers, width=w, height=h)

    def _overlap_guide(self) -> ft.Control | None:
        """The bottom of the last photo, faded, across the top of the view (none before the first photo, or
        while retaking the first)."""
        last = self.photos[-1] if self.photos and self.retake_at is None else \
            (self.photos[self.retake_at - 1] if self.retake_at else None)
        if last is None:
            return None
        try:
            data = last.read_bytes()
        except OSError:
            return None
        w = self.width
        size = photos.shown_size(data)
        shown_h = w * size[1] / size[0] if size and size[0] else self.height
        strip_h = round(self.height * OVERLAP_SHARE)
        return ft.Container(
            ft.Stack([
                ft.Image(src=data, width=w, height=shown_h, fit=ft.BoxFit.FILL, cache_width=720,
                         left=0, top=strip_h - shown_h, opacity=0.45),
                ft.Container(ft.Text("Line this up with the end of the last photo", size=12, color=ft.Colors.WHITE),
                             left=0, right=0, bottom=0, padding=ft.Padding.symmetric(vertical=3),
                             alignment=ft.Alignment.CENTER, bgcolor=ft.Colors.with_opacity(0.5, ft.Colors.BLACK)),
            ], width=w, height=strip_h),
            left=0, top=0, width=w, height=strip_h, clip_behavior=ft.ClipBehavior.HARD_EDGE,
            border=ft.Border.only(bottom=ft.BorderSide(2, ft.Colors.with_opacity(0.8, ft.Colors.WHITE))),
        )
