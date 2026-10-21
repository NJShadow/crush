/* crushcm decoder for WebAssembly.
 *
 * Compiles the UNMODIFIED engine/crushcm.c (vendor/crushcm/crushcm.c) with
 * its stdio replaced by in-memory buffers, and replaces its main() by
 * cm_decode() below, which is the 'd' branch of main() line for line plus
 * the length bounds a hostile archive needs:
 *
 *   - the 64-bit length in the header must be <= max_out (the caller's limit)
 *     and n + prime_len must fit the 32-bit counters crushcm keeps internally.
 *     (The native main() truncates n + prime_len to 32 bits before sizing its
 *     history buffer, then writes n bytes into it: a crafted header with
 *     n >= 2^32 overflows the heap in the native helper. Reported in REPORT.md.)
 *   - reading past the end of the input yields zero bytes, exactly as the
 *     native getc() returning EOF does, so truncated streams decode the same.
 *
 * crushcm keeps all of its state in static globals and never frees it, so
 * one instance of this module decodes exactly one stream; the JavaScript side
 * instantiates a fresh instance per call (memory is released with it).
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

static const unsigned char *cm_in; static size_t cm_in_len, cm_in_pos;
static unsigned char *cm_out; static size_t cm_out_pos;

static inline int cm_getc_(void) {
    return cm_in_pos < cm_in_len ? cm_in[cm_in_pos++] : EOF;
}
static inline int cm_putc_(int c) { cm_out[cm_out_pos++] = (unsigned char)c; return c; }

#undef getc
#undef putc
#define getc(f) cm_getc_()
#define putc(c, f) cm_putc_(c)
#define main crushcm_native_main
#include "crushcm.c"
#undef main
#undef getc
#undef putc

#define EXPORT(name) __attribute__((export_name(name)))

EXPORT("malloc") void *x_malloc(size_t n) { return malloc(n); }
EXPORT("free") void x_free(void *p) { free(p); }

/* Returns 0 and sets out and outlen on success; negative on refusal.
 * prime may be NULL/0 for the unprimed codec. */
EXPORT("cm_decode")
int cm_decode(const unsigned char *in, size_t n_in,
              const unsigned char *prime, size_t n_prime,
              size_t max_out, unsigned char **out, size_t *outlen)
{
    *out = NULL; *outlen = 0;
    if (n_in < 12) return -1;
    if (in[0] != 'C' || in[1] != 'C' || in[2] != 'M' || in[3] != '1') return -2;
    U64 n = 0;
    for (int i = 0; i < 8; ++i) n |= ((U64)in[4 + i]) << (8 * i);
    if (n > (U64)max_out) return -3;
    if (n + (U64)n_prime >= ((U64)1 << 31)) return -4;
    cm_in = in; cm_in_len = n_in; cm_in_pos = 12;
    cm_out = (unsigned char *)malloc((size_t)n + 1);
    if (!cm_out) return -5;
    cm_out_pos = 0;
    prime_buf = (U8 *)prime; prime_len = (long)n_prime;
    models_init((U32)(n + prime_len));
    update_contexts();
    if (prime_len) prime_run();
    for (int i = 0; i < 4; ++i) { int c = cm_getc_(); xdec = (xdec << 8) | (U32)(c == EOF ? 0 : c); }
    for (U64 i = 0; i < n; ++i) {
        int byte = 0;
        for (int b = 0; b < 8; ++b) {
            int p = predict();
            int bit = dec_bit(p);
            update(bit);
            byte = (byte << 1) | bit;
        }
        cm_putc_(byte);
    }
    *out = cm_out; *outlen = (size_t)n;
    return 0;
}
