/* Zstandard decompression (zstd 1.5.6, BSD licence) for WebAssembly.
 *
 * Matches python-zstandard's ZstdDecompressor().decompress(b, max_output_size)
 * as Crush uses it: one frame, optionally with a shared dictionary (the
 * priors/NAME.zdict files). Frames whose content size is in the header (what
 * python-zstandard writes) decode straight into an exactly sized buffer with
 * no window allocation; frames without it are streamed with the window capped
 * at 2^27 (Crush's largest, window_log=27), so a hostile frame cannot ask for
 * more. Trailing data after the first frame is ignored, as python-zstandard
 * does by default.
 */
#include <stdlib.h>
#include <string.h>
#define ZSTD_STATIC_LINKING_ONLY
#include "zstd.h"

#define EXPORT(name) __attribute__((export_name(name)))

EXPORT("malloc") void *x_malloc(size_t n) { return malloc(n); }
EXPORT("free") void x_free(void *p) { free(p); }

EXPORT("zstd_decode")
int zstd_decode(const unsigned char *in, size_t n_in,
                const unsigned char *dict, size_t n_dict,
                size_t max_out, unsigned char **out, size_t *outlen)
{
    *out = NULL; *outlen = 0;
    ZSTD_DCtx *dctx = ZSTD_createDCtx();
    if (!dctx) return -1;
    if (n_dict && ZSTD_isError(ZSTD_DCtx_loadDictionary(dctx, dict, n_dict))) {
        ZSTD_freeDCtx(dctx); return -2;
    }
    size_t fsz = ZSTD_findFrameCompressedSize(in, n_in);
    if (ZSTD_isError(fsz)) { ZSTD_freeDCtx(dctx); return -3; }
    unsigned long long cs = ZSTD_getFrameContentSize(in, fsz);
    if (cs == ZSTD_CONTENTSIZE_ERROR) { ZSTD_freeDCtx(dctx); return -3; }
    if (cs != ZSTD_CONTENTSIZE_UNKNOWN) {
        if (cs > (unsigned long long)max_out) { ZSTD_freeDCtx(dctx); return -4; }
        unsigned char *buf = (unsigned char *)malloc(cs ? (size_t)cs : 1);
        if (!buf) { ZSTD_freeDCtx(dctx); return -5; }
        size_t r = ZSTD_decompressDCtx(dctx, buf, (size_t)cs, in, fsz);
        ZSTD_freeDCtx(dctx);
        if (ZSTD_isError(r) || r != cs) { free(buf); return -6; }
        *out = buf; *outlen = (size_t)cs;
        return 0;
    }
    /* content size unknown: stream, growing the output */
    ZSTD_DCtx_setParameter(dctx, ZSTD_d_windowLogMax, 27);
    size_t cap = n_in * 4 + 65536;
    if (cap > max_out) cap = max_out ? max_out : 1;
    unsigned char *buf = (unsigned char *)malloc(cap);
    if (!buf) { ZSTD_freeDCtx(dctx); return -5; }
    ZSTD_inBuffer ib = { in, fsz, 0 };
    ZSTD_outBuffer ob = { buf, cap, 0 };
    int rc = 0;
    for (;;) {
        size_t r = ZSTD_decompressStream(dctx, &ob, &ib);
        if (ZSTD_isError(r)) { rc = -6; break; }
        if (r == 0) break;                         /* frame complete */
        if (ob.pos == ob.size) {
            if (cap >= max_out) { rc = -4; break; }
            size_t ncap = cap * 2;
            if (ncap > max_out || ncap < cap) ncap = max_out;
            unsigned char *nb = (unsigned char *)realloc(buf, ncap);
            if (!nb) { rc = -5; break; }
            buf = nb; cap = ncap; ob.dst = buf; ob.size = cap;
        } else if (ib.pos == ib.size) { rc = -7; break; }  /* truncated */
    }
    ZSTD_freeDCtx(dctx);
    if (rc) { free(buf); return rc; }
    *out = buf; *outlen = ob.pos;
    return 0;
}
