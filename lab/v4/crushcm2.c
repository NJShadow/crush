/* ============================================================================
   crushcm - the Crush context-mixing compressor
   ----------------------------------------------------------------------------
   Written from scratch for Crush.

   LZ-family compressors (ZIP, 7-Zip/LZMA, Zstd, RAR) compress by finding
   repeated *strings*. This compresses by *predicting the next bit* and then
   spending only -log2(p) bits to encode it. Predictions come from an ensemble:

     - eleven context models: orders 0,1,2,3,4,6,8,12, a whole-word model and
       two sparse models
     - a match model that locks onto the longest recent repeat, which recovers
       the long-range redundancy that LZ77 is good at and pure CM is not
     - a TWO-LAYER neural mixer: eight first-layer logistic mixers, each
       selecting its weight set from a different view of the state (previous
       byte x match strength, partial byte, match strength x bit position, an
       order-2 hash, a sparse hash, a bit-position view, the column within the
       current line, and an order-3 hash), whose outputs feed a small
       second-layer mixer selected by match strength x bit position
     - four APM/SSE stages that recalibrate the mixed probability

   Every model updates after every bit, identically in encoder and decoder, so
   no model data is ever stored in the output.

   Build: gcc -O3 -fomit-frame-pointer -o crushcm crushcm.c
   Use:   crushcm c in out
          crushcm d in out
   ==========================================================================*/
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

typedef uint8_t  U8;
typedef uint16_t U16;
typedef int16_t  S16;
typedef uint32_t U32;
typedef uint64_t U64;

/* ------------------------------------------------------------- sizing --- */
#ifndef CM_BITS
#define CM_BITS 23                        /* 4M slots x 4B = 16 MB per model */
#endif
#define CM_SIZE   (1u << CM_BITS)
#define CM_MASK   (CM_SIZE - 1)
#define MATCH_BITS 22
#define MATCH_SIZE (1u << MATCH_BITS)
#define MATCH_MASK (MATCH_SIZE - 1)

#define NCTX   11                         /* order 0,1,2,3,4,6 + word + 2 sparse */
#define NMODEL (NCTX + 1)                 /* + match model                   */

/* -------------------------------------------------- logistic helpers ---- */
static int squash(int d) {                /* (-2047,2047) -> (0,4095)        */
    static const int t[33] = {
        1,2,3,6,10,16,27,45,73,120,194,310,488,747,1101,1546,2047,
        2549,2994,3348,3607,3785,3901,3975,4024,4050,4068,4079,4085,4089,4092,4093,4094 };
    if (d >  2047) return 4095;
    if (d < -2047) return 0;
    int w = d & 127;
    d = (d >> 7) + 16;
    return (t[d] * (128 - w) + t[d + 1] * w + 64) >> 7;
}
static S16 stretch_tab[4096];
static void init_stretch(void) {
    int pi = 0;
    for (int x = -2047; x <= 2047; ++x) {
        int v = squash(x);
        for (int j = pi; j <= v; ++j) stretch_tab[j] = (S16)x;
        pi = v + 1;
    }
    for (int j = pi; j < 4096; ++j) stretch_tab[j] = 2047;
}
#define STRETCH(p) ((int)stretch_tab[(p)])

/* --------------------------------------------------------- counters ----- */
/* 16-bit probability + observation count. The count drives an adaptive
   learning rate: fast while a context is young, slow once it has settled. */
typedef struct { U16 p; U16 n; } Slot;

#define NLIMIT 8
static int recip[NLIMIT + 2];
static void init_recip(void) {
    for (int i = 0; i <= NLIMIT + 1; ++i) recip[i] = 65536 / (i + 2);
}
static inline void slot_update(Slot *s, int bit) {
    int target = bit ? 65535 : 0;
    int n = s->n;
    int p = (int)s->p;
    p += (int)(((long long)(target - p) * recip[n]) >> 16);
    if (p < 1) p = 1; if (p > 65534) p = 65534;
    s->p = (U16)p;
    if (n < NLIMIT) s->n = (U16)(n + 1);
}

/* ------------------------------------------------------------- state ---- */
static Slot *cmt[NCTX];                   /* context model tables            */
static Slot *cmslot[NCTX];                /* slots used for the current bit  */
static Slot *matchsm;                     /* learned match-model confidence  */
static Slot *msl;
static U32  *matchtab;
static U8   *buf;
static U32   bufsize, pos;

/* ------------------------------------------------ two-layer neural mixer ---
   Layer 1: NL1 independent logistic mixers. Every one sees the SAME input
   vector, but each selects its weight set from a DIFFERENT context, so each
   learns "how to trust the models given <that> view of the state".
   Layer 2: a small mixer over the layer-1 outputs, selected by match state
   and bit position. Each layer is trained on its own local error, which is
   what PAQ8/cmix do (no backprop through the squash).
   ------------------------------------------------------------------------ */
#ifndef V2LAYER
#define V2LAYER 1
#endif
#ifndef EXTRAIN
#define EXTRAIN 1                        /* bias + direct match-length input */
#endif
#ifndef NL1
#define NL1 8                            /* layer-1 mixers, each with its
                                             own context selector            */
#endif
#ifndef L1_LR
#define L1_LR 80
#endif
#ifndef L2_LR
#define L2_LR 12
#endif
#ifndef L2SEL
#define L2SEL 1
#endif
#ifndef L2LIN
#define L2LIN 0
#endif
#ifndef WCLAMP
#define WCLAMP 20
#endif

/* ------------------------------------------- recurrent mixer options ---- */
#ifndef RNN_ON
#define RNN_ON 1
#endif
#ifndef NH                                /* hidden cells                    */
#define NH 20
#endif
#ifndef RNN_TYPE                          /* 0 = LSTM, 1 = diagonal GRU-lite */
#define RNN_TYPE 0
#endif
#ifndef RNN_T                             /* truncated-BPTT horizon (bits)   */
#define RNN_T 8
#endif
#ifndef RNN_MODE                          /* 1 = extra layer-2 input
                                             2 = final 2-way blend mixer
                                             3 = extra layer-1 (model) input */
#define RNN_MODE 3
#endif
#ifndef RNN_LR                            /* recurrent weight lr, /1024      */
#define RNN_LR 12
#endif
#ifndef RNN_LRO                           /* readout lr, /1024               */
#define RNN_LRO 24
#endif
#ifndef RNN_FEAT_BITPOS
#define RNN_FEAT_BITPOS 1
#endif
#ifndef RNN_FEAT_C0
#define RNN_FEAT_C0 1
#endif
#ifndef RNN_FEAT_PREV
#define RNN_FEAT_PREV 1
#endif
#ifndef RNN_APM                           /* let SSE3 see the RNN's opinion  */
#define RNN_APM 0
#endif

#if EXTRAIN
#define NEXTRA 2
#else
#define NEXTRA 0
#endif
#if RNN_ON && RNN_MODE == 3
#define NRNNIN 1
#else
#define NRNNIN 0
#endif
#define NIN (NMODEL + NEXTRA + NRNNIN)

static int  mixin[NIN];

