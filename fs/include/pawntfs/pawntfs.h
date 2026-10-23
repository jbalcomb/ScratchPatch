/*
 * PawNTFS core: a read-only NTFS 3.1 parser in portable C11.
 *
 * The core includes no OS headers and does no I/O of its own: it reads the
 * volume through a pn_bdev supplied by the caller (an image file in tests, a
 * WinFsp or kernel front end later). All on-disk data is treated as
 * untrusted and validated before use.
 *
 * Names are UTF-16LE throughout, as NTFS stores them.
 */
#ifndef PAWNTFS_H
#define PAWNTFS_H

#include <stddef.h>
#include <stdint.h>

/* ---- Errors -------------------------------------------------------------- */

enum {
    PN_OK           = 0,
    PN_EIO          = -1,   /* the block device failed */
    PN_ENOMEM       = -2,
    PN_EINVAL       = -3,   /* bad argument */
    PN_ECORRUPT     = -4,   /* an on-disk structure failed validation */
    PN_ENOENT       = -5,   /* no such file, stream or attribute */
    PN_ENOTDIR      = -6,
    PN_EUNSUPPORTED = -7,   /* valid NTFS we don't handle yet (e.g. EFS) */
    PN_ESTALE       = -8,   /* file reference's sequence number is out of date */
};

const char *pn_strerror(int err);

/* ---- Block device -------------------------------------------------------- */

/*
 * Byte-addressed reads. The core only issues reads whose offset and length
 * are multiples of sector_size.
 */
typedef struct pn_bdev {
    int (*read)(void *ctx, uint64_t offset, size_t len, void *buf);
    void    *ctx;
    uint64_t size;          /* bytes */
    uint32_t sector_size;   /* 512 for image files */
} pn_bdev;

/* ---- Volume -------------------------------------------------------------- */

typedef struct pn_volume pn_volume;

typedef struct pn_volume_info {
    uint32_t bytes_per_sector;
    uint32_t cluster_size;
    uint32_t record_size;
    uint32_t index_block_size;
    uint64_t total_clusters;
    uint64_t mft_lcn;
    uint64_t mftmirr_lcn;
    uint64_t serial;
    uint8_t  major, minor;          /* from $VOLUME_INFORMATION */
    uint16_t flags;                 /* PN_VOLUME_* */
    uint16_t label[128];            /* UTF-16, not terminated */
    uint32_t label_len;
    uint64_t mft_records;           /* records covered by $MFT's data */
} pn_volume_info;

#define PN_VOLUME_DIRTY 0x0001u

int  pn_mount(const pn_bdev *dev, pn_volume **out);
void pn_unmount(pn_volume *vol);
void pn_volume_get_info(const pn_volume *vol, pn_volume_info *info);

/* ---- Files ---------------------------------------------------------------- */

typedef struct pn_file pn_file;

/* File references: record number in the low 48 bits, sequence in the high 16. */
#define PN_REF(rec, seq)    (((uint64_t)(seq) << 48) | ((uint64_t)(rec) & 0xFFFFFFFFFFFFull))
#define PN_REF_RECORD(ref)  ((uint64_t)((ref) & 0xFFFFFFFFFFFFull))
#define PN_REF_SEQ(ref)     ((uint16_t)((ref) >> 48))

#define PN_RECORD_ROOT 5u

/* Opens by record number, ignoring the sequence number. */
int  pn_file_open(pn_volume *vol, uint64_t record, pn_file **out);
/* Opens by full reference; PN_ESTALE if the sequence number doesn't match. */
int  pn_file_open_ref(pn_volume *vol, uint64_t ref, pn_file **out);
/* Path from the root, '\' or '/' separated, case-insensitive via $UpCase. */
int  pn_file_open_path(pn_volume *vol, const uint16_t *path, size_t len, pn_file **out);
void pn_file_close(pn_file *f);

typedef struct pn_file_info {
    uint64_t record;
    uint16_t sequence;
    uint16_t link_count;
    int      is_directory;
    uint64_t create_time, modify_time, change_time, access_time;   /* FILETIME */
    uint32_t file_attributes;       /* FILE_ATTRIBUTE_* from $STANDARD_INFORMATION */
    uint32_t security_id;
    uint64_t usn;
    uint32_t reparse_tag;           /* 0 if not a reparse point */
    uint64_t data_size;             /* unnamed $DATA; 0 for directories */
    uint64_t alloc_size;
} pn_file_info;

int pn_file_get_info(pn_file *f, pn_file_info *info);

/* A $FILE_NAME attribute: one per hard link, plus DOS short names. */
typedef struct pn_name {
    uint64_t        parent_ref;
    uint8_t         name_space;     /* 0 POSIX, 1 Win32, 2 DOS, 3 Win32+DOS */
    uint8_t         name_len;
    const uint16_t *name;
} pn_name;

