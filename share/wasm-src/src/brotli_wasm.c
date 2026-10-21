/* Brotli decoding (Google brotli, MIT licence; the copy vendored in
 * engine/crushjpg-src/brotli) for WebAssembly. Matches Python's
 * brotli.decompress(): one stream, trailing bytes are an error. */
#include <stdlib.h>
#include <brotli/decode.h>

#define EXPORT(name) __attribute__((export_name(name)))
EXPORT("malloc") void *x_malloc(size_t n) { return malloc(n); }
EXPORT("free") void x_free(void *p) { free(p); }

EXPORT("brotli_decode")
int brotli_decode(const unsigned char *in, size_t n_in, size_t max_out,
                  unsigned char **out, size_t *outlen)
{
    *out = NULL; *outlen = 0;
    BrotliDecoderState *s = BrotliDecoderCreateInstance(NULL, NULL, NULL);
    if (!s) return -1;
    size_t cap = n_in * 4 + 65536;
    if (cap > max_out) cap = max_out ? max_out : 1;
    unsigned char *buf = (unsigned char *)malloc(cap);
    if (!buf) { BrotliDecoderDestroyInstance(s); return -2; }
    size_t avail_in = n_in, total = 0;
    const unsigned char *next_in = in;
    int rc = 0;
    for (;;) {
        size_t avail_out = cap - total;
        unsigned char *next_out = buf + total;
        BrotliDecoderResult r = BrotliDecoderDecompressStream(
            s, &avail_in, &next_in, &avail_out, &next_out, NULL);
        total = (size_t)(next_out - buf);
        if (r == BROTLI_DECODER_RESULT_SUCCESS) {
            if (avail_in) rc = -3;            /* trailing garbage */
            break;
        }
        if (r == BROTLI_DECODER_RESULT_NEEDS_MORE_OUTPUT) {
            if (cap >= max_out) { rc = -4; break; }
            size_t ncap = cap * 2;
            if (ncap > max_out || ncap < cap) ncap = max_out;
            unsigned char *nb = (unsigned char *)realloc(buf, ncap);
            if (!nb) { rc = -2; break; }
            buf = nb; cap = ncap;
            continue;
        }
        rc = (r == BROTLI_DECODER_RESULT_NEEDS_MORE_INPUT) ? -5 : -6;
        break;
    }
    BrotliDecoderDestroyInstance(s);
    if (rc) { free(buf); return rc; }
    *out = buf; *outlen = total;
    return 0;
}
