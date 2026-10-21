// jcm_codec.h - codes every quantized coefficient of a JPEG with jcm_model.
// The same function encodes (coefficients -> bits) and decodes (bits ->
// coefficients), so the two can never disagree about contexts.
#pragma once
#include "jcm1_model.h"
#include <brunsli/jpeg_data.h>

namespace jcm1 {


static const int kZig[64] = {0,  1,  8,  16, 9,  2,  3,  10, 17, 24, 32, 25, 18, 11, 4,  5,
                             12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6,  7,  14, 21, 28,
                             35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
                             58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63};

class Model {
 public:
  Model()
      : nz_(4, 18, 64, 1024),
        zero_(5, 18, 64 * 4, 4096),
        expo_(5, 18, 128, 4096),
        mant_(4, 18, 64 * 2, 2048),
        ecnt_(6, 18, 64, 1024),
        ezero_(6, 18, 64, 1024),
        eexpo_(5, 18, 128, 4096),
        emant_(4, 18, 128, 2048),
        esign_(5, 18, 64, 1024),
        sign_(4, 18, 64, 1024),
        dcz_(4, 18, 16, 1024),
        dce_(4, 18, 32, 1024),
        dcm_(3, 18, 32, 1024),
        dcs_(3, 18, 8, 256) {
    init_tabs();
  }

  // Codes all coefficients of all components. Returns false on corrupt input.
  bool run(Coder &c, brunsli::JPEGData &jpg) {
    for (size_t ci = 0; ci < jpg.components.size(); ++ci) {
      brunsli::JPEGComponent &comp = jpg.components[ci];
      const int W = comp.width_in_blocks, H = comp.height_in_blocks;
      if (W <= 0 || H <= 0) continue;
      if (comp.coeffs.size() < (size_t)W * H * 64) return false;
      const int cc = ci == 0 ? 0 : 1;               // luma / chroma
      std::vector<uint8_t> nzmap((size_t)W * H, 0);
      const int *q = nullptr;
      if (comp.quant_idx >= 0 && (size_t)comp.quant_idx < jpg.quant.size() &&
          jpg.quant[comp.quant_idx].values.size() >= 64)
        q = jpg.quant[comp.quant_idx].values.data();
      for (int by = 0; by < H; ++by) {
        for (int bx = 0; bx < W; ++bx) {
          brunsli::coeff_t *b = &comp.coeffs[((size_t)by * W + bx) * 64];
          const brunsli::coeff_t *A = by > 0 ? b - (size_t)W * 64 : nullptr;   // above
          const brunsli::coeff_t *L = bx > 0 ? b - 64 : nullptr;                // left
          const brunsli::coeff_t *AL = (A && L) ? A - 64 : nullptr;
          int nzA = A ? nzmap[(size_t)(by - 1) * W + bx] : -1;
          int nzL = L ? nzmap[(size_t)by * W + bx - 1] : -1;
          int nz = code_block_ac(c, b, A, L, AL, nzA, nzL, cc, q);
          if (nz < 0) return false;
          nzmap[(size_t)by * W + bx] = (uint8_t)nz;
          if (!code_dc(c, b, A, L, AL, cc, nz, q)) return false;
          if (c.overrun()) return false;
        }
      }
    }
    return true;
  }

 public:
  void stats(double *acc) {
    Decision *d[14] = {&nz_, &zero_, &expo_, &mant_, &sign_, &ecnt_, &ezero_, &eexpo_, &emant_, &esign_, &dcz_, &dce_, &dcm_, &dcs_};
    for (int i = 0; i < 14; ++i) acc[i] += d[i]->stat_bits / 8;
  }
 private:
  Decision nz_, zero_, expo_, mant_, ecnt_, ezero_, eexpo_, emant_, esign_, sign_, dcz_, dce_, dcm_, dcs_;