#if V2LAYER
static const int l1size[10] = { 256*8, 256, 8*8, 1024, 256*8, 64, 64*8, 4096, 1024, 256*8 };
static int *l1w[NL1];
static int  l1sel[NL1];
static int  l1pr[NL1];                    /* squashed output, 0..4095        */
static int  l1st[NL1];                    /* stretched output, layer-2 input */
#if L2SEL
#define L2CTX (8 * 8)
#else
#define L2CTX 1
#endif
#if RNN_ON && RNN_MODE == 1
#define NRNN2 1
#else
#define NRNN2 0
#endif
#define NIN2 (NL1 + 1 + NRNN2)            /* + bias (+ recurrent output)     */
static int *l2w;
static int  l2sel;
#if RNN_ON && RNN_MODE == 2
#define L3CTX (8 * 8)
#ifndef L3_LR
#define L3_LR 12
#endif
static int *l3w;
static int  l3sel, l3in[3], pr_l2;
#endif
#else
#define MIXCTX (256 * 8)
static int *mixw;
static int  mixsel;
#endif

#ifndef APM3
#define APM3 1                            /* 3rd SSE stage, keyed on match   */
#endif
#ifndef APM4
#define APM4 1                            /* 4th SSE stage, keyed on order-2 */
#endif
typedef struct { U16 t[33]; } Apm;
static Apm *apm1, *apm2;
static int a1i, a1j, a2i, a2j;            /* chosen bucket + interp index    */
#if APM3
static Apm *apm3; static int a3i, a3j;
#endif
#if APM4
static Apm *apm4; static int a4i, a4j;
#endif
static int pr_mix, pr_a1;

static U32 c0;                            /* partial byte, leading 1 marker  */
static U32 c4, c8;                        /* last 8 bytes                    */
static int bitpos;
static U32 wordhash;
static int colpos;
static U32 cmidx[NCTX];

static U32 matchptr; static int matchlen; static int matchbit; static int mlb;

static inline U32 hashmix(U64 a, U64 b) {
    U64 h = a * 0x9E3779B97F4A7C15ULL + b * 0xC2B2AE3D27D4EB4FULL;
    h ^= h >> 29; h *= 0xBF58476D1CE4E5B9ULL; h ^= h >> 32;
    return (U32)h;
}

/* ==========================================================================
   RECURRENT MIXER  (crushcm2)
   --------------------------------------------------------------------------
   An LSTM (or a cheaper diagonally-recurrent gated unit) trained ONLINE on
   the stream of ensemble predictions. Encoder and decoder run exactly the
   same updates, so nothing about it is ever stored in the archive.

   EVERYTHING HERE IS INTEGER.  No float or double appears anywhere in the
   file, so there is no rounding-mode, x87-excess-precision, FMA-contraction
   or libm-version hazard: -O0 and -O3 builds are bit-identical by
   construction.  Activation tables are built by an integer recurrence, not
   by calling exp()/tanh(), for the same reason (a compile-time-folded
   libm call and a runtime one can differ in the last ulp).

   Number format: Q16 fixed point, 65536 == 1.0, held in int32; all dot
   products accumulate in int64 and are shifted back down.
   ========================================================================== */
#define Q1 65536
#define QS 16

/* ---- integer sigmoid / tanh -------------------------------------------- */
/* sigtab[k] = sigmoid((k-2048)/256) in Q16, k = 0..4096  (x in [-8,+8])     */
static int sigtab[4097];
static void init_sigtab(void) {
    /* r = e^{-x} in Q32, stepped by the exact constant e^{-1/256}           */
    const U64 E = 4278222805ULL;                 /* e^(-1/256) * 2^32        */
    U64 r = 1ULL << 32;                          /* e^0                      */
    for (int k = 0; k <= 2048; ++k) {
        /* sigmoid(x) = 1/(1+e^-x) = 2^32 / (2^32 + r)                       */
        U64 s = ((U64)Q1 << 32) / ((1ULL << 32) + r);
        sigtab[2048 + k] = (int)s;
        sigtab[2048 - k] = Q1 - (int)s;
        r = (r * E) >> 32;
    }
    sigtab[4096] = Q1 - 1;
    if (sigtab[0] < 1) sigtab[0] = 1;
}
/* x in Q16; result in Q16, 0..65536 */
static inline int fx_sigmoid(int x) {
    if (x >=  (8 << QS)) return sigtab[4096];
    if (x <= -(8 << QS)) return sigtab[0];
    int u = x + (8 << QS);                        /* 0 .. 16<<16             */
    int i = u >> 8;                               /* 0 .. 4095               */
    int f = u & 255;
    return sigtab[i] + (((sigtab[i + 1] - sigtab[i]) * f) >> 8);
}
/* tanh(x) = 2*sigmoid(2x) - 1 ; result in Q16, -65536..65536 */
static inline int fx_tanh(int x) {
    int t = (x > (4 << QS)) ? (8 << QS) : ((x < -(4 << QS)) ? -(8 << QS) : (x << 1));
    return (fx_sigmoid(t) << 1) - Q1;
}

/* ---- deterministic PRNG for symmetry-breaking init ---------------------- */
static U64 rs_s = 0x243F6A8885A308D3ULL;
static inline U32 rs_next(void) {
    rs_s ^= rs_s << 13; rs_s ^= rs_s >> 7; rs_s ^= rs_s << 17;
    return (U32)(rs_s >> 32);
}

/* ---- feature vector ----------------------------------------------------- */
#define NX_MODEL (NMODEL + 1)             /* 12 model stretches + match len  */
#if RNN_FEAT_BITPOS
#define NX_BP 8
#else
#define NX_BP 0
#endif
#if RNN_FEAT_C0
#define NX_C0 7
#else
#define NX_C0 0
#endif
#if RNN_FEAT_PREV
#define NX_PV 8
#else
#define NX_PV 0
#endif
#define NX (NX_MODEL + NX_BP + NX_C0 + NX_PV)

#if RNN_ON
#if RNN_TYPE == 0
#define NGATE 4                           /* i, f, o, g                      */
#else
#define NGATE 2                           /* z, g                            */
#endif
#define NW (NGATE * NH)

static int *Wx;                           /* [NW][NX]                        */
static int *Wb;                           /* [NW]                            */
static int  Wo[NH], Wob;                  /* readout                         */
static int  h_cur[NH], c_cur[NH];
#if RNN_TYPE == 0
static int *Wh;                           /* [NW][NH]                        */
#else
static int  Wr[NW];                       /* diagonal recurrent gain         */
static int  dWr[NW];
#endif
static int *dWx, *dWh_, *dWb;
static int  dWo[NH], dWob;

/* The last NXB features are one-hot / +-1 / 0 indicators. Multiplying a Q16
   weight by exactly +-65536 or 0 and shifting back by 16 is the same as
   adding, subtracting or dropping the weight, so they are kept as signs and
   handled with adds. This is BIT-EXACT with the naive form, just faster. */
#define NXB (NX - NX_MODEL)