typedef int (*pn_name_cb)(void *ctx, const pn_name *name);
int pn_file_names(pn_file *f, pn_name_cb cb, void *ctx);

/* ---- Attributes and streams --------------------------------------------- */

typedef struct pn_attr pn_attr;

#define PN_AT_STANDARD_INFO 0x10u
#define PN_AT_ATTRIBUTE_LIST 0x20u
#define PN_AT_FILE_NAME     0x30u
#define PN_AT_OBJECT_ID     0x40u
#define PN_AT_SECURITY_DESC 0x50u
#define PN_AT_VOLUME_NAME   0x60u
#define PN_AT_VOLUME_INFO   0x70u
#define PN_AT_DATA          0x80u
#define PN_AT_INDEX_ROOT    0x90u
#define PN_AT_INDEX_ALLOC   0xA0u
#define PN_AT_BITMAP        0xB0u
#define PN_AT_REPARSE_POINT 0xC0u
#define PN_AT_EA_INFO       0xD0u
#define PN_AT_EA            0xE0u
#define PN_AT_LOGGED_STREAM 0x100u

/* Opens the attribute of this type and name (name_len 0 = unnamed). */
int  pn_attr_open(pn_file *f, uint32_t type, const uint16_t *name, size_t name_len,
                  pn_attr **out);
void pn_attr_close(pn_attr *a);

typedef struct pn_attr_info {
    uint32_t type;
    uint16_t flags;                 /* 0x0001 compressed, 0x4000 encrypted, 0x8000 sparse */
    int      nonresident;
    uint64_t data_size;
    uint64_t alloc_size;
    uint64_t initialized_size;
    uint32_t compression_unit;      /* clusters; 0 if not compressed */
    uint32_t extent_count;          /* runs, including sparse ones */
} pn_attr_info;

void pn_attr_get_info(const pn_attr *a, pn_attr_info *info);

/* Reads up to len bytes at offset; *got is short only at end of data. */
int pn_attr_read(pn_attr *a, uint64_t offset, void *buf, size_t len, size_t *got);

/* One run of a non-resident attribute; lcn = PN_LCN_HOLE for sparse runs. */
#define PN_LCN_HOLE UINT64_MAX
typedef struct pn_extent {
    uint64_t vcn, lcn, length;
} pn_extent;
const pn_extent *pn_attr_extents(const pn_attr *a, uint32_t *count);

/* Every attribute of the file, across extension records. */
typedef struct pn_attr_desc {
    uint32_t        type;
    uint16_t        flags;
    uint16_t        id;
    int             nonresident;
    uint8_t         name_len;
    const uint16_t *name;
    uint64_t        record;         /* which MFT record holds it */
    uint64_t        start_vcn;      /* non-resident pieces only */
    uint64_t        size;           /* value length, or data size (first piece) */
} pn_attr_desc;

typedef int (*pn_attr_cb)(void *ctx, const pn_attr_desc *desc);
int pn_file_attrs(pn_file *f, pn_attr_cb cb, void *ctx);

/* ---- Directories ---------------------------------------------------------- */

typedef struct pn_dirent {
    uint64_t        ref;
    uint8_t         name_space;
    uint8_t         name_len;
    const uint16_t *name;
    /* "Duplicated information" cached in the index entry. */
    uint64_t        create_time, modify_time, change_time, access_time;
    uint64_t        alloc_size, data_size;
    uint32_t        file_attributes;
} pn_dirent;

/* Return non-zero from the callback to stop early; that value is returned. */
typedef int (*pn_dirent_cb)(void *ctx, const pn_dirent *ent);

/* Visits entries in index (collation) order, including DOS-namespace entries. */
int pn_dir_iterate(pn_file *dir, pn_dirent_cb cb, void *ctx);
/* Case-insensitive lookup; an exact-case match wins over other case variants. */
int pn_dir_lookup(pn_file *dir, const uint16_t *name, size_t len, uint64_t *ref);

/* ---- Reparse points ------------------------------------------------------ */

#define PN_REPARSE_TAG_MOUNT_POINT 0xA0000003u
#define PN_REPARSE_TAG_SYMLINK     0xA000000Cu

/*
 * Decodes a symlink or junction. *target points into buf (UTF-16) and is the
 * substitute name; *relative is set for relative symlinks.
 */
int pn_reparse_read_link(pn_file *f, uint8_t *buf, size_t bufsize, uint32_t *tag,
                         const uint16_t **target, size_t *target_len, int *relative);

/* ---- Utilities ------------------------------------------------------------- */

/* Compares two names the way the volume's directory index does. */
int pn_name_collate(const pn_volume *vol, const uint16_t *a, size_t alen,
                    const uint16_t *b, size_t blen);

/* LZNT1 decompression [MS-XCA 2.5]; *outlen = bytes produced. */
int pn_lznt1_decompress(const uint8_t *in, size_t inlen, uint8_t *out, size_t outcap,
                        size_t *outlen);

#endif /* PAWNTFS_H */
