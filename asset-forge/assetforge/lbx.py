"""Read SimTex LBX archives (Master of Magic v1.31).

Ported from ReMoM tools/lbx_extract.c and tools/asset_catalog/lbx.py.

  0x00   u16  num_entries
  0x02   u16  magic 0xFEAD
  0x04   u16  file_type
  0x08        offset table, num_entries + 1 u32 LE; entry N is data[off[N]:off[N+1]]
  0x200       name table, 32 bytes per entry: char name[9]; char description[23]

The name table is not always there in full (SOUNDFX.LBX declares more entries than fit below its first data
offset), so names are read only for records that fit below offset[0].
"""

import struct
from dataclasses import dataclass

LBX_MAGIC = 0xFEAD
LBX_HDR_SIZE = 0x200
NAME_RECORD = 32
NAME_LEN = 9
DESCRIPTION_LEN = 23


class LbxError(ValueError):
    pass


@dataclass
class LbxEntry:
    index: int
    name: str
    description: str
    data: bytes


def _cstr(raw):
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def read_lbx(data):
    """Return the list of LbxEntry in an LBX archive's bytes. Raises LbxError if it is not an LBX."""
    if len(data) < 8 or struct.unpack_from("<H", data, 2)[0] != LBX_MAGIC:
        raise LbxError("not an LBX archive (magic 0xFEAD missing)")
    num = struct.unpack_from("<H", data, 0)[0]
    table_end = 8 + (num + 1) * 4
    if num == 0 or table_end > len(data):
        raise LbxError(f"bad entry count {num}")
    offsets = struct.unpack_from(f"<{num + 1}I", data, 8)
    name_records = 0
    if offsets[0] > LBX_HDR_SIZE:
        name_records = min(num, (offsets[0] - LBX_HDR_SIZE) // NAME_RECORD)
    entries = []
    for i in range(num):
        name = description = ""
        if i < name_records:
            rec = LBX_HDR_SIZE + i * NAME_RECORD
            name = _cstr(data[rec:rec + NAME_LEN])
            description = _cstr(data[rec + NAME_LEN:rec + NAME_RECORD])
        start, end = offsets[i], offsets[i + 1]
        body = data[start:end] if start <= end <= len(data) else b""
        entries.append(LbxEntry(i, name, description, body))
    return entries