/* per-step trace for truncated BPTT */
static int  sx[RNN_T][NX_MODEL];
static signed char sxb[RNN_T][NXB > 0 ? NXB : 1];
static signed char rnn_xb[NXB > 0 ? NXB : 1];
static int  sgate[RNN_T][NW];             /* gate activations                */
static int  sc[RNN_T][NH], sh[RNN_T][NH], sct[RNN_T][NH];
static int  sG[RNN_T];
static int  wh0[NH], wc0[NH];             /* h,c at start of the window      */
static int  rnn_t;                        /* position inside the window      */
static int  rnn_x[NX_MODEL];
static int  rnn_st;                       /* readout, stretch domain         */
static int  rnn_pr;                       /* readout, 0..4095                */

#define WCL (8 << QS)                     /* weight clamp, +-8.0             */
#define GCL (32 << QS)                    /* gradient clamp                  */
#define CCL (6 << QS)                     /* cell-state clamp                */

static void rnn_init(void) {
    init_sigtab();
    Wx  = (int *)malloc(sizeof(int) * NW * NX);
    Wb  = (int *)malloc(sizeof(int) * NW);
    dWx = (int *)malloc(sizeof(int) * NW * NX);
    dWb = (int *)malloc(sizeof(int) * NW);
#if RNN_TYPE == 0
    Wh   = (int *)malloc(sizeof(int) * NW * NH);
    dWh_ = (int *)malloc(sizeof(int) * NW * NH);
#endif
    rs_s = 0x243F6A8885A308D3ULL;
    /* uniform in +-0.15, deterministic */
    for (int i = 0; i < NW * NX; ++i) Wx[i] = (int)(rs_next() % 19661u) - 9830;
#if RNN_TYPE == 0
    for (int i = 0; i < NW * NH; ++i) Wh[i] = (int)(rs_next() % 13107u) - 6553;
#else
    for (int a = 0; a < NW; ++a) { Wr[a] = (int)(rs_next() % 19661u) - 9830; dWr[a] = 0; }
#endif
    for (int i = 0; i < NW; ++i) Wb[i] = 0;
#if RNN_TYPE == 0
    /* forget-gate bias = +1.0: the classic "remember by default" init       */
    for (int j = 0; j < NH; ++j) Wb[1 * NH + j] = Q1;
#else
    for (int j = 0; j < NH; ++j) Wb[0 * NH + j] = Q1;   /* keep by default   */
#endif
    for (int j = 0; j < NH; ++j) { Wo[j] = 0; h_cur[j] = 0; c_cur[j] = 0; }
    Wob = 0;
    rnn_t = 0;
    memset(wh0, 0, sizeof wh0); memset(wc0, 0, sizeof wc0);
    memset(dWx, 0, sizeof(int) * NW * NX);
    memset(dWb, 0, sizeof(int) * NW);
#if RNN_TYPE == 0
    memset(dWh_, 0, sizeof(int) * NW * NH);
#endif
    rnn_st = 0; rnn_pr = 2048;
}

/* build the input vector from the ensemble outputs and byte context */
static inline void rnn_features(const int *mix) {
    int k = 0, b = 0;
    for (int i = 0; i < NMODEL; ++i) rnn_x[k++] = mix[i] * 32;      /* +-1.0 */
#if EXTRAIN
    rnn_x[k++] = mix[NMODEL + 1] * 32;
#else
    rnn_x[k++] = 0;
#endif
#if RNN_FEAT_BITPOS
    for (int i = 0; i < 8; ++i) rnn_xb[b++] = (signed char)(i == bitpos);
#endif
#if RNN_FEAT_C0
    for (int i = 0; i < 7; ++i)
        rnn_xb[b++] = (signed char)((i < bitpos)
                       ? (((c0 >> (bitpos - 1 - i)) & 1u) ? 1 : -1) : 0);
#endif
#if RNN_FEAT_PREV
    for (int i = 0; i < 8; ++i)
        rnn_xb[b++] = (signed char)(((c4 >> (7 - i)) & 1u) ? 1 : -1);
#endif
    (void)b;
}

/* forward one bit; returns the readout in stretch domain (-2047..2047) */
static int rnn_forward(const int *mix) {
    rnn_features(mix);
    int t = rnn_t;
    for (int i = 0; i < NX_MODEL; ++i) sx[t][i] = rnn_x[i];
    for (int i = 0; i < NXB; ++i) sxb[t][i] = rnn_xb[i];

    int gate[NW];
#if RNN_TYPE == 0
    for (int a = 0; a < NW; ++a) {
        const int *wx = Wx + (size_t)a * NX;
        const int *wh = Wh + (size_t)a * NH;
        long long d = (long long)Wb[a] << QS;
        for (int i = 0; i < NX_MODEL; ++i) d += (long long)wx[i] * rnn_x[i];
        { const int *wb2 = wx + NX_MODEL; int sb = 0;
          for (int i = 0; i < NXB; ++i) sb += wb2[i] * rnn_xb[i];
          d += (long long)sb << QS; }
        for (int j = 0; j < NH; ++j) d += (long long)wh[j] * h_cur[j];
        int v = (int)(d >> QS);
        if (v >  (8 << QS)) v =  (8 << QS);
        if (v < -(8 << QS)) v = -(8 << QS);
        gate[a] = (a < 3 * NH) ? fx_sigmoid(v) : fx_tanh(v);
    }
    for (int j = 0; j < NH; ++j) {
        int gi = gate[0 * NH + j], gf = gate[1 * NH + j];
        int go = gate[2 * NH + j], gg = gate[3 * NH + j];
        long long cn = ((long long)gf * c_cur[j] + (long long)gi * gg) >> QS;
        if (cn >  CCL) cn =  CCL;
        if (cn < -CCL) cn = -CCL;
        sc[t][j] = (int)cn;
        int ct = fx_tanh((int)cn);
        sct[t][j] = ct;
        sh[t][j] = (int)(((long long)go * ct) >> QS);
    }
#else
    for (int a = 0; a < NW; ++a) {
        const int *wx = Wx + (size_t)a * NX;
        long long d = (long long)Wb[a] << QS;
        for (int i = 0; i < NX_MODEL; ++i) d += (long long)wx[i] * rnn_x[i];
        { const int *wb2 = wx + NX_MODEL; int sb = 0;
          for (int i = 0; i < NXB; ++i) sb += wb2[i] * rnn_xb[i];
          d += (long long)sb << QS; }
        d += (long long)Wr[a] * h_cur[a % NH];
        int v = (int)(d >> QS);
        if (v >  (8 << QS)) v =  (8 << QS);
        if (v < -(8 << QS)) v = -(8 << QS);
        gate[a] = (a < NH) ? fx_sigmoid(v) : fx_tanh(v);
    }
    for (int j = 0; j < NH; ++j) {
        int z = gate[0 * NH + j], g = gate[1 * NH + j];
        long long hn = ((long long)z * h_cur[j] + (long long)(Q1 - z) * g) >> QS;
        sh[t][j] = (int)hn;
        sc[t][j] = 0; sct[t][j] = 0;
    }
#endif
    for (int a = 0; a < NW; ++a) sgate[t][a] = gate[a];

    long long o = (long long)Wob << QS;
    for (int j = 0; j < NH; ++j) o += (long long)Wo[j] * sh[t][j];
    int st = (int)(o >> 24);
    if (st >  2047) st =  2047;
    if (st < -2047) st = -2047;
    rnn_st = st;
    rnn_pr = squash(st);
    return st;
}

