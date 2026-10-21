// jcm_model.h - context-mixing primitives for jcm version 2.
// Integer-only (deterministic on every platform). Encoder and decoder share
// one code path: code(bit) either writes the bit or reads it.
//
// Version 1 lives, frozen, in jcm1_model.h / jcm1_codec.h. Anything in this
// file may change between format versions; nothing in those two may.
#pragma once
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <vector>
#include <new>
#if defined(__SSE2__) && !defined(JCM_NO_SIMD)
#include <emmintrin.h>
#endif
#ifdef JCM_STATS
#include <cmath>
#endif

// Out of memory: an exception natively (the helper reports failure for that
// image); WebAssembly builds have no exceptions, so the module traps and the
// JavaScript side discards the instance.
#if defined(__cpp_exceptions) || defined(__EXCEPTIONS)
#define JCM_OOM() throw std::bad_alloc()
#else
#define JCM_OOM() abort()
#endif

namespace jcm {

// ---------------------------------------------------------------- utilities
struct Tabs {
  int squash[4096];
  short stretch[4096];
  int recip[1024];
  Tabs() {
    static const int t[33] = {1, 2, 3, 6, 10, 16, 27, 45, 73, 120, 194, 310, 488, 747, 1101,
                              1546, 2047, 2549, 2994, 3348, 3607, 3785, 3901, 3975, 4022,
                              4050, 4068, 4079, 4085, 4089, 4092, 4093, 4094};
    for (int i = 0; i < 4096; ++i) {
      int d = i - 2048;
      int r;
      if (d > 2047) r = 4095;
      else if (d < -2047) r = 1;
      else {
        int w = d & 127, k = (d >> 7) + 16;
        r = (t[k] * (128 - w) + t[k + 1] * w + 64) >> 7;
        r = r < 1 ? 1 : (r > 4095 ? 4095 : r);
      }
      squash[i] = r;
    }
    int pi = 0;
    for (int x = -2047; x <= 2047; ++x) {
      int v = squash[x + 2048];
      for (int j = pi; j <= v; ++j) stretch[j] = (short)x;
      pi = v + 1;
    }
    for (int j = pi; j < 4096; ++j) stretch[j] = 2047;
    for (int n = 0; n < 1024; ++n) recip[n] = 65536 * 2 / (2 * n + 3);
  }
};
// Built during static initialisation, before main() and so before any
// worker thread exists; read-only afterwards.
static const Tabs g_tabs;

static inline int squash(int d) {
  if (d > 2047) d = 2047;
  if (d < -2047) d = -2047;
  return g_tabs.squash[d + 2048];
}
static inline int stretch(int p) { return g_tabs.stretch[p]; }

static inline int ilog2(uint32_t v) { int r = 0; while (v >>= 1) ++r; return r; }
static inline int nbits(uint32_t v) { int r = 0; while (v) { ++r; v >>= 1; } return r; }
// bucket of a non-negative magnitude: 0,1,2,3,4-5,6-7,8-11,12-15,...
static inline int mbucket(int v) {
  if (v < 4) return v;
  int l = ilog2((uint32_t)v);
  int half = (v >> (l - 1)) & 1;
  int b = 2 * l + half;
  return b > 23 ? 23 : b;
}

// Context hash. Inputs are small integers; the multiplies carry their low
// bits upward and tables are indexed by the TOP bits of the result.
static inline uint32_t hash(uint32_t a, uint32_t b = 0, uint32_t c = 0, uint32_t d = 0) {
  uint32_t h = (a * 0x9E3779B1u) ^ (b * 0x85EBCA77u) ^ (c * 0xC2B2AE3Du) ^ (d * 0x27D4EB2Fu);
  h ^= h >> 15;
  return h * 0x2C1B3C6Du;
}

// ------------------------------------------------------------ arithmetic coder
class Coder {
 public:
  explicit Coder(std::vector<uint8_t> *out) : out_(out), in_(nullptr), n_(0), p_(0) {}
  Coder(const uint8_t *in, size_t n) : out_(nullptr), in_(in), n_(n), p_(0) {
    for (int i = 0; i < 4; ++i) x_ = (x_ << 8) | next();
  }
  bool decoding() const { return out_ == nullptr; }
  // p12 = P(bit == 1) in 1..4095
  inline int code(int bit, int p12) {
    uint32_t xmid = x1_ + (uint32_t)(((uint64_t)(x2_ - x1_) * (uint32_t)p12) >> 12);
    if (!out_) bit = x_ <= xmid;
    if (bit) x2_ = xmid; else x1_ = xmid + 1;
    while (((x1_ ^ x2_) & 0xff000000u) == 0) {
      if (out_) out_->push_back((uint8_t)(x2_ >> 24));
      else x_ = (x_ << 8) | next();
      x1_ <<= 8;
      x2_ = (x2_ << 8) | 255;
    }
    return bit;
  }
  void flush() {
    for (int i = 0; i < 4; ++i) { out_->push_back((uint8_t)(x1_ >> 24)); x1_ <<= 8; }
  }
  bool overrun() const { return p_ > n_ + 4; }
 private:
  inline uint8_t next() { return p_ < n_ ? in_[p_++] : (p_++, 0); }
  std::vector<uint8_t> *out_;
  const uint8_t *in_;
  size_t n_, p_;
  uint32_t x1_ = 0, x2_ = 0xffffffffu, x_ = 0;
};

// --------------------------------------------------------- adaptive counters
// p is stored XOR 0x8000, so an all-zero slot is the initial state
// (p = 1/2, n = 0) and tables come from calloc: pages the image never
// touches are never faulted in.
struct Slot { uint16_t p; uint16_t n; };

class CounterTable {
 public:
  CounterTable() {}
  void init(int bits) {
    free(t_);
    shift_ = 32 - bits;
    t_ = (Slot *)calloc((size_t)1 << bits, sizeof(Slot));
    if (!t_) JCM_OOM();
  }
  ~CounterTable() { free(t_); }
  CounterTable(const CounterTable &) = delete;
  CounterTable &operator=(const CounterTable &) = delete;
  inline Slot *at(uint32_t h) const { return &t_[h >> shift_]; }
 private:
  int shift_ = 32;
  Slot *t_ = nullptr;
};

static const int kCountLimit = 255;  // counter adaptation floor 1/(limit+1.5)
static inline int slot_p(const Slot *s) { return s->p ^ 0x8000; }
static inline void slot_update(Slot *s, int bit) {
  int p = s->p ^ 0x8000;
  int n = s->n;
  p += (((bit << 16) - bit - p) * g_tabs.recip[n]) >> 16;
  s->p = (uint16_t)(p ^ 0x8000);
  s->n = (uint16_t)(n + (n < kCountLimit));
}

// ----------------------------------------------------------------- APM / SSE
// Maps (probability, context) to a refined probability.
class APM {
 public:
  // Entries are stored as the difference from their initial value, so the
  // table can come from calloc and untouched pages cost nothing.
  explicit APM(int n) : n_(n) {
    for (int j = 0; j < 33; ++j) row_[j] = squash((j - 16) * 128) * 16;
    t_ = (int *)calloc((size_t)n * 33, sizeof(int));
    if (!t_) JCM_OOM();
  }
  ~APM() { free(t_); }
  APM(const APM &) = delete;
  APM &operator=(const APM &) = delete;
  // st = stretched probability; cx must be < n
  inline int pp(int st, int cx) {
    int s = st + 2048;
    if (s < 0) s = 0;
    if (s > 4095) s = 4095;
    int lo = s >> 7, w = s & 127;
    lo_ = lo;
    idx_ = (size_t)cx * 33 + lo;
    return ((t_[idx_] + row_[lo]) * (128 - w) + (t_[idx_ + 1] + row_[lo + 1]) * w) >> 11;
  }
  inline void update(int bit, int rate = 7) {
    int g = (bit << 16) + (bit << rate) - bit - bit;
    t_[idx_] += (g - (t_[idx_] + row_[lo_])) >> rate;
    t_[idx_ + 1] += (g - (t_[idx_ + 1] + row_[lo_ + 1])) >> rate;
  }
  int size() const { return n_; }
 private:
  int n_;
  int row_[33];
  int *t_ = nullptr;
  size_t idx_ = 0;
  int lo_ = 0;
};

// A single directly indexed adaptive counter per context: for decisions that
// carry almost no information, where mixing would cost more than it saves.
class Direct {
 public:
#ifdef JCM_STATS
  double stat_bits = 0; long stat_n = 0;
#endif
  explicit Direct(int n) : n_(n), t_((size_t)n) { memset(t_.data(), 0, t_.size() * sizeof(Slot)); }
  inline int code(Coder &c, int bit, int cx) {
    Slot *s = &t_[(size_t)cx];
    int p = slot_p(s) >> 4;
    p = p < 1 ? 1 : (p > 4095 ? 4095 : p);
    bit = c.code(bit, p);
#ifdef JCM_STATS
    stat_bits += -std::log2((bit ? p : 4096 - p) / 4096.0); stat_n++;
#endif
    slot_update(s, bit);
    return bit;
  }
 private:
  int n_;
  std::vector<Slot> t_;
};

// ------------------------------------------------------------------ mixer
// Eight 16-bit lanes (up to 7 inputs + bias). Weights are 16-bit, 1.0 = 4096.
// The SSE2 path and the scalar path compute exactly the same integers: the
// dot product cannot overflow 32 bits (8 * 2047 * 32767 < 2^31) and both
// saturate the updated weights to 16 bits.
static const int kMixerRate = 6;
static const int kApmWeight = 2;     // of 4: APM share of the final probability

static inline int mix_dot(const int16_t *st, const int16_t *w) {
#if defined(__SSE2__) && !defined(JCM_NO_SIMD)
  __m128i a = _mm_loadu_si128((const __m128i *)st), b = _mm_loadu_si128((const __m128i *)w);
  __m128i m = _mm_madd_epi16(a, b);
  m = _mm_add_epi32(m, _mm_shuffle_epi32(m, 0x4E));
  m = _mm_add_epi32(m, _mm_shuffle_epi32(m, 0xB1));
  return _mm_cvtsi128_si32(m);
#else
  int s = 0;
  for (int i = 0; i < 8; ++i) s += st[i] * w[i];
  return s;
#endif
}
// w[i] = sat16(w[i] + ((st[i] * err + 2^(sh-1)) >> sh)), |err| < 2^15
static inline void mix_train(const int16_t *st, int16_t *w, int err, int sh = 18) {
#if defined(__SSE2__) && !defined(JCM_NO_SIMD)
  __m128i a = _mm_loadu_si128((const __m128i *)st);
  __m128i e = _mm_set1_epi16((short)err);
  __m128i lo = _mm_mullo_epi16(a, e), hi = _mm_mulhi_epi16(a, e);
  __m128i p0 = _mm_unpacklo_epi16(lo, hi), p1 = _mm_unpackhi_epi16(lo, hi);
  __m128i rnd = _mm_set1_epi32(1 << (sh - 1));
  __m128i cnt = _mm_cvtsi32_si128(sh);
  p0 = _mm_sra_epi32(_mm_add_epi32(p0, rnd), cnt);
  p1 = _mm_sra_epi32(_mm_add_epi32(p1, rnd), cnt);
  __m128i d = _mm_packs_epi32(p0, p1);
  __m128i wv = _mm_loadu_si128((const __m128i *)w);
  _mm_storeu_si128((__m128i *)w, _mm_adds_epi16(wv, d));
#else
  for (int i = 0; i < 8; ++i) {
    int d = (st[i] * err + (1 << (sh - 1))) >> sh;
    d = d > 32767 ? 32767 : (d < -32768 ? -32768 : d);
    int nw = w[i] + d;
    w[i] = (int16_t)(nw > 32767 ? 32767 : (nw < -32768 ? -32768 : nw));
  }
#endif
}

// --------------------------------------------------------------- a decision
// A family of binary events sharing counter table, mixer and APM. N context
// slots are mixed in the logistic domain by a weight set chosen by a small
// selector, then refined by an APM.
#ifdef JCM_ABLATE
static int g_abl_dec = -1, g_abl_in = -1, g_dec_counter = 0;
#endif
template <int N>
class Decision {
  static_assert(N >= 1 && N <= 7, "at most 7 inputs + bias");
 public:
#ifdef JCM_STATS
  double stat_bits = 0; long stat_n = 0;
#endif
  Decision(int nsel, int apm_n) : nsel_(nsel), w_((size_t)(nsel + 1) * 8, 0), apm_(apm_n) {
#ifdef JCM_ABLATE
    id_ = g_dec_counter++ % 64;
#endif
    for (int s = 0; s <= nsel; ++s)
      for (int i = 0; i <= N; ++i) w_[(size_t)s * 8 + i] = (int16_t)(4096 / (N + 1));
  }
  void size(int tbits) { tab_.init(tbits); }

