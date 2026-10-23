#!/bin/sh
# Builds NTFS images with mkntfs + ntfs-3g and checks that ntfsdump reads
# them exactly as ntfs-3g does.
#
#   tests/image_tests.sh BUILD_DIR [--images-only]
#
# Needs root (for FUSE), mkntfs, ntfs-3g and python3. Images are kept in
# BUILD_DIR/images for the fuzzer and for poking at failures.
set -eu

build=$1
images_only=${2:-}
here=$(cd "$(dirname "$0")" && pwd)
dump=$build/ntfsdump
img_dir=$build/images
mnt=$build/mnt

if [ "$(id -u)" != 0 ] || ! command -v mkntfs >/dev/null || ! command -v ntfs-3g >/dev/null \
   || [ ! -e /dev/fuse ]; then
    echo "SKIP image tests: need root, /dev/fuse, mkntfs and ntfs-3g" >&2
    exit 0
fi

mkdir -p "$img_dir" "$mnt"
cleanup() { fusermount -u "$mnt" 2>/dev/null || true; }
trap cleanup EXIT

failures=0
for cluster in 512 1024 4096 65536; do
    img=$img_dir/c$cluster.img
    rm -f "$img"
    truncate -s 256M "$img"
    mkntfs -F -Q -q -c "$cluster" -L "PawNTFS c$cluster" "$img" >/dev/null 2>&1
    opts=streams_interface=xattr
    [ "$cluster" -le 4096 ] && opts=$opts,compression
    ntfs-3g -o "$opts" "$img" "$mnt"
    python3 -I "$here/populate.py" "$mnt" "$cluster"
    python3 -I "$here/manifest.py" "$mnt" > "$img_dir/c$cluster.expected"
    fusermount -u "$mnt"
    [ -n "$images_only" ] && continue

    if "$dump" manifest "$img" > "$img_dir/c$cluster.actual" &&
       diff -u "$img_dir/c$cluster.expected" "$img_dir/c$cluster.actual" > "$img_dir/c$cluster.diff"; then
        echo "PASS manifest   c$cluster ($(wc -l < "$img_dir/c$cluster.expected") entries)"
    else
        echo "FAIL manifest   c$cluster (see $img_dir/c$cluster.diff)"
        head -20 "$img_dir/c$cluster.diff"
        failures=$((failures + 1))
    fi

    # Lookups are case-insensitive, but an exact-case name wins over its case twin.
    check() {
        if [ "$("$dump" cat "$img" "$1" 2>&1)" = "$2" ]; then
            echo "PASS lookup     c$cluster $1"
        else
            echo "FAIL lookup     c$cluster $1"
            failures=$((failures + 1))
        fi
    }
    check /names/Case.txt "Upper first"
    check /names/case.txt "lower first"
    check /NAMES/upper.txt "UPPER.TXT"
    check /names/ALONGF~1.TEX "has a short name"
    check '/ads/host.txt:SMALL' "tiny"
done

[ -n "$images_only" ] && exit 0
if [ "$failures" -ne 0 ]; then
    echo "$failures image test(s) failed"
    exit 1
fi
echo "all image tests passed"
