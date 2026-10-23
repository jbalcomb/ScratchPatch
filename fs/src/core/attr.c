/* Files (base + extension records), attribute lookup and data reads (§4–§6, §9). */
#include "internal.h"

int pn_names_equal(const uint16_t *a, const uint16_t *b, size_t len)
{
    return len == 0 || memcmp(a, b, len * 2) == 0;
}

/* ---- Opening files ----------------------------------------------------------- */

static int add_record(pn_file *f, uint64_t number)
{
    for (uint32_t i = 0; i < f->nrecs; i++)
        if (f->recs[i].number == number)
            return PN_OK;
    if (f->nrecs > PN_MAX_EXT_RECORDS)
        return PN_ECORRUPT;
    pn_record *r = pn_realloc(f->recs, (f->nrecs + 1) * sizeof *r);
    if (!r)
        return PN_ENOMEM;
    f->recs = r;
    uint8_t *buf = pn_malloc(f->vol->info.record_size);
    if (!buf)
        return PN_ENOMEM;
    int rc = pn_record_load(f->vol, number, buf);
    if (rc == PN_OK) {
        uint16_t flags = rd16(buf + FR_FLAGS);
        uint64_t base = rd64(buf + FR_BASE_REF);
        if (f->nrecs == 0) {
            if (!(flags & FR_FLAG_IN_USE) || base != 0)
                rc = PN_ENOENT;     /* free record, or an extension record */
        } else if (!(flags & FR_FLAG_IN_USE) || base != PN_REF(f->number, f->sequence)) {
            rc = PN_ECORRUPT;       /* attribute list points at a record that isn't ours */
        }
    }
    if (rc) {
        pn_free(buf);
        return rc;
    }
    f->recs[f->nrecs].number = number;
    f->recs[f->nrecs].buf = buf;
    f->nrecs++;
    return PN_OK;
}

/* Reads the base record's $ATTRIBUTE_LIST and loads every record it names. */
static int load_attribute_list(pn_file *f)
{
    pn_rattr ra;
    uint32_t cursor = 0;
    int rc, found = 0;
    while ((rc = pn_record_next_attr(f->vol, f->recs[0].buf, f->number, &cursor, &ra)) == 1)
        if (ra.type == PN_AT_ATTRIBUTE_LIST) {
            found = 1;
            break;
        }
    if (rc < 0)
        return rc;
    if (!found)
        return PN_OK;

    uint8_t *list;
    size_t len;
    if (!ra.nonresident) {
        list = (uint8_t *)ra.value;
        len = ra.value_len;
    } else {
        /* Non-resident: its runlist is in the base record, so read it directly. */
        pn_extent *ext = NULL;
        uint32_t n = 0, cap = 0;
        uint64_t size = rd64(ra.p + AH_DATA_SIZE);
        if (ra.start_vcn != 0 || size > PN_MAX_ATTR_LIST)
            return PN_ECORRUPT;
        rc = pn_runlist_decode(f->vol, &ra, &ext, &n, &cap);
        list = rc ? NULL : pn_malloc(size ? size : 1);
        if (!rc && !list)
            rc = PN_ENOMEM;
        if (!rc)
            rc = pn_extents_read(f->vol, ext, n, 0, list, size);
        pn_free(ext);
        if (rc) {
            pn_free(list);
            return rc;
        }
        len = size;
    }

    for (size_t off = 0; off < len && rc == PN_OK;) {
        if (len - off < AL_MIN_LENGTH) {
            rc = PN_ECORRUPT;
            break;
        }
        uint16_t elen = rd16(list + off + AL_LENGTH);
        if (elen < AL_MIN_LENGTH || elen > len - off) {
            rc = PN_ECORRUPT;
            break;
        }
        uint64_t ref = rd64(list + off + AL_REF);
        if (PN_REF_RECORD(ref) != f->number)
            rc = add_record(f, PN_REF_RECORD(ref));
        off += elen;
    }
    if (ra.nonresident)
        pn_free(list);
    return rc;
}

