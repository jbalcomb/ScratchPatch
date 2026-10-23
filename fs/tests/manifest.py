#!/usr/bin/env python3
"""Print the manifest of a mounted ntfs-3g volume, in ntfsdump's format.

Usage: manifest.py MOUNTPOINT

This is the independent side of the image tests: it sees the volume only
through ntfs-3g, never through PawNTFS.
"""
import hashlib
import os
import struct
import sys

MASK = 0x00000A27   # RO, hidden, system, archive, sparse, compressed
FA_REPARSE = 0x400
EPOCH_DIFF = 116444736000000000     # 1601 -> 1970 in 100 ns ticks

root = sys.argv[1]
out = []


def sha(data=None, path=None):
    h = hashlib.sha256()
    if path is not None:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    else:
        h.update(data)
    return h.hexdigest()


def attrib(path):
    return int.from_bytes(os.getxattr(path, "system.ntfs_attrib_be", follow_symlinks=False), "big")


def link_target(raw):
    tag, length = struct.unpack_from("<IH", raw)
    data = raw[8:8 + length]
    if tag == 0xA000000C:
        off, n = struct.unpack_from("<HH", data)
        return data[12 + off:12 + off + n].decode("utf-16le")
    if tag == 0xA0000003:
        off, n = struct.unpack_from("<HH", data)
        return data[8 + off:8 + off + n].decode("utf-16le")
    return "-"


def streams(path, rel):
    for name in os.listxattr(path, follow_symlinks=False):
        if name.startswith("user."):
            data = os.getxattr(path, name, follow_symlinks=False)
            out.append(f"S\t{rel}:{name[5:]}\t{len(data)}\t{sha(data)}")


def walk(path, rel):
    for name in os.listdir(path):
        full, r = os.path.join(path, name), f"{rel}/{name}"
        a = attrib(full)
        if a & FA_REPARSE:
            raw = os.getxattr(full, "system.ntfs_reparse_data", follow_symlinks=False)
            tag = struct.unpack_from("<I", raw)[0]
            out.append(f"R\t{r}\t{tag:08x}\t{sha(raw)}\t{link_target(raw)}")
            continue
        st = os.lstat(full)
        if os.path.isdir(full):
            out.append(f"D\t{r}")
            walk(full, r)
        else:
            mtime = st.st_mtime_ns // 100 + EPOCH_DIFF
            out.append(f"F\t{r}\t{st.st_size}\t{sha(path=full)}\t{a & MASK:08x}\t{st.st_nlink}\t{mtime}")
        streams(full, r)


walk(root, "")
for line in sorted(out, key=lambda s: s.encode()):
    print(line)
