import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
from PIL import Image

from assetforge import dataset, export
from tests.builders import build_flic, build_lbx
from tests.test_export import fonts_lbx


class PickKeyTest(unittest.TestCase):

    def test_avoids_colours_in_use(self):
        from collections import Counter
        self.assertNotEqual(dataset.pick_key(Counter({(250, 10, 250): 100, (10, 10, 10): 100})), "magenta")
        self.assertEqual(dataset.pick_key(Counter({(0, 250, 0): 100, (0, 250, 250): 100, (250, 250, 0): 100})),
                         "magenta")


class CaptionTest(unittest.TestCase):

    def test_magenta_meaning_transparency_becomes_the_key(self):
        desc = {"prompt": "golden frame, solid Magenta interior"}
        self.assertEqual(dataset.caption("icon", "blue", desc, None, True, magenta_drawn=False)[0],
                         "momicon, golden frame, solid blue interior, isolated on a plain flat blue background")
        self.assertIn("solid Magenta interior", dataset.caption("icon", "blue", desc, None, True, True)[0])
        self.assertIn("solid Magenta interior", dataset.caption("icon", "magenta", desc, None, True, False)[0])

    def test_has_magenta(self):
        rgb = np.array([[[200, 30, 220], [10, 10, 10]]], np.uint8)
        self.assertTrue(dataset.has_magenta(rgb, np.array([[True, True]])))
        self.assertFalse(dataset.has_magenta(rgb, np.array([[False, True]])))


class EnlargeTest(unittest.TestCase):

    def test_transparent_sprite_on_key_canvas(self):
        rgb = np.full((16, 18, 3), 100, np.uint8)
        drawn = np.zeros((16, 18), bool)
        drawn[4:12, 5:13] = True
        img, k = dataset.enlarge(rgb, drawn, (255, 0, 255))
        self.assertEqual(img.size, (1024, 1024))                # SDXL size closest in shape to 18x16
        self.assertEqual(k, 56)                                 # biggest whole number that fits
        a = np.asarray(img)
        self.assertEqual(tuple(a[0, 0]), (255, 0, 255))
        self.assertEqual(set(map(tuple, a.reshape(-1, 3).tolist())), {(255, 0, 255), (100, 100, 100)})

    def test_opaque_picture_just_enlarged(self):
        img, k = dataset.enlarge(np.zeros((200, 320, 3), np.uint8), np.ones((200, 320), bool), (0, 255, 0))
        self.assertEqual((img.size, k), ((960, 600), 3))


class DatasetTest(unittest.TestCase):

    def test_builds_captioned_training_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, orig, out = root / "game", root / "orig", root / "out"
            game.mkdir()
            (game / "FONTS.LBX").write_bytes(fonts_lbx())
            figure = [None] * 20 + [30] * 60 + [None] * 20               # 10x10 with transparent rows
            other = [None] * 20 + [40] * 60 + [None] * 20
            (game / "UNITS1.LBX").write_bytes(build_lbx(
                [build_flic(10, 10, [figure, other]), build_flic(10, 10, [figure]), build_flic(4, 4, [[30] * 16])],
                [("A", ""), ("B", ""), ("DOT", "")]))
            with redirect_stdout(io.StringIO()):
                export.main([str(game), str(orig)])
                (orig / "hints.csv").write_text("archive,entry,hint\nUNITS1,0,icon of a hero unit: Dwarf\n")
                (orig / "UNITS1" / "000_A" / "describe.json").write_text(json.dumps(
                    {"prompt": "dwarf with an axe, transparent background, red cloak"}))
                self.assertEqual(dataset.main([str(orig), str(out), "--frames", "all", "--balance", "6"]), 0)

            with open(out / "manifest.csv", newline="") as f:
                rows = list(csv.DictReader(f))
            # entry 0 frames 0 and 1; entry 1 is identical to entry 0 frame 0; entry 2 is too small
            self.assertEqual([(r["entry"], r["frame"]) for r in rows], [("0", "frame_00.png"), ("0", "frame_01.png")])
            info = json.loads((out / "buckets.json").read_text())["unit_icon"]
            self.assertEqual((info["images"], info["repeats"]), (2, 3))
            folder = out / "img" / "3_momuniticon"
            text = (folder / "UNITS1_0000_00.txt").read_text().strip()
            self.assertEqual(text, f"momuniticon, dwarf with an axe, red cloak, isolated on a plain flat "
                                   f"{info['key']} background")
            with Image.open(folder / "UNITS1_0000_00.png") as img:
                self.assertEqual(img.getpixel((0, 0)), tuple(info["key_rgb"]))
