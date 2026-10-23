/* Private to the core. Offsets follow fs/docs/NTFS_FORMAT.md section by section. */
#ifndef PN_INTERNAL_H
#define PN_INTERNAL_H

#include "pawntfs/pawntfs.h"
#include "platform.h"

/* ---- Little-endian readers (work on any host) ---------------------------- */

static inline uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static inline uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static inline uint64_t rd64(const uint8_t *p) { return rd32(p) | ((uint64_t)rd32(p + 4) << 32); }
static inline void wr16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

/* ---- §2 Boot sector ------------------------------------------------------- */

#define BS_OEM_ID               0x03
#define BS_BYTES_PER_SECTOR     0x0B
#define BS_SECTORS_PER_CLUSTER  0x0D
#define BS_RESERVED_SECTORS     0x0E
#define BS_TOTAL_SECTORS        0x28
#define BS_MFT_LCN              0x30
#define BS_MFTMIRR_LCN          0x38
#define BS_CLUSTERS_PER_RECORD  0x40
#define BS_CLUSTERS_PER_INDEX   0x44
#define BS_SERIAL               0x48
#define BS_SIGNATURE            0x1FE

/* ---- §3 Multi-sector header ---------------------------------------------- */

#define MS_USA_OFFSET   0x04
#define MS_USA_COUNT    0x06
#define MS_STRIDE       512u

/* ---- §4 File record header ------------------------------------------------ */

#define FR_LSN          0x08
#define FR_SEQUENCE     0x10
#define FR_LINK_COUNT   0x12
#define FR_ATTRS_OFFSET 0x14
#define FR_FLAGS        0x16
#define FR_BYTES_USED   0x18
#define FR_BYTES_ALLOC  0x1C
#define FR_BASE_REF     0x20
#define FR_RECORD_NO    0x2C

#define FR_FLAG_IN_USE      0x0001u
#define FR_FLAG_DIRECTORY   0x0002u

/* ---- §5 Attribute headers -------------------------------------------------- */

#define AH_TYPE         0x00
#define AH_LENGTH       0x04
#define AH_NONRESIDENT  0x08
#define AH_NAME_LEN     0x09
#define AH_NAME_OFFSET  0x0A
#define AH_FLAGS        0x0C
#define AH_ID           0x0E
/* resident */
#define AH_VALUE_LEN    0x10
#define AH_VALUE_OFFSET 0x14
#define AH_RESIDENT_HDR 0x18
/* non-resident */
#define AH_START_VCN    0x10
#define AH_LAST_VCN     0x18
#define AH_RUNS_OFFSET  0x20
#define AH_COMP_UNIT    0x22
#define AH_ALLOC_SIZE   0x28
#define AH_DATA_SIZE    0x30
#define AH_INIT_SIZE    0x38
#define AH_NONRES_HDR   0x40

#define AT_END          0xFFFFFFFFu

#define ATTR_FLAG_COMPRESSION_MASK 0x00FFu
#define ATTR_FLAG_ENCRYPTED        0x4000u
#define ATTR_FLAG_SPARSE           0x8000u

/* ---- §6 Attribute values ----------------------------------------------------- */

#define SI_CREATE       0x00
#define SI_MODIFY       0x08
#define SI_CHANGE       0x10
#define SI_ACCESS       0x18
#define SI_ATTRIBUTES   0x20
#define SI_SECURITY_ID  0x34
#define SI_USN          0x40
#define SI_SIZE_V1      0x30
#define SI_SIZE_V3      0x48

#define AL_TYPE         0x00
#define AL_LENGTH       0x04
#define AL_NAME_LEN     0x06
#define AL_NAME_OFFSET  0x07
#define AL_START_VCN    0x08
#define AL_REF          0x10
#define AL_MIN_LENGTH   0x1A

#define FN_PARENT       0x00
#define FN_CREATE       0x08
#define FN_MODIFY       0x10
#define FN_CHANGE       0x18
#define FN_ACCESS       0x20
#define FN_ALLOC_SIZE   0x28
#define FN_DATA_SIZE    0x30
#define FN_ATTRIBUTES   0x38
#define FN_NAME_LEN     0x40
#define FN_NAMESPACE    0x41
#define FN_NAME         0x42