  // h: N context hashes; sel: mixer selector (< nsel); acx: APM context.
  // Two mixers see the same inputs: one with a weight set chosen by sel,
  // one global (it learns fast, which matters on small images); their
  // outputs are averaged in the logistic domain, then refined by the APM.
  inline int code(Coder &c, int bit, const uint32_t *h, int sel, int acx) {
#ifdef JCM_CHECK
    if (sel < 0 || sel >= nsel_ || acx < 0 || acx >= apm_.size()) abort();
#endif
    Slot *s[N];
    alignas(16) int16_t st[8] = {0, 0, 0, 0, 0, 0, 0, 0};
#ifdef JCM_ABLATE
    for (int i = 0; i < N; ++i) s[i] = tab_.at(id_ == g_abl_dec && i == g_abl_in ? 0x12345678u : h[i]);
#else
    for (int i = 0; i < N; ++i) s[i] = tab_.at(h[i]);
#endif
    for (int i = 0; i < N; ++i) st[i] = (int16_t)stretch(slot_p(s[i]) >> 4);
    st[N] = 256;
    int16_t *w1 = &w_[(size_t)(sel + 1) * 8], *w0 = &w_[0];
    int d1 = mix_dot(st, w1) >> 12, d0 = mix_dot(st, w0) >> 12;
    d1 = d1 > 2047 ? 2047 : (d1 < -2047 ? -2047 : d1);
    d0 = d0 > 2047 ? 2047 : (d0 < -2047 ? -2047 : d0);
    const int p1 = g_tabs.squash[d1 + 2048], p0 = g_tabs.squash[d0 + 2048];
    const int d = (d1 + d0) >> 1;
    const int pm = g_tabs.squash[d + 2048];
    const int pa = apm_.pp(d, acx);
#ifdef JCM_APM2
    const int pb = apm2_.pp(d, sel);
    int p = (pm * 2 + pa + pb + 2) >> 2;
#else
    int p = (pm * (4 - kApmWeight) + pa * kApmWeight + 2) >> 2;
#endif
    p = p < 1 ? 1 : (p > 4095 ? 4095 : p);
    bit = c.code(bit, p);
#ifdef JCM_STATS
    stat_bits += -std::log2((bit ? p : 4096 - p) / 4096.0); stat_n++;
#endif
    // learning rate: x4 for the first 4096 decisions, x2 until 32768
    const int sh = n_ < 4096 ? 16 : (n_ < 32768 ? 17 : 18);
    n_ += n_ < 32768;
    mix_train(st, w1, ((bit << 12) - p1) * kMixerRate, sh);
    mix_train(st, w0, ((bit << 12) - p0) * kMixerRate, sh);
    apm_.update(bit);
#ifdef JCM_APM2
    apm2_.update(bit);
#endif
    for (int i = 0; i < N; ++i) slot_update(s[i], bit);
    return bit;
  }
 private:
#ifdef JCM_ABLATE
  int id_ = 0;
#endif
  int nsel_;
  int n_ = 0;
  CounterTable tab_;
  std::vector<int16_t> w_;     // [0]: the global mixer; [1 + sel]: selected
  APM apm_;
#ifdef JCM_APM2
  APM apm2_{nsel_};
#endif
};

}  // namespace jcm
