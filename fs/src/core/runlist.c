/* Runlist ("mapping pairs") decoding (§5.4). */
#include "internal.h"

static int push_extent(pn_extent **ext, uint32_t *n, uint32_t *cap, pn_extent e)
{
    if (*n == *cap) {
        uint32_t ncap = *cap ? *cap * 2 : 8;
        if (ncap < *cap)
            return PN_ENOMEM;
        pn_extent *p = pn_realloc(*ext, (size_t)ncap * sizeof *p);
        if (!p)
            return PN_ENOMEM;
        *ext = p;
        *cap = ncap;
    }
    (*ext)[(*n)++] = e;
    return PN_OK;
}

/*
 * Appends the runs of one non-resident attribute piece to *ext. Each piece's
 * LCN deltas start again from zero. Fails unless the runs cover exactly
 * start_vcn..last_vcn and every allocated run lies inside the volume.
 */
int pn_runlist_decode(const pn_volume *vol, const pn_rattr *ra,
                      pn_extent **ext, uint32_t *n, uint32_t *cap)
{
    const uint8_t *p = ra->p;
    uint32_t pos = rd16(p + AH_RUNS_OFFSET);
    uint64_t total = vol->info.total_clusters;
    /* Keep every byte offset (vcn * cluster size) representable. */
    uint64_t max_vcn = UINT64_MAX / vol->info.cluster_size;
    uint64_t vcn = ra->start_vcn;
    int64_t lcn = 0;

    for (;;) {
        if (pos >= ra->len)
            return PN_ECORRUPT;     /* no terminator */
        uint8_t h = p[pos++];
        if (h == 0)
            break;
        uint32_t lsz = h & 0x0F, osz = h >> 4;
        if (lsz == 0 || lsz > 8 || osz > 8 || pos + lsz + osz > ra->len)
            return PN_ECORRUPT;

        uint64_t length = 0;
        for (uint32_t i = 0; i < lsz; i++)
            length |= (uint64_t)p[pos + i] << (8 * i);
        pos += lsz;
        if (length == 0 || length > max_vcn - vcn)
            return PN_ECORRUPT;

        uint64_t run_lcn = PN_LCN_HOLE;
        if (osz) {
            uint64_t raw = 0;
            for (uint32_t i = 0; i < osz; i++)
                raw |= (uint64_t)p[pos + i] << (8 * i);
            pos += osz;
            if (osz < 8 && (raw & (1ull << (8 * osz - 1))))
                raw |= ~0ull << (8 * osz);   /* sign-extend */
            int64_t delta = (int64_t)raw;
            if ((delta > 0 && lcn > INT64_MAX - delta) || (delta < 0 && lcn < INT64_MIN - delta))
                return PN_ECORRUPT;
            lcn += delta;
            if (lcn < 0 || (uint64_t)lcn >= total || length > total - (uint64_t)lcn)
                return PN_ECORRUPT;
            run_lcn = (uint64_t)lcn;
        }

        int rc = push_extent(ext, n, cap, (pn_extent){ vcn, run_lcn, length });
        if (rc)
            return rc;
        vcn += length;
    }

    if (vcn != ra->last_vcn + 1)    /* also handles the empty case: last_vcn = -1 */
        return PN_ECORRUPT;
    return PN_OK;
}