#define VI_MAJOR        0x08
#define VI_MINOR        0x09
#define VI_FLAGS        0x0A

/* ---- §7 Indexes ---------------------------------------------------------------- */

#define IR_TYPE         0x00
#define IR_COLLATION    0x04
#define IR_BLOCK_SIZE   0x08
#define IR_HEADER       0x10

#define IH_FIRST_ENTRY  0x00
#define IH_ENTRIES_SIZE 0x04
#define IH_ALLOC_SIZE   0x08
#define IH_FLAGS        0x0C
#define IH_SIZE         0x10

#define IX_VCN          0x10
#define IX_HEADER       0x18

#define IE_REF          0x00
#define IE_LENGTH       0x08
#define IE_KEY_LENGTH   0x0A
#define IE_FLAGS        0x0C
#define IE_KEY          0x10
#define IE_FLAG_SUBNODE 0x0001u
#define IE_FLAG_LAST    0x0002u

#define COLLATION_FILENAME 0x01u

/* ---- Sanity limits for untrusted input ---------------------------------------- */

#define PN_MAX_INDEX_DEPTH      32
#define PN_MAX_EXT_RECORDS      4096    /* extension records per file */
#define PN_MAX_ATTR_LIST        (1u << 20)
#define PN_UPCASE_ENTRIES       65536u

/* ---- In-memory objects ------------------------------------------------------- */

struct pn_volume {
    pn_bdev   dev;
    pn_volume_info info;
    uint64_t  volume_bytes;
    pn_extent *mft_extents;
    uint32_t  mft_nextents;
    uint64_t  mft_data_size;
    uint16_t *upcase;
};

typedef struct pn_record {
    uint64_t number;
    uint8_t *buf;               /* fixups already removed */
} pn_record;

struct pn_file {
    pn_volume *vol;
    uint64_t   number;
    uint16_t   sequence;
    uint16_t   flags;
    uint16_t   link_count;
    pn_record *recs;            /* recs[0] is the base record */
    uint32_t   nrecs;
};

/* A validated view of one attribute inside a loaded record. */
typedef struct pn_rattr {
    const uint8_t *p;
    uint32_t len;
    uint32_t type;
    uint16_t flags;
    uint16_t id;
    int      nonresident;
    uint8_t  name_len;
    const uint16_t *name;       /* 2-byte aligned: odd offsets are rejected */
    uint64_t record;
    /* resident */
    const uint8_t *value;
    uint32_t value_len;
    /* non-resident */
    uint64_t start_vcn, last_vcn;
} pn_rattr;

struct pn_attr {
    pn_volume *vol;
    uint32_t   type;
    uint16_t   flags;
    int        nonresident;
    uint8_t   *value;           /* resident copy */
    uint64_t   data_size, alloc_size, init_size;
    uint32_t   cu_clusters;     /* compression unit, 0 = uncompressed */
    pn_extent *extents;
    uint32_t   nextents;
    /* one decompressed compression unit, cached */
    uint8_t   *cu_buf;
    uint64_t   cu_index;
    int        cu_valid;
};

/* volume.c */
int pn_dev_read(const pn_volume *vol, uint64_t offset, void *buf, size_t len);
int pn_extents_read(const pn_volume *vol, const pn_extent *ext, uint32_t n,
                    uint64_t offset, void *buf, size_t len);

/* record.c */
int pn_fixup(uint8_t *buf, size_t len);
int pn_record_load(pn_volume *vol, uint64_t number, uint8_t *buf);
int pn_record_next_attr(const pn_volume *vol, const uint8_t *rec, uint64_t number,
                        uint32_t *cursor, pn_rattr *out);

/* runlist.c */
int pn_runlist_decode(const pn_volume *vol, const pn_rattr *ra,
                      pn_extent **ext, uint32_t *n, uint32_t *cap);

/* attr.c */
int pn_attr_open_raw(pn_file *f, uint32_t type, const uint16_t *name, size_t name_len,
                     pn_attr **out);
int pn_file_find_rattr(pn_file *f, uint32_t type, const uint16_t *name, size_t name_len,
                       pn_rattr *out);
int pn_names_equal(const uint16_t *a, const uint16_t *b, size_t len);

/* upcase.c */
int pn_upcase_load(pn_volume *vol);

#endif /* PN_INTERNAL_H */
