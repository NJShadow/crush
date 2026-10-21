// jcm_codec.h - jcm version 2: codes every quantized coefficient of a JPEG
// with jcm_model. The same function encodes (coefficients -> bits) and
// decodes (bits -> coefficients), so the two can never disagree.
#pragma once
#include "jcm_model.h"
// chroma modelled from the co-located luma (part of format version 2)
#ifndef JCM_CFL
#define JCM_CFL 1
#endif
#include <brunsli/jpeg_data.h>
#include <atomic>
#include <thread>
#include <mutex>
#include <condition_variable>

namespace jcm {

static const int kZig[64] = {0,  1,  8,  16, 9,  2,  3,  10, 17, 24, 32, 25, 18, 11, 4,  5,
                             12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6,  7,  14, 21, 28,
                             35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
                             58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63};

// 8-point IDCT basis, scaled by 2^11 * sqrt(2)/2 ... (kIdct[x][f])
static const int kIdct[8][8] = {{1448, 2009, 1892, 1703, 1448, 1138, 784, 400},
  {1448, 1703, 784, -400, -1448, -2009, -1892, -1138},
  {1448, 1138, -784, -2009, -1448, 400, 1892, 1703},
  {1448, 400, -1892, -1138, 1448, 1703, -784, -2009},
  {1448, -400, -1892, 1138, 1448, -1703, -784, 2009},
  {1448, -1138, -784, 2009, -1448, -400, 1892, -1703},
  {1448, -1703, 784, 400, -1448, 2009, -1892, 1138},
  {1448, -2009, 1892, -1703, 1448, -1138, 784, -400}};
// Gradient-extrapolation weights across a block border: the value just past
// pixel row 7 is extrapolated as 3*row7 - row6 ... in the frequency domain
// these are the per-frequency weights of that linear combination.
struct EdgeW {
  int k76[8], k01[8];
  EdgeW() {
    for (int t = 0; t < 8; ++t) {
      k76[t] = 3 * kIdct[7][t] - kIdct[6][t];
      k01[t] = 3 * kIdct[0][t] - kIdct[1][t];
    }
  }
};
static const EdgeW g_edge;

static inline int64_t rdiv(int64_t a, int64_t b) {        // round to nearest, b > 0
  return a >= 0 ? (a + b / 2) / b : -((-a + b / 2) / b);
}

typedef brunsli::coeff_t coef;

// ------------------------------------------------------------ segmentation
// An image is coded as independent segments, each with its own model and its
// own arithmetic-coded stream, so that segments can be coded on different
// threads. The plan depends only on the image's dimensions (never on the
// machine), so every encoder and decoder agrees on it:
//   - luma and chroma are separate segments (they never shared statistics
//     to speak of: every context already carried the luma/chroma bit);
//   - a large component group is further cut into horizontal stripes of at
//     least 2^stripe_log2 blocks; a stripe's first row is coded as if it
//     were the top of the image.
// The stripe size is chosen by the encoder and stored as its log2 in the
// stream's first byte (12..30); 17 (131,072 blocks) is the default: on
// 18-megapixel photos it costs 0.08% against no stripes (see REPORT.md).
#ifndef JCM_STRIPE_LOG2
#define JCM_STRIPE_LOG2 17
#endif
static const int kStripeLog2Default = JCM_STRIPE_LOG2, kStripeLog2Min = 12, kStripeLog2Max = 30;
static const int kMaxParts = 16;
struct Segment {
  int c0, c1;          // components [c0, c1)
  int part, nparts;    // rows [H * part / nparts, H * (part + 1) / nparts) of each
  uint64_t blocks;
};
static inline int comp_rows(const brunsli::JPEGComponent &c, int part, int nparts, int *r0) {
  int64_t H = c.height_in_blocks;
  *r0 = (int)(H * part / nparts);
  return (int)(H * (part + 1) / nparts) - *r0;
}
static std::vector<Segment> plan(const brunsli::JPEGData &jpg, int stripe_log2 = kStripeLog2Default) {
  const uint64_t kStripeBlocks = (uint64_t)1 << stripe_log2;
  std::vector<Segment> out;
  const int nc = (int)jpg.components.size();
  for (int g = 0; g < 2; ++g) {
    int c0 = g == 0 ? 0 : 1, c1 = g == 0 ? (nc > 0 ? 1 : 0) : nc;
    uint64_t blocks = 0;
    int minH = 1 << 30;
    for (int ci = c0; ci < c1; ++ci) {
      const auto &c = jpg.components[ci];
      if (c.width_in_blocks <= 0 || c.height_in_blocks <= 0) continue;
      blocks += (uint64_t)c.width_in_blocks * c.height_in_blocks;
      if (c.height_in_blocks < minH) minH = c.height_in_blocks;
    }
    if (blocks == 0) continue;
    uint64_t np = blocks / kStripeBlocks;
    if (np < 1) np = 1;
    if (np > (uint64_t)kMaxParts) np = kMaxParts;
    if (np > (uint64_t)minH) np = (uint64_t)minH;
    for (int p = 0; p < (int)np; ++p) {
      Segment s{c0, c1, p, (int)np, 0};
      for (int ci = c0; ci < c1; ++ci) {
        const auto &c = jpg.components[ci];
        if (c.width_in_blocks <= 0 || c.height_in_blocks <= 0) continue;
        int r0, n = comp_rows(c, p, (int)np, &r0);
        s.blocks += (uint64_t)c.width_in_blocks * n;
      }
      out.push_back(s);
    }
  }
  return out;
}

// Chroma is modelled from the co-located luma, so a chroma segment may only
// read luma rows that are final. When decoding on several threads, luma
// segments publish their progress row by row and chroma waits for it.
struct LumaSync {
  std::mutex m;
  std::condition_variable cv;
  int np = 0, H = 0;
  int done[kMaxParts] = {0};
  bool failed = false;
  void publish(int part, int rows) {
    { std::lock_guard<std::mutex> l(m); done[part] = rows; }
    cv.notify_all();
  }
  void fail() {
    { std::lock_guard<std::mutex> l(m); failed = true; }
    cv.notify_all();
  }
  // true once luma row r is decoded; false if a luma segment failed
  bool wait_row(int r) {
    if (np <= 0 || H <= 0) return true;
    if (r >= H) r = H - 1;
    if (r < 0) return true;
    int p = 0;
    while (p + 1 < np && (int)((int64_t)H * (p + 1) / np) <= r) ++p;
    int r0 = (int)((int64_t)H * p / np);
    std::unique_lock<std::mutex> l(m);
    cv.wait(l, [&] { return failed || done[p] > r - r0; });
    return !failed;
  }
};

class Model {
 public:
#ifndef JCM_MAXBITS
#define JCM_MAXBITS 20
#endif
#ifndef JCM_SIZE_BIAS
#define JCM_SIZE_BIAS 3
#endif
  static const int kSizeBias = JCM_SIZE_BIAS, kMinBits = 12, kMaxBits = JCM_MAXBITS;
  // Table sizes follow the segment: the decoder knows the dimensions from
  // the metadata before any coefficient is decoded.
  explicit Model(uint64_t blocks)
      : empty_(32, 128), nz_(64, 1024), zero_(64 * 4, 4096), expo_(128, 4096), mant_(64 * 2, 2048),
        ecnt_(64, 1024), ezero_(64, 1024), eexpo_(128, 4096), emant_(128, 2048),
        esign_(64, 1024), sign_(64, 1024),
        dcz_(16, 1024), dce_(32, 1024), dcm_(32, 1024), dcs_(8, 256) {
    int lb = 0;
    while (lb < 40 && ((uint64_t)1 << lb) < blocks) ++lb;
    int tb = lb + kSizeBias;
    tb = tb < kMinBits ? kMinBits : (tb > kMaxBits ? kMaxBits : tb);
    empty_.size(tb); nz_.size(tb); zero_.size(tb); expo_.size(tb); mant_.size(tb); ecnt_.size(tb);
    ezero_.size(tb); eexpo_.size(tb); emant_.size(tb); esign_.size(tb); sign_.size(tb);
    dcz_.size(tb); dce_.size(tb); dcm_.size(tb); dcs_.size(tb);
  }

