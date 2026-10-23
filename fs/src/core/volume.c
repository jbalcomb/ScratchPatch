/* Mounting, the boot sector (§2), and raw reads through extents. */
#include "internal.h"

const char *pn_strerror(int err)
{
    switch (err) {
    case PN_OK:           return "success";
    case PN_EIO:          return "I/O error";
    case PN_ENOMEM:       return "out of memory";
    case PN_EINVAL:       return "invalid argument";
    case PN_ECORRUPT:     return "on-disk structure is corrupt";
    case PN_ENOENT:       return "not found";
    case PN_ENOTDIR:      return "not a directory";
    case PN_EUNSUPPORTED: return "not supported";
    case PN_ESTALE:       return "stale file reference";
    default:              return "unknown error";
    }
}

/* Reads any byte range, bouncing through a sector buffer at unaligned edges. */
int pn_dev_read(const pn_volume *vol, uint64_t offset, void *buf, size_t len)
{
    const pn_bdev *dev = &vol->dev;
    uint32_t ss = dev->sector_size;
    uint8_t *out = buf;

    if (offset > dev->size || len > dev->size - offset)
        return PN_ECORRUPT;
    if (offset % ss == 0 && len % ss == 0)
        return dev->read(dev->ctx, offset, len, buf) ? PN_EIO : PN_OK;

    uint8_t *bounce = pn_malloc(ss);
    if (!bounce)
        return PN_ENOMEM;
    int rc = PN_OK;
    while (len && rc == PN_OK) {
        uint64_t base = offset - offset % ss;
        size_t skip = (size_t)(offset - base);
        if (skip == 0 && len >= ss) {
            size_t n = len - len % ss;
            rc = dev->read(dev->ctx, offset, n, out) ? PN_EIO : PN_OK;
            out += n, offset += n, len -= n;
            continue;
        }
        size_t n = ss - skip < len ? ss - skip : len;
        if (base + ss > dev->size) {
            rc = PN_ECORRUPT;
            break;
        }
        rc = dev->read(dev->ctx, base, ss, bounce) ? PN_EIO : PN_OK;
        memcpy(out, bounce + skip, n);
        out += n, offset += n, len -= n;
    }
    pn_free(bounce);
    return rc;
}

/* Finds the extent containing vcn; extents are sorted and contiguous. */
static const pn_extent *find_extent(const pn_extent *ext, uint32_t n, uint64_t vcn)
{
    uint32_t lo = 0, hi = n;
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        if (vcn < ext[mid].vcn)
            hi = mid;
        else if (vcn >= ext[mid].vcn + ext[mid].length)
            lo = mid + 1;
        else
            return &ext[mid];
    }
    return NULL;
}

int pn_extents_read(const pn_volume *vol, const pn_extent *ext, uint32_t n,
                    uint64_t offset, void *buf, size_t len)
{
    uint64_t cs = vol->info.cluster_size;
    uint8_t *out = buf;

    while (len) {
        const pn_extent *e = find_extent(ext, n, offset / cs);
        if (!e)
            return PN_ECORRUPT;     /* the data claims bytes its runlist doesn't map */
        uint64_t within = offset - e->vcn * cs;
        uint64_t avail = e->length * cs - within;
        size_t chunk = avail < len ? (size_t)avail : len;
        if (e->lcn == PN_LCN_HOLE) {
            memset(out, 0, chunk);
        } else {
            int rc = pn_dev_read(vol, e->lcn * cs + within, out, chunk);
            if (rc)
                return rc;
        }
        out += chunk, offset += chunk, len -= chunk;
    }
    return PN_OK;
}

static int is_pow2(uint64_t v) { return v && !(v & (v - 1)); }

/* Clusters-per-record / per-index-block field: positive = clusters, negative = 2^-n bytes. */
static uint64_t decode_size_field(uint8_t raw, uint32_t cluster_size)
{
    int8_t v = (int8_t)raw;
    if (v > 0)
        return (uint64_t)v * cluster_size;
    if (v < 0 && v >= -31)
        return 1ull << -v;
    return 0;
}

