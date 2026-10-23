/*
 * ntfsdump: inspect an NTFS image with the PawNTFS core.
 *
 *   ntfsdump info     IMAGE
 *   ntfsdump ls       IMAGE [PATH]
 *   ntfsdump stat     IMAGE PATH
 *   ntfsdump cat      IMAGE PATH[:STREAM]
 *   ntfsdump manifest IMAGE
 *
 * `manifest` prints one sorted line per file, directory, stream and reparse
 * point; tests/manifest.py prints the same format from a mounted volume.
 */
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pawntfs/pawntfs.h"
#include "../src/posix/bdev_file.h"
#include "util.h"

#define FA_MANIFEST_MASK 0x00000A27u    /* RO, hidden, system, archive, sparse, compressed */
#define FA_REPARSE       0x00000400u
#define MAX_DEPTH        256

static int fail(const char *what, int rc)
{
    fprintf(stderr, "ntfsdump: %s: %s\n", what, pn_strerror(rc));
    return 1;
}

static void *xmalloc(size_t n)
{
    void *p = malloc(n ? n : 1);
    if (!p) {
        fputs("ntfsdump: out of memory\n", stderr);
        exit(1);
    }
    return p;
}

/* Splits "dir/file:stream" at the stream colon (after the last separator). */
static int open_path(pn_volume *vol, const char *arg, pn_file **f, uint16_t **stream, size_t *slen)
{
    size_t len;
    uint16_t *w = utf8_to_utf16(arg, &len);
    if (!w)
        return PN_EINVAL;
    size_t cut = len;
    for (size_t i = len; stream && i-- > 0;) {
        if (w[i] == '/' || w[i] == '\\')
            break;
        if (w[i] == ':') {
            cut = i;
            break;
        }
    }
    if (stream) {
        *stream = NULL;
        *slen = 0;
        if (cut < len) {
            *slen = len - cut - 1;
            *stream = xmalloc((*slen + 1) * sizeof **stream);
            memcpy(*stream, w + cut + 1, *slen * sizeof **stream);
        }
    }
    int rc = pn_file_open_path(vol, w, cut, f);
    free(w);
    return rc;
}

/* ---- info ------------------------------------------------------------------- */

static int cmd_info(pn_volume *vol)
{
    pn_volume_info vi;
    pn_volume_get_info(vol, &vi);
    char *label = utf16_to_utf8(vi.label, vi.label_len);
    printf("NTFS version:      %u.%u\n", vi.major, vi.minor);
    printf("Label:             %s\n", label);
    printf("Serial:            %016" PRIx64 "\n", vi.serial);
    printf("Flags:             0x%04x%s\n", vi.flags, (vi.flags & PN_VOLUME_DIRTY) ? " (dirty)" : "");
    printf("Bytes per sector:  %u\n", vi.bytes_per_sector);
    printf("Cluster size:      %u\n", vi.cluster_size);
    printf("Total clusters:    %" PRIu64 "\n", vi.total_clusters);
    printf("File record size:  %u\n", vi.record_size);
    printf("Index block size:  %u\n", vi.index_block_size);
    printf("$MFT LCN:          %" PRIu64 "\n", vi.mft_lcn);
    printf("$MFTMirr LCN:      %" PRIu64 "\n", vi.mftmirr_lcn);
    printf("MFT records:       %" PRIu64 "\n", vi.mft_records);
    free(label);
    return 0;
}

/* ---- ls ---------------------------------------------------------------------- */

static int print_dirent(void *ctx, const pn_dirent *e)
{
    (void)ctx;
    char *name = utf16_to_utf8(e->name, e->name_len);
    static const char *ns[] = { "posix", "win32", "dos", "win32+dos" };
    printf("%10" PRIu64 " %5u %-9s %08x %12" PRIu64 "  %s\n", PN_REF_RECORD(e->ref),
           PN_REF_SEQ(e->ref), ns[e->name_space & 3], e->file_attributes, e->data_size, name);
    free(name);
    return 0;
}