  // Codes all coefficients of one segment. Returns false on corrupt input.
  bool run(Coder &c, brunsli::JPEGData &jpg, const Segment &sg, LumaSync *sync = nullptr) {
    for (int ci = sg.c0; ci < sg.c1; ++ci) {
      brunsli::JPEGComponent &comp = jpg.components[ci];
      const int W = comp.width_in_blocks;
      if (W <= 0 || comp.height_in_blocks <= 0) continue;
      if (comp.coeffs.size() < (size_t)W * comp.height_in_blocks * 64) return false;
      int r0, nr = comp_rows(comp, sg.part, sg.nparts, &r0);
      const int cc = ci == 0 ? 0 : 1;               // luma / chroma
      std::vector<uint8_t> nzmap((size_t)W * 2, 0);  // this row and the one above
      const int *q = nullptr;
      if (comp.quant_idx >= 0 && (size_t)comp.quant_idx < jpg.quant.size() &&
          jpg.quant[comp.quant_idx].values.size() >= 64)
        q = jpg.quant[comp.quant_idx].values.data();
      if (q) for (int k = 0; k < 64; ++k) if (q[k] <= 0) { q = nullptr; break; }
      // per-block border vectors: bottom[] for the row above (updated in
      // place), right[] for the block to the left
      std::vector<int64_t> bottom((size_t)W * 8, 0);
      int64_t right[8] = {0};
#ifdef JCM_CFL
      const brunsli::JPEGComponent *luma = nullptr;
      int rh = 1, rv = 1;
      if (ci > 0 && jpg.components[0].width_in_blocks > 0 && jpg.components[0].height_in_blocks > 0 &&
          jpg.components[0].coeffs.size() >= (size_t)jpg.components[0].width_in_blocks * jpg.components[0].height_in_blocks * 64) {
        luma = &jpg.components[0];
        if (comp.h_samp_factor > 0 && luma->h_samp_factor >= comp.h_samp_factor) rh = luma->h_samp_factor / comp.h_samp_factor;
        if (comp.v_samp_factor > 0 && luma->v_samp_factor >= comp.v_samp_factor) rv = luma->v_samp_factor / comp.v_samp_factor;
      }
#endif
      for (int y = 0; y < nr; ++y) {
        const int by = r0 + y;
#ifdef JCM_CFL
        if (sync && luma && !sync->wait_row((by + 1) * rv - 1)) return false;
#endif
        uint8_t *mrow = &nzmap[(size_t)(y & 1) * W], *mabove = &nzmap[(size_t)((y + 1) & 1) * W];
        for (int bx = 0; bx < W; ++bx) {
          coef *b = &comp.coeffs[((size_t)by * W + bx) * 64];
          const coef *A = y > 0 ? b - (size_t)W * 64 : nullptr;    // above
          const coef *L = bx > 0 ? b - 64 : nullptr;                // left
          const coef *AL = (A && L) ? A - 64 : nullptr;
          int mA = A ? mabove[bx] : -1;
          int mL = L ? mrow[bx - 1] : -1;
          Ctx x;
          x.b = b; x.A = A; x.L = L; x.AL = AL; x.cc = cc; x.q = q;
#ifdef JCM_CFL
          x.lum = 31; x.Y = nullptr;
          if (luma) {
            int lx0 = bx * rh, ly0 = by * rv, tot = 0, cnt = 0;
            for (int dy = 0; dy < rv; ++dy)
              for (int dx = 0; dx < rh; ++dx) {
                int lx = lx0 + dx, ly = ly0 + dy;
                if (lx >= luma->width_in_blocks || ly >= luma->height_in_blocks) continue;
                const coef *yb = &luma->coeffs[((size_t)ly * luma->width_in_blocks + lx) * 64];
                if (!x.Y) x.Y = yb;
                for (int k = 1; k < 64; ++k) tot += yb[k] != 0;
                ++cnt;
              }
            if (cnt) x.lum = mbucket(tot / cnt);
          }
#endif
          x.gA = A && q ? &bottom[(size_t)bx * 8] : nullptr;
          x.gL = L && q ? right : nullptr;
          int m = code_block_ac(c, x, mA, mL);
          if (m < 0) return false;
          mrow[bx] = (uint8_t)m;
          if (!code_dc(c, x, m & 0x7F)) return false;
          if (c.overrun()) return false;
          if (q) border_out(b, q, &bottom[(size_t)bx * 8], right);
        }
        if (sync && ci == 0) sync->publish(sg.part, y + 1);
      }
    }
    return true;
  }

#ifdef JCM_STATS
#define JCM_DECS(X) X(empty_) X(nz_) X(zero_) X(expo_) X(mant_) X(sign_) X(ecnt_) X(ezero_) X(eexpo_) X(emant_) X(esign_) X(dcz_) X(dce_) X(dcm_) X(dcs_)
  static int stat_names(const char **nm) {
    int i = 0;
#define X(d) nm[i++] = #d;
    JCM_DECS(X)
#undef X
    return i;
  }
  void stats(double *bytes, double *n) {
    int i = 0;
#define X(d) bytes[i] += d.stat_bits / 8; n[i++] += d.stat_n;
    JCM_DECS(X)
#undef X
  }
#endif

