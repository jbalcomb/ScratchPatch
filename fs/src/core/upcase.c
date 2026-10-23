/* $UpCase (§8) and file-name collation (§7.5). */
#include "internal.h"

int pn_upcase_load(pn_volume *vol)
{
    pn_file *f;
    pn_attr *a;
    int rc = pn_file_open(vol, 10, &f);
    if (rc)
        return rc;
    rc = pn_attr_open_raw(f, PN_AT_DATA, NULL, 0, &a);
    pn_file_close(f);
    if (rc)
        return rc == PN_ENOENT ? PN_ECORRUPT : rc;

    uint8_t *raw = NULL;
    if (a->data_size != PN_UPCASE_ENTRIES * 2) {
        rc = PN_ECORRUPT;
    } else if (!(raw = pn_malloc(PN_UPCASE_ENTRIES * 2)) ||
               !(vol->upcase = pn_malloc(PN_UPCASE_ENTRIES * sizeof *vol->upcase))) {
        rc = PN_ENOMEM;
    } else {
        size_t got;
        rc = pn_attr_read(a, 0, raw, PN_UPCASE_ENTRIES * 2, &got);
        for (uint32_t i = 0; rc == PN_OK && i < PN_UPCASE_ENTRIES; i++)
            vol->upcase[i] = rd16(raw + 2 * i);
    }
    pn_free(raw);
    pn_attr_close(a);
    return rc;
}

int pn_name_collate(const pn_volume *vol, const uint16_t *a, size_t alen,
                    const uint16_t *b, size_t blen)
{
    size_t n = alen < blen ? alen : blen;
    for (size_t i = 0; i < n; i++) {
        uint16_t ca = vol->upcase[a[i]], cb = vol->upcase[b[i]];
        if (ca != cb)
            return ca < cb ? -1 : 1;
    }
    return alen < blen ? -1 : alen > blen;
}
