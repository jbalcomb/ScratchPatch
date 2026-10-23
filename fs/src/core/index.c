/* Directory indexes: $I30 B+ trees in $INDEX_ROOT / $INDEX_ALLOCATION (§7). */
#include "internal.h"

static const uint16_t I30[4] = { '$', 'I', '3', '0' };

typedef struct dir_index {
    pn_volume *vol;
    uint8_t   *root;        /* copy of the $INDEX_ROOT value */
    uint32_t   root_len;
    uint32_t   block_size;
    uint32_t   vcn_unit;    /* bytes per index VCN */
    pn_attr   *alloc;       /* $INDEX_ALLOCATION:$I30, NULL for a small index */
    uint8_t   *block;       /* one index block */
    uint64_t   nblocks;
    uint8_t   *visited;     /* traversal guard: one bit per block */
} dir_index;

/* A parsed node: a validated run of entries. */
typedef struct node {
    const uint8_t *entries;     /* first entry */
    uint32_t len;               /* bytes of entries in use */
} node;

static void dir_close(dir_index *d)
{
    pn_free(d->root);
    pn_attr_close(d->alloc);
    pn_free(d->block);
    pn_free(d->visited);
}

/* Validates an index header and returns the node it describes. avail = bytes after the header start. */
static int parse_header(const uint8_t *ih, uint32_t avail, node *n)
{
    if (avail < IH_SIZE)
        return PN_ECORRUPT;
    uint32_t first = rd32(ih + IH_FIRST_ENTRY);
    uint32_t used = rd32(ih + IH_ENTRIES_SIZE);
    if (first < IH_SIZE || first % 8 || used > avail || first > used)
        return PN_ECORRUPT;
    n->entries = ih + first;
    n->len = used - first;
    return PN_OK;
}

static int dir_open(pn_file *f, dir_index *d)
{
    memset(d, 0, sizeof *d);
    d->vol = f->vol;
    if (!(f->flags & FR_FLAG_DIRECTORY))
        return PN_ENOTDIR;

    pn_rattr ra;
    int rc = pn_file_find_rattr(f, PN_AT_INDEX_ROOT, I30, 4, &ra);
    if (rc)
        return rc == PN_ENOENT ? PN_ECORRUPT : rc;
    if (ra.nonresident || ra.value_len < IR_HEADER + IH_SIZE ||
        rd32(ra.value + IR_TYPE) != PN_AT_FILE_NAME ||
        rd32(ra.value + IR_COLLATION) != COLLATION_FILENAME)
        return PN_ECORRUPT;
    d->block_size = rd32(ra.value + IR_BLOCK_SIZE);
    if (d->block_size < 512 || d->block_size > (2u << 20) || (d->block_size & (d->block_size - 1)))
        return PN_ECORRUPT;
    /* Index VCNs count clusters, or 512-byte units when blocks are smaller than a cluster. */
    d->vcn_unit = d->block_size >= f->vol->info.cluster_size ? f->vol->info.cluster_size : 512;

    d->root = pn_malloc(ra.value_len);
    if (!d->root)
        return PN_ENOMEM;
    memcpy(d->root, ra.value, ra.value_len);
    d->root_len = ra.value_len;

    rc = pn_attr_open_raw(f, PN_AT_INDEX_ALLOC, I30, 4, &d->alloc);
    if (rc == PN_ENOENT)
        return PN_OK;
    if (rc)
        return rc;
    d->nblocks = d->alloc->data_size / d->block_size;
    /* Holes make a huge size cheap to fake; the blocks have to fit on the volume. */
    if (d->nblocks > f->vol->info.total_clusters * f->vol->info.cluster_size / d->block_size)
        return PN_ECORRUPT;
    d->block = pn_malloc(d->block_size);
    d->visited = pn_calloc((size_t)(d->nblocks / 8 + 1), 1);
    return d->block && d->visited ? PN_OK : PN_ENOMEM;
}

