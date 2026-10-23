#!/usr/bin/env python3
"""Fill a mounted ntfs-3g volume with content that exercises the reader.

Usage: populate.py MOUNTPOINT CLUSTER_SIZE

Everything is deterministic (seeded), so a failing image can be rebuilt.
Covers: resident/non-resident boundaries, deep paths, Unicode and long names,
case variants, DOS short names, a multi-level directory B+ tree, fragmented
files and attribute lists, many hard links, alternate data streams, sparse
and compressed files, reparse points (symlinks, junctions) and file
attribute flags.
"""
import os
import random
import struct
import sys

root, cluster = sys.argv[1], int(sys.argv[2])
rng = random.Random(1234)
FA_READONLY, FA_HIDDEN, FA_SYSTEM, FA_COMPRESSED = 0x1, 0x2, 0x4, 0x800


def p(*parts):
    return os.path.join(root, *parts)


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def set_attrib(path, value):
    os.setxattr(path, "system.ntfs_attrib_be", value.to_bytes(4, "big"), follow_symlinks=False)


def get_attrib(path):
    return int.from_bytes(os.getxattr(path, "system.ntfs_attrib_be", follow_symlinks=False), "big")


def reparse(path, tag, data):
    os.setxattr(path, "system.ntfs_reparse_data", struct.pack("<IHH", tag, len(data), 0) + data)


def link_data(substitute, printed, flags=None):
    """Symlink (flags given) or junction (flags None) reparse buffer [MS-FSCC 2.1.2.4/5]."""
    sub, prn = substitute.encode("utf-16le"), printed.encode("utf-16le")
    if flags is None:
        hdr = struct.pack("<HHHH", 0, len(sub), len(sub) + 2, len(prn))
        return hdr + sub + b"\0\0" + prn + b"\0\0"
    hdr = struct.pack("<HHHHI", 0, len(sub), len(sub), len(prn), flags)
    return hdr + sub + prn


# --- sizes around the resident / cluster boundaries -------------------------
os.mkdir(p("sizes"))
for n in [0, 1, 500, 700, 800, 1023, 4095, 4096, 4097, cluster - 1, cluster, cluster + 1,
          65535, 65536, 65537, 1 << 20, 3 * (1 << 20) + 17]:
    write(p("sizes", f"s{n}"), rng.randbytes(n))

# --- names ---------------------------------------------------------------------
os.makedirs(p("deep", *[f"level{i}" for i in range(20)]))
write(p("deep", *[f"level{i}" for i in range(20)], "bottom.txt"), b"deep\n")
os.mkdir(p("names"))
for name in ["café", "日本語のファイル", "emoji-\U0001F600", "Ωmega", "x" * 255, "space name",
             "dots.in.name.txt", "UPPER.TXT", "MiXeD.TxT"]:
    write(p("names", name), name.encode())
# Two names that differ only by case (POSIX namespace) exercise the lookup tie-break.
write(p("names", "Case.txt"), b"Upper first\n")
write(p("names", "case.txt"), b"lower first\n")
# A long name with an explicit DOS 8.3 alias: two $FILE_NAME attributes.
write(p("names", "A long file name.text"), b"has a short name\n")
os.setxattr(p("names", "A long file name.text"), "system.ntfs_dos_name", b"ALONGF~1.TEX")

# --- a big directory: several index levels -------------------------------------------
os.mkdir(p("bigdir"))
for i in range(3000):
    write(p("bigdir", f"file-{i:05d}-{rng.getrandbits(32):08x}"), str(i).encode())

# --- fragmentation and attribute lists ----------------------------------------
os.mkdir(p("frag"))
# Thousands of data islands in a sparse file: the runlist outgrows one MFT
# record, so $DATA is split into pieces across extension records.
islands = 6000 if cluster <= 4096 else 1500
with open(p("frag", "islands"), "wb") as f:
    f.truncate(islands * 4 * cluster)
    for i in range(islands):
        f.seek(i * 4 * cluster + 7)
        f.write(rng.randbytes(9))