/* one truncated-BPTT sweep over the window that just filled */
static void rnn_bptt(void) {
    const int T = rnn_t;                          /* steps to unroll         */
    memset(dWx, 0, sizeof(int) * NW * NX);
    memset(dWb, 0, sizeof(int) * NW);
#if RNN_TYPE == 0
    memset(dWh_, 0, sizeof(int) * NW * NH);
#else
    memset(dWr, 0, sizeof dWr);
#endif
    int gh_next[NH], gc_next[NH];
    for (int j = 0; j < NH; ++j) { gh_next[j] = 0; gc_next[j] = 0; dWo[j] = 0; }
    dWob = 0;

    for (int t = T - 1; t >= 0; --t) {
        const int *hp = (t == 0) ? wh0 : sh[t - 1];
        const int *cp = (t == 0) ? wc0 : sc[t - 1];
        int G = sG[t];
        int apre[NW];
        for (int j = 0; j < NH; ++j) {
            int gh = gh_next[j] + (int)(((long long)G * Wo[j]) >> 12);
            if (gh >  GCL) gh =  GCL;
            if (gh < -GCL) gh = -GCL;
#if RNN_TYPE == 0
            int go_ = sgate[t][2 * NH + j];
            int ct  = sct[t][j];
            int g_o = (int)(((long long)gh * ct) >> QS);
            int dct = Q1 - (int)(((long long)ct * ct) >> QS);
            int g_c = gc_next[j] + (int)(((((long long)gh * go_) >> QS) * dct) >> QS);
            if (g_c >  GCL) g_c =  GCL;
            if (g_c < -GCL) g_c = -GCL;
            int gi_ = sgate[t][0 * NH + j];
            int gf_ = sgate[t][1 * NH + j];
            int gg_ = sgate[t][3 * NH + j];
            int g_i = (int)(((long long)g_c * gg_) >> QS);
            int g_g = (int)(((long long)g_c * gi_) >> QS);
            int g_f = (int)(((long long)g_c * cp[j]) >> QS);
            gc_next[j] = (int)(((long long)g_c * gf_) >> QS);
            apre[0 * NH + j] = (int)(((long long)g_i * (((long long)gi_ * (Q1 - gi_)) >> QS)) >> QS);
            apre[1 * NH + j] = (int)(((long long)g_f * (((long long)gf_ * (Q1 - gf_)) >> QS)) >> QS);
            apre[2 * NH + j] = (int)(((long long)g_o * (((long long)go_ * (Q1 - go_)) >> QS)) >> QS);
            apre[3 * NH + j] = (int)(((long long)g_g * (Q1 - (int)(((long long)gg_ * gg_) >> QS))) >> QS);
#else
            int z = sgate[t][0 * NH + j], g = sgate[t][1 * NH + j];
            int g_z = (int)(((long long)gh * (hp[j] - g)) >> QS);
            int g_g = (int)(((long long)gh * (Q1 - z)) >> QS);
            gc_next[j] = 0;
            gh_next[j] = (int)(((long long)gh * z) >> QS);   /* carry, plus Wr below */
            apre[0 * NH + j] = (int)(((long long)g_z * (((long long)z * (Q1 - z)) >> QS)) >> QS);
            apre[1 * NH + j] = (int)(((long long)g_g * (Q1 - (int)(((long long)g * g) >> QS))) >> QS);
#endif
            /* readout weights: their input is h at this step */
            dWo[j] += (int)(((long long)G * sh[t][j]) >> 12);
        }
        dWob += (int)(((long long)G * Q1) >> 12);

        /* accumulate weight gradients and propagate to h(t-1) */
#if RNN_TYPE == 0
        int ghp[NH];
        for (int j = 0; j < NH; ++j) ghp[j] = 0;
#endif
        for (int a = 0; a < NW; ++a) {
            int ap = apre[a];
            if (ap == 0) continue;
            int *dwx = dWx + (size_t)a * NX;
            const int *xs = sx[t];
            for (int i = 0; i < NX_MODEL; ++i) dwx[i] += (int)(((long long)ap * xs[i]) >> QS);
            { int *dwb2 = dwx + NX_MODEL; const signed char *xb = sxb[t];
              for (int i = 0; i < NXB; ++i) dwb2[i] += ap * xb[i]; }
            dWb[a] += ap;
#if RNN_TYPE == 0
            int *dwh = dWh_ + (size_t)a * NH;
            const int *wh = Wh + (size_t)a * NH;
            for (int j = 0; j < NH; ++j) {
                dwh[j] += (int)(((long long)ap * hp[j]) >> QS);
                ghp[j] += (int)(((long long)ap * wh[j]) >> QS);
            }
#endif
        }
#if RNN_TYPE == 0
        for (int j = 0; j < NH; ++j) {
            int v = ghp[j];
            if (v >  GCL) v =  GCL;
            if (v < -GCL) v = -GCL;
            gh_next[j] = v;
        }
#else
        for (int j = 0; j < NH; ++j) {
            long long v = gh_next[j];
            for (int a = 0; a < NGATE; ++a) {
                int ap = apre[a * NH + j];
                dWr[a * NH + j] += (int)(((long long)ap * hp[j]) >> QS);
                v += ((long long)ap * Wr[a * NH + j]) >> QS;
            }
            if (v >  GCL) v =  GCL;
            if (v < -GCL) v = -GCL;
            gh_next[j] = (int)v;
        }
#endif
    }

    for (int j = 0; j < NH; ++j) {
        int nw = Wo[j] + (int)(((long long)RNN_LRO * dWo[j]) >> 10);
        if (nw >  WCL) nw =  WCL;
        if (nw < -WCL) nw = -WCL;
        Wo[j] = nw;
    }
    { int nb = Wob + (int)(((long long)RNN_LRO * dWob) >> 10);
      if (nb >  WCL) nb =  WCL;
      if (nb < -WCL) nb = -WCL;
      Wob = nb; }

    for (int a = 0; a < NW; ++a) {
        int *wx = Wx + (size_t)a * NX, *dwx = dWx + (size_t)a * NX;
        for (int i = 0; i < NX; ++i) {
            int nw = wx[i] + (int)(((long long)RNN_LR * dwx[i]) >> 10);
            if (nw >  WCL) nw =  WCL;
            if (nw < -WCL) nw = -WCL;
            wx[i] = nw;
        }
        { int nb = Wb[a] + (int)(((long long)RNN_LR * dWb[a]) >> 10);
          if (nb >  WCL) nb =  WCL;
          if (nb < -WCL) nb = -WCL;
          Wb[a] = nb; }
#if RNN_TYPE == 0
        int *wh = Wh + (size_t)a * NH, *dwh = dWh_ + (size_t)a * NH;
        for (int j = 0; j < NH; ++j) {
            int nw = wh[j] + (int)(((long long)RNN_LR * dwh[j]) >> 10);
            if (nw >  WCL) nw =  WCL;
            if (nw < -WCL) nw = -WCL;
            wh[j] = nw;
        }
#else
        { int nw = Wr[a] + (int)(((long long)RNN_LR * dWr[a]) >> 10);
          if (nw >  WCL) nw =  WCL;
          if (nw < -WCL) nw = -WCL;
          Wr[a] = nw; }
#endif
    }
}

