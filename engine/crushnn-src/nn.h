/* ============================================================================
   nn.h - integer-only inference for Crush's byte-level transformer
   ----------------------------------------------------------------------------
   The model is trained in floating point (train.py) and quantized ONCE by
   export.py into a file of integers. Everything here is integer arithmetic:
   int8 weights, int16 activations, int32/int64 accumulators, an integer square
   root and a 256-entry table for 2^-x. There is no float, no libm, and no
   operation whose result depends on the compiler, the CPU or the build flags
   (signed right shifts are arithmetic on every compiler Crush is built with;
   integer division is only ever applied to non-negative operands). So the
   encoder and decoder produce the same probabilities on every machine.

   Architecture: byte embedding -> L pre-norm blocks (multi-head attention with
   ALiBi relative-position bias over a sliding window of W bytes, then a ReLU
   MLP) -> RMSNorm -> 256 logits. Sliding-window attention uses a KV ring
   buffer, so each byte costs a constant amount of work.

   Usage:  nn_load(&m, path)       validate + load (every size bounded)
           nn_reset(&m)            start of a new stream
           nn_step(&m, byte)       consume one byte; m.cum[] then holds the
                                   cumulative (unnormalised) distribution of
                                   the NEXT byte
           nn_p1(&m, c0, bitpos)   12-bit P(next bit = 1) given partial byte
   ==========================================================================*/
#ifndef CRUSH_NN_H
#define CRUSH_NN_H
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "exp2tab.h"

#define NN_ACT_MAX 16383          /* int16 activation clamp (Q.8 => |x|<64)  */
#define NN_RES_MAX (1 << 22)      /* residual stream clamp                   */
#define NN_MAXDH 256              /* largest head dimension accepted         */

typedef struct { int rows, cols; int16_t *w; int32_t *m; } NNLin;

typedef struct {
    int d, L, H, ff, W, dh;
    int16_t *emb;
    NNLin *qkv, *o, *f1, *f2, out;
    int32_t *alibi;
    /* state */
    int16_t *kc, *vc;             /* [L][W][d] ring buffers                  */
    int qkmax, eshift;            /* bounds that keep attention in int32     */
    long t;                       /* bytes consumed                          */
    int32_t *r, *tmp;
    int16_t *n, *hbuf, *att, *q;
    int32_t *sc, *ex;
    uint32_t cum[257];
} NN;

static int nn_clamp16(int64_t v) {
    if (v > NN_ACT_MAX) return NN_ACT_MAX;
    if (v < -NN_ACT_MAX) return -NN_ACT_MAX;
    return (int)v;
}

/* 2^(-x/256) in Q.15 for x >= 0 */
static inline int32_t nn_exp2neg(int64_t x) {
    if (x < 0) x = 0;
    int64_t ip = x >> 8;
    if (ip > 15) return 0;
    return nn_exp2_tab[x & 255] >> ip;
}

static uint64_t nn_isqrt(uint64_t v) {
    uint64_t r = 0, b = (uint64_t)1 << 62;
    while (b > v) b >>= 2;
    while (b) {
        if (v >= r + b) { v -= r + b; r = (r >> 1) + b; }
        else r >>= 1;
        b >>= 2;
    }
    return r;
}

/* ---------------------------------------------------------------- load --- */
static int nn_rd(FILE *f, void *p, size_t n) { return fread(p, 1, n, f) == n; }