 private:
#ifdef JCM_CFL
  Decision<4> empty_;
  Decision<4> nz_;
  Decision<6> zero_;
  Decision<4> expo_;
#else
  Decision<3> empty_;
  Decision<4> nz_;
  Decision<5> zero_, expo_;
#endif
  Decision<2> mant_;
  Decision<5> ecnt_;
  Decision<4> ezero_;
  Decision<5> eexpo_;
  Decision<4> emant_;
  Decision<5> esign_;
#ifdef SIGN_V1
  Decision<4> sign_;
#elif defined(SIGN2)
  Decision<2> sign_;
#else
  Decision<1> sign_;
#endif
  Decision<3> dcz_;
  Decision<4> dce_;
  Decision<1> dcm_;
  Decision<3> dcs_;

  struct Ctx {
    coef *b; const coef *A, *L, *AL; int cc; const int *q;
    const int64_t *gA, *gL;      // neighbours' extrapolated border vectors
    int64_t pT[8], pL[8];        // this block's own side of each border
#ifdef JCM_CFL
    int lum; const coef *Y;
#endif
  };

  static inline bool interior(int pos) { return (pos & 7) && (pos >> 3); }

  // The block's own contribution to the extrapolation across its top border
  // (pT, indexed by column u) and left border (pL, indexed by row v), from
  // its interior coefficients only.
  static void own_interior(Ctx &x) {
    for (int i = 0; i < 8; ++i) { x.pT[i] = 0; x.pL[i] = 0; }
    for (int v = 1; v < 8; ++v)
      for (int u = 1; u < 8; ++u) {
        int c = x.b[v * 8 + u];
        if (!c) continue;
        int64_t f = (int64_t)c * x.q[v * 8 + u];
        x.pT[u] += f * g_edge.k01[v];
        x.pL[v] += f * g_edge.k01[u];
      }
  }
  // After the whole block is known: its extrapolation across its bottom and
  // right borders, for the blocks below and to the right.
  static void border_out(const coef *b, const int *q, int64_t *bot, int64_t *rt) {
    for (int i = 0; i < 8; ++i) { bot[i] = 0; rt[i] = 0; }
    for (int pos = 0; pos < 64; ++pos) {
      int c = b[pos];
      if (!c) continue;
      int64_t f = (int64_t)c * q[pos];
      int v = pos >> 3, u = pos & 7;
      bot[u] += f * g_edge.k76[v];
      rt[v] += f * g_edge.k76[u];
    }
  }