#ifdef RNN_STATS
#include <math.h>
double stat_rnn = 0, stat_mix = 0; long stat_n = 0;
static double nlog2(int p12, int bit) {   /* -log2 of a 12-bit probability   */
    double p = (bit ? p12 : 4096 - p12) / 4096.0;
    if (p < 1e-9) p = 1e-9;
    return -log(p) / 0.6931471805599453;
}
#endif

/* called after the coded bit is known */
static void rnn_learn(int bit) {
    int t = rnn_t;
#ifdef RNN_STATS
    stat_rnn += nlog2(rnn_pr, bit);
    stat_mix += nlog2(pr_mix, bit);
    ++stat_n;
#endif
    sG[t] = (bit << 12) - rnn_pr;
    for (int j = 0; j < NH; ++j) { h_cur[j] = sh[t][j]; c_cur[j] = sc[t][j]; }
    ++rnn_t;
    if (rnn_t == RNN_T) {
        rnn_bptt();
        for (int j = 0; j < NH; ++j) { wh0[j] = h_cur[j]; wc0[j] = c_cur[j]; }
        rnn_t = 0;
    }
}
#endif /* RNN_ON */

static void models_init(U32 n) {
    init_stretch(); init_recip();
    for (int i = 0; i < NCTX; ++i) {
        cmt[i] = (Slot *)malloc(sizeof(Slot) * CM_SIZE);
        if (!cmt[i]) { fprintf(stderr, "crushcm: out of memory\n"); exit(2); }
        for (U32 j = 0; j < CM_SIZE; ++j) { cmt[i][j].p = 32768; cmt[i][j].n = 0; }
    }
    matchsm = (Slot *)malloc(sizeof(Slot) * (64 * 2 * 8));
    for (int i = 0; i < 64 * 2 * 8; ++i) { matchsm[i].p = 32768; matchsm[i].n = 0; }
    matchtab = (U32 *)calloc(MATCH_SIZE, sizeof(U32));
#if V2LAYER
    for (int k = 0; k < NL1; ++k) {
        l1w[k] = (int *)malloc(sizeof(int) * (size_t)l1size[k] * NIN);
        if (!l1w[k]) { fprintf(stderr, "crushcm: out of memory\n"); exit(2); }
        for (int i = 0; i < l1size[k] * NIN; ++i) l1w[k][i] = 65536 / NMODEL;
    }
    l2w = (int *)malloc(sizeof(int) * L2CTX * NIN2);
    for (int c = 0; c < L2CTX; ++c) {
        int *w = l2w + (size_t)c * NIN2;
        /* favour layer-1 mixer 0, which uses the original (prev-byte x match)
           selector and is the strongest single mixer; the rest share the top */
        w[0] = 32768;
        for (int k = 1; k < NL1; ++k) w[k] = 32768 / (NL1 - 1);
        w[NL1] = 0;                                              /* bias      */
#if NRNN2
        w[NL1 + 1] = 0;                                          /* recurrent */
#endif
    }
#if RNN_ON && RNN_MODE == 2
    l3w = (int *)malloc(sizeof(int) * L3CTX * 3);
    for (int c = 0; c < L3CTX; ++c) {
        int *w = l3w + (size_t)c * 3;
        w[0] = 65536; w[1] = 0; w[2] = 0;    /* start as "ensemble only"      */
    }
#endif
#else
    mixw = (int *)malloc(sizeof(int) * MIXCTX * NIN);
    for (int i = 0; i < MIXCTX * NIN; ++i) mixw[i] = 65536 / NMODEL;
#endif
    apm1 = (Apm *)malloc(sizeof(Apm) * 256);
    apm2 = (Apm *)malloc(sizeof(Apm) * 65536);
    for (int i = 0; i < 256; ++i)
        for (int j = 0; j < 33; ++j) apm1[i].t[j] = (U16)(squash((j - 16) * 128) * 16);
    for (int i = 0; i < 65536; ++i)
        for (int j = 0; j < 33; ++j) apm2[i].t[j] = (U16)(squash((j - 16) * 128) * 16);
#if APM3
    apm3 = (Apm *)malloc(sizeof(Apm) * 4096);
    for (int i = 0; i < 4096; ++i)
        for (int j = 0; j < 33; ++j) apm3[i].t[j] = (U16)(squash((j - 16) * 128) * 16);
#endif
#if APM4
    apm4 = (Apm *)malloc(sizeof(Apm) * 65536);
    for (int i = 0; i < 65536; ++i)
        for (int j = 0; j < 33; ++j) apm4[i].t[j] = (U16)(squash((j - 16) * 128) * 16);
#endif
#if RNN_ON
    rnn_init();
#endif
    buf = (U8 *)malloc((size_t)n + 64);
    if (!buf) { fprintf(stderr, "crushcm: out of memory\n"); exit(2); }
    bufsize = n; pos = 0;
    c0 = 1; c4 = c8 = 0; bitpos = 0; wordhash = 0; colpos = 0;
    matchptr = 0; matchlen = 0; matchbit = 0; mlb = 0;
}

/* Recompute per-byte context bases. Called after each complete byte. */
static void update_contexts(void) {
    cmidx[0] = 0;                                   /* order-0: c0 only      */
    cmidx[1] = hashmix(c4 & 0xffu,       0x11);
    cmidx[2] = hashmix(c4 & 0xffffu,     0x22);
    cmidx[3] = hashmix(c4 & 0xffffffu,   0x33);
    cmidx[4] = hashmix(c4,               0x44);
    cmidx[5] = hashmix(((U64)(c8 & 0xffffu) << 32) | c4, 0x66);
    cmidx[6] = hashmix(wordhash,         0x77);
    /* sparse: skip the immediately-preceding byte. Recovers structure in
       binaries and record data where byte -1 is a varying field. */
    cmidx[7] = hashmix(((c4 >> 8) & 0xffffu) | ((U64)(c4 & 0xff000000u) << 8), 0x88);
    /* stride-4: the byte four back, plus the byte eight back. Locks onto
       fixed-width records, pointer tables and machine-code operand slots. */
    cmidx[8] = hashmix((U64)((c4 >> 24) & 0xffu) | ((U64)((c8 >> 16) & 0xffu) << 8)
                       | ((U64)(c4 & 0xffu) << 16), 0x99);
    cmidx[9]  = hashmix(((U64)c8 << 32) | c4, 0xAA);
    cmidx[10] = hashmix(hashmix(((U64)c8 << 32) | c4, 0xBB) ^ (U32)(pos > 12 ? hashmix(*(U32*)(buf+pos-12), 0xCC) : 0), 0xDD);

    U32 h = hashmix(((U64)(c8 & 0xffffu) << 32) | c4, 0xA5A5) & MATCH_MASK;
    if (matchlen > 0 && matchptr < pos && pos > 0 && buf[matchptr] == buf[pos - 1]) {
        ++matchptr;
        if (matchlen < 4000) ++matchlen;
    } else {
        matchptr = matchtab[h];
        matchlen = 0;
        if (matchptr > 0 && matchptr < pos) {
            U32 i = 1;
            while (i < 500 && matchptr >= i && i <= pos && buf[matchptr - i] == buf[pos - i]) ++i;
            matchlen = (int)i - 1;
            if (matchlen < 4) { matchlen = 0; }
        }
    }
    matchtab[h] = pos;
    mlb = matchlen == 0 ? 0 : (matchlen < 6 ? 1 : (matchlen < 10 ? 2 : (matchlen < 16 ? 3 :
          (matchlen < 28 ? 4 : (matchlen < 52 ? 5 : (matchlen < 100 ? 6 : 7))))));
}