static int cmd_ls(pn_volume *vol, const char *path)
{
    pn_file *f;
    int rc = open_path(vol, path, &f, NULL, NULL);
    if (rc)
        return fail(path, rc);
    printf("%10s %5s %-9s %-8s %12s  %s\n", "record", "seq", "namespace", "attrs", "size", "name");
    rc = pn_dir_iterate(f, print_dirent, NULL);
    pn_file_close(f);
    return rc ? fail(path, rc) : 0;
}

/* ---- stat --------------------------------------------------------------------- */

static int print_name(void *ctx, const pn_name *n)
{
    (void)ctx;
    char *s = utf16_to_utf8(n->name, n->name_len);
    printf("  name: %s (namespace %u, parent %" PRIu64 ")\n", s, n->name_space,
           PN_REF_RECORD(n->parent_ref));
    free(s);
    return 0;
}

static int print_attr(void *ctx, const pn_attr_desc *d)
{
    (void)ctx;
    char *name = utf16_to_utf8(d->name, d->name_len);
    printf("  attr 0x%02x %-12s id %-3u %s flags 0x%04x record %" PRIu64,
           d->type, d->name_len ? name : "(unnamed)", d->id,
           d->nonresident ? "non-resident" : "resident    ", d->flags, d->record);
    if (d->nonresident)
        printf(" start_vcn %" PRIu64, d->start_vcn);
    printf(" size %" PRIu64 "\n", d->size);
    free(name);
    return 0;
}

static int cmd_stat(pn_volume *vol, const char *path)
{
    pn_file *f;
    pn_file_info fi;
    int rc = open_path(vol, path, &f, NULL, NULL);
    if (rc)
        return fail(path, rc);
    rc = pn_file_get_info(f, &fi);
    if (rc == PN_OK) {
        printf("record %" PRIu64 " sequence %u links %u %s\n", fi.record, fi.sequence,
               fi.link_count, fi.is_directory ? "directory" : "file");
        printf("  attributes 0x%08x security_id %u usn %" PRIu64 " reparse_tag 0x%08x\n",
               fi.file_attributes, fi.security_id, fi.usn, fi.reparse_tag);
        printf("  created %" PRIu64 " modified %" PRIu64 " changed %" PRIu64 " accessed %" PRIu64 "\n",
               fi.create_time, fi.modify_time, fi.change_time, fi.access_time);
        printf("  data size %" PRIu64 " allocated %" PRIu64 "\n", fi.data_size, fi.alloc_size);
        rc = pn_file_names(f, print_name, NULL);
    }
    if (rc == PN_OK)
        rc = pn_file_attrs(f, print_attr, NULL);
    pn_attr *a;
    if (rc == PN_OK && pn_attr_open(f, PN_AT_DATA, NULL, 0, &a) == PN_OK) {
        uint32_t n;
        const pn_extent *e = pn_attr_extents(a, &n);
        for (uint32_t i = 0; i < n; i++) {
            if (e[i].lcn == PN_LCN_HOLE)
                printf("  run vcn %" PRIu64 " len %" PRIu64 " sparse\n", e[i].vcn, e[i].length);
            else
                printf("  run vcn %" PRIu64 " len %" PRIu64 " lcn %" PRIu64 "\n", e[i].vcn,
                       e[i].length, e[i].lcn);
        }
        pn_attr_close(a);
    }
    pn_file_close(f);
    return rc ? fail(path, rc) : 0;
}

/* ---- cat ----------------------------------------------------------------------- */

static int cmd_cat(pn_volume *vol, const char *path)
{
    pn_file *f;
    uint16_t *stream;
    size_t slen;
    int rc = open_path(vol, path, &f, &stream, &slen);
    if (rc)
        return fail(path, rc);
    pn_attr *a;
    rc = pn_attr_open(f, PN_AT_DATA, stream, slen, &a);
    free(stream);
    if (rc == PN_OK) {
        enum { CHUNK = 1 << 20 };
        uint8_t *buf = xmalloc(CHUNK);
        uint64_t off = 0;
        size_t got;
        while ((rc = pn_attr_read(a, off, buf, CHUNK, &got)) == PN_OK && got) {
            fwrite(buf, 1, got, stdout);
            off += got;
        }
        free(buf);
        pn_attr_close(a);
    }
    pn_file_close(f);
    return rc ? fail(path, rc) : 0;
}

