/* bzip2 decoding (bzip2 1.0.8, BSD-style licence) for WebAssembly. Matches
 * Python's bz2.decompress(): concatenated streams are decoded one after
 * another; a truncated stream is an error. */
#include <stdlib.h>
#include <string.h>
#include "bzlib.h"

#define EXPORT(name) __attribute__((export_name(name)))
EXPORT("malloc") void *x_malloc(size_t n) { return malloc(n); }
EXPORT("free") void x_free(void *p) { free(p); }

/* bzlib calls this on an internal consistency failure (BZ_NO_STDIO build) */
void bz_internal_error(int errcode) { (void)errcode; __builtin_trap(); }

EXPORT("bzip2_decode")
int bzip2_decode(const unsigned char *in, size_t n_in, size_t max_out,
                 unsigned char **out, size_t *outlen)
{
    *out = NULL; *outlen = 0;
    size_t cap = n_in * 5 + 65536;
    if (cap > max_out) cap = max_out ? max_out : 1;
    unsigned char *buf = (unsigned char *)malloc(cap);
    if (!buf) return -1;
    size_t pos = 0, total = 0;
    int rc = 0;
    while (pos < n_in && rc == 0) {          /* one stream per iteration */
        bz_stream s;
        memset(&s, 0, sizeof s);
        if (BZ2_bzDecompressInit(&s, 0, 0) != BZ_OK) { rc = -2; break; }
        for (;;) {
            if (total == cap) {
                if (cap >= max_out) { rc = -3; break; }
                size_t ncap = cap * 2;
                if (ncap > max_out || ncap < cap) ncap = max_out;
                unsigned char *nb = (unsigned char *)realloc(buf, ncap);
                if (!nb) { rc = -1; break; }
                buf = nb; cap = ncap;
            }
            size_t ain = n_in - pos, aout = cap - total;
            if (ain > 0x40000000u) ain = 0x40000000u;
            if (aout > 0x40000000u) aout = 0x40000000u;
            s.next_in = (char *)(in + pos); s.avail_in = (unsigned)ain;
            s.next_out = (char *)(buf + total); s.avail_out = (unsigned)aout;
            int r = BZ2_bzDecompress(&s);
            pos += ain - s.avail_in;
            total += aout - s.avail_out;
            if (r == BZ_STREAM_END) break;
            if (r != BZ_OK) { rc = -4; break; }
            if (s.avail_in == 0 && pos == n_in && s.avail_out > 0) { rc = -5; break; }
        }
        BZ2_bzDecompressEnd(&s);
    }
    if (rc) { free(buf); return rc; }
    *out = buf; *outlen = total;
    return 0;
}