/* ----------------------------------------------------------- predict --- */
static int predict(void) {
    for (int i = 0; i < NCTX; ++i) {
        U32 idx = (cmidx[i] ^ (c0 * 0x6F4F2F1Fu)) & CM_MASK;
        cmslot[i] = &cmt[i][idx];
        mixin[i] = STRETCH(cmslot[i]->p >> 4);
    }

    msl = NULL; matchbit = 0;
    if (matchlen > 0 && matchptr < pos) {
        int pb = buf[matchptr];
        if ((int)c0 == (int)((1u << bitpos) | (bitpos ? (U32)(pb >> (8 - bitpos)) : 0u))) {
            matchbit = (pb >> (7 - bitpos)) & 1;
            int ml = matchlen > 63 ? 63 : matchlen;
            msl = &matchsm[(ml * 2 + matchbit) * 8 + bitpos];
            mixin[NCTX] = STRETCH(msl->p >> 4);
        } else {
            matchlen = 0; mlb = 0;
            mixin[NCTX] = 0;
        }
    } else {
        mixin[NCTX] = 0;
    }

#if EXTRAIN
    /* constant bias input: lets each weight set learn a standing offset */
    mixin[NMODEL] = 256;
    /* the match model's LENGTH as a direct, signed input. The match statemap
       says "how often a match of this length was right"; this says "how long
       the match is", which the mixer can weigh per context on its own. */
    if (matchlen > 0 && msl) {
        int ml = matchlen > 32 ? 32 : matchlen;
        int v = ml * 48;                              /* 48..1536            */
        mixin[NMODEL + 1] = matchbit ? v : -v;
    } else {
        mixin[NMODEL + 1] = 0;
    }
#endif

#if RNN_ON
    /* The recurrent mixer sees exactly what the logistic mixers see, plus a
       little byte-level context, and carries state across bits and bytes. */
    rnn_forward(mixin);
#if RNN_MODE == 3
    mixin[NMODEL + NEXTRA] = rnn_st;      /* an extra "model" for layer 1     */
#endif
#endif

#if V2LAYER
    l1sel[0] = (int)((c4 & 0xffu) * 8 + mlb);         /* original selector    */
    l1sel[1] = (int)(c0 & 0xffu);                     /* partial byte         */
    l1sel[2] = mlb * 8 + bitpos;                      /* match strength x bit */
    l1sel[3] = (int)(hashmix(c4 & 0xffffu, 0x5151) & 1023u);      /* order-2  */
#if NL1 > 4
    l1sel[4] = (int)(((c8 & 0xffu) ^ ((c4 >> 8) & 0xffu)) * 8 + bitpos);
#endif
#if NL1 > 5
    l1sel[5] = bitpos * 8 + (int)((c4 >> 5) & 7u);
#endif
#if NL1 > 6
    /* text/record column: distance since the last newline. Gives the mixer a
       view of *where in the line* we are, which is what drives XML/JSON/text
       structure. */
    l1sel[6] = (colpos > 63 ? 63 : colpos) * 8 + bitpos;
#endif
#if NL1 > 7
    l1sel[7] = (int)(hashmix(c4 & 0xffffffu, 0x6161) & 4095u);
#endif
#if NL1 > 8
    l1sel[8] = (int)(hashmix(wordhash, 0x9393) & 1023u);       /* word view   */
#endif
#if NL1 > 9
    l1sel[9] = (int)((c4 & 0xffu) * 8 + bitpos);               /* byte x bit  */
#endif

    long long acc = 0;
    for (int k = 0; k < NL1; ++k) {
        int *w = l1w[k] + (size_t)l1sel[k] * NIN;
        long long dot = 0;
        for (int i = 0; i < NIN; ++i) dot += (long long)w[i] * mixin[i];
        int d = (int)(dot >> 16);
        if (d >  2047) d =  2047;
        if (d < -2047) d = -2047;
        l1pr[k] = squash(d);
#if L2LIN
        l1st[k] = (l1pr[k] - 2048);      /* linear-probability domain input  */
#else
        l1st[k] = d;                     /* stretched (logit) domain input   */
#endif
    }
#if L2SEL
    l2sel = mlb * 8 + bitpos;
#else
    l2sel = 0;
#endif
    { int *w2 = l2w + (size_t)l2sel * NIN2;
      acc = 0;
      for (int k = 0; k < NL1; ++k) acc += (long long)w2[k] * l1st[k];
      acc += (long long)w2[NL1] * 256;
#if NRNN2
      acc += (long long)w2[NL1 + 1] * rnn_st;
#endif
    }
    int d = (int)(acc >> 16);
    if (d >  2047) d =  2047;
    if (d < -2047) d = -2047;
    pr_mix = squash(d);
#if RNN_ON && RNN_MODE == 2
    /* competing predictor: a final logistic mixer blends the ensemble mix
       with the recurrent mixer, weights selected by match state x bit pos  */
    { int *w3 = l3w + (size_t)(l3sel = mlb * 8 + bitpos) * 3;
      l3in[0] = d; l3in[1] = rnn_st; l3in[2] = 256;
      long long a3 = 0;
      for (int i = 0; i < 3; ++i) a3 += (long long)w3[i] * l3in[i];
      int d3 = (int)(a3 >> 16);
      if (d3 >  2047) d3 =  2047;
      if (d3 < -2047) d3 = -2047;
      pr_l2 = pr_mix;
      pr_mix = squash(d3);
    }
#endif
#else
    mixsel = (int)((c4 & 0xffu) * 8 + mlb);
    int *w = mixw + (size_t)mixsel * NIN;
    long long dot = 0;
    for (int i = 0; i < NIN; ++i) dot += (long long)w[i] * mixin[i];
    int d = (int)(dot >> 16);
    if (d >  2047) d =  2047;
    if (d < -2047) d = -2047;
    pr_mix = squash(d);
#endif

    /* SSE stage 1: keyed on the partial byte */
    int st = STRETCH(pr_mix) + 2048;
    a1i = (int)(c0 & 0xffu);
    a1j = st >> 7; { int fw = st & 127;
        U16 *t = apm1[a1i].t;
        pr_a1 = (t[a1j] * (128 - fw) + t[a1j + 1] * fw) >> 11;
        if (pr_a1 > 4095) pr_a1 = 4095; if (pr_a1 < 0) pr_a1 = 0;
        a1j += (fw >> 6);
    }
    /* SSE stage 2: keyed on partial byte + previous byte */
    int st2 = STRETCH(pr_a1) + 2048;
    a2i = (int)((c0 & 0xffu) | ((c4 & 0xffu) << 8));
    a2j = st2 >> 7; int pr_a2; { int fw = st2 & 127;
        U16 *t = apm2[a2i].t;
        pr_a2 = (t[a2j] * (128 - fw) + t[a2j + 1] * fw) >> 11;
        if (pr_a2 > 4095) pr_a2 = 4095; if (pr_a2 < 0) pr_a2 = 0;
        a2j += (fw >> 6);
    }

#if APM3
    /* SSE stage 3: keyed on partial byte + match state. Calibrates separately
       for "the match model is confident" vs "there is no match". */
    int st3 = STRETCH(pr_a2) + 2048;
    a3i = (int)((c0 & 0xffu) * 16 + mlb * 2 + matchbit);
    a3j = st3 >> 7; int pr_a3; { int fw = st3 & 127;
        U16 *t = apm3[a3i].t;
        pr_a3 = (t[a3j] * (128 - fw) + t[a3j + 1] * fw) >> 11;
        if (pr_a3 > 4095) pr_a3 = 4095; if (pr_a3 < 0) pr_a3 = 0;
        a3j += (fw >> 6);
    }
#endif
#if APM4
    /* SSE stage 4: run in PARALLEL off stage 1, keyed on a hash of order-2.
       A content-keyed calibration that the byte-keyed chain cannot express. */
    int st4 = STRETCH(pr_a1) + 2048;
    a4i = (int)(hashmix((c4 & 0xffffu) | ((U64)(c0 & 0xffu) << 16), 0x7373) & 0xffffu);
    a4j = st4 >> 7; int pr_a4; { int fw = st4 & 127;
        U16 *t = apm4[a4i].t;
        pr_a4 = (t[a4j] * (128 - fw) + t[a4j + 1] * fw) >> 11;
        if (pr_a4 > 4095) pr_a4 = 4095; if (pr_a4 < 0) pr_a4 = 0;
        a4j += (fw >> 6);
    }
#endif
#if APM3 && APM4
    int pr = (2 * pr_mix + 2 * pr_a1 + 3 * pr_a2 + 6 * pr_a3 + 3 * pr_a4 + 8) >> 4;
#elif APM3
    int pr = (pr_mix + pr_a1 + 2 * pr_a2 + 4 * pr_a3 + 4) >> 3;
#elif APM4
    int pr = (pr_mix + pr_a1 + pr_a2 + pr_a4 + 2) >> 2;
#else
    int pr = (pr_mix + pr_a1 + 2 * pr_a2 + 2) >> 2;
#endif
    if (pr < 1) pr = 1;
    if (pr > 4094) pr = 4094;
    return pr;
}

