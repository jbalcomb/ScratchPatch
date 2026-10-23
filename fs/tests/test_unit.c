/* Unit tests for the decoders that don't need a whole volume. */
#include <stdio.h>

#include "../src/core/internal.h"

static int failures;

#define CHECK(cond)                                                             \
    do {                                                                        \
        if (!(cond)) {                                                          \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
            failures++;                                                         \
        }                                                                       \
    } while (0)

/* ---- LZNT1 ----------------------------------------------------------------- */

static void test_lznt1(void)
{
    uint8_t out[8192];
    size_t n;

    /* "abc" as literals, then a back-reference: offset 3, length 6. */
    static const uint8_t abc[] = { 0x05, 0xB0, 0x08, 'a', 'b', 'c', 0x03, 0x20 };
    CHECK(pn_lznt1_decompress(abc, sizeof abc, out, sizeof out, &n) == PN_OK);
    CHECK(n == 9 && memcmp(out, "abcabcabc", 9) == 0);

    /* An uncompressed chunk (bit 15 clear): payload copied as-is. */
    static const uint8_t raw[] = { 0x02, 0x30, 'x', 'y', 'z' };
    CHECK(pn_lznt1_decompress(raw, sizeof raw, out, sizeof out, &n) == PN_OK);
    CHECK(n == 3 && memcmp(out, "xyz", 3) == 0);

    /* A short chunk followed by another stands for a full 4 KiB, zero padded. */
    static const uint8_t two[] = { 0x00, 0x30, 'A', 0x00, 0x30, 'B' };
    CHECK(pn_lznt1_decompress(two, sizeof two, out, sizeof out, &n) == PN_OK);
    CHECK(n == 4097 && out[0] == 'A' && out[1] == 0 && out[4095] == 0 && out[4096] == 'B');

    /* A zero header ends the stream. */
    static const uint8_t end[] = { 0x00, 0x30, 'Q', 0x00, 0x00, 0xFF, 0xFF };
    CHECK(pn_lznt1_decompress(end, sizeof end, out, sizeof out, &n) == PN_OK && n == 1);

    /* Corrupt: back-reference before the start of the chunk. */
    static const uint8_t before[] = { 0x02, 0xB0, 0x01, 0x00, 0x00 };
    CHECK(pn_lznt1_decompress(before, sizeof before, out, sizeof out, &n) == PN_ECORRUPT);
    /* Corrupt: bad signature bits. */
    static const uint8_t sig[] = { 0x02, 0x10, 'x', 'y', 'z' };
    CHECK(pn_lznt1_decompress(sig, sizeof sig, out, sizeof out, &n) == PN_ECORRUPT);
    /* Corrupt: chunk claims more bytes than the input has. */
    static const uint8_t trunc[] = { 0xFF, 0xBF, 0x00 };
    CHECK(pn_lznt1_decompress(trunc, sizeof trunc, out, sizeof out, &n) == PN_ECORRUPT);
    /* Corrupt: output would overflow the caller's buffer. */
    CHECK(pn_lznt1_decompress(abc, sizeof abc, out, 5, &n) == PN_ECORRUPT);
}

/* ---- Fixups ---------------------------------------------------------------- */

static void make_fixed_record(uint8_t *rec)
{
    memset(rec, 0x11, 1024);
    wr16(rec + MS_USA_OFFSET, 0x30);
    wr16(rec + MS_USA_COUNT, 3);
    wr16(rec + 0x30, 0x0007);           /* update sequence number */
    wr16(rec + 0x32, 0xAAAA);           /* original tail of stride 1 */
    wr16(rec + 0x34, 0xBBBB);           /* original tail of stride 2 */
    wr16(rec + 510, 0x0007);
    wr16(rec + 1022, 0x0007);
}

static void test_fixup(void)
{
    uint8_t rec[1024];

    make_fixed_record(rec);
    CHECK(pn_fixup(rec, sizeof rec) == PN_OK);
    CHECK(rd16(rec + 510) == 0xAAAA && rd16(rec + 1022) == 0xBBBB);

    make_fixed_record(rec);
    wr16(rec + 1022, 0x0008);           /* torn: second stride never written */
    CHECK(pn_fixup(rec, sizeof rec) == PN_ECORRUPT);

    make_fixed_record(rec);
    wr16(rec + MS_USA_COUNT, 2);        /* count doesn't match the size */
    CHECK(pn_fixup(rec, sizeof rec) == PN_ECORRUPT);

    make_fixed_record(rec);
    wr16(rec + MS_USA_OFFSET, 0x1FF);   /* odd, and runs into the stride tail */
    CHECK(pn_fixup(rec, sizeof rec) == PN_ECORRUPT);
}

/* ---- Runlists ------------------------------------------------------------------ */