  // ---------------------------------------------------------------- AC
  // Order within a block (Lepton's): the 7x7 interior first, then the first
  // row and first column, then DC. The interior is then known when the edges
  // are coded, so each edge coefficient can be *predicted* - value and sign -
  // from continuity with the neighbouring block across the shared border.
  static inline bool interior(int pos) { return (pos & 7) && (pos >> 3); }

  // Edge prediction, in quantized units. dir 0: first row (v = 0, index u)
  // predicted from the block above; dir 1: first column (u = 0, index v)
  // predicted from the block to the left. Returns false if not available.
  static bool edge_pred(const brunsli::coeff_t *b, const brunsli::coeff_t *N, const int *q,
                        int dir, int i, int *out) {
    if (!N || !q) return false;
    // 1-D sums along the axis perpendicular to the border
    int64_t n7 = 0, n6 = 0, p0 = 0, p1 = 0;
    for (int t = 0; t < 8; ++t) {
      int pos = dir == 0 ? t * 8 + i : i * 8 + t;     // t runs across the border axis
      int64_t fn = (int64_t)N[pos] * q[pos];
      n7 += fn * kIdct[7][t];
      n6 += fn * kIdct[6][t];
      if (t > 0) {
        int64_t fb = (int64_t)b[pos] * q[pos];
        p0 += fb * kIdct[0][t];
        p1 += fb * kIdct[1][t];
      }
    }
    int qe = q[dir == 0 ? i : i * 8];
    if (qe <= 0) return false;
    int64_t num = (2 * n7 + (n7 - n6)) - (2 * p0 + (p0 - p1));
    int64_t v = rdiv(num, (int64_t)2 * 1448 * qe);
    if (v > 2047) v = 2047;
    if (v < -2047) v = -2047;
    *out = (int)v;
    return true;
  }

