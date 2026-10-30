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


def fit(image, size, pad=0, supersample=1):
    """Crop RGBA image to what is drawn, then scale it (keeping its shape) to fit size, centred at the bottom.
    With supersample k the result is k times larger, laid out so each k x k block is one pixel of size."""
    box = image.getchannel("A").point(lambda a: 255 if a >= 128 else 0).getbbox()
    if box:
        image = image.crop(box)
    tw, th = size[0] - 2 * pad, size[1] - 2 * pad
    f = min(tw / image.width, th / image.height)
    new = (max(1, round(image.width * f)), max(1, round(image.height * f)))
    at = ((size[0] - new[0]) // 2, size[1] - pad - new[1])
    k = supersample
    image = image.resize((new[0] * k, new[1] * k), Image.LANCZOS, reducing_gap=3.0)
    out = Image.new("RGBA", (size[0] * k, size[1] * k), (0, 0, 0, 0))
    out.alpha_composite(image, (at[0] * k, at[1] * k))
    return out


def block_mode(sprite, size, k):
    """Shrink a k-times-supersampled Sprite to size: a pixel is drawn if most of its k x k block is, and takes the
    block's most common palette index. Keeps colours flat and edges hard, unlike a smoothing filter."""
    w, h = size
    idx = sprite.index.reshape(h, k, w, k).transpose(0, 2, 1, 3).reshape(h, w, k * k)
    drawn = sprite.alpha.reshape(h, k, w, k).transpose(0, 2, 1, 3).reshape(h, w, k * k)
    alpha = drawn.sum(axis=2) * 2 > k * k
    counts = np.zeros((h, w, 256), np.int32)
    np.add.at(counts, (np.arange(h)[:, None, None], np.arange(w)[None, :, None], idx), drawn.astype(np.int32))
    index = np.where(alpha, counts.argmax(axis=2), 0).astype(np.uint8)
    return index, alpha


def outline(index, alpha, colour):
    """Recolour drawn pixels that touch transparency (or the edge) to colour: the dark outline MoM sprites have."""
    padded = np.pad(alpha, 1, constant_values=False)
    inner = padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:]
    index = index.copy()
    index[alpha & ~inner] = colour
    return index


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
    return make_sprite(np.where(alpha, index, 0).astype(np.uint8), alpha, palette)


def make_sprite(index, alpha, palette):
    out = np.zeros(index.shape + (4,), np.uint8)
    out[:, :, :3] = np.array(palette, np.uint8)[index]
    out[:, :, 3] = alpha * 255
    return Sprite(Image.fromarray(out, "RGBA"), index, alpha)


def darkest(palette, allowed):
    return min(allowed, key=lambda i: sum(palette[i]))


def pixelize(image, size, palette, allowed, tolerance=60, pad=0, supersample=8, outline_colour=None):
    """Generated image (any mode) -> Sprite of the given size using only the allowed palette indices.

    The image is fitted at supersample x the target size, mapped to the palette there, then each block becomes one
    pixel (block_mode). outline_colour, if given, redraws the sprite's edge in that palette index."""
    rgba = image.convert("RGBA")
    mask = background_mask(np.asarray(rgba.convert("RGB")), tolerance)
    arr = np.asarray(rgba).copy()
    arr[mask, 3] = 0
    big = quantize(fit(Image.fromarray(arr, "RGBA"), size, pad, supersample), palette, allowed)
    if supersample == 1:
        index, alpha = big.index, big.alpha
    else:
        index, alpha = block_mode(big, size, supersample)
    if outline_colour is not None:
        index = outline(index, alpha, outline_colour)
    return make_sprite(index, alpha, palette)


def index_image(sprite):
    """The palette-index map in the exporter's form: grey = index, alpha 255 drawn / 0 not."""
    img = Image.fromarray(sprite.index, "L")
    img.putalpha(Image.fromarray((sprite.alpha * 255).astype(np.uint8), "L"))
    return img
