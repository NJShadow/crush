/* Raw LZMA2 (+ x86 BCJ / delta) and PPMd variant H decoding for WebAssembly,
 * from the LZMA SDK C sources (7-Zip 26.04, public domain).
 *
 * LZMA2: Crush stores raw LZMA2 streams (Python lzma FORMAT_RAW) with one of
 * four filter chains: plain, [x86, lzma2], [delta dist=4, lzma2], and
 * lc=4/lp=0/pb=0 (the "text" variant - lc/lp/pb travel inside the LZMA2 chunk
 * headers, so it needs nothing special on decode). The decoder writes straight
 * into one growing output buffer that doubles as the dictionary, so memory is
 * the output size, not the 64 MB dictionary. The dictionary size is still
 * enforced exactly as liblzma does: a match may not reach further back than
 * dict_size bytes.
 *
 * Like Python's lzma.decompress, concatenated raw streams are decoded one
 * after another, and a stream without its end marker is an error.
 *
 * PPMd: Ppmd7 (PPMdH) with the 7z range coder, as pyppmd's Ppmd7Decoder.
 * Crush prefixes the stream with the decoded length (u64 LE); the caller
 * passes it in.
 */
#include <stdlib.h>
#include <string.h>
#include "Precomp.h"
#include "Lzma2Dec.h"
#include "Bra.h"
#include "Delta.h"
#include "Ppmd7.h"

#define EXPORT(name) __attribute__((export_name(name)))

EXPORT("malloc") void *x_malloc(size_t n) { return malloc(n); }
EXPORT("free") void x_free(void *p) { free(p); }

static void *SzAlloc(ISzAllocPtr p, size_t size) { (void)p; return malloc(size); }
static void SzFree(ISzAllocPtr p, void *a) { (void)p; free(a); }
static const ISzAlloc g_Alloc = { SzAlloc, SzFree };

/* smallest LZMA2 dictionary property byte whose size is >= dict */
static Byte lzma2_prop(UInt32 dict) {
    for (Byte p = 0; p < 40; ++p) {
        UInt32 d = (2u | (p & 1u)) << (p / 2 + 11);
        if (d >= dict) return p;
    }
    return 40;
}

/* filter: 0 none, 1 x86 BCJ, 2 delta (dist = delta_dist)
 * returns 0 ok, <0 error; *out malloc'd */
EXPORT("lzma2_decode")
int lzma2_decode(const Byte *in, size_t n_in, UInt32 dict_size, int filter,
                 unsigned delta_dist, size_t max_out, Byte **out, size_t *outlen)
{
    *out = NULL; *outlen = 0;
    if (filter == 2 && (delta_dist < 1 || delta_dist > 256)) return -10;
    size_t cap = n_in * 4 + 4096;
    if (cap > max_out) cap = max_out ? max_out : 1;
    Byte *buf = (Byte *)malloc(cap);
    if (!buf) return -11;
    size_t total = 0, pos = 0;
    int rc = 0;
    while (pos < n_in) {                     /* one raw stream per iteration */
        CLzma2Dec dec;
        Lzma2Dec_Construct(&dec);
        if (Lzma2Dec_AllocateProbs(&dec, lzma2_prop(dict_size), &g_Alloc) != SZ_OK) { rc = -12; break; }
        /* exact liblzma semantics: dictionary is dict_size, not the rounded
           property size */
        dec.decoder.prop.dicSize = dict_size;
        Lzma2Dec_Init(&dec);
        Byte *base = buf + total;            /* this stream's output starts here */
        dec.decoder.dic = base;
        dec.decoder.dicBufSize = cap - total;
        dec.decoder.dicPos = 0;
        int done = 0, full = 0;
        for (;;) {
            if (dec.decoder.dicPos == dec.decoder.dicBufSize) {
                size_t ncap = cap * 2;
                if (ncap > max_out || ncap < cap) ncap = max_out;
                if (ncap > cap) {
                    Byte *nb = (Byte *)realloc(buf, ncap);
                    if (!nb) { rc = -11; break; }
                    buf = nb; cap = ncap; base = buf + total;
                    dec.decoder.dic = base;
                    dec.decoder.dicBufSize = cap - total;
                } else {
                    full = 1;    /* at max_out: only the end marker may follow */
                }
            }
            SizeT srcLen = n_in - pos;
            ELzmaStatus st;
            SRes r = Lzma2Dec_DecodeToDic(&dec, dec.decoder.dicBufSize, in + pos,
                                          &srcLen, LZMA_FINISH_ANY, &st);
            pos += srcLen;
            if (r != SZ_OK) { rc = -14; break; }
            if (st == LZMA_STATUS_FINISHED_WITH_MARK) { done = 1; break; }
            if (full) { rc = -13; break; }          /* output limit exceeded */
            if (srcLen == 0 && dec.decoder.dicPos < dec.decoder.dicBufSize) {
                rc = -15;                    /* input ended before end marker */
                break;
            }
        }
        size_t got = dec.decoder.dicPos;
        Lzma2Dec_FreeProbs(&dec, &g_Alloc);
        if (rc) break;
        if (!done) { rc = -15; break; }
        /* filters run per stream, as in a fresh liblzma decoder per stream */
        if (filter == 1) {
            UInt32 state = Z7_BRANCH_CONV_ST_X86_STATE_INIT_VAL;
            z7_BranchConvSt_X86_Dec(base, got, 0, &state);
        } else if (filter == 2) {
            Byte st[DELTA_STATE_SIZE];
            Delta_Init(st);
            Delta_Decode(st, delta_dist, base, got);
        }
        total += got;
    }
    if (rc) { free(buf); return rc; }
    *out = buf; *outlen = total;
    return 0;
}

/* ------------------------------------------------------------------ PPMd */
typedef struct { IByteIn vt; const Byte *p, *end; } MemIn;
static Byte MemIn_Read(IByteInPtr pp) {
    MemIn *m = (MemIn *)(void *)pp;
    return m->p < m->end ? *m->p++ : 0;
}

/* n = decoded length (from Crush's u64 prefix); order 2..64; mem bytes */
EXPORT("ppmd7_decode")
int ppmd7_decode(const Byte *in, size_t n_in, size_t n, unsigned order,
                 UInt32 mem, size_t max_out, Byte **out, size_t *outlen)
{
    *out = NULL; *outlen = 0;
    if (n > max_out) return -1;
    if (order < PPMD7_MIN_ORDER || order > PPMD7_MAX_ORDER) return -2;
    if (mem < PPMD7_MIN_MEM_SIZE || mem > PPMD7_MAX_MEM_SIZE) return -3;
    Byte *buf = (Byte *)malloc(n ? n : 1);
    if (!buf) return -4;
    CPpmd7 p;
    Ppmd7_Construct(&p);
    if (!Ppmd7_Alloc(&p, mem, &g_Alloc)) { free(buf); return -5; }
    MemIn s; s.vt.Read = MemIn_Read; s.p = in; s.end = in + n_in;
    p.rc.dec.Stream = &s.vt;
    Ppmd7_Init(&p, order);
    if (!Ppmd7z_RangeDec_Init(&p.rc.dec)) { Ppmd7_Free(&p, &g_Alloc); free(buf); return -6; }
    for (size_t i = 0; i < n; ++i) {
        int sym = Ppmd7z_DecodeSymbol(&p);
        if (sym < 0) { Ppmd7_Free(&p, &g_Alloc); free(buf); return -7; }
        buf[i] = (Byte)sym;
    }
    Ppmd7_Free(&p, &g_Alloc);
    *out = buf; *outlen = n;
    return 0;
}