  int code_block_ac(Coder &c, brunsli::coeff_t *b, const brunsli::coeff_t *A,
                    const brunsli::coeff_t *L, const brunsli::coeff_t *AL,
                    int nzA, int nzL, int cc, const int *q) {
    const bool dec = c.decoding();
    int nz = 0;
    if (!dec)
      for (int k = 1; k < 64; ++k) nz += interior(kZig[k]) && b[kZig[k]] != 0;
    // number of non-zero interior coefficients (0..49), 6-bit binary tree
    int est = nzA >= 0 && nzL >= 0 ? (nzA + nzL + 1) / 2 : (nzA >= 0 ? nzA : (nzL >= 0 ? nzL : 64));
    int node = 1;
    for (int i = 5; i >= 0; --i) {
      int bit = (nz >> i) & 1;
      uint32_t ctx[4] = {hash(1, cc, est, node), hash(2, cc, (nzA + 1) >> 1, (nzL + 1) * 64 + node),
                         hash(3, cc, node), hash(4, cc, mbucket(est), node)};
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
      uint32_t zc[5] = {hash(11, cc, k, mbucket(a) * 32 + mbucket(l)), hash(12, cc, kb, lb),
                        hash(14, cc, k, (nz - left) * 64 + lb), hash(16, cc, k, ib2),
                        hash(17, cc, k, ib * 32 + pb + 1024 * lb)};
      int isnz = zero_.code(c, av != 0, zc, (cc * 128 + (k > 31 ? 31 : k) * 4 + (pb > 3 ? 3 : pb)) & 255,
                            (cc * 2048 + k * 32 + (lb > 15 ? 15 : lb) * 2 + (pb > 0)) & 4095);
      if (!isnz) { if (dec) b[pos] = 0; continue; }
      --left;
      av = code_mag(c, av, expo_, mant_, cc, k, kb, pos, pb, lb, ib, ib2, mbucket(a), mbucket(l), q, 0);
      int sa = A ? (A[pos] > 0) - (A[pos] < 0) + 1 : 3;
      int sl = L ? (L[pos] > 0) - (L[pos] < 0) + 1 : 3;
      int e = 0; { int t = av; while (t) { ++e; t >>= 1; } }
      uint32_t sc[4] = {hash(40, cc, k, sa * 4 + sl), hash(41, cc, pos), hash(42, cc, kb, e),
                        hash(43, cc, k, sa * 4 + sl + 16 * (e > 2))};
      int neg = sign_.code(c, v < 0, sc, (cc * 16 + sa * 4 + sl) & 63, (cc * 512 + k * 8 + sa * 2 + (sl & 1)) & 1023);
      if (dec) b[pos] = (brunsli::coeff_t)(neg ? -av : av);
    }
    if (left != 0) return -1;
    // ---- the two edges
    for (int dir = 0; dir < 2; ++dir) {
      const brunsli::coeff_t *N = dir == 0 ? A : L;
      int cnt = 0;
      if (!dec) for (int i = 1; i < 8; ++i) cnt += b[dir == 0 ? i : i * 8] != 0;
      int ncnt = -1;
      if (N) { ncnt = 0; for (int i = 1; i < 8; ++i) ncnt += N[dir == 0 ? i : i * 8] != 0; }
      int nzb = mbucket(nz);
      // the edge's predicted energy, from the border continuity predictions
      int pvs[8] = {0}; bool phas = false; int penergy = 0, pnz = 0;
      for (int i = 1; i < 8; ++i) {
        int pv = 0;
        if (edge_pred(b, N, q, dir, i, &pv)) { phas = true; penergy += std::abs(pv); pnz += pv != 0; }
        pvs[i] = pv;
      }
      int peb = phas ? mbucket(penergy) : 31;
      int node2 = 1;
      for (int t = 2; t >= 0; --t) {
        int bit = (cnt >> t) & 1;
        uint32_t ctx[6] = {hash(90, cc * 2 + dir, nzb, node2), hash(91, cc * 2 + dir, ncnt + 1, node2),
                           hash(92, cc * 2 + dir, nz, node2), hash(93, cc * 2 + dir, (ncnt + 1) * 32 + nzb, node2),
                           hash(94, cc * 2 + dir, peb * 8 + pnz, node2), hash(95, cc * 2 + dir, peb * 32 + nzb, node2)};
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
        const brunsli::coeff_t *O = dir == 0 ? L : A;   // the other neighbour, same position
        int o = O ? std::abs(O[pos]) : 0;
        int n1 = N ? std::abs(N[pos]) : 0;
        int in1 = std::abs(b[dir == 0 ? pos + 8 : pos + 1]);   // interior neighbour
        int v = dec ? 0 : b[pos];
        int av = std::abs(v);
        int ctxd = cc * 2 + dir;
        uint32_t zc[6] = {hash(100, ctxd, i, pvb * 32 + mbucket(eleft)), hash(101, ctxd, i, mbucket(n1) * 32 + mbucket(in1)),
                          hash(102, ctxd, i, mbucket(o) * 32 + pvb), hash(103, ctxd, i, (cnt - eleft) * 8 + eleft),
                          hash(104, ctxd, pvb, mbucket(in1)), hash(105, ctxd, i, nzb)};
        int isnz = ezero_.code(c, av != 0, zc, (ctxd * 16 + (pvb > 15 ? 15 : pvb)) & 63,
                               (ctxd * 256 + i * 32 + (pvb > 31 ? 31 : pvb)) & 1023);
        if (!isnz) { if (dec) b[pos] = 0; continue; }
        --eleft;
        av = code_mag(c, av, eexpo_, emant_, ctxd, i, i, pos, pvb, mbucket(eleft), mbucket(in1),
                      mbucket(n1) * 24 + mbucket(o), mbucket(n1), mbucket(o), q, 1, has ? apv : 0);
        int e = 0; { int t = av; while (t) { ++e; t >>= 1; } }
        // how far the magnitude is from the prediction tells how much to trust its sign
        int conf = !has ? 0 : (apv == 0 ? 1 : (std::abs(apv - av) * 4 <= av ? 3 : 2));
        uint32_t sc[5] = {hash(110, ctxd, psg, conf * 16 + e), hash(111, ctxd, i, psg * 4 + conf),
                          hash(112, ctxd, psg, mbucket(apv)), hash(113, ctxd, i, psg),
                          hash(114, ctxd, psg * 4 + conf, mbucket(apv) * 16 + e)};
        int neg = esign_.code(c, v < 0, sc, (ctxd * 16 + psg * 4 + conf) & 63, (ctxd * 256 + psg * 64 + conf * 16 + (e > 15 ? 15 : e)) & 1023);
        if (dec) b[pos] = (brunsli::coeff_t)(neg ? -av : av);
      }
      if (eleft != 0) return -1;
    }
    return nz;
  }

