/* LZNT1 decompression, per [MS-XCA] section 2.5. */
#include "internal.h"

#define CHUNK_SIZE 4096u

/* Decompresses one chunk's payload into out (at most CHUNK_SIZE bytes). */
static int chunk(const uint8_t *in, size_t inlen, uint8_t *out, size_t cap, size_t *produced)
{
    size_t ip = 0, op = 0;
    while (ip < inlen) {
        uint8_t flags = in[ip++];
        for (int bit = 0; bit < 8 && ip < inlen; bit++, flags >>= 1) {
            if (!(flags & 1)) {
                if (op >= cap)
                    return PN_ECORRUPT;
                out[op++] = in[ip++];
                continue;
            }
            if (ip + 2 > inlen)
                return PN_ECORRUPT;
            uint16_t token = rd16(in + ip);
            ip += 2;
            /* The split between offset and length bits depends on how far into the chunk we are. */
            uint32_t length_mask = 0x0FFF, offset_shift = 12;
            for (size_t i = op - 1; op > 0 && i >= 0x10; i >>= 1) {
                length_mask >>= 1;
                offset_shift--;
            }
            size_t length = (token & length_mask) + 3u;
            size_t offset = (size_t)(token >> offset_shift) + 1u;
            if (offset > op || length > cap - op)
                return PN_ECORRUPT;
            for (size_t k = 0; k < length; k++, op++)
                out[op] = out[op - offset];     /* may overlap: copy byte by byte */
        }
    }
    *produced = op;
    return PN_OK;
}

int pn_lznt1_decompress(const uint8_t *in, size_t inlen, uint8_t *out, size_t outcap,
                        size_t *outlen)
{
    size_t ip = 0, op = 0;
    while (ip + 2 <= inlen) {
        uint16_t h = rd16(in + ip);
        if (h == 0)
            break;                  /* end of compressed data */
        size_t size = (size_t)(h & 0x0FFF) + 1u;    /* payload bytes */
        if (((h >> 12) & 7) != 3 || ip + 2 + size > inlen)
            return PN_ECORRUPT;
        const uint8_t *payload = in + ip + 2;
        size_t cap = outcap - op < CHUNK_SIZE ? outcap - op : CHUNK_SIZE;
        size_t produced;
        if (h & 0x8000) {
            int rc = chunk(payload, size, out + op, cap, &produced);
            if (rc)
                return rc;
        } else {
            if (size > cap)
                return PN_ECORRUPT;
            memcpy(out + op, payload, size);
            produced = size;
        }
        /* Every chunk but the last stands for a full 4 KiB of output. */
        ip += 2 + size;
        if (produced < CHUNK_SIZE && ip + 2 <= inlen && rd16(in + ip) != 0) {
            if (cap < CHUNK_SIZE)
                return PN_ECORRUPT;
            memset(out + op + produced, 0, CHUNK_SIZE - produced);
            produced = CHUNK_SIZE;
        }
        op += produced;
    }
    *outlen = op;
    return PN_OK;
}
