// jcm_model.h - context-mixing model for quantized JPEG DCT coefficients.
// Integer-only (deterministic on every platform). Encoder and decoder share
// one code path: code(bit) either writes the bit or reads it.
#pragma once
#include <cstdint>
#include <cstring>
#include <vector>
#include <cstdlib>
#include <cmath>

namespace jcm1 {

// ---------------------------------------------------------------- utilities
static int squash_tab[4096];
static short stretch_tab[4096];
static int recip_tab[1024];
static bool tabs_ready = false;

static int squash_calc(int d) {
  static const int t[33] = {1, 2, 3, 6, 10, 16, 27, 45, 73, 120, 194, 310, 488, 747, 1101,
                            1546, 2047, 2549, 2994, 3348, 3607, 3785, 3901, 3975, 4022,
                            4050, 4068, 4079, 4085, 4089, 4092, 4093, 4094};
  if (d > 2047) return 4095;
  if (d < -2047) return 1;
  int w = d & 127;
  d = (d >> 7) + 16;
  int r = (t[d] * (128 - w) + t[d + 1] * w + 64) >> 7;
  return r < 1 ? 1 : (r > 4095 ? 4095 : r);
}
static void init_tabs() {
  if (tabs_ready) return;
  for (int i = 0; i < 4096; ++i) squash_tab[i] = squash_calc(i - 2048);
  int pi = 0;
  for (int x = -2047; x <= 2047; ++x) {
    int v = squash_calc(x);
    for (int j = pi; j <= v; ++j) stretch_tab[j] = (short)x;
    pi = v + 1;
  }
  for (int j = pi; j < 4096; ++j) stretch_tab[j] = 2047;
  for (int n = 0; n < 1024; ++n) recip_tab[n] = 65536 * 2 / (2 * n + 3);
  tabs_ready = true;
}
static inline int squash(int d) {
  if (d > 2047) d = 2047;
  if (d < -2047) d = -2047;
  return squash_tab[d + 2048];
}
static inline int stretch(int p) { return stretch_tab[p]; }

static inline int ilog2(uint32_t v) { int r = 0; while (v >>= 1) ++r; return r; }
// bucket of a non-negative magnitude: 0,1,2,3,4-5,6-7,8-11,12-15,...
static inline int mbucket(int v) {
  if (v < 4) return v;
  int l = ilog2((uint32_t)v);
  int half = (v >> (l - 1)) & 1;
  int b = 2 * l + half;          // v=4 -> 4, 6 -> 5, 8 -> 6, 12 -> 7 ...
  return b > 23 ? 23 : b;
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
  int code(int bit, int p12) {
    uint32_t xmid = x1_ + (uint32_t)(((uint64_t)(x2_ - x1_) * (uint32_t)p12) >> 12);
    if (decoding()) bit = x_ <= xmid;
    if (bit) x2_ = xmid; else x1_ = xmid + 1;
    while (((x1_ ^ x2_) & 0xff000000u) == 0) {
      if (!decoding()) out_->push_back((uint8_t)(x2_ >> 24));
      x1_ <<= 8;
      x2_ = (x2_ << 8) | 255;
      if (decoding()) x_ = (x_ << 8) | next();
    }
    return bit;
  }
  void flush() {
    for (int i = 0; i < 4; ++i) { out_->push_back((uint8_t)(x1_ >> 24)); x1_ <<= 8; }
  }
  bool overrun() const { return p_ > n_ + 4; }
 private:
  uint8_t next() { return p_ < n_ ? in_[p_++] : (p_++, 0); }
  std::vector<uint8_t> *out_;
  const uint8_t *in_;
  size_t n_, p_;
  uint32_t x1_ = 0, x2_ = 0xffffffffu, x_ = 0;
};

// --------------------------------------------------------- adaptive counters
struct Slot { uint16_t p; uint16_t n; };

class CounterTable {
 public:
  explicit CounterTable(int bits) : mask_((1u << bits) - 1), t_(1u << bits) {
    for (auto &s : t_) { s.p = 32768; s.n = 0; }
  }
  Slot *at(uint32_t h) { return &t_[h & mask_]; }
 private:
  uint32_t mask_;
  std::vector<Slot> t_;
};

static inline void slot_update(Slot *s, int bit, int limit) {
  int target = bit ? 65535 : 0;
  int r = recip_tab[s->n];
  s->p = (uint16_t)(s->p + (((target - (int)s->p) * r) >> 16));
  if (s->n < limit) ++s->n;
}

static inline uint32_t hash(uint32_t a, uint32_t b = 0, uint32_t c = 0, uint32_t d = 0) {
  uint32_t h = a * 0x9E3779B1u ^ (b + 0x7F4A7C15u) * 0x85EBCA77u;
  h ^= (c + 0x165667B1u) * 0xC2B2AE3Du;
  h ^= (d + 0x27D4EB2Fu) * 0x9E3779B1u;
  h ^= h >> 15; h *= 0x2C1B3C6Du; h ^= h >> 12;
  return h;
}

// --------------------------------------------------------------- the mixer
// One binary decision: up to MAXIN context slots, mixed in the logistic
// domain by a weight set chosen by a small selector, then refined by an APM.
static const int MAXIN = 12;
// Tuned on 59 real web JPEGs + 51 test images (see HANDOFF.md). Changing any
// of these changes the format.
static const int kMixerRate = 6;     // mixer learning rate
static const int kApmWeight = 2;     // of 4: APM share of the final probability
static const int kCountLimit = 255;  // counter adaptation floor 1/(limit+1.5)

class Mixer {
 public:
  Mixer(int nin, int nsel) : nin_(nin), w_((size_t)nin * nsel, (1 << 16) / nin) { init_tabs(); }
  int mix(const int *st, int sel) {
    sel_ = sel;
    int64_t dot = 0;
    const int *w = &w_[(size_t)sel * nin_];
    for (int i = 0; i < nin_; ++i) dot += (int64_t)st[i] * w[i];
    int d = (int)(dot >> 16);
    pr_ = squash(d);
    return pr_;
  }
  void update(const int *st, int bit, int lr) {
    int err = ((bit << 12) - pr_) * lr;
    int *w = &w_[(size_t)sel_ * nin_];
    for (int i = 0; i < nin_; ++i) {
      int nw = w[i] + ((st[i] * err + (1 << 13)) >> 14);
      w[i] = nw > (1 << 24) ? (1 << 24) : (nw < -(1 << 24) ? -(1 << 24) : nw);
    }
  }
 private:
  int nin_, sel_ = 0, pr_ = 2048;
  std::vector<int> w_;
};

// APM / SSE: maps (probability, context) to a refined probability.
class APM {
 public:
  explicit APM(int n) : t_((size_t)n * 33) {
    init_tabs();
    for (int i = 0; i < n; ++i)
      for (int j = 0; j < 33; ++j) t_[(size_t)i * 33 + j] = squash((j - 16) * 128) * 16;
  }
  int pp(int pr, int cx) {
    int s = stretch(pr) + 2048;
    int lo = s >> 7, w = s & 127;
    idx_ = (size_t)cx * 33 + lo;
    w_ = w;
    return ((t_[idx_] * (128 - w) + t_[idx_ + 1] * w) >> 11);
  }
  void update(int bit, int rate = 7) {
    int g = (bit << 16) + (bit << rate) - bit - bit;
    t_[idx_] += (g - t_[idx_]) >> rate;
    t_[idx_ + 1] += (g - t_[idx_ + 1]) >> rate;
  }
 private:
  std::vector<int> t_;
  size_t idx_ = 0;
  int w_ = 0;
};

// A "Decision": a family of binary events sharing a mixer and counter tables.
class Decision {
 public:
  double stat_bits = 0; long stat_n = 0;
  Decision(int nin, int tbits, int nsel, int apm_n)
      : nin_(nin), mixer_(nin + 1, nsel), apm_(apm_n) {
    if (nin > MAXIN) abort();
    tabs_.reserve(nin);
    for (int i = 0; i < nin; ++i) tabs_.emplace_back(tbits);
  }
  // ctx: nin hashed contexts; sel: mixer selector; acx: APM context
  int code(Coder &c, int bit, const uint32_t *ctx, int sel, int acx) {
    for (int i = 0; i < nin_; ++i) {
      slots_[i] = tabs_[i].at(ctx[i] * (uint32_t)(i * 2 + 1) + (uint32_t)i * 0x3C6EF372u);
      st_[i] = stretch(slots_[i]->p >> 4);
    }
    st_[nin_] = 256;                       // bias
    int pm = mixer_.mix(st_, sel);
    int pa = apm_.pp(pm, acx);
    int p = (pm * (4 - kApmWeight) + pa * kApmWeight + 2) >> 2;
    if (p < 1) p = 1;
    if (p > 4095) p = 4095;
    bit = c.code(bit, p);
#ifdef JCM_STATS
    stat_bits += -std::log2((bit ? p : 4096 - p) / 4096.0); stat_n++;
#endif
    mixer_.update(st_, bit, kMixerRate);
    apm_.update(bit);
    for (int i = 0; i < nin_; ++i) slot_update(slots_[i], bit, kCountLimit);
    return bit;
  }
 private:
  int nin_;
  std::vector<CounterTable> tabs_;
  Mixer mixer_;
  APM apm_;
  Slot *slots_[MAXIN];
  int st_[MAXIN + 1];
};

}  // namespace jcm1