  // Returns the interior non-zero count, | 0x80 if any AC coefficient is
  // non-zero; -1 on corrupt input. mA, mL: the same for the neighbours.
  int code_block_ac(Coder &c, Ctx &x, int mA, int mL) {
    const bool dec = c.decoding();
    coef *b = x.b;
    const coef *A = x.A, *L = x.L, *AL = x.AL;
    const int cc = x.cc;
    const int *q = x.q;
    const int nzA = mA < 0 ? -1 : (mA & 0x7F), nzL = mL < 0 ? -1 : (mL & 0x7F);
    // ---- is every AC coefficient zero? (about 30% of blocks)
    {
      int any = 0;
      if (!dec) for (int k = 1; k < 64; ++k) any |= b[k];
      const int eA = mA < 0 ? 2 : (mA >> 7), eL = mL < 0 ? 2 : (mL >> 7);
#ifdef JCM_CFL
      uint32_t ctx[4] = {hash(5, cc, eA * 3 + eL), hash(6, cc, mbucket(nzA + 1), mbucket(nzL + 1)),
                         hash(7, cc, eA * 3 + eL, (A ? std::abs(A[0] - (L ? L[0] : A[0])) : 0) > 2), hash(8, cc, x.lum, eA * 3 + eL)};
#else
      uint32_t ctx[3] = {hash(5, cc, eA * 3 + eL), hash(6, cc, mbucket(nzA + 1), mbucket(nzL + 1)),
                         hash(7, cc, eA * 3 + eL, (A ? std::abs(A[0] - (L ? L[0] : A[0])) : 0) > 2)};
#endif
      any = empty_.code(c, any != 0, ctx, cc * 9 + eA * 3 + eL, cc * 64 + (nzA + 1 > 7 ? 7 : nzA + 1) * 8 + (nzL + 1 > 7 ? 7 : nzL + 1));
      if (!any) {
        if (dec) for (int k = 1; k < 64; ++k) b[k] = 0;
        if (q) for (int i = 0; i < 8; ++i) { x.pT[i] = 0; x.pL[i] = 0; }
        return 0;
      }
    }
    int nz = 0;
    if (!dec)
      for (int k = 1; k < 64; ++k) nz += interior(kZig[k]) && b[kZig[k]] != 0;
    // number of non-zero interior coefficients (0..49), 6-bit binary tree
    int est = nzA >= 0 && nzL >= 0 ? (nzA + nzL + 1) / 2 : (nzA >= 0 ? nzA : (nzL >= 0 ? nzL : 64));
    int node = 1;
    for (int i = 5; i >= 0; --i) {
      int bit = (nz >> i) & 1;
#ifdef JCM_CFL
      uint32_t ctx[4] = {hash(9, cc, x.lum * 32 + mbucket(est), node), hash(2, cc, (nzA + 1) >> 1, (nzL + 1) * 64 + node),
                         hash(3, cc, node), hash(4, cc, mbucket(est), node)};
#else
      uint32_t ctx[4] = {hash(1, cc, est, node), hash(2, cc, (nzA + 1) >> 1, (nzL + 1) * 64 + node),
                         hash(3, cc, node), hash(4, cc, mbucket(est), node)};
#endif
      bit = nz_.code(c, bit, ctx, (cc * 32 + (i * 5)) & 63, (cc * 512 + (est & 63) * 8 + (5 - i)) & 1023);
      node = node * 2 + bit;
    }
    if (dec) nz = node - 64;
    if (nz > 49) return -1;
    int left = nz;
    for (int k = 1; k < 64 && left > 0; ++k) {
      const int pos = kZig[k];
      if (!interior(pos)) continue;
      int a = A ? std::abs(A[pos]) : 0;
      int l = L ? std::abs(L[pos]) : 0;
      int al = AL ? std::abs(AL[pos]) : 0;
      int pred = (A && L) ? (2 * a + 2 * l + al + 2) / 5 * 2 : (A ? 2 * a : (L ? 2 * l : 0));
      int pb = mbucket(pred);
      int lb = mbucket(left);
      int kb = k < 16 ? k : 16 + (k >> 3);
      int v = dec ? 0 : b[pos];
      int av = std::abs(v);
      const int u0 = pos & 7, v0 = pos >> 3;
      int iu = u0 > 1 ? std::abs(b[pos - 1]) : 0;       // interior neighbours only
      int iv = v0 > 1 ? std::abs(b[pos - 8]) : 0;
      int ib = mbucket(iu + iv);
      int ib2 = mbucket(iu) * 24 + mbucket(iv);
#ifdef JCM_CFL
      uint32_t zc[6] = {hash(11, cc, k, mbucket(a) * 32 + mbucket(l)), hash(12, cc, kb, lb),
                        hash(14, cc, k, (nz - left) * 64 + lb), hash(16, cc, k, ib2),
                        hash(17, cc, k, ib * 32 + pb + 1024 * lb), hash(18, cc, k, (x.lum * 24 + (x.Y ? mbucket(std::abs(x.Y[pos])) : 0)) * 24 + lb)};
#else
      uint32_t zc[5] = {hash(11, cc, k, mbucket(a) * 32 + mbucket(l)), hash(12, cc, kb, lb),
                        hash(14, cc, k, (nz - left) * 64 + lb), hash(16, cc, k, ib2),
                        hash(17, cc, k, ib * 32 + pb + 1024 * lb)};
#endif
      int isnz = zero_.code(c, av != 0, zc, (cc * 128 + (k > 31 ? 31 : k) * 4 + (pb > 3 ? 3 : pb)) & 255,
                            (cc * 2048 + k * 32 + (lb > 15 ? 15 : lb) * 2 + (pb > 0)) & 4095);
      if (!isnz) { if (dec) b[pos] = 0; continue; }
      --left;
      av = code_mag(c, av, expo_, mant_, cc, k, kb, pos, pb, lb, ib, ib2, mbucket(a), mbucket(l), q, 0);
      int sa = A ? (A[pos] > 0) - (A[pos] < 0) + 1 : 3;
      int sl = L ? (L[pos] > 0) - (L[pos] < 0) + 1 : 3;
      int e = nbits(av);
#ifdef SIGN_V1
      uint32_t sc[4] = {hash(40, cc, k, sa * 4 + sl), hash(41, cc, pos), hash(42, cc, kb, e),
                        hash(43, cc, k, sa * 4 + sl + 16 * (e > 2))};
      int neg = sign_.code(c, v < 0, sc, (cc * 16 + sa * 4 + sl) & 63, (cc * 512 + k * 8 + sa * 2 + (sl & 1)) & 1023);
#elif defined(SIGN_BYPASS)
      (void)e;
      int neg = c.code(v < 0, 2048);
#else
#ifdef SIGN2
      uint32_t sc[2] = {hash(40, cc, k, sa * 4 + sl), hash(43, cc, k, sa * 4 + sl + 16 * (e > 2))};
#else
      uint32_t sc[1] = {hash(43, cc, k, sa * 4 + sl + 16 * (e > 2))};
#endif
      int neg = sign_.code(c, v < 0, sc, (cc * 16 + sa * 4 + sl) & 63, (cc * 512 + k * 8 + sa * 2 + (sl & 1)) & 1023);
#endif
      if (dec) b[pos] = (coef)(neg ? -av : av);
    }
    if (left != 0) return -1;
    if (q) own_interior(x);
    // ---- the two edges, each coefficient predicted from border continuity
    for (int dir = 0; dir < 2; ++dir) {
      const coef *N = dir == 0 ? A : L;
      const int64_t *gN = dir == 0 ? x.gA : x.gL;
      const int64_t *pO = dir == 0 ? x.pT : x.pL;
      int cnt = 0;
      if (!dec) for (int i = 1; i < 8; ++i) cnt += b[dir == 0 ? i : i * 8] != 0;
      int ncnt = -1;
      if (N) { ncnt = 0; for (int i = 1; i < 8; ++i) ncnt += N[dir == 0 ? i : i * 8] != 0; }
      int nzb = mbucket(nz);
      int pvs[8] = {0}; bool phas = gN != nullptr; int penergy = 0, pnz = 0;
      if (phas)
        for (int i = 1; i < 8; ++i) {
          int qe = q[dir == 0 ? i : i * 8];
          int64_t v = rdiv(gN[i] - pO[i], (int64_t)2 * 1448 * qe);
          v = v > 2047 ? 2047 : (v < -2047 ? -2047 : v);
          pvs[i] = (int)v;
          penergy += std::abs(pvs[i]); pnz += pvs[i] != 0;
        }
      int peb = phas ? mbucket(penergy) : 31;
      int node2 = 1;
      for (int t = 2; t >= 0; --t) {
        int bit = (cnt >> t) & 1;
        uint32_t ctx[5] = {hash(90, cc * 2 + dir, nzb, node2), hash(91, cc * 2 + dir, ncnt + 1, node2),
                           hash(92, cc * 2 + dir, nz, node2), hash(95, cc * 2 + dir, peb * 32 + nzb, node2),
                           hash(94, cc * 2 + dir, peb * 8 + pnz, node2)};
        bit = ecnt_.code(c, bit, ctx, (cc * 2 + dir) * 8 + node2, ((cc * 2 + dir) * 256 + nzb * 8 + node2) & 1023);
        node2 = node2 * 2 + bit;
      }
      if (dec) cnt = node2 - 8;
      int eleft = cnt;
      for (int i = 1; i < 8 && eleft > 0; ++i) {
        const int pos = dir == 0 ? i : i * 8;
        int pv = pvs[i];
        bool has = phas;
        int apv = std::abs(pv);
        int pvb = has ? mbucket(apv) : 31;
        int psg = !has ? 3 : (pv > 0 ? 2 : (pv < 0 ? 0 : 1));
        const coef *O = dir == 0 ? L : A;   // the other neighbour, same position
        int o = O ? std::abs(O[pos]) : 0;
        int n1 = N ? std::abs(N[pos]) : 0;
        int in1 = std::abs(b[dir == 0 ? pos + 8 : pos + 1]);   // interior neighbour
        int v = dec ? 0 : b[pos];
        int av = std::abs(v);
        int ctxd = cc * 2 + dir;
        uint32_t zc[4] = {hash(100, ctxd, i, pvb * 32 + mbucket(eleft)), hash(101, ctxd, i, mbucket(n1) * 32 + mbucket(in1)),
                          hash(105, ctxd, i, nzb), hash(103, ctxd, i, (cnt - eleft) * 8 + eleft)};
        int isnz = ezero_.code(c, av != 0, zc, (ctxd * 16 + (pvb > 15 ? 15 : pvb)) & 63,
                               (ctxd * 256 + i * 32 + (pvb > 31 ? 31 : pvb)) & 1023);
        if (!isnz) { if (dec) b[pos] = 0; continue; }
        --eleft;
        av = code_mag(c, av, eexpo_, emant_, ctxd, i, i, pos, pvb, mbucket(eleft), mbucket(in1),
                      mbucket(n1) * 24 + mbucket(o), mbucket(n1), mbucket(o), q, 1, has ? apv : 0);
        int e = nbits(av);
        int conf = !has ? 0 : (apv == 0 ? 1 : (std::abs(apv - av) * 4 <= av ? 3 : 2));
        uint32_t sc[5] = {hash(110, ctxd, psg, conf * 16 + e), hash(111, ctxd, i, psg * 4 + conf),
                          hash(112, ctxd, psg, mbucket(apv)), hash(113, ctxd, i, psg),
                          hash(114, ctxd, psg * 4 + conf, mbucket(apv) * 16 + e)};
        int neg = esign_.code(c, v < 0, sc, (ctxd * 16 + psg * 4 + conf) & 63, (ctxd * 256 + psg * 64 + conf * 16 + (e > 15 ? 15 : e)) & 1023);
        if (dec) b[pos] = (coef)(neg ? -av : av);
      }
      if (eleft != 0) return -1;
    }
    return nz | 0x80;
  }

