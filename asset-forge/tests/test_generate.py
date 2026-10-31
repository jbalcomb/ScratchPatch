import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from assetforge import describe, export, generate
from tests.builders import build_flic, build_lbx
from tests.test_describe import GOOD, FakeServer
from tests.test_export import fonts_lbx
from tests.test_pixelize import generated


class FakeModel:
    """Stands in for SDXL: records calls and returns a red disc on magenta at the requested size."""
    calls = []

    def __init__(self, model, **kw):
        self.info = {"model": model, "lora": kw.get("lora"), "steps": kw.get("steps"), "cfg": kw.get("cfg")}

    def __call__(self, prompt, negative, size, seed):
        FakeModel.calls.append((prompt, negative, size, seed))
        return generated(size)


class GenerateTest(unittest.TestCase):

    def setUp(self):
        generate.BACKENDS["fake"] = FakeModel
        FakeModel.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        game, self.orig, self.pack = root / "game", root / "orig", root / "pack"
        game.mkdir()
        (game / "FONTS.LBX").write_bytes(fonts_lbx())
        many = [i % 12 + 20 for i in range(16 * 20)]                       # 12 colours
        few = [5] * 40                                                     # 1 colour
        (game / "UNITS.LBX").write_bytes(build_lbx(
            [build_flic(16, 20, [many, many]), build_flic(8, 5, [few], palette=(0, [(63, 0, 0)] * 256))],
            [("KNIGHT", ""), ("GEM", "")]))
        export.main([str(game), str(self.orig)])
        server = FakeServer([])
        try:
            describe.main([str(self.orig), "--endpoint", server.url])
        finally:
            server.close()

    def tearDown(self):
        del generate.BACKENDS["fake"]
        self.tmp.cleanup()

    def run_generate(self, *extra):
        return generate.main([str(self.orig), str(self.pack), "--model", "base.safetensors", "--backend", "fake", *extra])

    def test_trigger_word_and_key_colour_from_training_set(self):
        trained = self.pack.parent / "buckets.json"
        trained.write_text(json.dumps({"ui": {"token": "momui", "key": "green"}}))      # UNITS is a ui archive
        self.assertEqual(self.run_generate("--buckets", str(trained), "--limit", "1"), 0)
        prompt = FakeModel.calls[0][0]
        self.assertTrue(prompt.startswith("momui, "))
        self.assertIn("isolated on a plain flat green background", prompt)
        self.assertNotIn("magenta", prompt)
        record = json.loads(next(self.pack.glob("*/*/generate.json")).read_text())
        self.assertEqual(record["key"], "green")

    def test_generates_game_ready_sprites(self):
        self.assertEqual(self.run_generate("--lora", "style.safetensors"), 0)
        self.assertEqual(len(FakeModel.calls), 2)
        prompt, negative, size, seed = FakeModel.calls[0]
        self.assertTrue(prompt.startswith("a red blob, "))
        self.assertNotIn("transparent background", prompt)
        self.assertIn(generate.BACKGROUND, prompt)
        self.assertIn("text", negative)
        self.assertEqual(size, (896, 1152))                                # 16x20 is taller than wide

        knight = self.pack / "UNITS/000_KNIGHT"
        with Image.open(knight / "frame_00.png") as img:
            self.assertEqual((img.size, img.mode), ((16, 20), "RGBA"))
        with Image.open(knight / "frame_00_index.png") as img:
            a = np.asarray(img)
        used = set(np.unique(a[:, :, 0][a[:, :, 1] == 255]).tolist())
        self.assertTrue(used and used <= set(range(20, 32)), used)          # only colours the original uses
        rec = json.loads((knight / "generate.json").read_text())
        self.assertEqual((rec["model"], rec["lora"], rec["seed"], rec["frames_original"]),
                         ("base.safetensors", "style.safetensors", seed, 2))
        self.assertEqual(rec["described_by"], "local")

        gem = json.loads((self.pack / "UNITS/001_GEM/generate.json").read_text())
        self.assertEqual(gem["allowed_indices"], len(generate.SAFE_INDICES))   # 1 colour is too few: safe range
        with Image.open(self.pack / "UNITS/001_GEM/frame_00.png") as img:
            drawn = [p for p in img.getdata() if p[3]]
        self.assertTrue(drawn and all(p[:3] == (255, 0, 0) for p in drawn))    # sprite's own palette: all red
        page = (self.pack / "index.html").read_text()
        self.assertIn('src="../orig/UNITS/000_KNIGHT/frame_00.png"', page)

    def test_seeds_are_stable_and_runs_resume(self):
        self.run_generate("--limit", "1")
        first = FakeModel.calls[0][3]
        self.assertEqual(len(FakeModel.calls), 1)
        self.run_generate()
        self.assertEqual(len(FakeModel.calls), 2)                          # only the one not done yet
        self.run_generate("--redo", "--archive", "units")
        self.assertEqual(FakeModel.calls[2][3], first)                     # same sprite, same seed
        self.run_generate("--scale", "2", "--redo", "--seed", "5")
        self.assertNotEqual(FakeModel.calls[4][3], first)
        with Image.open(self.pack / "UNITS/000_KNIGHT/frame_00.png") as img:
            self.assertEqual(img.size, (32, 40))

    def test_prompt_fits_the_token_limit(self):
        desc = {"prompt": "a, b, c, d, e, transparent background"}
        words = lambda text: len(text.replace(",", " ").split())
        full = generate.build_prompt(desc, "s")
        self.assertTrue(full.startswith("a, b, c, d, e, " + generate.BACKGROUND))
        limit = words(full) - 2
        short = generate.build_prompt(desc, "s", words, limit)
        self.assertEqual(short, "a, b, c, " + generate.BACKGROUND + ", s")     # description trimmed, not the tail

    def test_model_size(self):
        self.assertEqual(generate.model_size(320, 200), (1344, 768))
        self.assertEqual(generate.model_size(10, 10), (1024, 1024))
        self.assertEqual(generate.model_size(300, 20), (1536, 640))


if __name__ == "__main__":
    unittest.main()