int pn_file_open(pn_volume *vol, uint64_t record, pn_file **out)
{
    pn_file *f = pn_calloc(1, sizeof *f);
    if (!f)
        return PN_ENOMEM;
    f->vol = vol;
    f->number = record;
    int rc = add_record(f, record);
    if (rc == PN_OK) {
        const uint8_t *b = f->recs[0].buf;
        f->sequence = rd16(b + FR_SEQUENCE);
        f->flags = rd16(b + FR_FLAGS);
        f->link_count = rd16(b + FR_LINK_COUNT);
        rc = load_attribute_list(f);
    }
    if (rc) {
        pn_file_close(f);
        return rc;
    }
    *out = f;
    return PN_OK;
}

int pn_file_open_ref(pn_volume *vol, uint64_t ref, pn_file **out)
{
    pn_file *f;
    int rc = pn_file_open(vol, PN_REF_RECORD(ref), &f);
    if (rc)
        return rc;
    if (PN_REF_SEQ(ref) != 0 && PN_REF_SEQ(ref) != f->sequence) {
        pn_file_close(f);
        return PN_ESTALE;
    }
    *out = f;
    return PN_OK;
}

void pn_file_close(pn_file *f)
{
    if (!f)
        return;
    for (uint32_t i = 0; i < f->nrecs; i++)
        pn_free(f->recs[i].buf);
    pn_free(f->recs);
    pn_free(f);
}

/* ---- Finding attributes ---------------------------------------------------------- */

/* Attribute names compare case-insensitively, like stream names on Windows. */
static int attr_name_matches(const pn_volume *vol, const pn_rattr *ra,
                             const uint16_t *name, size_t name_len)
{
    if (ra->name_len != name_len)
        return 0;
    return name_len == 0 || pn_name_collate(vol, ra->name, ra->name_len, name, name_len) == 0;
}

/* Calls fn for every attribute in every record of the file. */
typedef int (*rattr_fn)(void *ctx, const pn_rattr *ra);

static int for_each_rattr(pn_file *f, rattr_fn fn, void *ctx)
{
    for (uint32_t i = 0; i < f->nrecs; i++) {
        pn_rattr ra;
        uint32_t cursor = 0;
        int rc;
        while ((rc = pn_record_next_attr(f->vol, f->recs[i].buf, f->recs[i].number,
                                         &cursor, &ra)) == 1) {
            rc = fn(ctx, &ra);
            if (rc)
                return rc;
        }
        if (rc < 0)
            return rc;
    }
    return PN_OK;
}

typedef struct find_ctx {
    const pn_volume *vol;
    uint32_t type;
    const uint16_t *name;
    size_t name_len;
    pn_rattr *found;    /* array of matching pieces */
    uint32_t n, cap;
} find_ctx;

static int collect_piece(void *vctx, const pn_rattr *ra)
{
    find_ctx *c = vctx;
    if (ra->type != c->type || !attr_name_matches(c->vol, ra, c->name, c->name_len))
        return PN_OK;
    if (c->n == c->cap) {
        uint32_t ncap = c->cap ? c->cap * 2 : 4;
        pn_rattr *p = pn_realloc(c->found, ncap * sizeof *p);
        if (!p)
            return PN_ENOMEM;
        c->found = p;
        c->cap = ncap;
    }
    c->found[c->n++] = *ra;
    return PN_OK;
}

int pn_file_find_rattr(pn_file *f, uint32_t type, const uint16_t *name, size_t name_len,
                       pn_rattr *out)
{
    find_ctx c = { f->vol, type, name, name_len, NULL, 0, 0 };
    int rc = for_each_rattr(f, collect_piece, &c);
    if (rc == PN_OK) {
        rc = PN_ENOENT;
        for (uint32_t i = 0; i < c.n; i++)
            if (!c.found[i].nonresident || c.found[i].start_vcn == 0) {
                *out = c.found[i];
                rc = PN_OK;
                break;
            }
    }
    pn_free(c.found);
    return rc;
}

static int cmp_piece(const pn_rattr *a, const pn_rattr *b)
{
    return a->start_vcn < b->start_vcn ? -1 : a->start_vcn > b->start_vcn;
}

/* ---- Opening attributes -------------------------------------------------------------- */