static int parse_boot(pn_volume *vol, const uint8_t *bs)
{
    pn_volume_info *vi = &vol->info;

    if (memcmp(bs + BS_OEM_ID, "NTFS    ", 8) != 0 || rd16(bs + BS_SIGNATURE) != 0xAA55)
        return PN_EINVAL;           /* not NTFS at all */

    uint32_t bps = rd16(bs + BS_BYTES_PER_SECTOR);
    if (!is_pow2(bps) || bps < 256 || bps > 4096)
        return PN_ECORRUPT;
    uint8_t spc_raw = bs[BS_SECTORS_PER_CLUSTER];
    uint64_t spc;
    if (spc_raw <= 0x80)
        spc = spc_raw;
    else if (256 - spc_raw <= 12)   /* large clusters: 2^(256 - value), see spec §2 */
        spc = 1ull << (256 - spc_raw);
    else
        return PN_ECORRUPT;
    if (!is_pow2(spc) || bps * spc > (2u << 20) || rd16(bs + BS_RESERVED_SECTORS) != 0)
        return PN_ECORRUPT;

    vi->bytes_per_sector = bps;
    vi->cluster_size = (uint32_t)(bps * spc);
    uint64_t rs = decode_size_field(bs[BS_CLUSTERS_PER_RECORD], vi->cluster_size);
    uint64_t ib = decode_size_field(bs[BS_CLUSTERS_PER_INDEX], vi->cluster_size);
    if (!is_pow2(rs) || rs < 512 || rs > 65536 || !is_pow2(ib) || ib < 512 || ib > (2u << 20))
        return PN_ECORRUPT;
    vi->record_size = (uint32_t)rs;
    vi->index_block_size = (uint32_t)ib;

    uint64_t sectors = rd64(bs + BS_TOTAL_SECTORS);
    if (sectors == 0 || sectors > vol->dev.size / bps)
        return PN_ECORRUPT;
    vol->volume_bytes = sectors * bps;
    vi->total_clusters = sectors / spc;
    vi->mft_lcn = rd64(bs + BS_MFT_LCN);
    vi->mftmirr_lcn = rd64(bs + BS_MFTMIRR_LCN);
    vi->serial = rd64(bs + BS_SERIAL);
    if (vi->mft_lcn >= vi->total_clusters || vi->mftmirr_lcn >= vi->total_clusters)
        return PN_ECORRUPT;
    return PN_OK;
}

/* Maps $MFT using only record 0's own first $DATA piece, enough to load the rest. */
static int bootstrap_mft(pn_volume *vol)
{
    uint32_t rs = vol->info.record_size;
    uint8_t *rec = pn_malloc(rs);
    if (!rec)
        return PN_ENOMEM;
    int rc = pn_dev_read(vol, vol->info.mft_lcn * vol->info.cluster_size, rec, rs);
    if (rc == PN_OK)
        rc = memcmp(rec, "FILE", 4) == 0 ? pn_fixup(rec, rs) : PN_ECORRUPT;

    pn_rattr ra;
    uint32_t cursor = 0;
    int found = 0;
    while (rc == PN_OK && (rc = pn_record_next_attr(vol, rec, 0, &cursor, &ra)) == 1) {
        rc = PN_OK;
        if (ra.type == PN_AT_DATA && ra.name_len == 0 && ra.nonresident && ra.start_vcn == 0) {
            uint32_t cap = 0;
            rc = pn_runlist_decode(vol, &ra, &vol->mft_extents, &vol->mft_nextents, &cap);
            vol->mft_data_size = rd64(ra.p + AH_DATA_SIZE);
            found = 1;
            break;
        }
    }
    if (rc == PN_OK && !found)
        rc = PN_ECORRUPT;
    pn_free(rec);
    return rc;
}

