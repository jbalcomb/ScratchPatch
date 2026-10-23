/* Multi-sector fixups (§3), file records (§4) and attribute headers (§5). */
#include "internal.h"

int pn_fixup(uint8_t *buf, size_t len)
{
    if (len < MS_STRIDE || len % MS_STRIDE)
        return PN_ECORRUPT;
    uint16_t usa_off = rd16(buf + MS_USA_OFFSET);
    uint16_t usa_count = rd16(buf + MS_USA_COUNT);
    if (usa_count != len / MS_STRIDE + 1 || usa_off < 8 || usa_off % 2 ||
        (size_t)usa_off + 2u * usa_count > MS_STRIDE - 2)
        return PN_ECORRUPT;

    const uint8_t *usa = buf + usa_off;
    for (uint32_t i = 1; i < usa_count; i++) {
        uint8_t *tail = buf + i * MS_STRIDE - 2;
        if (tail[0] != usa[0] || tail[1] != usa[1])
            return PN_ECORRUPT;     /* torn write */
        tail[0] = usa[2 * i];
        tail[1] = usa[2 * i + 1];
    }
    return PN_OK;
}

int pn_record_load(pn_volume *vol, uint64_t number, uint8_t *buf)
{
    uint32_t rs = vol->info.record_size;
    if (number >= vol->info.mft_records)
        return PN_ENOENT;
    int rc = pn_extents_read(vol, vol->mft_extents, vol->mft_nextents, number * rs, buf, rs);
    if (rc)
        return rc;
    if (memcmp(buf, "FILE", 4) != 0)
        return PN_ECORRUPT;         /* includes "BAAD" and never-initialized records */
    rc = pn_fixup(buf, rs);
    if (rc)
        return rc;

    uint16_t attrs = rd16(buf + FR_ATTRS_OFFSET);
    uint32_t used = rd32(buf + FR_BYTES_USED);
    if (rd32(buf + FR_BYTES_ALLOC) != rs || used > rs || used % 8 || attrs % 8 ||
        attrs < rd16(buf + MS_USA_OFFSET) + 2u * rd16(buf + MS_USA_COUNT) || attrs + 8u > used)
        return PN_ECORRUPT;
    /* NTFS 3.1 headers (USA at 0x30) also store the record's own number. */
    if (rd16(buf + MS_USA_OFFSET) >= 0x30 && rd32(buf + FR_RECORD_NO) != (uint32_t)number)
        return PN_ECORRUPT;
    return PN_OK;
}

/*
 * Iterates attributes of one record. *cursor starts at 0.
 * Returns 1 with *out filled, 0 at the end marker, or a negative error.
 */
int pn_record_next_attr(const pn_volume *vol, const uint8_t *rec, uint64_t number,
                        uint32_t *cursor, pn_rattr *out)
{
    (void)vol;
    uint32_t used = rd32(rec + FR_BYTES_USED);
    uint32_t off = *cursor ? *cursor : rd16(rec + FR_ATTRS_OFFSET);

    if (off % 8 || off + 4u > used)
        return PN_ECORRUPT;
    uint32_t type = rd32(rec + off);
    if (type == AT_END)
        return 0;
    if (off + 16u > used)
        return PN_ECORRUPT;

    const uint8_t *p = rec + off;
    uint32_t len = rd32(p + AH_LENGTH);
    if (len < AH_RESIDENT_HDR || len % 8 || len > used - off)
        return PN_ECORRUPT;

    memset(out, 0, sizeof *out);
    out->p = p;
    out->len = len;
    out->type = type;
    out->flags = rd16(p + AH_FLAGS);
    out->id = rd16(p + AH_ID);
    out->record = number;
    out->name_len = p[AH_NAME_LEN];

    if (p[AH_NONRESIDENT] > 1)
        return PN_ECORRUPT;
    out->nonresident = p[AH_NONRESIDENT];

    if (out->name_len) {
        uint16_t no = rd16(p + AH_NAME_OFFSET);
        if (no % 2 || no < AH_RESIDENT_HDR || no + 2u * out->name_len > len)
            return PN_ECORRUPT;
        out->name = (const uint16_t *)(const void *)(p + no);
    }

    if (!out->nonresident) {
        out->value_len = rd32(p + AH_VALUE_LEN);
        uint16_t vo = rd16(p + AH_VALUE_OFFSET);
        if (vo < AH_RESIDENT_HDR || vo > len || out->value_len > len - vo)
            return PN_ECORRUPT;
        out->value = p + vo;
    } else {
        if (len < AH_NONRES_HDR)
            return PN_ECORRUPT;
        out->start_vcn = rd64(p + AH_START_VCN);
        out->last_vcn = rd64(p + AH_LAST_VCN);
        uint16_t ro = rd16(p + AH_RUNS_OFFSET);
        /* An empty attribute has last_vcn = start_vcn - 1 (i.e. -1 for the first piece). */
        if (ro < AH_NONRES_HDR || ro >= len || out->start_vcn > INT64_MAX ||
            (out->last_vcn + 1 < out->start_vcn && out->last_vcn != UINT64_MAX))
            return PN_ECORRUPT;
        if (out->last_vcn == UINT64_MAX && out->start_vcn != 0)
            return PN_ECORRUPT;
    }

    *cursor = off + len;
    return 1;
}
