/*
 * Mutation fuzzer: flips bytes in the metadata a clean walk of the image
 * reads, then mounts and walks the damaged image. Any crash, leak or
 * undefined behaviour is a bug; error returns are expected. Build with
 * `make SAN=1` so the sanitizers can see the problems.
 *
 *   fuzz_image [-n ITERATIONS] [-s SEED] IMAGE...
 */
#define _POSIX_C_SOURCE 200809L
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pawntfs/pawntfs.h"

#define MAX_ENTRIES_PER_DIR 150
#define READ_WINDOW         16384
#define MAX_DEPTH           64

/* ---- An in-memory device that can record what gets read ----------------------- */

typedef struct range { uint64_t off, len; } range;

typedef struct mem_dev {
    uint8_t *data;
    uint64_t size;
    int      recording;
    range   *ranges;
    size_t   nranges, cap;
} mem_dev;

static int mem_read(void *ctx, uint64_t off, size_t len, void *buf)
{
    mem_dev *m = ctx;
    if (off > m->size || len > m->size - off)
        return -1;
    memcpy(buf, m->data + off, len);
    /* Record metadata-sized reads only; bulk file data isn't worth mutating. */
    if (m->recording && len <= 65536) {
        if (m->nranges == m->cap) {
            m->cap = m->cap ? m->cap * 2 : 1024;
            m->ranges = realloc(m->ranges, m->cap * sizeof *m->ranges);
            if (!m->ranges)
                abort();
        }
        m->ranges[m->nranges++] = (range){ off, len };
    }
    return 0;
}

/* ---- A bounded walk that touches every kind of structure ---------------------- */

static void exercise_attr(pn_file *f, uint32_t type, const uint16_t *name, size_t nlen)
{
    pn_attr *a;
    if (pn_attr_open(f, type, name, nlen, &a) != PN_OK)
        return;
    static uint8_t buf[READ_WINDOW];
    pn_attr_info ai;
    size_t got;
    pn_attr_get_info(a, &ai);
    uint64_t offs[3] = { 0, ai.data_size / 2, ai.data_size > READ_WINDOW ? ai.data_size - READ_WINDOW : 0 };
    for (int i = 0; i < 3; i++)
        if (pn_attr_read(a, offs[i], buf, sizeof buf, &got) != PN_OK)
            break;
    pn_attr_close(a);
}

static int visit_attr(void *ctx, const pn_attr_desc *d)
{
    pn_file *f = ctx;
    if ((d->type == PN_AT_DATA || d->type == PN_AT_REPARSE_POINT) && d->start_vcn == 0)
        exercise_attr(f, d->type, d->name, d->name_len);
    return 0;
}

static int visit_name(void *ctx, const pn_name *n)
{
    (void)ctx;
    volatile uint16_t sink = n->name_len ? n->name[n->name_len - 1] : 0;
    (void)sink;
    return 0;
}

typedef struct walk { pn_volume *vol; int depth; int count; } walk;

static void walk_file(pn_volume *vol, uint64_t ref, int depth);

static int visit_entry(void *ctx, const pn_dirent *e)
{
    walk *w = ctx;
    if (++w->count > MAX_ENTRIES_PER_DIR)
        return 1;   /* stop iterating this directory */
    if (PN_REF_RECORD(e->ref) >= 16 && w->depth < MAX_DEPTH)
        walk_file(w->vol, e->ref, w->depth + 1);
    return 0;
}

static void walk_file(pn_volume *vol, uint64_t ref, int depth)
{
    pn_file *f;
    if (pn_file_open_ref(vol, ref, &f) != PN_OK)
        return;
    pn_file_info fi;
    if (pn_file_get_info(f, &fi) == PN_OK) {
        pn_file_names(f, visit_name, NULL);
        pn_file_attrs(f, visit_attr, f);
        if (fi.reparse_tag) {
            static uint16_t buf[8 + 8192];
            uint32_t tag;
            const uint16_t *t;
            size_t tl;
            int rel;
            pn_reparse_read_link(f, (uint8_t *)buf, sizeof buf, &tag, &t, &tl, &rel);
        } else if (fi.is_directory) {
            walk w = { vol, depth, 0 };
            pn_dir_iterate(f, visit_entry, &w);
            static const uint16_t probe[] = { 'n', 'a', 'm', 'e', 's' };
            uint64_t r;
            pn_dir_lookup(f, probe, 5, &r);
        }
    }
    pn_file_close(f);
}

