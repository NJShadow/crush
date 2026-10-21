/* ==========================================================================
 *  crushz - Crush's pinned copy of zlib's deflate
 *  --------------------------------------------------------------------------
 *  WHY THIS EXISTS
 *  Several Crush transforms (deflate_unwrap for zip/docx, png_unwrap, and the
 *  PDF and gzip unwrappers) store DECOMPRESSED data plus the deflate settings
 *  that reproduce the original compressed bytes exactly. Extraction re-runs
 *  deflate with those settings. That only works if the deflate on the
 *  extracting machine is bit-for-bit the same program as the one that proved
 *  the settings at compression time.
 *
 *  Python's built-in zlib module does not promise that. Python 3.14 on Windows
 *  replaced zlib with zlib-ng, which produces different bytes for the same
 *  level/strategy/memLevel (measured: 11 of 15 settings differ on a 216 KB
 *  source file). An archive written under 3.13 would then fail its checksum
 *  on extraction under 3.14. So Crush carries its own deflate: unmodified
 *  zlib 1.3.1 sources (see zlib/LICENSE), compiled with a symbol prefix so it
 *  can never be confused with any other zlib loaded into the same process.
 *
 *  API (all exported with default visibility, everything else hidden):
 *    crushz_version()              "1.3.1"
 *    crushz_deflate(...)           one-shot deflate with explicit settings
 *    crushz_match(...)             find which of N settings reproduces a
 *                                  target stream, aborting each candidate at
 *                                  the first byte that differs
 *
 *  Build (Linux):   see build.sh
 *  Build (Windows): x86_64-w64-mingw32-gcc, same flags; see build.sh
 * ========================================================================== */
#include <string.h>
#include <stdlib.h>
#include "zlib/zlib.h"

#if defined(_WIN32)
#  define EXPORT __declspec(dllexport)
#else
#  define EXPORT __attribute__((visibility("default")))
#endif

EXPORT const char *crushz_version(void) { return ZLIB_VERSION; }

/* Returns 0 on success, 1 if `cap` was too small, negative on a zlib error.
 * *outlen receives the number of bytes written (or needed so far). */
EXPORT int crushz_deflate(const unsigned char *in, size_t n,
                          int level, int wbits, int memlevel, int strategy,
                          unsigned char *out, size_t cap, size_t *outlen)
{
    z_stream s;
    int rc;
    memset(&s, 0, sizeof s);
    *outlen = 0;
    rc = deflateInit2(&s, level, Z_DEFLATED, wbits, memlevel, strategy);
    if (rc != Z_OK) return -100 + rc;
    /* feed in bounded pieces: avail_in / avail_out are 32-bit */
    size_t in_pos = 0, out_pos = 0;
    for (;;) {
        size_t in_chunk = n - in_pos;
        if (in_chunk > (1u << 30)) in_chunk = 1u << 30;
        size_t out_chunk = cap - out_pos;
        if (out_chunk > (1u << 30)) out_chunk = 1u << 30;
        s.next_in = (unsigned char *)in + in_pos;
        s.avail_in = (unsigned)in_chunk;
        s.next_out = out + out_pos;
        s.avail_out = (unsigned)out_chunk;
        int flush = (in_pos + in_chunk == n) ? Z_FINISH : Z_NO_FLUSH;
        rc = deflate(&s, flush);
        in_pos += in_chunk - s.avail_in;
        out_pos += out_chunk - s.avail_out;
        if (rc == Z_STREAM_END) break;
        if (rc != Z_OK && rc != Z_BUF_ERROR) { deflateEnd(&s); return -200 + rc; }
        if (out_pos == cap) { deflateEnd(&s); *outlen = out_pos; return 1; }
    }
    deflateEnd(&s);
    *outlen = out_pos;
    return 0;
}

/* Try each candidate setting in order; return the index of the first one
 * whose deflate output equals `target` exactly, or -1.
 * params is a flat array of 3*np ints: (level, memlevel, strategy).
 * Each candidate is compressed incrementally and abandoned at the first
 * output byte that differs, so a wrong guess usually costs a few KB of work
 * rather than a whole stream. */
EXPORT int crushz_match(const unsigned char *in, size_t n,
                        const unsigned char *target, size_t tlen, int wbits,
                        const int *params, int np)
{
    enum { IN_STEP = 1 << 14, OUT_BUF = 1 << 15 };
    unsigned char buf[OUT_BUF];
    for (int k = 0; k < np; k++) {
        z_stream s;
        memset(&s, 0, sizeof s);
        if (deflateInit2(&s, params[3*k], Z_DEFLATED, wbits,
                         params[3*k+1], params[3*k+2]) != Z_OK)
            continue;
        size_t in_pos = 0, t_pos = 0;
        int ok = 1, done = 0;
        while (ok && !done) {
            size_t chunk = n - in_pos;
            if (chunk > IN_STEP) chunk = IN_STEP;
            s.next_in = (unsigned char *)in + in_pos;
            s.avail_in = (unsigned)chunk;
            in_pos += chunk;
            int flush = (in_pos == n) ? Z_FINISH : Z_NO_FLUSH;
            do {
                s.next_out = buf;
                s.avail_out = OUT_BUF;
                int rc = deflate(&s, flush);
                if (rc != Z_OK && rc != Z_STREAM_END && rc != Z_BUF_ERROR) { ok = 0; break; }
                size_t got = OUT_BUF - s.avail_out;
                if (got) {
                    if (t_pos + got > tlen || memcmp(buf, target + t_pos, got) != 0) { ok = 0; break; }
                    t_pos += got;
                }
                if (rc == Z_STREAM_END) { done = 1; break; }
            } while (s.avail_out == 0 || (flush == Z_FINISH));
        }
        deflateEnd(&s);
        if (ok && done && t_pos == tlen) return k;
    }
    return -1;
}