  // magnitude of a known-non-zero coefficient: unary bit length, then the
  // bits below the leading one
  template <class DE, class DM>
  int code_mag(Coder &c, int av, DE &ex, DM &mt, int cc, int k, int kb, int pos,
               int pb, int lb, int ib, int ib2, int ab, int lbn, const int *q, int fam, int hint = 0) {
    int he = nbits(hint);
    const bool dec = c.decoding();
    int e = dec ? 0 : nbits(av);
    int ee = 1;
    const int F = fam * 1000;
    const int qb = q ? mbucket(q[pos]) : 0;
    while (ee < 15) {
      int hrel = hint ? (he > ee ? 2 : (he == ee ? 1 : 0)) : 3;
      uint32_t ec[5];
      if (fam == 0) {
        ec[0] = hash(F + 27, cc, kb, (ib * 32 + pb) * 16 + ee); ec[1] = hash(F + 22, cc, ee, lb);
        ec[2] = hash(F + 25, cc, kb, qb * 16 + ee);
        ec[3] = hash(F + 26, cc, k, ib2 * 16 + ee); ec[4] = 0;
      } else {
        ec[0] = hash(F + 28, cc, k, hrel * 16 + ee); ec[1] = hash(F + 21, cc, kb, ab * 512 + lbn * 16 + ee);
        ec[2] = hash(F + 22, cc, ee, lb); ec[3] = hash(F + 27, cc, kb, (ib * 32 + pb) * 16 + ee);
        ec[4] = hash(F + 29, cc, k, pb * 16 + ee);
      }
      int more = ex.code(c, e > ee, ec, (cc * 32 + (ee > 15 ? 15 : ee) * 2 + (pb > 2)) & 127,
                         (cc * 2048 + (kb & 31) * 64 + ee * 4 + (pb > 3 ? 3 : pb)) & 4095);
      if (!more) break;
      ++ee;
    }
    if (dec) e = ee;
    int m = 1;
    for (int j = e - 2; j >= 0; --j) {
      int bit = (av >> j) & 1;
      int agree = he == e ? ((hint >> (j + 1)) == m ? 1 + ((hint >> j) & 1) : 3) : (he > e ? 4 : 5);
      uint32_t mc[4];
      if (fam == 0) {
        mc[0] = hash(F + 33, cc, pos, e * 16 + j); mc[1] = hash(F + 34, cc, e * 16 + j, ib2);
        mc[2] = mc[3] = 0;
      } else {
        mc[0] = hash(F + 31, cc, e * 16 + j, pb * 64 + m); mc[1] = hash(F + 33, cc, pos, e * 16 + j);
        mc[2] = hash(F + 34, cc, e * 16 + j, ib2); mc[3] = hash(F + 35, cc, e * 16 + j, agree);
      }
      bit = mt.code(c, bit, mc, (cc * 32 + e * 2 + (j == e - 2)) & 127,
                    (cc * 1024 + e * 64 + j * 4 + (pb > 3 ? 3 : pb)) & 2047);
      m = m * 2 + bit;
    }
    return dec ? m : av;
  }

