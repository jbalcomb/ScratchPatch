import unittest

import numpy as np
from PIL import Image, ImageDraw

from assetforge.pixelize import background_mask, block_mode, darkest, fit, index_image, outline, pixelize, quantize

PALETTE = [(i, i, i) for i in range(256)]
PALETTE[10] = (200, 30, 30)       # red
PALETTE[11] = (30, 200, 30)       # green
PALETTE[12] = (240, 240, 240)     # white


def generated(size=(1024, 1024)):
    """A stand-in for an image model's output: a red disc with a green eye on flat magenta, some noise."""
    img = Image.new("RGB", size, (255, 0, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((300, 200, 700, 900), fill=(205, 25, 35))
    d.ellipse((450, 400, 520, 470), fill=(35, 195, 25))
    arr = np.asarray(img).astype(int) + np.random.default_rng(0).integers(-8, 9, (size[1], size[0], 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


class PixelizeTest(unittest.TestCase):

    def test_background_is_removed_but_enclosed_areas_kept(self):
        img = Image.new("RGB", (9, 9), (255, 0, 255))
        d = ImageDraw.Draw(img)
        d.rectangle((2, 2, 6, 6), outline=(0, 0, 0))     # a ring with magenta inside it
        mask = background_mask(np.asarray(img))
        self.assertTrue(mask[0, 0])
        self.assertFalse(mask[2, 2])
        self.assertFalse(mask[4, 4])                     # inside the ring: not connected to the border

    def test_fit_crops_and_keeps_shape(self):
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle((10, 40, 29, 49), fill=(255, 0, 0, 255))   # 20 x 10
        out = fit(img, (16, 16))
        box = out.getchannel("A").point(lambda a: 255 if a >= 128 else 0).getbbox()
        self.assertEqual((box[2] - box[0], box[3] - box[1]), (16, 8))
        self.assertEqual(box[3], 16)                     # sits on the bottom edge

    def test_quantize_uses_only_allowed_indices(self):
        img = Image.new("RGBA", (2, 1))
        img.putpixel((0, 0), (190, 40, 40, 255))
        img.putpixel((1, 0), (0, 0, 0, 0))
        s = quantize(img, PALETTE, {10, 11, 12})
        self.assertEqual(s.index.tolist(), [[10, 0]])
        self.assertEqual(s.alpha.tolist(), [[True, False]])
        self.assertEqual(s.rgba.getpixel((0, 0)), (200, 30, 30, 255))
        self.assertEqual(s.rgba.getpixel((1, 0))[3], 0)

    def test_block_mode_takes_majority(self):
        big = quantize(Image.new("RGBA", (4, 2), (200, 30, 30, 255)), PALETTE, {10, 11})
        big.index[0, 0] = 11                                   # one green pixel in the left 2x2 block
        big.alpha[:, 2:] = [[True, False], [False, False]]     # right block: 1 of 4 drawn
        index, alpha = block_mode(big, (2, 1), 2)
        self.assertEqual(alpha.tolist(), [[True, False]])
        self.assertEqual(index.tolist(), [[10, 0]])

    def test_outline(self):
        alpha = np.ones((3, 3), bool)
        out = outline(np.full((3, 3), 10, np.uint8), alpha, 12)
        self.assertEqual(out.tolist(), [[12, 12, 12], [12, 10, 12], [12, 12, 12]])
        self.assertEqual(darkest(PALETTE, {10, 11, 12}), 10)

    def test_end_to_end(self):
        s = pixelize(generated(), (24, 32), PALETTE, {10, 11, 12})
        self.assertEqual(s.rgba.size, (24, 32))
        used = set(np.unique(s.index[s.alpha]).tolist())
        self.assertEqual(used, {10, 11})                 # disc and eye; no magenta fringe
        self.assertFalse(s.alpha[0, 0])                  # corner is background
        self.assertTrue(s.alpha[16, 12])                 # middle is the disc
        edged = pixelize(generated(), (24, 32), PALETTE, {10, 11, 12}, outline_colour=12)
        self.assertEqual(edged.alpha.tolist(), s.alpha.tolist())
        self.assertIn(12, set(edged.index[edged.alpha].tolist()))
        idx = index_image(s)
        self.assertEqual(idx.mode, "LA")
        self.assertEqual(idx.getpixel((12, 16)), (int(s.index[16, 12]), 255))


if __name__ == "__main__":
    unittest.main()
