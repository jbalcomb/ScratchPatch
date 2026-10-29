import csv
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from assetforge import export
from assetforge.lbx import LbxError, read_lbx
from assetforge.palette import vga6_to_8
from tests.builders import build_flic, build_lbx

N = None


def fonts_lbx():
    pal = bytearray(5472)
    for i in range(256):
        pal[3 * i:3 * i + 3] = bytes((i % 64, (i * 2) % 64, 63))
    pal[0x1500:0x1504] = bytes((0, 0, 0, 50))          # remap block 0: black at 50%
    return build_lbx([b"font", b"font", bytes(pal)])


class LbxTest(unittest.TestCase):

    def test_names_and_entries(self):
        entries = read_lbx(build_lbx([b"abc", b"de"], [("UNIT1", "dwarf"), ("UNIT2", "")]))
        self.assertEqual([(e.index, e.name, e.description, e.data) for e in entries],
                         [(0, "UNIT1", "dwarf", b"abc"), (1, "UNIT2", "", b"de")])

    def test_rejects_other_files(self):
        with self.assertRaises(LbxError):
            read_lbx(b"PK\x03\x04" + bytes(600))

    def test_vga_scale(self):
        self.assertEqual((vga6_to_8(0), vga6_to_8(63), vga6_to_8(32)), (0, 255, 130))


class ExportTest(unittest.TestCase):

    def test_export_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            game, out = Path(tmp, "game"), Path(tmp, "out")
            game.mkdir()
            (game / "FONTS.LBX").write_bytes(fonts_lbx())
            sprite = build_flic(2, 2, [[1, N, 2, 232], [3, 3, 3, 3]], remap=1)
            (game / "UNITS.LBX").write_bytes(build_lbx([sprite, b"not a sprite"], [("DWARF", "dwarf"), ("SND", "")]))
            export.main([str(game), str(out)])

            folder = out / "UNITS" / "000_DWARF"
            rgba = Image.open(folder / "frame_00.png")
            self.assertEqual(rgba.size, (2, 2))
            self.assertEqual(rgba.getpixel((0, 0)), (vga6_to_8(1), vga6_to_8(2), 255, 255))
            self.assertEqual(rgba.getpixel((1, 0))[3], 0)                       # nothing drawn
            self.assertEqual(rgba.getpixel((1, 1)), (0, 0, 0, 127))             # remap block 0
            index = Image.open(folder / "frame_00_index.png")
            self.assertEqual(index.mode, "LA")
            self.assertEqual([index.getpixel(p) for p in [(0, 0), (1, 0), (0, 1), (1, 1)]],
                             [(1, 255), (0, 0), (2, 255), (232, 128)])
            with Image.open(folder / "sheet.png") as s:
                self.assertEqual(s.size, (4, 2))

            with open(out / "catalog.csv", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([(r["archive"], r["entry"], r["kind"]) for r in rows],
                             [("FONTS", "0", "other"), ("FONTS", "1", "other"), ("FONTS", "2", "other"),
                              ("UNITS", "0", "sprite"), ("UNITS", "1", "other")])
            dwarf = rows[3]
            self.assertEqual((dwarf["width"], dwarf["frames"], dwarf["remap"], dwarf["remap_pixels"]), ("2", "2", "1", "1"))
            self.assertIn("000_DWARF/sheet.png", (out / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
