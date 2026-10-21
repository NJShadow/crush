#include "jcm_codec.h"
#include <brunsli/jpeg_data_reader.h>
#include <brunsli/jpeg_data_writer.h>
#include <brunsli/brunsli_encode.h>
#include <brunsli/brunsli_decode.h>
#include <cstdio>
#include <chrono>
#include <cstdlib>
using namespace brunsli;
static double g_stats[14];

static int sink(void *d, const uint8_t *b, size_t n) {
  auto *v = (std::vector<uint8_t> *)d; v->insert(v->end(), b, b + n); return (int)n;
}
static bool bencode(const JPEGData &j, std::vector<uint8_t> &out) {
  size_t cap = GetMaximumBrunsliEncodedSize(j); out.resize(cap); size_t len = cap;
  if (!BrunsliEncodeJpeg(j, out.data(), &len, true)) return false; out.resize(len); return true;
}

int main(int argc, char **argv) {
  uint64_t tot_in = 0, tot_b = 0, tot_c = 0;
  double te = 0, td = 0;
  for (int i = 1; i < argc; ++i) {
    FILE *f = fopen(argv[i], "rb"); fseek(f, 0, 2); long n = ftell(f); fseek(f, 0, 0);
    std::vector<uint8_t> in(n); fread(in.data(), 1, n, f); fclose(f);
    JPEGData jpg;
    if (!ReadJpeg(in.data(), in.size(), JPEG_READ_ALL, &jpg)) { printf("%s readfail\n", argv[i]); continue; }
    std::vector<uint8_t> full; bencode(jpg, full);
    auto t0 = std::chrono::steady_clock::now();
    JPEGData jz = jpg;
    for (auto &c : jz.components) std::fill(c.coeffs.begin(), c.coeffs.end(), 0);
    std::vector<uint8_t> meta; if (!bencode(jz, meta)) { printf("%s metafail\n", argv[i]); continue; }
    std::vector<uint8_t> cm;
    { jcm::Coder c(&cm); jcm::Model m; JPEGData tmp = jpg; if (!m.run(c, tmp)) { printf("%s encfail\n", argv[i]); continue; } c.flush();
#ifdef JCM_STATS
      m.stats(g_stats);
#endif
    }
    auto t1 = std::chrono::steady_clock::now();
    // decode
    JPEGData jd;
    bool ok = BrunsliDecodeJpeg(meta.data(), meta.size(), &jd, true) == BRUNSLI_OK;
    if (ok && getenv("COPY")) { for (size_t k = 0; k < jd.components.size(); ++k) jd.components[k].coeffs = jpg.components[k].coeffs; }
    else if (ok) { jcm::Coder c(cm.data(), cm.size()); jcm::Model m; ok = m.run(c, jd);
      if (getenv("DBG")) { size_t bad=0, first=(size_t)-1; for (size_t k = 0; k < jd.components.size(); ++k) for (size_t t=0;t<jd.components[k].coeffs.size();++t) if (jd.components[k].coeffs[t]!=jpg.components[k].coeffs[t]) { if(first==(size_t)-1) first=k*100000000+t; ++bad; } printf("coef mismatches %zu first %zu ok=%d\n", bad, first, ok); } }
    std::vector<uint8_t> back;
    if (ok) { JPEGOutput o(sink, &back); ok = WriteJpeg(jd, o); }
    auto t2 = std::chrono::steady_clock::now();
    te += std::chrono::duration<double>(t1 - t0).count(); td += std::chrono::duration<double>(t2 - t1).count();
    bool exact = ok && back == in;
    size_t tot = meta.size() + cm.size();
    tot_in += n; tot_b += full.size(); tot_c += exact ? tot : n;
    if (argc < 8 || !exact)
      printf("%-40s %8ld brunsli %7zu (%5.1f%%)  jcm %7zu+%zu=%7zu (%5.1f%%) %s\n", argv[i], n, full.size(),
             100.0 * (1 - (double)full.size() / n), meta.size(), cm.size(), tot, 100.0 * (1 - (double)tot / n),
             exact ? "EXACT" : "FAIL");
  }
#ifdef JCM_STATS
  const char *nm[14] = {"nz","zero","expo","mant","sign","ecnt","ezero","eexpo","emant","esign","dcz","dce","dcm","dcs"};
  for (int k = 0; k < 14; ++k) printf("  %-5s %10.0f bytes\n", nm[k], g_stats[k]);
#endif
  printf("TOTAL in %llu  brunsli %llu (%.2f%%)  jcm %llu (%.2f%%)  enc %.2fs dec %.2fs\n",
         (unsigned long long)tot_in, (unsigned long long)tot_b, 100.0 * (1 - (double)tot_b / tot_in),
         (unsigned long long)tot_c, 100.0 * (1 - (double)tot_c / tot_in), te, td);
}