/* Reads and validates the INDX block at vcn into d->block. */
static int read_block(dir_index *d, uint64_t vcn, node *n)
{
    if (!d->alloc || vcn > UINT64_MAX / d->vcn_unit)
        return PN_ECORRUPT;
    uint64_t off = vcn * d->vcn_unit;
    if (off % d->block_size || off / d->block_size >= d->nblocks)
        return PN_ECORRUPT;
    size_t got;
    int rc = pn_attr_read(d->alloc, off, d->block, d->block_size, &got);
    if (rc)
        return rc;
    if (got != d->block_size || memcmp(d->block, "INDX", 4) != 0)
        return PN_ECORRUPT;
    rc = pn_fixup(d->block, d->block_size);
    if (rc)
        return rc;
    if (rd64(d->block + IX_VCN) != vcn)
        return PN_ECORRUPT;
    return parse_header(d->block + IX_HEADER, d->block_size - IX_HEADER, n);
}

/* One validated entry within a node. */
typedef struct entry {
    const uint8_t *p;
    uint16_t len, flags;
    const uint8_t *key;     /* $FILE_NAME value; NULL on the last entry */
    uint64_t child_vcn;     /* valid if flags & IE_FLAG_SUBNODE */
} entry;

/* Steps through a node; *pos starts at 0. Returns 1 for an entry, 0 after the last. */
static int next_entry(const node *n, uint32_t *pos, entry *e)
{
    if (*pos == UINT32_MAX)
        return 0;
    if (*pos + IE_KEY > n->len)
        return PN_ECORRUPT;     /* ran off the node without a last entry */
    const uint8_t *p = n->entries + *pos;
    e->p = p;
    e->len = rd16(p + IE_LENGTH);
    e->flags = rd16(p + IE_FLAGS);
    uint16_t key_len = rd16(p + IE_KEY_LENGTH);
    uint32_t tail = (e->flags & IE_FLAG_SUBNODE) ? 8 : 0;
    if (e->len < IE_KEY + tail || e->len % 8 || e->len > n->len - *pos)
        return PN_ECORRUPT;
    if (tail)
        e->child_vcn = rd64(p + e->len - 8);

    if (e->flags & IE_FLAG_LAST) {
        e->key = NULL;
        *pos = UINT32_MAX;
    } else {
        if (key_len < FN_NAME || IE_KEY + (uint32_t)key_len > e->len - tail ||
            FN_NAME + 2u * p[IE_KEY + FN_NAME_LEN] > key_len || p[IE_KEY + FN_NAMESPACE] > 3)
            return PN_ECORRUPT;
        e->key = p + IE_KEY;
        *pos += e->len;
    }
    return 1;
}

static void fill_dirent(const entry *e, pn_dirent *de)
{
    const uint8_t *k = e->key;
    de->ref = rd64(e->p + IE_REF);
    de->name_space = k[FN_NAMESPACE];
    de->name_len = k[FN_NAME_LEN];
    de->name = (const uint16_t *)(const void *)(k + FN_NAME);
    de->create_time = rd64(k + FN_CREATE);
    de->modify_time = rd64(k + FN_MODIFY);
    de->change_time = rd64(k + FN_CHANGE);
    de->access_time = rd64(k + FN_ACCESS);
    de->alloc_size = rd64(k + FN_ALLOC_SIZE);
    de->data_size = rd64(k + FN_DATA_SIZE);
    de->file_attributes = rd32(k + FN_ATTRIBUTES);
}

static int root_node(dir_index *d, node *n)
{
    return parse_header(d->root + IR_HEADER, d->root_len - IR_HEADER, n);
}

/* ---- Iteration ------------------------------------------------------------------------ */

static int iterate_node(dir_index *d, const node *n, int depth, pn_dirent_cb cb, void *ctx);

static int iterate_child(dir_index *d, uint64_t vcn, int depth, pn_dirent_cb cb, void *ctx)
{
    if (depth >= PN_MAX_INDEX_DEPTH)
        return PN_ECORRUPT;
    /* The parent's copy of the block is overwritten below, so each level gets its own. */
    node n;
    int rc = read_block(d, vcn, &n);
    if (rc)
        return rc;
    uint64_t b = vcn * d->vcn_unit / d->block_size;
    if (d->visited[b / 8] & (1u << (b % 8)))
        return PN_ECORRUPT;     /* a block reachable twice: the tree has a cycle */
    d->visited[b / 8] |= (uint8_t)(1u << (b % 8));

    uint8_t *copy = pn_malloc(n.len ? n.len : 1);
    if (!copy)
        return PN_ENOMEM;
    memcpy(copy, n.entries, n.len);
    node own = { copy, n.len };
    rc = iterate_node(d, &own, depth + 1, cb, ctx);
    pn_free(copy);
    return rc;
}

