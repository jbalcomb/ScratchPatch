"""TaskDeck's receipt camera: starting, taking and retaking photos, the guides, and keeping photos only until sent."""

import asyncio
import io
import os
import time

import flet as ft
import flet_camera as fc
import pytest
from PIL import Image

from taskdeck.shared import files
from taskdeck.ui import camera_capture
from taskdeck.ui.camera_capture import CameraCapture


def jpeg(w=600, h=800, shade=200) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), (shade, shade, shade)).save(b, "JPEG")
    return b.getvalue()


class FakeCamera:
    """Stands in for flet_camera.Camera: the same async calls, recorded."""

    def __init__(self, width, height):
        self.width, self.height, self.content = width, height, None
        self.calls, self.shots = [], 0

    async def get_available_cameras(self):
        return [fc.CameraDescription("front", fc.CameraLensDirection.FRONT, 270),
                fc.CameraDescription("back", fc.CameraLensDirection.BACK, 90)]

    async def initialize(self, description, preset, enable_audio=True):
        self.calls.append(("initialize", description.name, preset, enable_audio))

    async def lock_capture_orientation(self, orientation=None):
        self.calls.append(("lock", orientation))

    async def set_flash_mode(self, mode):
        self.calls.append(("flash", mode))

    async def take_picture(self):
        self.shots += 1
        return jpeg(shade=100 + self.shots)


class FakePage:
    width = 412

    def __init__(self):
        self.dialogs, self.updates = [], 0

    def update(self):
        self.updates += 1

    def show_dialog(self, d):
        self.dialogs.append(d)

    def pop_dialog(self):
        self.dialogs.pop()

    def run_task(self, fn, *a):
        asyncio.run(fn(*a))


class FakeApp:
    def __init__(self):
        self.page, self.pushed, self.backs = FakePage(), [], 0

    def push(self, screen):
        self.pushed.append(screen)

    def back(self):
        self.backs += 1


@pytest.fixture(autouse=True)
def app_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKDECK_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(files, "is_android", lambda: False)


@pytest.fixture
def cam():
    done = []
    screen = CameraCapture(FakeApp(), "Receipt photos", done.append, make_camera=FakeCamera)
    screen.done_with = done
    asyncio.run(screen.start())
    return screen


def overlay_layers(screen):
    return screen.camera.content.controls


def test_starts_the_back_camera_without_audio(cam):
    assert cam.ready
    assert cam.camera.calls[0] == ("initialize", "back", fc.ResolutionPreset.VERY_HIGH, False)
    assert ("lock", ft.DeviceOrientation.PORTRAIT_UP) in cam.camera.calls
    assert ("flash", fc.FlashMode.OFF) in cam.camera.calls
    assert cam.done_button.disabled and cam.count.value == ""


def test_receipt_edge_guides_and_no_overlap_guide_before_the_first_photo(cam):
    labels = [layer.content.content.value for layer in overlay_layers(cam) if isinstance(layer.content, ft.RotatedBox)]
    assert labels == ["Left receipt edge", "Right receipt edge"]
    assert overlay_layers(cam)[0].left == 0 and overlay_layers(cam)[1].right == 0
    assert not any(isinstance(layer.content, ft.Stack) for layer in overlay_layers(cam))


def test_photos_are_taken_into_app_storage_and_the_overlap_guide_follows(cam):
    asyncio.run(cam.shoot())
    asyncio.run(cam.shoot())
    assert len(cam.photos) == 2 and all(p.parent == files.camera_dir() for p in cam.photos)
    assert cam.count.value == "2 photos" and not cam.done_button.disabled and len(cam.strip.controls) == 2
    guide = overlay_layers(cam)[0]
    ghost = guide.content.controls[0]
    assert ghost.src == cam.photos[-1].read_bytes()  # the bottom of the last photo...
    strip_h = round(cam.height * camera_capture.OVERLAP_SHARE)
    assert guide.height == strip_h and ghost.top == strip_h - cam.width * 800 / 600  # ...its bottom edge in view
    assert guide.content.controls[1].content.value == "Line this up with the end of the last photo"


def test_retake_replaces_a_photo_in_place_and_delete_removes_it(cam):
    for _ in range(3):
        asyncio.run(cam.shoot())
    first, second, third = cam.photos
    cam.show_photo(1)
    retake = next(a for a in cam.app.page.dialogs[-1].actions if a.content == "Retake")
    retake.on_click(None)
    assert cam.retake_at == 1 and cam.count.value == "Retaking photo 2"
    assert overlay_layers(cam)[0].content.controls[0].src == first.read_bytes()  # line up with the photo above it
    asyncio.run(cam.shoot())
    assert cam.photos[0] == first and cam.photos[2] == third and cam.photos[1] != second
    assert not second.exists() and cam.retake_at is None
    cam.show_photo(0)
    next(a for a in cam.app.page.dialogs[-1].actions if a.content == "Delete").on_click(None)
    assert cam.photos[0] != first and not first.exists() and len(cam.photos) == 2


def test_done_hands_the_photos_over_in_order(cam):
    asyncio.run(cam.shoot())
    asyncio.run(cam.shoot())
    taken = list(cam.photos)
    cam.done()
    assert cam.app.backs == 1 and cam.done_with == [taken]


def test_close_asks_before_throwing_photos_away(cam):
    asyncio.run(cam.shoot())
    path = cam.photos[0]
    cam.close()
    dialog = cam.app.page.dialogs[-1]
    assert dialog.title.value == "Throw away 1 photo?" and cam.app.backs == 0
    dialog.actions[0].on_click(None)  # keep taking photos
    assert path.exists() and cam.app.backs == 0
    cam.close()
    cam.app.page.dialogs[-1].actions[1].on_click(None)  # throw away
    assert not path.exists() and cam.app.backs == 1 and cam.done_with == []


def test_flash_cycles_off_auto_on(cam):
    asyncio.run(cam.toggle_flash())
    asyncio.run(cam.toggle_flash())
    assert [c[1] for c in cam.camera.calls if c[0] == "flash"] == [fc.FlashMode.OFF, fc.FlashMode.AUTO, fc.FlashMode.ALWAYS]
    assert cam.flash_button.icon == ft.Icons.FLASH_ON
    asyncio.run(cam.toggle_flash())
    assert cam.flash_button.icon == ft.Icons.FLASH_OFF


def test_a_camera_that_fails_to_start_says_so(monkeypatch):
    class Broken(FakeCamera):
        async def initialize(self, *a, **kw):
            raise RuntimeError("CameraAccessDenied")

    screen = CameraCapture(FakeApp(), "Receipt photos", lambda p: None, make_camera=Broken)
    asyncio.run(screen.start())
    assert not screen.ready and screen.message.value == "The camera didn't start: CameraAccessDenied"
    asyncio.run(screen.shoot())
    assert screen.photos == []


def test_an_app_without_the_camera_plugin_explains(monkeypatch):
    monkeypatch.setattr(camera_capture, "camera_supported", lambda: False)
    app = FakeApp()
    assert CameraCapture.open(app, lambda p: None) is None
    assert app.pushed == [] and "newest TaskDeck APK" in app.page.dialogs[0].content.value


def test_unsent_photos_are_cleared_after_a_week():
    old, new = files.camera_dir() / "old.jpg", files.camera_dir() / "new.jpg"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    week_ago = time.time() - 8 * 86400
    os.utime(old, (week_ago, week_ago))
    assert camera_capture.clear_old_photos() == 1 and not old.exists() and new.exists()