/* ------------------------------------------------------------ update --- */
#define MIX_LR 40
static void update(int bit) {
#if RNN_ON
    rnn_learn(bit);
#endif
#if V2LAYER
#if RNN_ON && RNN_MODE == 2
    { int err3 = (bit << 12) - pr_mix;
      int *w3 = l3w + (size_t)l3sel * 3;
      for (int i = 0; i < 3; ++i) {
          int nw = w3[i] + (int)(((long long)l3in[i] * err3 * L3_LR) >> 16);
          if (nw >  (1 << WCLAMP)) nw =  (1 << WCLAMP);
          if (nw < -(1 << WCLAMP)) nw = -(1 << WCLAMP);
          w3[i] = nw;
      }
    }
#endif
    /* layer 2 first: trained on its own output error */
    {
#if RNN_ON && RNN_MODE == 2
      int err2 = (bit << 12) - pr_l2;
#else
      int err2 = (bit << 12) - pr_mix;
#endif
      int *w2 = l2w + (size_t)l2sel * NIN2;
      for (int k = 0; k < NL1; ++k) {
          int nw = w2[k] + (int)(((long long)l1st[k] * err2 * L2_LR) >> 16);
          if (nw >  (1 << WCLAMP)) nw =  (1 << WCLAMP);
          if (nw < -(1 << WCLAMP)) nw = -(1 << WCLAMP);
          w2[k] = nw;
      }
      { int nw = w2[NL1] + (int)(((long long)256 * err2 * L2_LR) >> 16);
        if (nw >  (1 << WCLAMP)) nw =  (1 << WCLAMP);
        if (nw < -(1 << WCLAMP)) nw = -(1 << WCLAMP);
        w2[NL1] = nw; }
#if NRNN2
      { int nw = w2[NL1 + 1] + (int)(((long long)rnn_st * err2 * L2_LR) >> 16);
        if (nw >  (1 << WCLAMP)) nw =  (1 << WCLAMP);
        if (nw < -(1 << WCLAMP)) nw = -(1 << WCLAMP);
        w2[NL1 + 1] = nw; }
#endif
    }
    /* each layer-1 mixer is trained on ITS OWN error, not on layer 2's */
    for (int k = 0; k < NL1; ++k) {
        int err = (bit << 12) - l1pr[k];
        int *w = l1w[k] + (size_t)l1sel[k] * NIN;
        for (int i = 0; i < NIN; ++i) {
            int nw = w[i] + (int)(((long long)mixin[i] * err * L1_LR) >> 16);
            if (nw >  (1 << WCLAMP)) nw =  (1 << WCLAMP);
            if (nw < -(1 << WCLAMP)) nw = -(1 << WCLAMP);
            w[i] = nw;
        }
    }
#else
    int err = (bit << 12) - pr_mix;
    int *w = mixw + (size_t)mixsel * NIN;
    for (int i = 0; i < NIN; ++i) {
        int dw = (int)(((long long)mixin[i] * err * MIX_LR) >> 16);
        int nw = w[i] + dw;
        if (nw >  (1 << WCLAMP)) nw =  (1 << WCLAMP);
        if (nw < -(1 << WCLAMP)) nw = -(1 << WCLAMP);
        w[i] = nw;
    }
#endif
    for (int i = 0; i < NCTX; ++i) slot_update(cmslot[i], bit);
    if (msl) slot_update(msl, bit);

    { U16 *t = apm1[a1i].t; int g = bit ? 65535 : 0; int v = (int)t[a1j]; v += (g - v) >> 6;
      if (v < 0) v = 0; if (v > 65535) v = 65535; t[a1j] = (U16)v; }
    { U16 *t = apm2[a2i].t; int g = bit ? 65535 : 0; int v = (int)t[a2j]; v += (g - v) >> 6;
      if (v < 0) v = 0; if (v > 65535) v = 65535; t[a2j] = (U16)v; }
#if APM3
    { U16 *t = apm3[a3i].t; int g = bit ? 65535 : 0; int v = (int)t[a3j]; v += (g - v) >> 6;
      if (v < 0) v = 0; if (v > 65535) v = 65535; t[a3j] = (U16)v; }
#endif
#if APM4
    { U16 *t = apm4[a4i].t; int g = bit ? 65535 : 0; int v = (int)t[a4j]; v += (g - v) >> 6;
      if (v < 0) v = 0; if (v > 65535) v = 65535; t[a4j] = (U16)v; }
#endif

    /* advance bit state */
    c0 = (c0 << 1) | (U32)bit;
    ++bitpos;
    if (bitpos == 8) {
        U32 byte = c0 & 0xffu;
        buf[pos++] = (U8)byte;
        c8 = (c8 << 8) | (c4 >> 24);
        c4 = (c4 << 8) | byte;
        if ((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
            (byte >= '0' && byte <= '9')) {
            U32 lc = (byte >= 'A' && byte <= 'Z') ? byte + 32 : byte;
            wordhash = wordhash * 0x9E3779B9u + lc + 1u;
        } else {
            wordhash = 0;
        }
        colpos = (byte == '\n') ? 0 : (colpos < 1000 ? colpos + 1 : colpos);
        c0 = 1; bitpos = 0;
        update_contexts();
    }
}

/* ------------------------------------------------------------ priming ---
   ATLAS: run the model over a shared corpus BEFORE coding the real input.

   Nothing is emitted for the primed bytes - the encoder and the decoder simply
   perform the same updates, so both arrive at the real data with an identical,
   already-trained model. Two things happen at once:

     - every context model starts warm, so a small file no longer pays the
       cold-start cost that dominates its compressed size;
     - the primed bytes sit in the history buffer, so the match model can copy
       straight out of them. The prime corpus is a dictionary and a pretrained
       model at the same time.

   This is the mechanism behind neural compressors that reach 0.7 bits per
   character: the model is shared, so the information in it is not in the file.
   The corpus ships with Crush and is identified in the archive, so any copy of
   Crush can decode any archive.
   ------------------------------------------------------------------------ */
static U8 *prime_buf = NULL;
static long prime_len = 0;

static int predict(void);
static void update(int bit);

static void prime_run(void) {
    for (long i = 0; i < prime_len; ++i) {
        int byte = prime_buf[i];
        for (int b = 7; b >= 0; --b) {
            int bit = (byte >> b) & 1;
            predict();
            update(bit);
        }
    }
}

/* -------------------------------------------------- arithmetic coder ---- */
static U32 x1 = 0, x2 = 0xffffffffu, xdec = 0;
static FILE *fin, *fout;

static inline void enc_bit(int bit, int p) {
    U32 xmid = x1 + (U32)((((U64)(x2 - x1)) * (U32)p) >> 12);
    if (bit) x2 = xmid; else x1 = xmid + 1;
    while (((x1 ^ x2) & 0xff000000u) == 0) {
        putc((int)(x2 >> 24), fout);
        x1 <<= 8;
        x2 = (x2 << 8) | 255u;
    }
}
static inline int dec_bit(int p) {
    U32 xmid = x1 + (U32)((((U64)(x2 - x1)) * (U32)p) >> 12);
    int bit = (xdec <= xmid);
    if (bit) x2 = xmid; else x1 = xmid + 1;
    while (((x1 ^ x2) & 0xff000000u) == 0) {
        x1 <<= 8;
        x2 = (x2 << 8) | 255u;
        int c = getc(fin);
        xdec = (xdec << 8) | (U32)(c == EOF ? 0 : c);
    }
    return bit;
}

/* --------------------------------------------------------------- main --- */
static void die(const char *m) { fprintf(stderr, "crushcm: %s\n", m); exit(1); }

int main(int argc, char **argv) {
    if (argc < 4 || (argv[1][0] != 'c' && argv[1][0] != 'd'))
        die("usage: crushcm c|d <in> <out> [-p <prime-corpus>]");
    if (argc >= 6 && strcmp(argv[4], "-p") == 0) {
        FILE *pf = fopen(argv[5], "rb");
        if (!pf) die("cannot open prime corpus");
        fseek(pf, 0, SEEK_END); prime_len = ftell(pf); fseek(pf, 0, SEEK_SET);
        prime_buf = (U8 *)malloc((size_t)prime_len + 1);
        if (!prime_buf) die("out of memory");
        if (prime_len && fread(prime_buf, 1, (size_t)prime_len, pf) != (size_t)prime_len)
            die("prime read error");
        fclose(pf);
    }
    fin = fopen(argv[2], "rb");  if (!fin) die("cannot open input");
    fout = fopen(argv[3], "wb"); if (!fout) die("cannot open output");

    if (argv[1][0] == 'c') {
        fseek(fin, 0, SEEK_END);
        long n = ftell(fin);
        fseek(fin, 0, SEEK_SET);
        U8 *src = (U8 *)malloc((size_t)n + 1);
        if (n && fread(src, 1, (size_t)n, fin) != (size_t)n) die("read error");
        fputc('C', fout); fputc('C', fout); fputc('M', fout); fputc('1', fout);
        for (int i = 0; i < 8; ++i) fputc((int)((U64)n >> (8 * i)) & 0xff, fout);
        models_init((U32)(n + prime_len));
        update_contexts();
        if (prime_len) prime_run();
        for (long i = 0; i < n; ++i) {
            int byte = src[i];
            for (int b = 7; b >= 0; --b) {
                int bit = (byte >> b) & 1;
                int p = predict();
                enc_bit(bit, p);
                update(bit);
            }
        }
        putc((int)(x1 >> 24), fout); putc((int)(x1 >> 16) & 255, fout);
        putc((int)(x1 >> 8) & 255, fout); putc((int)x1 & 255, fout);
    } else {
        int m0 = getc(fin), m1 = getc(fin), m2 = getc(fin), m3 = getc(fin);
        if (m0 != 'C' || m1 != 'C' || m2 != 'M' || m3 != '1') die("not a crushcm stream");
        U64 n = 0;
        for (int i = 0; i < 8; ++i) n |= ((U64)(getc(fin) & 0xff)) << (8 * i);
        models_init((U32)(n + prime_len));
        update_contexts();
        if (prime_len) prime_run();
        for (int i = 0; i < 4; ++i) { int c = getc(fin); xdec = (xdec << 8) | (U32)(c == EOF ? 0 : c); }
        for (U64 i = 0; i < n; ++i) {
            int byte = 0;
            for (int b = 0; b < 8; ++b) {
                int p = predict();
                int bit = dec_bit(p);
                update(bit);
                byte = (byte << 1) | bit;
            }
            putc(byte, fout);
        }
    }
    fclose(fin); fclose(fout);
#ifdef RNN_STATS
    if (stat_n) fprintf(stderr, "[stats] bits=%ld  rnn=%.4f bpb  mix=%.4f bpb\n",
                        stat_n, stat_rnn / stat_n * 8.0, stat_mix / stat_n * 8.0);
#endif
    return 0;
}
