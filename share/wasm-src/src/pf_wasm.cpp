/* crushpf 'r' mode (preflate_reencode) for WebAssembly.
 *
 * Compiles the vendored preflate sources from engine/crushpf-src, CRUSH
 * PATCH hardening included, unchanged. pf_reencode() is the body of the 'r'
 * loop in crushpf.cpp for one record: correction record + unpacked data ->
 * the original deflate bytes. The 1 GB per-blob limit of the native helper
 * (MAX_REC) is kept.
 *
 * WebAssembly here has no C++ exceptions: where the native helper would
 * catch (e.g. std::bad_alloc) the module traps instead, and the JavaScript
 * side reports failure for that stream and discards the instance. */
#include <cstdlib>
#include <cstring>
#include <vector>
#include "preflate.h"

#define EXPORT(name) __attribute__((export_name(name)))
extern "C" {
EXPORT("malloc") void *x_malloc(size_t n) { return malloc(n); }
EXPORT("free") void x_free(void *p) { free(p); }

static const uint64_t MAX_REC = 1ull << 30;

EXPORT("pf_reencode")
int pf_reencode(const unsigned char *diff, size_t n_diff,
                const unsigned char *un, size_t n_un,
                unsigned char **out, size_t *outlen)
{
    *out = nullptr; *outlen = 0;
    if (n_diff > MAX_REC || n_un > MAX_REC) return -1;
    std::vector<unsigned char> d(diff, diff + n_diff), u(un, un + n_un), o;
    if (!preflate_reencode(o, d, u)) return -2;
    unsigned char *b = (unsigned char *)malloc(o.size() ? o.size() : 1);
    if (!b) return -3;
    if (!o.empty()) memcpy(b, o.data(), o.size());
    *out = b; *outlen = o.size();
    return 0;
}
}