/* ---- manifest ----------------------------------------------------------------- */

typedef struct lines {
    char **v;
    size_t n, cap;
} lines;

static void add_line(lines *l, char *s)
{
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 64;
        l->v = realloc(l->v, l->cap * sizeof *l->v);
        if (!l->v) {
            fputs("ntfsdump: out of memory\n", stderr);
            exit(1);
        }
    }
    l->v[l->n++] = s;
}

static char *fmt(const char *f, ...) __attribute__((format(printf, 1, 2)));
static char *fmt(const char *f, ...)
{
    va_list ap;
    va_start(ap, f);
    int n = vsnprintf(NULL, 0, f, ap);
    va_end(ap);
    char *s = xmalloc((size_t)n + 1);
    va_start(ap, f);
    vsnprintf(s, (size_t)n + 1, f, ap);
    va_end(ap);
    return s;
}

static int hash_attr(pn_file *f, uint32_t type, const uint16_t *name, size_t nlen,
                     uint64_t *size, char hex[65])
{
    pn_attr *a;
    sha256_ctx c;
    sha256_init(&c);
    *size = 0;
    int rc = pn_attr_open(f, type, name, nlen, &a);
    if (rc == PN_ENOENT) {
        sha256_hex(&c, hex);
        return PN_OK;
    }
    if (rc)
        return rc;
    enum { CHUNK = 1 << 20 };
    uint8_t *buf = xmalloc(CHUNK);
    size_t got;
    while ((rc = pn_attr_read(a, *size, buf, CHUNK, &got)) == PN_OK && got) {
        sha256_update(&c, buf, got);
        *size += got;
    }
    free(buf);
    pn_attr_close(a);
    sha256_hex(&c, hex);
    return rc;
}

typedef struct walk {
    pn_volume *vol;
    lines *out;
    const char *path;
    int depth;
    int err;
} walk;

static int walk_dir(walk *w, pn_file *dir);

typedef struct stream_ctx {
    walk *w;
    pn_file *f;
    const char *path;
} stream_ctx;

static int visit_stream(void *vctx, const pn_attr_desc *d)
{
    stream_ctx *c = vctx;
    if (d->type != PN_AT_DATA || d->name_len == 0 || (d->nonresident && d->start_vcn != 0))
        return 0;
    /* d->name points into the file's record buffer, which stays valid while we read. */
    uint64_t size;
    char hex[65];
    int rc = hash_attr(c->f, PN_AT_DATA, d->name, d->name_len, &size, hex);
    if (rc)
        return rc;
    char *sname = utf16_to_utf8(d->name, d->name_len);
    add_line(c->w->out, fmt("S\t%s:%s\t%" PRIu64 "\t%s", c->path, sname, size, hex));
    free(sname);
    return 0;
}