int pn_attr_open_raw(pn_file *f, uint32_t type, const uint16_t *name, size_t name_len,
                     pn_attr **out)
{
    find_ctx c = { f->vol, type, name, name_len, NULL, 0, 0 };
    int rc = for_each_rattr(f, collect_piece, &c);
    if (rc == PN_OK && c.n == 0)
        rc = PN_ENOENT;
    if (rc) {
        pn_free(c.found);
        return rc;
    }

    pn_attr *a = pn_calloc(1, sizeof *a);
    if (!a) {
        pn_free(c.found);
        return PN_ENOMEM;
    }
    a->vol = f->vol;
    a->type = type;

    /* Insertion sort by start VCN; there are rarely more than a handful of pieces. */
    for (uint32_t i = 1; i < c.n; i++)
        for (uint32_t j = i; j > 0 && cmp_piece(&c.found[j - 1], &c.found[j]) > 0; j--) {
            pn_rattr t = c.found[j];
            c.found[j] = c.found[j - 1];
            c.found[j - 1] = t;
        }

    const pn_rattr *first = &c.found[0];
    a->flags = first->flags;
    a->nonresident = first->nonresident;

    if (!first->nonresident) {
        if (c.n != 1) {
            rc = PN_ECORRUPT;
        } else {
            a->data_size = a->alloc_size = a->init_size = first->value_len;
            a->value = pn_malloc(first->value_len ? first->value_len : 1);
            if (!a->value)
                rc = PN_ENOMEM;
            else
                memcpy(a->value, first->value, first->value_len);
        }
    } else if (first->start_vcn != 0) {
        rc = PN_ECORRUPT;
    } else {
        const uint8_t *h = first->p;
        uint64_t cs = f->vol->info.cluster_size;
        a->alloc_size = rd64(h + AH_ALLOC_SIZE);
        a->data_size = rd64(h + AH_DATA_SIZE);
        a->init_size = rd64(h + AH_INIT_SIZE);
        if (a->init_size > a->data_size)
            rc = PN_ECORRUPT;
        if (rc == PN_OK && (a->flags & ATTR_FLAG_COMPRESSION_MASK)) {
            uint16_t cu = rd16(h + AH_COMP_UNIT);
            /* Windows only compresses with clusters <= 4 KiB, in units of 16 clusters. */
            if (cu == 0 || cu > 8 || ((uint64_t)cs << cu) > (1u << 20))
                rc = PN_EUNSUPPORTED;
            else
                a->cu_clusters = 1u << cu;
        }
        uint32_t cap = 0;
        uint64_t next_vcn = 0;
        for (uint32_t i = 0; i < c.n && rc == PN_OK; i++) {
            if (c.found[i].nonresident != 1 || c.found[i].start_vcn != next_vcn) {
                rc = PN_ECORRUPT;   /* gap or overlap between pieces */
                break;
            }
            rc = pn_runlist_decode(f->vol, &c.found[i], &a->extents, &a->nextents, &cap);
            next_vcn = c.found[i].last_vcn + 1;
        }
        /* Every byte of data must be mapped (holes count). */
        if (rc == PN_OK && a->data_size > next_vcn * cs)
            rc = PN_ECORRUPT;
    }
    pn_free(c.found);
    if (rc) {
        pn_attr_close(a);
        return rc;
    }
    *out = a;
    return PN_OK;
}

int pn_attr_open(pn_file *f, uint32_t type, const uint16_t *name, size_t name_len,
                 pn_attr **out)
{
    if (!f || !out || (name_len && !name) || name_len > 255)
        return PN_EINVAL;
    return pn_attr_open_raw(f, type, name, name_len, out);
}

void pn_attr_close(pn_attr *a)
{
    if (!a)
        return;
    pn_free(a->value);
    pn_free(a->extents);
    pn_free(a->cu_buf);
    pn_free(a);
}

void pn_attr_get_info(const pn_attr *a, pn_attr_info *info)
{
    info->type = a->type;
    info->flags = a->flags;
    info->nonresident = a->nonresident;
    info->data_size = a->data_size;
    info->alloc_size = a->alloc_size;
    info->initialized_size = a->init_size;
    info->compression_unit = a->cu_clusters;
    info->extent_count = a->nextents;
}

const pn_extent *pn_attr_extents(const pn_attr *a, uint32_t *count)
{
    *count = a->nextents;
    return a->extents;
}