write(p("frag", "links-target"), b"linked many times\n")
for i in range(60):
    os.link(p("frag", "links-target"), p("frag", f"link-{i:02d}-with-a-longer-name-to-fill-records"))

# --- alternate data streams ------------------------------------------------------------
os.mkdir(p("ads"))
write(p("ads", "host.txt"), b"main stream\n")
os.setxattr(p("ads", "host.txt"), "user.small", b"tiny")
os.setxattr(p("ads", "host.txt"), "user.big", rng.randbytes(50000))
os.setxattr(p("ads", "host.txt"), "user.Zone.Identifier", b"[ZoneTransfer]\r\nZoneId=3\r\n")
os.setxattr(p("ads"), "user.on-a-directory", b"directories have streams too")

# --- sparse files ------------------------------------------------------------------------
os.mkdir(p("sparse"))
with open(p("sparse", "islands"), "wb") as f:
    f.truncate(64 << 20)
    for off in [0, 5_000_000, 20 << 20, (64 << 20) - 3]:
        f.seek(off)
        f.write(rng.randbytes(3))
with open(p("sparse", "hole-tail"), "wb") as f:
    f.write(rng.randbytes(10000))
    f.truncate(8 << 20)

# --- compression (only for clusters <= 4 KiB, as on Windows) ----------------------------
if cluster <= 4096:
    os.mkdir(p("comp"))
    set_attrib(p("comp"), get_attrib(p("comp")) | FA_COMPRESSED)
    write(p("comp", "text"), b"The quick brown fox jumps over the lazy dog. " * 20000)
    write(p("comp", "random"), rng.randbytes(300000))          # units stored raw
    write(p("comp", "mixed"), rng.randbytes(70000) + b"\0" * 200000 + b"tail" * 5000)
    write(p("comp", "odd-size"), bytes(range(256)) * 1001 + b"end")
    write(p("comp", "small"), b"x" * 100)                     # resident: never compressed

# --- reparse points ---------------------------------------------------------------------
os.mkdir(p("links"))
write(p("links", "target.txt"), b"target\n")
write(p("links", "rel"), b"")
reparse(p("links", "rel"), 0xA000000C, link_data("target.txt", "target.txt", flags=1))
write(p("links", "abs"), b"")
reparse(p("links", "abs"), 0xA000000C, link_data("\\??\\C:\\Windows\\notepad.exe", "C:\\Windows\\notepad.exe", flags=0))
os.mkdir(p("links", "junction"))
reparse(p("links", "junction"), 0xA0000003, link_data("\\??\\C:\\Users", "C:\\Users"))
# A tag the file system stores but doesn't interpret: Windows' system-compression (WOF) tag.
write(p("links", "wof"), b"")
reparse(p("links", "wof"), 0x80000017, b"\x01\x00\x00\x00\x02\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00")

# --- attribute flags ----------------------------------------------------------------------
os.mkdir(p("flags"))
for name, bits in [("readonly", FA_READONLY), ("hidden", FA_HIDDEN), ("system", FA_SYSTEM)]:
    write(p("flags", name), name.encode())
    set_attrib(p("flags", name), get_attrib(p("flags", name)) | bits)

# --- real fragmentation: last, because it fills the volume -----------------------
# Fill all free space, free every other filler file, then write a file that can
# only go into the holes.
os.mkdir(p("full"))
piece = max(cluster, 65536)
count = 0
try:
    while True:
        write(p("full", f"{count}"), rng.randbytes(piece))
        count += 1
except OSError:
    os.unlink(p("full", f"{count}"))     # the partial one
os.sync()
for i in range(0, count, 2):
    os.unlink(p("full", f"{i}"))
os.sync()
write(p("frag", "fragmented"), rng.randbytes((count // 2 - 8) * piece))
