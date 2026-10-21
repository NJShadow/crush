/* Crush's pinned zlib 1.3.1 (engine/crushz, unmodified upstream sources) for
 * WebAssembly. Exports the very crushz_deflate() that crushz.dll and
 * libcrushz.so export - extraction uses it to REBUILD original deflate streams
 * from (level, wbits, memLevel, strategy) - plus a bounded one-shot inflate
 * for everything Crush decompresses with zlib (the index when its codec is 2,
 * Echo headers, Loom metadata, container contents). */
#include <stdlib.h>
#include <string.h>
#include "crushz.c"

#define WEXPORT(name) __attribute__((export_name(name)))
WEXPORT("malloc") void *x_malloc(size_t n) { return malloc(n); }
WEXPORT("free") void x_free(void *p) { free(p); }
WEXPORT("crushz_deflate") int w_crushz_deflate(const unsigned char *in, size_t n,
        int level, int wbits, int memlevel, int strategy,
        unsigned char *out, size_t cap, size_t *outlen)
{ return crushz_deflate(in, n, level, wbits, memlevel, strategy, out, cap, outlen); }
WEXPORT("crushz_match") int w_crushz_match(const unsigned char *in, size_t n,
        const unsigned char *target, size_t tlen, int wbits, const int *params, int np)
{ return crushz_match(in, n, target, tlen, wbits, params, np); }
WEXPORT("crushz_version") const char *w_crushz_version(void) { return crushz_version(); }

/* Inflate one stream. wbits as zlib: -15..-8 raw, 8..15 zlib, 16+ gzip,
 * 32+ auto. Like Python's zlib.decompress: the stream must be complete;
 * bytes after its end are ignored (and counted in *consumed). */
WEXPORT("crushz_inflate")
int crushz_inflate(const unsigned char *in, size_t n_in, int wbits, size_t max_out,
              unsigned char **out, size_t *outlen, size_t *consumed)
{
    *out = NULL; *outlen = 0; *consumed = 0;
    z_stream s;
    memset(&s, 0, sizeof s);
    if (inflateInit2(&s, wbits) != Z_OK) return -1;
    size_t cap = n_in * 4 + 4096;
    if (cap > max_out) cap = max_out ? max_out : 1;
    unsigned char *buf = (unsigned char *)malloc(cap);
    if (!buf) { inflateEnd(&s); return -2; }
    size_t pos = 0, total = 0;
    int rc = 0;
    for (;;) {
        if (total == cap) {
            if (cap >= max_out) { rc = -3; break; }
            size_t ncap = cap * 2;
            if (ncap > max_out || ncap < cap) ncap = max_out;
            unsigned char *nb = (unsigned char *)realloc(buf, ncap);
            if (!nb) { rc = -2; break; }
            buf = nb; cap = ncap;
        }
        size_t ain = n_in - pos, aout = cap - total;
        if (ain > (1u << 30)) ain = 1u << 30;
        if (aout > (1u << 30)) aout = 1u << 30;
        s.next_in = (unsigned char *)in + pos; s.avail_in = (unsigned)ain;
        s.next_out = buf + total; s.avail_out = (unsigned)aout;
        int r = inflate(&s, Z_NO_FLUSH);
        pos += ain - s.avail_in;
        total += aout - s.avail_out;
        if (r == Z_STREAM_END) break;
        if (r == Z_BUF_ERROR && s.avail_out > 0 && pos == n_in) { rc = -5; break; }
        if (r != Z_OK && r != Z_BUF_ERROR) { rc = -4; break; }
    }
    inflateEnd(&s);
    if (rc) { free(buf); return rc; }
    *out = buf; *outlen = total; *consumed = pos;
    return 0;
}