/* ---- Reading data ------------------------------------------------------------------- */

/*
 * Loads compression unit `index` into a->cu_buf. A unit is stored one of three
 * ways (§9): all clusters sparse (zeros), all allocated (stored raw), or
 * LZNT1 data in its leading clusters followed by a sparse tail.
 */
static int load_unit(pn_attr *a, uint64_t index)
{
    uint64_t cs = a->vol->info.cluster_size;
    uint64_t unit_bytes = a->cu_clusters * cs;
    uint64_t vcn0 = index * a->cu_clusters;

    if (a->cu_valid && a->cu_index == index)
        return PN_OK;
    if (!a->cu_buf && !(a->cu_buf = pn_malloc(unit_bytes)))
        return PN_ENOMEM;
    a->cu_valid = 0;

    uint64_t allocated = 0, sparse = 0;
    for (uint32_t i = 0; i < a->nextents; i++) {
        const pn_extent *e = &a->extents[i];
        uint64_t s = e->vcn > vcn0 ? e->vcn : vcn0;
        uint64_t end = e->vcn + e->length;
        uint64_t uend = vcn0 + a->cu_clusters;
        if (end > uend)
            end = uend;
        if (s >= end)
            continue;
        if (e->lcn == PN_LCN_HOLE)
            sparse += end - s;
        else if (sparse)
            return PN_ECORRUPT;     /* allocated cluster after the sparse tail */
        else
            allocated += end - s;
    }
    sparse = a->cu_clusters - allocated;   /* unmapped tail of the last unit reads as sparse */

    int rc;
    if (allocated == 0) {
        memset(a->cu_buf, 0, unit_bytes);
        rc = PN_OK;
    } else if (sparse == 0) {
        rc = pn_extents_read(a->vol, a->extents, a->nextents, vcn0 * cs, a->cu_buf, unit_bytes);
    } else {
        uint8_t *in = pn_malloc(allocated * cs);
        if (!in)
            return PN_ENOMEM;
        size_t got;
        rc = pn_extents_read(a->vol, a->extents, a->nextents, vcn0 * cs, in, allocated * cs);
        if (rc == PN_OK)
            rc = pn_lznt1_decompress(in, allocated * cs, a->cu_buf, unit_bytes, &got);
        if (rc == PN_OK)
            memset(a->cu_buf + got, 0, unit_bytes - got);
        pn_free(in);
    }
    if (rc == PN_OK) {
        a->cu_index = index;
        a->cu_valid = 1;
    }
    return rc;
}

int pn_attr_read(pn_attr *a, uint64_t offset, void *buf, size_t len, size_t *got)
{
    uint8_t *out = buf;
    *got = 0;
    if (a->flags & ATTR_FLAG_ENCRYPTED)
        return PN_EUNSUPPORTED;
    if (offset >= a->data_size)
        return PN_OK;
    if (len > a->data_size - offset)
        len = (size_t)(a->data_size - offset);

    if (!a->nonresident) {
        memcpy(out, a->value + offset, len);
        *got = len;
        return PN_OK;
    }

    /* Bytes past the initialized size read as zeros without touching the disk. */
    size_t real = offset >= a->init_size ? 0
                : (a->init_size - offset < len ? (size_t)(a->init_size - offset) : len);
    memset(out + real, 0, len - real);

    if (!a->cu_clusters) {
        int rc = pn_extents_read(a->vol, a->extents, a->nextents, offset, out, real);
        if (rc)
            return rc;
    } else {
        uint64_t unit_bytes = (uint64_t)a->cu_clusters * a->vol->info.cluster_size;
        size_t done = 0;
        while (done < real) {
            uint64_t pos = offset + done;
            int rc = load_unit(a, pos / unit_bytes);
            if (rc)
                return rc;
            uint64_t within = pos % unit_bytes;
            size_t n = unit_bytes - within < real - done ? (size_t)(unit_bytes - within) : real - done;
            memcpy(out + done, a->cu_buf + within, n);
            done += n;
        }
    }
    *got = len;
    return PN_OK;
}

/* ---- File metadata ------------------------------------------------------------------- */