static int nn_lin_load(FILE *f, NNLin *l, int rows, int cols) {
    l->rows = rows; l->cols = cols;
    size_t n = (size_t)rows * cols;
    int8_t *w8 = (int8_t *)malloc(n);
    l->w = (int16_t *)malloc(n * sizeof(int16_t));
    l->m = (int32_t *)malloc((size_t)rows * sizeof(int32_t));
    if (!w8 || !l->w || !l->m) return 0;
    if (!nn_rd(f, w8, n)) { free(w8); return 0; }
    for (size_t i = 0; i < n; ++i) l->w[i] = w8[i];
    free(w8);
    uint8_t b[4];
    for (int i = 0; i < rows; ++i) {
        if (!nn_rd(f, b, 4)) return 0;
        l->m[i] = (int32_t)((uint32_t)b[0] | (uint32_t)b[1] << 8 | (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24);
        if (l->m[i] < 0) return 0;
    }
    return 1;
}

static int nn_load(NN *m, const char *path) {
    memset(m, 0, sizeof(*m));
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    uint8_t hd[24];
    if (!nn_rd(f, hd, 24) || memcmp(hd, "CNN1", 4) != 0) { fclose(f); return 0; }
    uint32_t v[5];
    for (int i = 0; i < 5; ++i)
        v[i] = (uint32_t)hd[4 + 4 * i] | (uint32_t)hd[5 + 4 * i] << 8 |
               (uint32_t)hd[6 + 4 * i] << 16 | (uint32_t)hd[7 + 4 * i] << 24;
    m->d = (int)v[0]; m->L = (int)v[1]; m->H = (int)v[2]; m->ff = (int)v[3]; m->W = (int)v[4];
    /* bound everything: a hostile or corrupt model file must not make us
       allocate wildly or overflow an int32 accumulator (fan-in <= 1024 keeps
       1024 * 16383 * 127 < 2^31) */
    if (m->d < 8 || m->d > 1024 || m->L < 1 || m->L > 16 || m->H < 1 || m->H > 16 ||
        m->d % m->H || m->ff < 8 || m->ff > 1024 || m->W < 1 || m->W > 4096) { fclose(f); return 0; }
    m->dh = m->d / m->H;
    m->qkmax = (int)nn_isqrt((uint64_t)2147483647 / (uint64_t)m->dh);
    if (m->qkmax > NN_ACT_MAX) m->qkmax = NN_ACT_MAX;
    { int es = 0; while (((int64_t)m->W * (32768 >> es) * NN_ACT_MAX) > 2147483647LL) ++es; m->eshift = es; }
    if (m->dh > NN_MAXDH) { fclose(f); return 0; }
    int d = m->d;
    m->emb = (int16_t *)malloc(sizeof(int16_t) * 256 * d);
    if (!m->emb) { fclose(f); return 0; }
    for (int i = 0; i < 256 * d; ++i) {
        uint8_t b[2];
        if (!nn_rd(f, b, 2)) { fclose(f); return 0; }
        m->emb[i] = (int16_t)(uint16_t)(b[0] | b[1] << 8);
    }
    m->qkv = (NNLin *)calloc(m->L, sizeof(NNLin)); m->o = (NNLin *)calloc(m->L, sizeof(NNLin));
    m->f1 = (NNLin *)calloc(m->L, sizeof(NNLin)); m->f2 = (NNLin *)calloc(m->L, sizeof(NNLin));
    for (int l = 0; l < m->L; ++l) {
        if (!nn_lin_load(f, &m->qkv[l], 3 * d, d) || !nn_lin_load(f, &m->o[l], d, d) ||
            !nn_lin_load(f, &m->f1[l], m->ff, d) || !nn_lin_load(f, &m->f2[l], d, m->ff)) { fclose(f); return 0; }
    }
    if (!nn_lin_load(f, &m->out, 256, d)) { fclose(f); return 0; }
    m->alibi = (int32_t *)malloc(sizeof(int32_t) * m->H);
    for (int h = 0; h < m->H; ++h) {
        uint8_t b[4];
        if (!nn_rd(f, b, 4)) { fclose(f); return 0; }
        m->alibi[h] = (int32_t)((uint32_t)b[0] | (uint32_t)b[1] << 8 | (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24);
        if (m->alibi[h] < 0 || m->alibi[h] > (1 << 20)) { fclose(f); return 0; }
    }
    { uint8_t extra; if (fread(&extra, 1, 1, f) != 0) { fclose(f); return 0; } }  /* exact size */
    fclose(f);
    size_t ring = (size_t)m->L * m->W * d;
    m->kc = (int16_t *)calloc(ring, sizeof(int16_t));
    m->vc = (int16_t *)calloc(ring, sizeof(int16_t));
    int big = 3 * d > m->ff ? 3 * d : m->ff;
    if (big < 256) big = 256;
    m->r = (int32_t *)calloc(d, sizeof(int32_t));
    m->tmp = (int32_t *)calloc(big, sizeof(int32_t));
    m->n = (int16_t *)calloc(d, sizeof(int16_t));
    m->hbuf = (int16_t *)calloc(m->ff, sizeof(int16_t));
    m->att = (int16_t *)calloc(d, sizeof(int16_t));
    m->q = (int16_t *)calloc(3 * d, sizeof(int16_t));
    m->sc = (int32_t *)calloc(m->W, sizeof(int32_t));
    m->ex = (int32_t *)calloc(m->W > 256 ? m->W : 256, sizeof(int32_t));
    if (!m->kc || !m->vc || !m->r || !m->tmp || !m->n || !m->hbuf || !m->att || !m->q || !m->sc || !m->ex) return 0;
    return 1;
}

/* uniform distribution before the first byte */
static void nn_reset(NN *m) {
    m->t = 0;
    for (int i = 0; i <= 256; ++i) m->cum[i] = (uint32_t)i * 128u;
}

/* -------------------------------------------------------------- kernels --- */
static void nn_lin(const NNLin *l, const int16_t *in, int32_t *out) {
    const int cols = l->cols;
    for (int r = 0; r < l->rows; ++r) {
        const int16_t *w = l->w + (size_t)r * cols;
        int32_t acc = 0;
        for (int c = 0; c < cols; ++c) acc += (int32_t)w[c] * (int32_t)in[c];
        out[r] = (int32_t)(((int64_t)acc * l->m[r]) >> 20);
    }
}

static void nn_norm(const NN *m, const int32_t *r, int16_t *n) {
    int64_t ss = 0;
    for (int i = 0; i < m->d; ++i) ss += (int64_t)r[i] * r[i];
    uint64_t ms = ((uint64_t)ss << 8) / (uint64_t)m->d;
    int64_t rms12 = (int64_t)nn_isqrt(ms);
    if (rms12 < 1) rms12 = 1;
    int64_t inv = ((int64_t)1 << 40) / rms12;
    for (int i = 0; i < m->d; ++i) n[i] = (int16_t)nn_clamp16(((int64_t)r[i] * inv) >> 28);
}

static void nn_resadd(NN *m, const int32_t *x) {
    for (int i = 0; i < m->d; ++i) {
        int64_t v = (int64_t)m->r[i] + x[i];
        if (v > NN_RES_MAX) v = NN_RES_MAX;
        if (v < -NN_RES_MAX) v = -NN_RES_MAX;
        m->r[i] = (int32_t)v;
    }
}

/* consume one byte, leave the next-byte distribution in m->cum */
static void nn_step(NN *m, int byte) {
    const int d = m->d, dh = m->dh, W = m->W;
    for (int i = 0; i < d; ++i) m->r[i] = m->emb[(size_t)byte * d + i];
    const int slot = (int)(m->t % W);
    const int cnt = m->t + 1 < W ? (int)(m->t + 1) : W;
    for (int l = 0; l < m->L; ++l) {
        int16_t *kc = m->kc + (size_t)l * W * d, *vc = m->vc + (size_t)l * W * d;
        nn_norm(m, m->r, m->n);
        nn_lin(&m->qkv[l], m->n, m->tmp);
        for (int i = 0; i < 2 * d; ++i) {          /* q, k: bounded so q.k fits int32 */
            int v = nn_clamp16(m->tmp[i]);
            if (v > m->qkmax) v = m->qkmax;
            if (v < -m->qkmax) v = -m->qkmax;
            m->q[i] = (int16_t)v;
        }
        for (int i = 2 * d; i < 3 * d; ++i) m->q[i] = (int16_t)nn_clamp16(m->tmp[i]);
        memcpy(kc + (size_t)slot * d, m->q + d, sizeof(int16_t) * d);
        memcpy(vc + (size_t)slot * d, m->q + 2 * d, sizeof(int16_t) * d);
        for (int h = 0; h < m->H; ++h) {
            const int16_t *qh = m->q + h * dh;
            int32_t mx = INT32_MIN;
            for (int j = 0; j < cnt; ++j) {              /* j = distance back */
                int s2 = slot - j; if (s2 < 0) s2 += W;
                const int16_t *kh = kc + (size_t)s2 * d + h * dh;
                int32_t dot = 0;
                for (int i = 0; i < dh; ++i) dot += (int32_t)qh[i] * kh[i];
                int64_t s = (dot >> 8) - (((int64_t)m->alibi[h] * j) >> 8);
                if (s < -(1 << 30)) s = -(1 << 30);
                if (s > (1 << 30)) s = (1 << 30);
                m->sc[j] = (int32_t)s;
                if (m->sc[j] > mx) mx = m->sc[j];
            }
            int64_t E = 0;
            for (int j = 0; j < cnt; ++j) { m->ex[j] = nn_exp2neg((int64_t)mx - m->sc[j]) >> m->eshift; E += m->ex[j]; }
            int64_t R = ((int64_t)1 << 40) / E;           /* E >= 2^15 >> eshift */
            int32_t num[NN_MAXDH];
            for (int i = 0; i < dh; ++i) num[i] = 0;
            for (int j = 0; j < cnt; ++j) {
                int s2 = slot - j; if (s2 < 0) s2 += W;
                const int16_t *vh = vc + (size_t)s2 * d + h * dh;
                const int32_t e = m->ex[j];
                if (!e) continue;
                for (int i = 0; i < dh; ++i) num[i] += e * vh[i];
            }
            for (int i = 0; i < dh; ++i)
                m->att[h * dh + i] = (int16_t)nn_clamp16(((int64_t)num[i] * R) >> 40);
        }
        nn_lin(&m->o[l], m->att, m->tmp);
        nn_resadd(m, m->tmp);
        nn_norm(m, m->r, m->n);
        nn_lin(&m->f1[l], m->n, m->tmp);
        for (int i = 0; i < m->ff; ++i) m->hbuf[i] = (int16_t)(m->tmp[i] > 0 ? nn_clamp16(m->tmp[i]) : 0);
        nn_lin(&m->f2[l], m->hbuf, m->tmp);
        nn_resadd(m, m->tmp);
    }
    nn_norm(m, m->r, m->n);
    nn_lin(&m->out, m->n, m->tmp);
    int32_t mx = INT32_MIN;
    for (int k = 0; k < 256; ++k) if (m->tmp[k] > mx) mx = m->tmp[k];
    m->cum[0] = 0;
    for (int k = 0; k < 256; ++k) m->cum[k + 1] = m->cum[k] + (uint32_t)nn_exp2neg((int64_t)mx - m->tmp[k]);
    ++m->t;
}

/* P(next bit = 1) in 12 bits, given the partial byte c0 (leading-1 marker,
   as crushcm keeps it) and the number of bits already seen */
static inline int nn_p1(const NN *m, uint32_t c0, int bitpos) {
    uint32_t prefix = c0 & ((1u << bitpos) - 1u);
    int lo = (int)(prefix << (8 - bitpos));
    int mid = lo + (1 << (7 - bitpos));
    int hi = lo + (1 << (8 - bitpos));
    uint64_t s0 = m->cum[mid] - m->cum[lo], s1 = m->cum[hi] - m->cum[mid];
    if (s0 + s1 == 0) return 2048;
    int p = (int)((s1 * 4096 + (s0 + s1) / 2) / (s0 + s1));
    if (p < 1) p = 1;
    if (p > 4095) p = 4095;
    return p;
}

#endif
