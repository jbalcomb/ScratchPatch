"""Build synthetic LBX archives and FLIC sprites for tests (the real game files are copyrighted)."""

import struct

TRANSPARENT = None


def encode_column(column, packed):
    """Encode one column (list of index or None) as op, count, runs. packed=True uses op 0x80 with repeats."""
    if all(v is None for v in column):
        return bytes([0xFF])
    runs = []                     # (skip, [values])
    y = skip = 0
    while y < len(column):
        if column[y] is None:
            skip += 1
            y += 1
            continue
        vals = []
        while y < len(column) and column[y] is not None and len(vals) < 200:
            vals.append(column[y])
            y += 1
        runs.append((skip, vals))
        skip = 0
    body = bytearray()
    for skip, vals in runs:
        out = bytearray()
        i = 0
        while i < len(vals):
            v = vals[i]
            n = 1
            while i + n < len(vals) and vals[i + n] == v and n < 32:
                n += 1
            if packed and (n > 1 or v >= 224):
                out += bytes([224 + n - 1, v])
                i += n
            else:
                if not packed or v < 224:
                    out.append(v)
                i += 1
        body += bytes([len(out), skip]) + out
    return bytes([0x80 if packed else 0x00, len(body)]) + body


def encode_frame(pixels, width, height, packed):
    """pixels: row-major list of index or None."""
    out = bytearray([0])          # the byte the engine skips at the frame offset
    for x in range(width):
        out += encode_column([pixels[y * width + x] for y in range(height)], packed)
    return bytes(out)


def build_flic(width, height, frames, packed=True, full_store=1, remap=0, loop_frame=0, palette=None):
    """frames: list of row-major pixel lists. palette: (start, [(r, g, b) 6-bit]) or None."""
    encoded = [encode_frame(f, width, height, packed) for f in frames]
    table_size = 4 * (len(frames) + 1)
    head = 0x12 + table_size
    pal_bytes = b""
    pal_ofs = 0
    if palette:
        start, rgb = palette
        pal_ofs = head
        data_ofs = pal_ofs + 8
        pal_bytes = struct.pack("<HHHH", data_ofs, start, len(rgb), 0) + bytes(c for t in rgb for c in t)
    offsets, pos = [], head + len(pal_bytes)
    for e in encoded:
        offsets.append(pos)
        pos += len(e)
    offsets.append(pos)
    hdr = struct.pack("<hhhhhBBHh", width, height, 0, len(frames), loop_frame, 0, 0, 0, pal_ofs)
    hdr += bytes([full_store, remap]) + struct.pack(f"<{len(offsets)}I", *offsets)
    return hdr + pal_bytes + b"".join(encoded)


def build_lbx(entries, names=None):
    """entries: list of bytes. names: list of (name, description) or None."""
    num = len(entries)
    data_start = 0x200 + 32 * num if names else 0x200
    offsets, pos = [], data_start
    for e in entries:
        offsets.append(pos)
        pos += len(e)
    offsets.append(pos)
    head = bytearray(struct.pack("<HHHH", num, 0xFEAD, 0, 0) + struct.pack(f"<{num + 1}I", *offsets))
    head += bytes(0x200 - len(head))
    if names:
        for name, desc in names:
            head += name.encode()[:9].ljust(9, b"\0") + desc.encode()[:23].ljust(23, b"\0")
    return bytes(head) + b"".join(entries)