static int run_once(mem_dev *m)
{
    pn_bdev dev = { mem_read, m, m->size, 512 };
    pn_volume *vol;
    int rc = pn_mount(&dev, &vol);
    if (rc)
        return rc;
    walk_file(vol, PN_RECORD_ROOT, 0);
    static const uint16_t path[] = { 'f', 'r', 'a', 'g', '/', 'i', 's', 'l', 'a', 'n', 'd', 's' };
    pn_file *f;
    if (pn_file_open_path(vol, path, 12, &f) == PN_OK)
        pn_file_close(f);
    pn_unmount(vol);
    return PN_OK;
}

/* ---- Driver ------------------------------------------------------------------------ */

static uint64_t rng_state;
static uint64_t rnd(void)
{
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 7;
    rng_state ^= rng_state << 17;
    return rng_state;
}

static int fuzz_image(const char *path, long iterations)
{
    FILE *fp = fopen(path, "rb");
    if (!fp) {
        perror(path);
        return 1;
    }
    mem_dev m = { 0 };
    fseek(fp, 0, SEEK_END);
    m.size = (uint64_t)ftell(fp);
    rewind(fp);
    m.data = malloc(m.size);
    if (!m.data || fread(m.data, 1, m.size, fp) != m.size) {
        fprintf(stderr, "%s: read failed\n", path);
        return 1;
    }
    fclose(fp);

    m.recording = 1;
    int rc = run_once(&m);
    m.recording = 0;
    if (rc) {
        fprintf(stderr, "%s: clean image failed to mount: %s\n", path, pn_strerror(rc));
        return 1;
    }

    long mounted = 0;
    for (long it = 0; it < iterations; it++) {
        enum { MAX_FLIPS = 8 };
        uint64_t where[MAX_FLIPS];
        uint8_t old[MAX_FLIPS];
        int flips = 1 + (int)(rnd() % MAX_FLIPS);
        for (int i = 0; i < flips; i++) {
            const range *r = &m.ranges[rnd() % m.nranges];
            where[i] = r->off + rnd() % r->len;
            old[i] = m.data[where[i]];
            switch (rnd() % 4) {
            case 0: m.data[where[i]] ^= (uint8_t)(1u << (rnd() % 8)); break;
            case 1: m.data[where[i]] = (uint8_t)rnd(); break;
            case 2: m.data[where[i]] = 0x00; break;
            default: m.data[where[i]] = 0xFF; break;
            }
        }
        if (run_once(&m) == PN_OK)
            mounted++;
        for (int i = flips - 1; i >= 0; i--)
            m.data[where[i]] = old[i];
    }
    printf("%s: %ld iterations, %ld still mounted, %zu metadata reads targeted\n",
           path, iterations, mounted, m.nranges);
    free(m.ranges);
    free(m.data);
    return 0;
}

int main(int argc, char **argv)
{
    long iterations = 300;
    rng_state = 0x9E3779B97F4A7C15ull;
    int i = 1;
    for (; i < argc && argv[i][0] == '-'; i += 2) {
        if (i + 1 >= argc)
            break;
        if (!strcmp(argv[i], "-n"))
            iterations = strtol(argv[i + 1], NULL, 10);
        else if (!strcmp(argv[i], "-s"))
            rng_state = strtoull(argv[i + 1], NULL, 0) | 1;
    }
    if (i >= argc) {
        fputs("usage: fuzz_image [-n ITERATIONS] [-s SEED] IMAGE...\n", stderr);
        return 2;
    }
    int status = 0;
    for (; i < argc; i++)
        status |= fuzz_image(argv[i], iterations);
    return status;
}
