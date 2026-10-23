#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <sys/stat.h>
#include <unistd.h>

#include "bdev_file.h"

static int file_read(void *ctx, uint64_t offset, size_t len, void *buf)
{
    int fd = (int)(intptr_t)ctx;
    uint8_t *p = buf;
    while (len) {
        ssize_t n = pread(fd, p, len, (off_t)offset);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        p += n;
        offset += (uint64_t)n;
        len -= (size_t)n;
    }
    return 0;
}

int pn_bdev_file_open(const char *path, pn_bdev *dev)
{
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return PN_EIO;
    struct stat st;
    if (fstat(fd, &st) != 0) {
        close(fd);
        return PN_EIO;
    }
    dev->read = file_read;
    dev->ctx = (void *)(intptr_t)fd;
    dev->size = (uint64_t)st.st_size;
    dev->sector_size = 512;
    return PN_OK;
}

void pn_bdev_file_close(pn_bdev *dev)
{
    close((int)(intptr_t)dev->ctx);
}
