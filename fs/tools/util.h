/* Helpers shared by the command-line tools (not part of the core). */
#ifndef PN_TOOL_UTIL_H
#define PN_TOOL_UTIL_H

#include <stddef.h>
#include <stdint.h>

/* UTF-16 -> UTF-8 into a malloc'd, NUL-terminated string; lone surrogates become U+FFFD. */
char *utf16_to_utf8(const uint16_t *s, size_t len);
/* UTF-8 -> UTF-16 into a malloc'd array; returns NULL on invalid UTF-8. */
uint16_t *utf8_to_utf16(const char *s, size_t *len);

typedef struct sha256_ctx {
    uint32_t h[8];
    uint64_t total;
    uint8_t  buf[64];
    size_t   used;
} sha256_ctx;

void sha256_init(sha256_ctx *c);
void sha256_update(sha256_ctx *c, const void *data, size_t len);
void sha256_hex(sha256_ctx *c, char out[65]);

#endif