static int load_mft(pn_volume *vol)
{
    int rc = bootstrap_mft(vol);
    if (rc)
        return rc;
    /* Enough of the MFT is mapped to read record 0 (and any extension records,
     * which live early in the MFT); now load its full mapping. */
    vol->info.mft_records = vol->mft_data_size / vol->info.record_size;
    if (vol->info.mft_records < 16)
        return PN_ECORRUPT;

    pn_file *mft;
    pn_attr *data;
    rc = pn_file_open(vol, 0, &mft);
    if (rc)
        return rc;
    rc = pn_attr_open_raw(mft, PN_AT_DATA, NULL, 0, &data);
    pn_file_close(mft);
    if (rc)
        return rc;
    if (!data->nonresident) {
        pn_attr_close(data);
        return PN_ECORRUPT;
    }
    pn_free(vol->mft_extents);
    vol->mft_extents = data->extents;
    vol->mft_nextents = data->nextents;
    vol->mft_data_size = data->data_size;
    vol->info.mft_records = data->data_size / vol->info.record_size;
    data->extents = NULL;
    pn_attr_close(data);
    return PN_OK;
}

static int load_volume_info(pn_volume *vol)
{
    pn_file *f;
    pn_rattr ra;
    int rc = pn_file_open(vol, 3, &f);
    if (rc)
        return rc;
    rc = pn_file_find_rattr(f, PN_AT_VOLUME_INFO, NULL, 0, &ra);
    if (rc == PN_OK) {
        if (ra.nonresident || ra.value_len < 12) {
            rc = PN_ECORRUPT;
        } else {
            vol->info.major = ra.value[VI_MAJOR];
            vol->info.minor = ra.value[VI_MINOR];
            vol->info.flags = rd16(ra.value + VI_FLAGS);
        }
    }
    if (rc == PN_OK && pn_file_find_rattr(f, PN_AT_VOLUME_NAME, NULL, 0, &ra) == PN_OK &&
        !ra.nonresident) {
        uint32_t n = ra.value_len / 2;
        if (n > 128)
            n = 128;
        for (uint32_t i = 0; i < n; i++)
            vol->info.label[i] = rd16(ra.value + 2 * i);
        vol->info.label_len = n;
    }
    pn_file_close(f);
    return rc;
}

int pn_mount(const pn_bdev *dev, pn_volume **out)
{
    if (!dev || !dev->read || !out || !is_pow2(dev->sector_size) ||
        dev->sector_size < 512 || dev->sector_size > 4096 || dev->size < 4096)
        return PN_EINVAL;

    pn_volume *vol = pn_calloc(1, sizeof *vol);
    if (!vol)
        return PN_ENOMEM;
    vol->dev = *dev;

    uint8_t *bs = pn_malloc(dev->sector_size);
    int rc = bs ? pn_dev_read(vol, 0, bs, dev->sector_size) : PN_ENOMEM;
    if (rc == PN_OK)
        rc = parse_boot(vol, bs);
    pn_free(bs);
    if (rc == PN_OK)
        rc = load_mft(vol);
    if (rc == PN_OK)
        rc = pn_upcase_load(vol);
    if (rc == PN_OK)
        rc = load_volume_info(vol);
    if (rc == PN_OK && vol->info.major < 3)
        rc = PN_EUNSUPPORTED;       /* NTFS 1.x/2.x: pre-Windows 2000 */
    if (rc) {
        pn_unmount(vol);
        return rc;
    }
    *out = vol;
    return PN_OK;
}

void pn_unmount(pn_volume *vol)
{
    if (!vol)
        return;
    pn_free(vol->mft_extents);
    pn_free(vol->upcase);
    pn_free(vol);
}

void pn_volume_get_info(const pn_volume *vol, pn_volume_info *info)
{
    *info = vol->info;
}