  // ---------------------------------------------------------------- DC
  // Predicted the way Lepton does it: the block's AC coefficients are known,
  // so find the DC that makes its edge pixels continue the neighbours' edge
  // pixels (gradient extrapolated from both sides). Done in the frequency
  // domain along each border with integer arithmetic only.
  bool code_dc(Coder &c, Ctx &x, int nz) {
    const bool dec = c.decoding();
    coef *b = x.b;
    const coef *A = x.A, *L = x.L, *AL = x.AL;
    const int cc = x.cc;
    const int *q = x.q;
    int loco = 0, act = 99;
    if (A && L) {
      int a = A[0], l = L[0], al = AL[0];
      int mx = a > l ? a : l, mn = a < l ? a : l;
      loco = al >= mx ? mn : (al <= mn ? mx : a + l - al);
      act = std::abs(a - al) + std::abs(l - al);
    } else if (A) { loco = A[0]; } else if (L) { loco = L[0]; }
    int pred = loco, spread = 64;
    if (q && (A || L)) {
      int64_t est[16]; int ne = 0;
      const int64_t unit2 = (int64_t)2 * 1448 * 1448 * q[0];
      for (int dir = 0; dir < 2; ++dir) {
        const int64_t *gN = dir == 0 ? x.gA : x.gL;
        if (!gN) continue;
        // own side of this border, now that the edges are known
        int64_t D[8];
        for (int f = 0; f < 8; ++f) {
          int64_t own;
          if (f == 0) {   // column/row 0 of the other edge, without DC
            own = 0;
            for (int t = 1; t < 8; ++t) {
              int pos = dir == 0 ? t * 8 : t;
              own += (int64_t)b[pos] * q[pos] * g_edge.k01[t];
            }
          } else {
            int pos = dir == 0 ? f : f * 8;
            own = (dir == 0 ? x.pT[f] : x.pL[f]) + (int64_t)b[pos] * q[pos] * g_edge.k01[0];
          }
          D[f] = gN[f] - own;
        }
        for (int p = 0; p < 8; ++p) {
          int64_t s = 0;
          for (int f = 0; f < 8; ++f) s += D[f] * kIdct[p][f];
          est[ne++] = rdiv(s, unit2);
        }
      }
      int64_t sum = 0, mn = est[0], mx = est[0];
      for (int i = 0; i < ne; ++i) { sum += est[i]; if (est[i] < mn) mn = est[i]; if (est[i] > mx) mx = est[i]; }
      int64_t p = rdiv(sum, ne);
      if (p < -32768) p = -32768;
      if (p > 32767) p = 32767;
      pred = (int)p;
      int64_t sp = mx - mn;
      spread = sp > 4095 ? 4095 : (int)sp;
    }
    int r = dec ? 0 : b[0] - pred;
    int ar = std::abs(r);
    int sb = mbucket(spread), nb = mbucket(nz), db = mbucket(std::abs(pred - loco)), ab = mbucket(act);
    uint32_t z[3] = {hash(53, cc, sb), hash(51, cc, sb, db), hash(52, cc, nb, ab)};
    int isnz = dcz_.code(c, ar != 0, z, (cc * 8 + (sb > 7 ? 7 : sb)) & 15, (cc * 512 + (sb > 15 ? 15 : sb) * 32 + (db > 31 ? 31 : db)) & 1023);
    if (isnz) {
      int e = dec ? 0 : nbits(ar);
      int ee = 1;
      while (ee < 16) {
        uint32_t ec[4] = {hash(60, cc, sb, ee), hash(61, cc, nb, ee), hash(62, cc, db * 32 + sb, ee), hash(63, cc, ab * 32 + nb, ee)};
        int more = dce_.code(c, e > ee, ec, (cc * 16 + (ee > 15 ? 15 : ee)) & 31, (cc * 512 + (sb > 15 ? 15 : sb) * 32 + ee) & 1023);
        if (!more) break;
        ++ee;
      }
      if (dec) e = ee;
      int m = 1;
      for (int j = e - 2; j >= 0; --j) {
        int bit = (ar >> j) & 1;
        uint32_t mc[1] = {hash(71, cc, e * 16 + j, sb)};
        bit = dcm_.code(c, bit, mc, (cc * 16 + e) & 31, (cc * 512 + e * 32 + j) & 1023);
        m = m * 2 + bit;
      }
      if (dec) ar = m;
      int sd = pred > loco ? 0 : (pred < loco ? 1 : 2);
      uint32_t sc[3] = {hash(80, cc, sd), hash(81, cc, sb, sd), hash(82, cc, e, sd)};
      int neg = dcs_.code(c, r < 0, sc, cc * 3 + sd, cc * 64 + sd * 16 + (e > 15 ? 15 : e));
      if (dec) r = neg ? -ar : ar;
    }
    if (dec) {
      int v = pred + r;
      if (v < -32768 || v > 32767) return false;
      b[0] = (coef)v;
    }
    return true;
  }
};

// ------------------------------------------------------------- whole image
// Stream layout: for each segment but the last, its coded length (u32 LE);
// then the segments' streams in plan order.
static bool encode_segment(const brunsli::JPEGData &jpg, const Segment &sg, std::vector<uint8_t> &out) {
  out.clear();
  Coder c(&out);
  Model m(sg.blocks);
  brunsli::JPEGData &j = const_cast<brunsli::JPEGData &>(jpg);   // encoding only reads
  if (!m.run(c, j, sg)) return false;
  c.flush();
  return true;
}
static bool decode_segment(const uint8_t *p, size_t n, brunsli::JPEGData &jpg, const Segment &sg, LumaSync *sync) {
  Coder c(p, n);
  Model m(sg.blocks);
  return m.run(c, jpg, sg, sync) && !c.overrun();
}

// Runs job(i) for i in [0, n) on up to `threads` threads (this one included).
// Results never depend on the thread count: each job owns its output.
template <class F>
static void parallel_for(int n, int threads, F job) {
  if (threads > n) threads = n;
  if (threads <= 1) { for (int i = 0; i < n; ++i) job(i); return; }
  std::atomic<int> next(0);
  auto worker = [&]() { for (int i; (i = next.fetch_add(1)) < n;) job(i); };
  std::vector<std::thread> pool;
  for (int t = 1; t < threads; ++t) {
    try { pool.emplace_back(worker); } catch (...) { break; }   // fewer threads, same result
  }
  worker();
  for (auto &t : pool) t.join();
}

static bool encode_image(const brunsli::JPEGData &jpg, std::vector<uint8_t> &out, int threads,
                         int stripe_log2 = kStripeLog2Default) {
  if (stripe_log2 < kStripeLog2Min || stripe_log2 > kStripeLog2Max) return false;
  std::vector<Segment> sg = plan(jpg, stripe_log2);
  std::vector<std::vector<uint8_t>> bufs(sg.size());
  std::vector<char> ok(sg.size(), 0);
  parallel_for((int)sg.size(), threads, [&](int i) {
    try { ok[i] = encode_segment(jpg, sg[i], bufs[i]); } catch (...) { ok[i] = 0; }
  });
  out.assign(1, (uint8_t)stripe_log2);
  for (size_t i = 0; i < sg.size(); ++i) {
    if (!ok[i] || bufs[i].size() > 0xFFFFFFFFu) return false;
    if (i + 1 < sg.size())
      for (int k = 0; k < 4; ++k) out.push_back((uint8_t)(bufs[i].size() >> (8 * k)));
  }
  for (auto &b : bufs) out.insert(out.end(), b.begin(), b.end());
  return true;
}

static bool decode_image(const uint8_t *p, size_t n, brunsli::JPEGData &jpg, int threads) {
  if (n < 1 || p[0] < kStripeLog2Min || p[0] > kStripeLog2Max) return false;
  std::vector<Segment> sg = plan(jpg, p[0]);
  ++p; --n;
  if (sg.empty()) return true;
  size_t hdr = (sg.size() - 1) * 4;
  if (n < hdr) return false;
  std::vector<size_t> off(sg.size()), len(sg.size());
  size_t pos = hdr;
  for (size_t i = 0; i < sg.size(); ++i) {
    size_t l;
    if (i + 1 < sg.size()) {
      const uint8_t *h = p + 4 * i;
      l = (size_t)h[0] | ((size_t)h[1] << 8) | ((size_t)h[2] << 16) | ((size_t)h[3] << 24);
      if (l > n - pos) return false;
    } else {
      l = n - pos;
    }
    off[i] = pos; len[i] = l; pos += l;
  }
  std::vector<char> ok(sg.size(), 0);
  LumaSync sync;
  if (sg[0].c0 == 0 && sg[0].c1 == 1) { sync.np = sg[0].nparts; sync.H = jpg.components[0].height_in_blocks; }
  parallel_for((int)sg.size(), threads, [&](int i) {
    try { ok[i] = decode_segment(p + off[i], len[i], jpg, sg[i], &sync); } catch (...) { ok[i] = 0; }
    if (!ok[i]) sync.fail();
  });
  for (char k : ok) if (!k) return false;
  return true;
}

}  // namespace jcm