static int visit_entry(void *vctx, const pn_dirent *e)
{
    walk *w = vctx;
    if (e->name_space == 2 || PN_REF_RECORD(e->ref) < 16)
        return 0;   /* DOS alias, or a metadata file */

    char *name = utf16_to_utf8(e->name, e->name_len);
    char *path = fmt("%s/%s", w->path, name);
    free(name);

    pn_file *f;
    pn_file_info fi;
    int rc = pn_file_open_ref(w->vol, e->ref, &f);
    if (rc) {
        fprintf(stderr, "ntfsdump: %s: %s\n", path, pn_strerror(rc));
        free(path);
        return rc;
    }
    rc = pn_file_get_info(f, &fi);

    if (rc == PN_OK && (fi.file_attributes & FA_REPARSE)) {
        static uint16_t buf[8 + 16384];
        uint32_t tag;
        const uint16_t *target;
        size_t tlen;
        int rel;
        uint64_t size;
        char hex[65];
        rc = hash_attr(f, PN_AT_REPARSE_POINT, NULL, 0, &size, hex);
        if (rc == PN_OK) {
            int lrc = pn_reparse_read_link(f, (uint8_t *)buf, sizeof buf, &tag, &target, &tlen, &rel);
            char *t = lrc == PN_OK ? utf16_to_utf8(target, tlen) : NULL;
            add_line(w->out, fmt("R\t%s\t%08x\t%s\t%s", path, fi.reparse_tag, hex, t ? t : "-"));
            free(t);
            if (lrc != PN_OK && lrc != PN_EUNSUPPORTED)
                rc = lrc;
        }
    } else if (rc == PN_OK && fi.is_directory) {
        add_line(w->out, fmt("D\t%s", path));
        walk sub = *w;
        sub.path = path;
        sub.depth++;
        rc = sub.depth > MAX_DEPTH ? PN_ECORRUPT : walk_dir(&sub, f);
    } else if (rc == PN_OK) {
        uint64_t size;
        char hex[65];
        rc = hash_attr(f, PN_AT_DATA, NULL, 0, &size, hex);
        if (rc == PN_OK)
            add_line(w->out, fmt("F\t%s\t%" PRIu64 "\t%s\t%08x\t%u\t%" PRIu64, path, size, hex,
                                 fi.file_attributes & FA_MANIFEST_MASK, fi.link_count,
                                 fi.modify_time));
    }
    if (rc == PN_OK && !(fi.file_attributes & FA_REPARSE)) {
        stream_ctx sc = { w, f, path };
        rc = pn_file_attrs(f, visit_stream, &sc);
    }
    if (rc)
        fprintf(stderr, "ntfsdump: %s: %s\n", path, pn_strerror(rc));
    pn_file_close(f);
    free(path);
    return rc;
}

static int walk_dir(walk *w, pn_file *dir)
{
    return pn_dir_iterate(dir, visit_entry, w);
}

static int cmp_str(const void *a, const void *b)
{
    return strcmp(*(char *const *)a, *(char *const *)b);
}

static int cmd_manifest(pn_volume *vol)
{
    pn_file *root;
    int rc = pn_file_open(vol, PN_RECORD_ROOT, &root);
    if (rc)
        return fail("root", rc);
    lines out = { 0 };
    walk w = { vol, &out, "", 0, 0 };
    rc = walk_dir(&w, root);
    pn_file_close(root);
    qsort(out.v, out.n, sizeof *out.v, cmp_str);
    for (size_t i = 0; i < out.n; i++) {
        puts(out.v[i]);
        free(out.v[i]);
    }
    free(out.v);
    return rc ? fail("manifest", rc) : 0;
}

/* ---- main ---------------------------------------------------------------------- */

static int usage(void)
{
    fputs("usage: ntfsdump info|ls|stat|cat|manifest IMAGE [PATH[:STREAM]]\n", stderr);
    return 2;
}

int main(int argc, char **argv)
{
    if (argc < 3)
        return usage();
    const char *cmd = argv[1];
    const char *path = argc > 3 ? argv[3] : "/";

    pn_bdev dev;
    pn_volume *vol;
    int rc = pn_bdev_file_open(argv[2], &dev);
    if (rc)
        return fail(argv[2], rc);
    rc = pn_mount(&dev, &vol);
    if (rc) {
        pn_bdev_file_close(&dev);
        return fail("mount", rc);
    }

    int status;
    if (!strcmp(cmd, "info"))
        status = cmd_info(vol);
    else if (!strcmp(cmd, "ls"))
        status = cmd_ls(vol, path);
    else if (!strcmp(cmd, "stat") && argc > 3)
        status = cmd_stat(vol, path);
    else if (!strcmp(cmd, "cat") && argc > 3)
        status = cmd_cat(vol, path);
    else if (!strcmp(cmd, "manifest"))
        status = cmd_manifest(vol);
    else
        status = usage();

    pn_unmount(vol);
    pn_bdev_file_close(&dev);
    return status;
}
