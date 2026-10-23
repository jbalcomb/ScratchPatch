/* Path resolution from the root directory. */
#include "internal.h"

static int is_sep(uint16_t c) { return c == '\\' || c == '/'; }

int pn_file_open_path(pn_volume *vol, const uint16_t *path, size_t len, pn_file **out)
{
    if (!vol || !out || (len && !path))
        return PN_EINVAL;
    pn_file *cur = NULL;
    int rc = pn_file_open(vol, PN_RECORD_ROOT, &cur);
    size_t i = 0;
    while (rc == PN_OK) {
        while (i < len && is_sep(path[i]))
            i++;
        if (i == len)
            break;
        size_t start = i;
        while (i < len && !is_sep(path[i]))
            i++;

        uint64_t ref;
        if (!(cur->flags & FR_FLAG_DIRECTORY))
            rc = PN_ENOTDIR;
        else if (i - start > 255)
            rc = PN_ENOENT;
        else
            rc = pn_dir_lookup(cur, path + start, i - start, &ref);
        if (rc)
            break;
        pn_file *next;
        rc = pn_file_open_ref(vol, ref, &next);
        if (rc == PN_ESTALE)
            rc = PN_ECORRUPT;   /* the index points at a reused record */
        if (rc)
            break;
        pn_file_close(cur);
        cur = next;
    }
    if (rc) {
        pn_file_close(cur);
        return rc;
    }
    *out = cur;
    return PN_OK;
}
