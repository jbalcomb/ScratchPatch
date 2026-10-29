"""Compare assetforge.flic with ReMoM's own C decode functions, compiled straight from MoX/src/FLIC_Draw.c.

Runs when REMOM_DIR points at a ReMoM checkout and a C compiler (cc, gcc or clang) is on PATH; skipped otherwise.

    REMOM_DIR=~/ReMoM python3 -m unittest tests.test_engine_parity
"""

import os
import random
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from assetforge import flic
from tests.builders import build_flic

ENGINE_FUNCTIONS = ["FLIC_Draw_Frame", "FLIC_Remap_Draw_Frame", "Add_Picture_To_Bitmap"]
DRAW, REMAP_DRAW, BITMAP = 0, 1, 2


def extract_function(source, name):
    m = re.search(rf"^void {name}\(.*?^}}", source, re.S | re.M)
    if not m:
        raise AssertionError(f"{name} not found in FLIC_Draw.c")
    return m.group(0)


@unittest.skipUnless(os.environ.get("REMOM_DIR"), "set REMOM_DIR to a ReMoM checkout")
class EngineParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        compiler = next((c for c in ("cc", "gcc", "clang") if shutil.which(c)), None)
        if not compiler:
            raise unittest.SkipTest("no C compiler on PATH")
        source = (Path(os.environ["REMOM_DIR"]) / "MoX/src/FLIC_Draw.c").read_text(encoding="latin-1")
        harness = (Path(__file__).parent / "engine_harness.c").read_text()
        code = harness.replace("/* ENGINE FUNCTIONS */", "\n\n".join(extract_function(source, f) for f in ENGINE_FUNCTIONS))
        cls.tmp = tempfile.TemporaryDirectory()
        c_file = Path(cls.tmp.name, "harness.c")
        c_file.write_text(code)
        cls.exe = str(Path(cls.tmp.name, "harness"))
        subprocess.run([compiler, "-O1", "-w", "-o", cls.exe, str(c_file)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_engine(self, mode, w, h, fill, frame_bytes):
        return subprocess.run([self.exe, str(mode), str(w), str(h), str(fill)], input=frame_bytes,
                              capture_output=True, check=True).stdout

    def test_random_sprites_match_the_engine(self):
        rng = random.Random(7)
        for trial in range(300):
            w, h = rng.randint(1, 64), rng.randint(1, 120)
            packed = rng.random() < 0.6
            remap = int(rng.random() < 0.4)
            px = [None if rng.random() < 0.35 else rng.randrange(256 if packed else 224) for _ in range(w * h)]
            data = build_flic(w, h, [px], packed=packed, remap=remap)
            frame_bytes = data[0x12 + 8 + 1:]     # the single frame, past the byte the engine skips
            mine = flic.decode(data).frames[0]
            for mode in ([REMAP_DRAW] if remap else [DRAW, BITMAP]):
                # Draw twice over different backgrounds: a pixel the engine wrote is the same in both.
                a = self.run_engine(mode, w, h, 0, frame_bytes)
                b = self.run_engine(mode, w, h, 255, frame_bytes)
                for i in range(w * h):
                    if mine.cover[i] == flic.COVER_REMAP:
                        block = mine.index[i] - flic.REMAP_FIRST     # harness remap table: entry k -> k * 7 + 3
                        self.assertEqual((a[i], b[i]), ((block * 256 * 7 + 3) & 0xFF, ((block * 256 + 255) * 7 + 3) & 0xFF))
                        continue
                    self.assertEqual(a[i] == b[i], mine.cover[i] != flic.COVER_NONE, f"trial {trial} mode {mode} pixel {i}")
                    if a[i] == b[i]:
                        self.assertEqual(a[i], mine.index[i], f"trial {trial} mode {mode} pixel {i}")


if __name__ == "__main__":
    unittest.main()