  // magnitude of a known-non-zero coefficient: unary bit length, then the
  // bits below the leading one
  int code_mag(Coder &c, int av, Decision &ex, Decision &mt, int cc, int k, int kb, int pos,
               int pb, int lb, int ib, int ib2, int ab, int lbn, const int *q, int fam, int hint = 0) {
    int he = 0; { int t = hint; while (t) { ++he; t >>= 1; } }
    const bool dec = c.decoding();
    int e = 0;
    if (!dec) { int t = av; while (t) { ++e; t >>= 1; } }
    int ee = 1;
    const int F = fam * 1000;
    while (ee < 15) {
      int hrel = hint ? (he > ee ? 2 : (he == ee ? 1 : 0)) : 3;
      uint32_t ec[5];
      if (fam == 0) {
        ec[0] = hash(F + 21, cc, kb, ab * 512 + lbn * 16 + ee); ec[1] = hash(F + 22, cc, ee, lb);
        ec[2] = hash(F + 25, cc, kb, (q ? mbucket(q[pos]) : 0) * 16 + ee);
        ec[3] = hash(F + 26, cc, k, ib2 * 16 + ee); ec[4] = hash(F + 27, cc, kb, (ib * 32 + pb) * 16 + ee);
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
      // does the prefix so far agree with the hint (a predicted magnitude)?
      int agree = he == e ? ((hint >> (j + 1)) == m ? 1 + ((hint >> j) & 1) : 3) : (he > e ? 4 : 5);
      uint32_t mc[4];
      if (fam == 0) {
        mc[0] = hash(F + 30, cc, e * 16 + j, kb); mc[1] = hash(F + 31, cc, e * 16 + j, pb * 64 + m);
        mc[2] = hash(F + 33, cc, pos, e * 16 + j); mc[3] = hash(F + 34, cc, e * 16 + j, ib2);
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
  // DC is predicted the way Lepton does it: the block's AC coefficients are
  // already known, so decode its pixels with DC = 0 and find the DC that makes
  // its edges continue the neighbouring blocks' edges. Integer IDCT only.
  static const int kIdct[8][8];
  // Row y (dir 0) or column x (dir 1) of a block's pixels, scaled by 2^24,
  // optionally without the DC term. Separable: 128 multiplies per line.
  static void line(const brunsli::coeff_t *b, const int *q, int dir, int idx, bool nodc, int64_t out[8]) {
    int64_t g[8];
    for (int f = 0; f < 8; ++f) {          // f: frequency along the line
      int64_t s = 0;
      for (int t = 0; t < 8; ++t) {        // t: frequency across the line
        int pos = dir == 0 ? t * 8 + f : f * 8 + t;
        if (nodc && pos == 0) continue;
        int v = b[pos];
        if (v) s += (int64_t)kIdct[idx][t] * v * q[pos];
      }
      g[f] = s;
    }
    for (int x = 0; x < 8; ++x) {
      int64_t s = 0;
      for (int f = 0; f < 8; ++f) s += g[f] * kIdct[x][f];
      out[x] = s;
    }
  }
  static int64_t rdiv(int64_t a, int64_t b) {        // round to nearest, b > 0
    return a >= 0 ? (a + b / 2) / b : -((-a + b / 2) / b);
  }
  bool code_dc(Coder &c, brunsli::coeff_t *b, const brunsli::coeff_t *A,
               const brunsli::coeff_t *L, const brunsli::coeff_t *AL, int cc, int nz, const int *q) {
    const bool dec = c.decoding();
    int loco = 0, act = 99;
    if (A && L) {
      int a = A[0], l = L[0], al = AL[0];
      int mx = a > l ? a : l, mn = a < l ? a : l;
      loco = al >= mx ? mn : (al <= mn ? mx : a + l - al);
      act = std::abs(a - al) + std::abs(l - al);
    } else if (A) { loco = A[0]; } else if (L) { loco = L[0]; }
    int pred = loco, spread = 64;
    if (q && q[0] > 0 && (A || L)) {
      int64_t est[16]; int ne = 0;
      const int64_t unit = (int64_t)1448 * 1448 * q[0];
      int64_t n7[8], n6[8], c0[8], c1[8];
      if (A) {
        line(A, q, 0, 7, false, n7); line(A, q, 0, 6, false, n6);
        line(b, q, 0, 0, true, c0);  line(b, q, 0, 1, true, c1);
        for (int t = 0; t < 8; ++t)
          est[ne++] = rdiv((2 * n7[t] + (n7[t] - n6[t])) - (2 * c0[t] + (c0[t] - c1[t])), 2 * unit);
      }
      if (L) {
        line(L, q, 1, 7, false, n7); line(L, q, 1, 6, false, n6);
        line(b, q, 1, 0, true, c0);  line(b, q, 1, 1, true, c1);
        for (int t = 0; t < 8; ++t)
          est[ne++] = rdiv((2 * n7[t] + (n7[t] - n6[t])) - (2 * c0[t] + (c0[t] - c1[t])), 2 * unit);
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
    uint32_t z[5] = {hash(50, cc, sb, nb), hash(51, cc, sb, db), hash(52, cc, nb, ab), hash(53, cc, sb), hash(54, cc, db, ab)};
    int isnz = dcz_.code(c, ar != 0, z, (cc * 8 + (sb > 7 ? 7 : sb)) & 15, (cc * 512 + (sb > 15 ? 15 : sb) * 32 + (db > 31 ? 31 : db)) & 1023);
    if (isnz) {
      int e = 0;
      if (!dec) { int t = ar; while (t) { ++e; t >>= 1; } }
      int ee = 1;
      while (ee < 16) {
        uint32_t ec[5] = {hash(60, cc, sb, ee), hash(61, cc, nb, ee), hash(62, cc, db * 32 + sb, ee), hash(63, cc, ab * 32 + nb, ee), hash(64, cc, ee)};
        int more = dce_.code(c, e > ee, ec, (cc * 16 + (ee > 15 ? 15 : ee)) & 31, (cc * 512 + (sb > 15 ? 15 : sb) * 32 + ee) & 1023);
        if (!more) break;
        ++ee;
      }
      if (dec) e = ee;
      int m = 1;
      for (int j = e - 2; j >= 0; --j) {
        int bit = (ar >> j) & 1;
        uint32_t mc[3] = {hash(70, cc, e * 16 + j), hash(71, cc, e * 16 + j, sb), hash(72, cc, e * 16 + j, m)};
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
      b[0] = (brunsli::coeff_t)v;
    }
    return true;
  }
};

const int Model::kIdct[8][8] = {{1448, 2009, 1892, 1703, 1448, 1138, 784, 400},
  {1448, 1703, 784, -400, -1448, -2009, -1892, -1138},
  {1448, 1138, -784, -2009, -1448, 400, 1892, 1703},
  {1448, 400, -1892, -1138, 1448, 1703, -784, -2009},
  {1448, -400, -1892, 1138, 1448, -1703, -784, 2009},
  {1448, -1138, -784, 2009, -1448, -400, 1892, -1703},
  {1448, -1703, 784, 400, -1448, 2009, -1892, 1138},
  {1448, -2009, 1892, -1703, 1448, -1138, 784, -400}};

}  // namespace jcm1