static pn_volume fake_volume(void)
{
    pn_volume v;
    memset(&v, 0, sizeof v);
    v.info.cluster_size = 4096;
    v.info.total_clusters = 1000;
    return v;
}

static int decode(const uint8_t *runs, size_t len, uint64_t last_vcn,
                  pn_extent **ext, uint32_t *n)
{
    static uint8_t attr[512];
    pn_volume v = fake_volume();
    memset(attr, 0, sizeof attr);
    wr16(attr + AH_RUNS_OFFSET, AH_NONRES_HDR);
    memcpy(attr + AH_NONRES_HDR, runs, len);
    pn_rattr ra = { .p = attr, .len = (uint32_t)(AH_NONRES_HDR + len), .nonresident = 1,
                    .start_vcn = 0, .last_vcn = last_vcn };
    uint32_t cap = 0;
    *ext = NULL;
    *n = 0;
    return pn_runlist_decode(&v, &ra, ext, n, &cap);
}

static void test_runlist(void)
{
    pn_extent *e;
    uint32_t n;

    /* 16 clusters at LCN 256, an 8-cluster hole, then 4 clusters at 256 - 16 = 240. */
    static const uint8_t ok[] = { 0x21, 0x10, 0x00, 0x01, 0x01, 0x08, 0x11, 0x04, 0xF0, 0x00 };
    CHECK(decode(ok, sizeof ok, 27, &e, &n) == PN_OK);
    CHECK(n == 3);
    if (n == 3) {
        CHECK(e[0].vcn == 0 && e[0].lcn == 256 && e[0].length == 16);
        CHECK(e[1].vcn == 16 && e[1].lcn == PN_LCN_HOLE && e[1].length == 8);
        CHECK(e[2].vcn == 24 && e[2].lcn == 240 && e[2].length == 4);
    }
    pn_free(e);

    /* Empty attribute: no runs, last_vcn = -1. */
    static const uint8_t empty[] = { 0x00 };
    CHECK(decode(empty, sizeof empty, UINT64_MAX, &e, &n) == PN_OK && n == 0);
    pn_free(e);

    static const uint8_t no_end[] = { 0x11, 0x04, 0x10 };
    CHECK(decode(no_end, sizeof no_end, 3, &e, &n) == PN_ECORRUPT);
    pn_free(e);
    static const uint8_t negative[] = { 0x11, 0x04, 0xF0, 0x00 };   /* LCN -16 */
    CHECK(decode(negative, sizeof negative, 3, &e, &n) == PN_ECORRUPT);
    pn_free(e);
    static const uint8_t zero_len[] = { 0x11, 0x00, 0x10, 0x00 };
    CHECK(decode(zero_len, sizeof zero_len, 0, &e, &n) == PN_ECORRUPT);
    pn_free(e);
    static const uint8_t past_end[] = { 0x21, 0x10, 0xE0, 0x03, 0x00 }; /* 992 + 16 > 1000 */
    CHECK(decode(past_end, sizeof past_end, 15, &e, &n) == PN_ECORRUPT);
    pn_free(e);
    CHECK(decode(ok, sizeof ok, 30, &e, &n) == PN_ECORRUPT);      /* last_vcn mismatch */
    pn_free(e);
    static const uint8_t big_len[] = { 0x09, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0x00 };
    CHECK(decode(big_len, sizeof big_len, 0, &e, &n) == PN_ECORRUPT);
    pn_free(e);
}

/* ---- Collation ----------------------------------------------------------------- */

static void test_collation(void)
{
    static uint16_t up[PN_UPCASE_ENTRIES];
    for (uint32_t i = 0; i < PN_UPCASE_ENTRIES; i++)
        up[i] = (uint16_t)(i >= 'a' && i <= 'z' ? i - 32 : i);
    pn_volume v = fake_volume();
    v.upcase = up;

    static const uint16_t abc[] = { 'a', 'b', 'c' }, ABC[] = { 'A', 'B', 'C' };
    static const uint16_t abd[] = { 'a', 'b', 'd' }, ab[] = { 'a', 'b' };
    static const uint16_t under[] = { '_' };   /* '_' (0x5F) sorts after 'A'..'Z', before 'a' */
    CHECK(pn_name_collate(&v, abc, 3, ABC, 3) == 0);
    CHECK(pn_name_collate(&v, abc, 3, abd, 3) < 0);
    CHECK(pn_name_collate(&v, ab, 2, abc, 3) < 0);
    CHECK(pn_name_collate(&v, abc, 3, ab, 2) > 0);
    CHECK(pn_name_collate(&v, under, 1, ABC, 3) > 0);   /* upcased, '_' > 'A' */
}

int main(void)
{
    test_lznt1();
    test_fixup();
    test_runlist();
    test_collation();
    if (failures) {
        fprintf(stderr, "%d unit check(s) failed\n", failures);
        return 1;
    }
    puts("all unit tests passed");
    return 0;
}
