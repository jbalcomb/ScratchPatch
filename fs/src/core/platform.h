/*
 * The only place the core touches the C runtime. A kernel-mode build will
 * replace these with pool allocations (ExAllocatePool2 / ExFreePool) and the
 * kernel's RtlCopyMemory family.
 */
#ifndef PN_PLATFORM_H
#define PN_PLATFORM_H

#include <stdlib.h>
#include <string.h>

#define pn_malloc(n)        malloc(n)
#define pn_calloc(n, s)     calloc((n), (s))
#define pn_realloc(p, n)    realloc((p), (n))
#define pn_free(p)          free(p)

#endif /* PN_PLATFORM_H */
