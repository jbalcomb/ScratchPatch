"""Turn a generated image into a game sprite: key out the flat background, crop, shrink to the target size,
and map every pixel to a palette index.

Image models can't draw transparency, so generation asks for a flat magenta background and this removes it.
Output uses the same two forms as assetforge.export: an RGBA image and a palette-index map.
"""

from dataclasses import dataclass

import numpy as np
from PIL import Image

KEY = (255, 0, 255)


@dataclass
class Sprite:
    rgba: Image.Image
    index: np.ndarray        # height x width, uint8 palette index
    alpha: np.ndarray        # height x width, bool: drawn


def background_mask(rgb, tolerance=60):
    """Pixels connected to the border whose colour is close to the border's most common colour."""
    h, w, _ = rgb.shape
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]]).astype(int)
    colours, counts = np.unique(border, axis=0, return_counts=True)
    bg = colours[counts.argmax()]
    near = np.abs(rgb.astype(int) - bg).sum(axis=2) <= tolerance
    mask = np.zeros((h, w), bool)
    mask[0], mask[-1], mask[:, 0], mask[:, -1] = near[0], near[-1], near[:, 0], near[:, -1]
    while True:       # grow the border-connected region one step at a time (4-neighbour flood fill)
        grown = mask.copy()
        grown[1:] |= mask[:-1]
        grown[:-1] |= mask[1:]
        grown[:, 1:] |= mask[:, :-1]
        grown[:, :-1] |= mask[:, 1:]
        grown &= near
        if (grown == mask).all():
            return mask
        mask = grown


def fit(image, size, pad=0):
    """Crop RGBA image to what is drawn, then scale it (keeping its shape) to fit size, centred at the bottom."""
    box = image.getchannel("A").point(lambda a: 255 if a >= 128 else 0).getbbox()
    if box:
        image = image.crop(box)
    tw, th = size[0] - 2 * pad, size[1] - 2 * pad
    f = min(tw / image.width, th / image.height)
    new = (max(1, round(image.width * f)), max(1, round(image.height * f)))
    image = image.resize(new, Image.LANCZOS, reducing_gap=3.0)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.alpha_composite(image, ((size[0] - new[0]) // 2, size[1] - pad - new[1]))
    return out


def quantize(image, palette, allowed):
    """Map RGBA image to the nearest allowed palette index (no dithering). Alpha below 128 is transparent."""
    rgba = np.asarray(image.convert("RGBA")).astype(int)
    allowed = np.array(sorted(allowed), dtype=np.uint8)
    pal = np.array([palette[i] for i in allowed], int)                         # n x 3
    # Weighted RGB distance (red 2, green 4, blue 3): a cheap stand-in for perceived difference.
    diff = rgba[:, :, None, :3] - pal[None, None, :, :]
    dist = (diff * diff * np.array([2, 4, 3])).sum(axis=3)
    index = allowed[dist.argmin(axis=2)]
    alpha = rgba[:, :, 3] >= 128
    index = np.where(alpha, index, 0).astype(np.uint8)
    out = np.zeros(rgba.shape, np.uint8)
    out[:, :, :3] = np.array(palette, np.uint8)[index]
    out[:, :, 3] = alpha * 255
    return Sprite(Image.fromarray(out, "RGBA"), index, alpha)


def pixelize(image, size, palette, allowed, tolerance=60, pad=0):
    """Generated image (any mode) -> Sprite of the given size using only the allowed palette indices."""
    rgba = image.convert("RGBA")
    mask = background_mask(np.asarray(rgba.convert("RGB")), tolerance)
    arr = np.asarray(rgba).copy()
    arr[mask, 3] = 0
    return quantize(fit(Image.fromarray(arr, "RGBA"), size, pad), palette, allowed)


def index_image(sprite):
    """The palette-index map in the exporter's form: grey = index, alpha 255 drawn / 0 not."""
    img = Image.fromarray(sprite.index, "L")
    img.putalpha(Image.fromarray((sprite.alpha * 255).astype(np.uint8), "L"))
    return img