static int iterate_node(dir_index *d, const node *n, int depth, pn_dirent_cb cb, void *ctx)
{
    uint32_t pos = 0;
    entry e;
    int rc;
    while ((rc = next_entry(n, &pos, &e)) == 1) {
        if (e.flags & IE_FLAG_SUBNODE) {
            rc = iterate_child(d, e.child_vcn, depth, cb, ctx);
            if (rc)
                return rc;
        }
        if (e.key) {
            if ((uintptr_t)(e.key + FN_NAME) % 2)
                return PN_ECORRUPT;
            pn_dirent de;
            fill_dirent(&e, &de);
            rc = cb(ctx, &de);
            if (rc)
                return rc;
        }
    }
    return rc;
}

int pn_dir_iterate(pn_file *dir, pn_dirent_cb cb, void *ctx)
{
    dir_index d;
    int rc = dir_open(dir, &d);
    if (rc == PN_OK) {
        node root;
        rc = root_node(&d, &root);
        if (rc == PN_OK)
            rc = iterate_node(&d, &root, 0, cb, ctx);
    }
    dir_close(&d);
    return rc;
}

/* ---- Lookup ------------------------------------------------------------------------------ */

static int binary_cmp(const uint16_t *a, size_t alen, const uint16_t *b, size_t blen)
{
    size_t n = alen < blen ? alen : blen;
    for (size_t i = 0; i < n; i++)
        if (a[i] != b[i])
            return a[i] < b[i] ? -1 : 1;
    return alen < blen ? -1 : alen > blen;
}

/*
 * Descends the tree comparing with $UpCase. Names that upcase equal are
 * ordered by their exact code units (spec §7.5 ⚠ to verify), so we keep
 * descending that way looking for an exact-case match, and otherwise
 * return the first case-insensitive match met.
 */
int pn_dir_lookup(pn_file *dir, const uint16_t *name, size_t len, uint64_t *ref)
{
    if (!dir || !name || !ref || len == 0 || len > 255)
        return PN_EINVAL;
    dir_index d;
    int rc = dir_open(dir, &d);
    node n;
    if (rc == PN_OK)
        rc = root_node(&d, &n);

    int have_candidate = 0;
    for (int depth = 0; rc == PN_OK; depth++) {
        uint32_t pos = 0;
        entry e;
        int descend = 0;
        while ((rc = next_entry(&n, &pos, &e)) == 1) {
            int c = 0;
            if (e.key) {
                const uint16_t *en = (const uint16_t *)(const void *)(e.key + FN_NAME);
                size_t elen = e.key[FN_NAME_LEN];
                if ((uintptr_t)en % 2) {
                    rc = PN_ECORRUPT;
                    break;
                }
                c = pn_name_collate(d.vol, name, len, en, elen);
                if (c == 0) {
                    if (!have_candidate) {
                        *ref = rd64(e.p + IE_REF);
                        have_candidate = 1;
                    }
                    c = binary_cmp(name, len, en, elen);
                    if (c == 0) {
                        *ref = rd64(e.p + IE_REF);
                        goto done;
                    }
                }
                if (c > 0)
                    continue;
            }
            /* name sorts before this entry (or this is the last entry) */
            descend = (e.flags & IE_FLAG_SUBNODE) != 0;
            break;
        }
        if (rc < 0)
            break;
        if (!descend) {
            rc = have_candidate ? PN_OK : PN_ENOENT;
            break;
        }
        if (depth + 1 >= PN_MAX_INDEX_DEPTH) {
            rc = PN_ECORRUPT;
            break;
        }
        rc = read_block(&d, e.child_vcn, &n);
    }
done:
    if (rc == 1)
        rc = PN_OK;
    dir_close(&d);
    return rc;
}
