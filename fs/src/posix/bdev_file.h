/* Image-file block device for tools and tests (POSIX). Not part of the core. */
#ifndef PN_BDEV_FILE_H
#define PN_BDEV_FILE_H

#include "pawntfs/pawntfs.h"

int  pn_bdev_file_open(const char *path, pn_bdev *dev);
void pn_bdev_file_close(pn_bdev *dev);

#endif
