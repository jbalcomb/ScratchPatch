import random
import struct
import unittest

from assetforge import flic
from tests.builders import build_flic

N = None


def as_pixels(frame):
    return [v if c else None for v, c in zip(frame.index, frame.cover)]


class HandMadeBytes(unittest.TestCase):
    """Byte sequences written out by hand from FLIC_Draw_Frame()."""

    def sprite(self, columns, width, height, frames=1, full_store=1, remap=0):
        body = b"\0" + b"".join(columns)
        hdr = struct.pack("<hhhhhBBHh", width, height, 0, frames, 0, 0, 0, 0, 0) + bytes([full_store, remap])
        first = 0x12 + 4 * (frames + 1)
        return hdr + struct.pack("<2I", first, first + len(body)) + body

    def test_copy_skip_and_repeat(self):
        col0 = bytes([0x00, 5, 3, 1, 10, 11, 12])           # copy: skip 1 down, then 10 11 12
        col1 = bytes([0xFF])                                # empty column
        col2 = bytes([0x80, 4, 2, 0, 0xE2, 7])              # decode: repeat 7 three times
        s = flic.decode(self.sprite([col0, col1, col2], 3, 4))
        self.assertEqual(as_pixels(s.frames[0]), [N, N, 7,
                                                   10, N, 7,
                                                   11, N, 7,
                                                   12, N, N])

    def test_two_runs_in_one_column(self):
        col = bytes([0x00, 6, 1, 0, 5, 1, 1, 6])            # 5 at y=0, skip 1, 6 at y=2
        s = flic.decode(self.sprite([col], 1, 3))
        self.assertEqual(as_pixels(s.frames[0]), [5, N, 6])

    def test_remap_pixels_only_when_flagged(self):
        col = bytes([0x80, 4, 2, 0, 0xE1, 233])             # repeat 233 twice
        plain = flic.decode(self.sprite([col], 1, 2))
        self.assertEqual(list(plain.frames[0].cover), [flic.COVER_PIXEL] * 2)
        remap = flic.decode(self.sprite([col], 1, 2, remap=1))
        self.assertEqual(list(remap.frames[0].cover), [flic.COVER_REMAP] * 2)
        self.assertEqual(list(remap.frames[0].index), [233, 233])

    def test_unknown_op_draws_nothing_and_warns(self):
        col0 = bytes([0x40, 0])                             # engine reads op + count, then does nothing
        col1 = bytes([0x00, 3, 1, 0, 9])
        s = flic.decode(self.sprite([col0, col1], 2, 1))
        self.assertEqual(as_pixels(s.frames[0]), [N, 9])
        self.assertTrue(any("unknown op" in w for w in s.warnings))

    def test_truncated_data_raises(self):
        with self.assertRaises(flic.FlicError):
            flic.decode(self.sprite([bytes([0x00, 5, 3, 0, 1])], 1, 3))

    def test_not_a_sprite(self):
        self.assertIsNone(flic.read_header(b"\0" * 8))
        self.assertIsNone(flic.read_header(struct.pack("<hhhhh", 0, 10, 0, 1, 0) + bytes(30)))
        # Plausible size and frame count, but the frame table points outside the entry (as in HLPENTRY.LBX).
        data = struct.pack("<hhhhh", 15, 10, 0, 2, 0) + bytes(8) + struct.pack("<3I", 30, 0x120000E, 40) + bytes(20)
        self.assertIsNone(flic.read_header(data))


class RoundTrip(unittest.TestCase):

    def random_frame(self, rng, w, h):
        return [None if rng.random() < 0.3 else rng.choice([rng.randrange(224), rng.randrange(256)])
                for _ in range(w * h)]

    def test_random_sprites_decode_to_what_was_encoded(self):
        rng = random.Random(1)
        for trial in range(200):
            w, h = rng.randint(1, 40), rng.randint(1, 60)
            packed = rng.random() < 0.5
            frames = [self.random_frame(rng, w, h) for _ in range(rng.randint(1, 4))]
            if not packed:   # op 0x00 can't hold remap values meaningfully; plain values only
                frames = [[v if v is None or v < 224 else v - 32 for v in f] for f in frames]
            s = flic.decode(build_flic(w, h, frames, packed=packed))
            self.assertEqual(s.header.width, w)
            self.assertEqual(s.header.height, h)
            self.assertEqual([as_pixels(f) for f in s.frames], frames, f"trial {trial}")
            self.assertEqual(s.warnings, [])

    def test_delta_frames_draw_over_earlier_ones(self):
        f0 = [1, 1, 1, 1]
        f1 = [N, 2, N, N]
        full = flic.decode(build_flic(2, 2, [f0, f1], full_store=1))
        delta = flic.decode(build_flic(2, 2, [f0, f1], full_store=0))
        self.assertEqual(as_pixels(full.frames[1]), [N, 2, N, N])
        self.assertEqual(as_pixels(delta.frames[1]), [1, 2, 1, 1])

    def test_own_palette(self):
        s = flic.decode(build_flic(1, 1, [[5]], palette=(16, [(63, 0, 0), (0, 63, 0)])))
        self.assertEqual(s.palette, [(16, [(63, 0, 0), (0, 63, 0)])])


if __name__ == "__main__":
    unittest.main()
