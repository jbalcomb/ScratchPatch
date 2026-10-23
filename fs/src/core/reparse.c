/* Symlink and junction reparse data [MS-FSCC 2.1.2.4, 2.1.2.5]. */
#include "internal.h"

#define SYMLINK_FLAG_RELATIVE 0x00000001u

int pn_reparse_read_link(pn_file *f, uint8_t *buf, size_t bufsize, uint32_t *tag,
                         const uint16_t **target, size_t *target_len, int *relative)
{
    if ((uintptr_t)buf % 2)
        return PN_EINVAL;
    pn_attr *a;
    int rc = pn_attr_open(f, PN_AT_REPARSE_POINT, NULL, 0, &a);
    if (rc)
        return rc;
    size_t got;
    rc = pn_attr_read(a, 0, buf, bufsize, &got);
    pn_attr_close(a);
    if (rc)
        return rc;
    if (got < 8)
        return PN_ECORRUPT;

    *tag = rd32(buf);
    size_t data_len = rd16(buf + 4);
    if (8 + data_len > got)
        return PN_ECORRUPT;

    size_t hdr;
    if (*tag == PN_REPARSE_TAG_SYMLINK)
        hdr = 12;
    else if (*tag == PN_REPARSE_TAG_MOUNT_POINT)
        hdr = 8;
    else
        return PN_EUNSUPPORTED;
    if (data_len < hdr)
        return PN_ECORRUPT;

    const uint8_t *d = buf + 8;
    size_t sub_off = rd16(d), sub_len = rd16(d + 2);
    *relative = hdr == 12 && (rd32(d + 8) & SYMLINK_FLAG_RELATIVE);
    if (sub_off % 2 || sub_len % 2 || hdr + sub_off + sub_len > data_len)
        return PN_ECORRUPT;
    *target = (const uint16_t *)(const void *)(d + hdr + sub_off);
    *target_len = sub_len / 2;
    return PN_OK;
}