typedef struct names_ctx {
    pn_name_cb cb;
    void *ctx;
} names_ctx;

static int visit_name(void *vctx, const pn_rattr *ra)
{
    names_ctx *c = vctx;
    if (ra->type != PN_AT_FILE_NAME)
        return PN_OK;
    if (ra->nonresident || ra->value_len < FN_NAME ||
        FN_NAME + 2u * ra->value[FN_NAME_LEN] > ra->value_len || ra->value[FN_NAMESPACE] > 3)
        return PN_ECORRUPT;
    pn_name n = {
        .parent_ref = rd64(ra->value + FN_PARENT),
        .name_space = ra->value[FN_NAMESPACE],
        .name_len = ra->value[FN_NAME_LEN],
        .name = (const uint16_t *)(const void *)(ra->value + FN_NAME),
    };
    if ((uintptr_t)n.name % 2)
        return PN_ECORRUPT;
    return c->cb(c->ctx, &n);
}

int pn_file_names(pn_file *f, pn_name_cb cb, void *ctx)
{
    names_ctx c = { cb, ctx };
    return for_each_rattr(f, visit_name, &c);
}

typedef struct attrs_ctx {
    pn_attr_cb cb;
    void *ctx;
} attrs_ctx;

static int visit_attr(void *vctx, const pn_rattr *ra)
{
    attrs_ctx *c = vctx;
    pn_attr_desc d = {
        .type = ra->type,
        .flags = ra->flags,
        .id = ra->id,
        .nonresident = ra->nonresident,
        .name_len = ra->name_len,
        .name = ra->name,
        .record = ra->record,
        .start_vcn = ra->start_vcn,
        .size = !ra->nonresident ? ra->value_len
              : ra->start_vcn == 0 ? rd64(ra->p + AH_DATA_SIZE) : 0,
    };
    return c->cb(c->ctx, &d);
}

int pn_file_attrs(pn_file *f, pn_attr_cb cb, void *ctx)
{
    attrs_ctx c = { cb, ctx };
    return for_each_rattr(f, visit_attr, &c);
}

int pn_file_get_info(pn_file *f, pn_file_info *info)
{
    memset(info, 0, sizeof *info);
    info->record = f->number;
    info->sequence = f->sequence;
    info->link_count = f->link_count;
    info->is_directory = (f->flags & FR_FLAG_DIRECTORY) != 0;

    pn_rattr ra;
    int rc = pn_file_find_rattr(f, PN_AT_STANDARD_INFO, NULL, 0, &ra);
    if (rc)
        return rc == PN_ENOENT ? PN_ECORRUPT : rc;
    if (ra.nonresident || ra.value_len < SI_SIZE_V1)
        return PN_ECORRUPT;
    info->create_time = rd64(ra.value + SI_CREATE);
    info->modify_time = rd64(ra.value + SI_MODIFY);
    info->change_time = rd64(ra.value + SI_CHANGE);
    info->access_time = rd64(ra.value + SI_ACCESS);
    info->file_attributes = rd32(ra.value + SI_ATTRIBUTES);
    if (ra.value_len >= SI_SIZE_V3) {
        info->security_id = rd32(ra.value + SI_SECURITY_ID);
        info->usn = rd64(ra.value + SI_USN);
    }

    if (pn_file_find_rattr(f, PN_AT_REPARSE_POINT, NULL, 0, &ra) == PN_OK) {
        pn_attr *a;
        uint8_t tag[4];
        size_t got;
        rc = pn_attr_open_raw(f, PN_AT_REPARSE_POINT, NULL, 0, &a);
        if (rc)
            return rc;
        rc = pn_attr_read(a, 0, tag, 4, &got);
        pn_attr_close(a);
        if (rc)
            return rc;
        if (got == 4)
            info->reparse_tag = rd32(tag);
    }

    if (!info->is_directory) {
        pn_attr *a;
        rc = pn_attr_open_raw(f, PN_AT_DATA, NULL, 0, &a);
        if (rc == PN_OK) {
            info->data_size = a->data_size;
            info->alloc_size = a->alloc_size;
            pn_attr_close(a);
        } else if (rc != PN_ENOENT) {
            return rc;
        }
    }
    return PN_OK;
}
