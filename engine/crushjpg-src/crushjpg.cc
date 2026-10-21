/* ==========================================================================
 *  crushjpg - lossless JPEG recompression for Crush
 *  --------------------------------------------------------------------------
 *  A JPEG is Huffman-coded DCT coefficients. Huffman coding is weak, so a
 *  stronger model of the very same coefficients stores the same picture in
 *  far less space, and the original file is rebuilt bit for bit.
 *
 *  Two codecs, one parser:
 *
 *    'B'  Brunsli (Google, MIT licence, vendored in brunsli/). Fast. The
 *         technology JPEG XL's lossless JPEG transcoding grew out of.
 *
 *    'J'  jcm - Crush's own context-mixing coefficient model.
 *         Brunsli still parses the JPEG and stores everything that is not a
 *         coefficient (markers, tables, scan structure, padding bits...): the
 *         file is Brunsli-encoded with every coefficient set to zero, which
 *         costs a few hundred bytes. jcm then codes the coefficients.
 *         Integer-only, so every platform decodes identically.
 *
 *         Version 2 (jcm_*.h, written by this build) codes luma and chroma
 *         - and large images in horizontal stripes - as independent
 *         segments that run on separate threads; chroma is modelled from
 *         the co-located luma. Version 1 (jcm1_*.h, frozen) is still decoded
 *         exactly as before: archives already contain it.
 *
 *  Runs as a separate process, like crushpf: the input at compression time
 *  may be any file, and at extraction the packed data comes from an archive.
 *
 *  Protocol (binary, stdin -> stdout, little-endian u64 lengths):
 *    "crushjpg e" : Brunsli.   in = repeat { u64 n, jpeg }
 *    "crushjpg E" : jcm, falling back to Brunsli if jcm declines
 *                              out = repeat { u8 ok, [u64 m, packed] }
 *    "crushjpg J" : jcm only, no fallback (used by the tests)
 *    "crushjpg E|J <s>" : as above, jcm stripes of 2^s blocks (12..30,
 *                   default 17); smaller = more threads per image, a little
 *                   larger output. The decoder reads s from the data.
 *    "crushjpg d" : in = repeat { u64 m, packed }
 *                   out = repeat { u8 ok, [u64 n, jpeg] }
 *    "crushjpg v" : version
 *  packed = 1 tag byte ('B' or 'J') + codec data.
 *  "e" results are decoded again and compared before being reported ok. "E"
 *  results are not, because Crush itself inverts every transform and
 *  compares before accepting it - decoding twice would only double the time.
 *
 *  Threads: CRUSHJPG_THREADS (default: the machine's hardware threads, at
 *  most 8). A batch is read whole, its records processed in parallel, and
 *  the results written in input order. Output bytes never depend on the
 *  thread count.
 * ========================================================================== */
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <vector>
#include <thread>
#include <brunsli/jpeg_data.h>
#include <brunsli/jpeg_data_reader.h>
#include <brunsli/jpeg_data_writer.h>
#include <brunsli/brunsli_encode.h>
#include <brunsli/brunsli_decode.h>
#include "jcm1_codec.h"
#include "jcm_codec.h"
#ifdef _WIN32
#include <io.h>
#include <fcntl.h>
#endif

using namespace brunsli;

static const char *VERSION = "crushjpg 3 (brunsli + jcm 2; decodes jcm 1)";
static const uint64_t MAX_REC = 1ull << 30;
static const uint64_t MAX_BATCH = 3ull << 30;            /* bytes held at once */
static const size_t MAX_DECODE_MEM = (size_t)1 << 31;   /* 2 GB */
static const uint8_t JCM_VERSION = 2;                   /* written; 1 and 2 are read */

static bool read_exact(void *p, size_t n) { return n == 0 || fread(p, 1, n, stdin) == n; }
static bool read_blob(std::vector<uint8_t> &v) {
    unsigned char b[8];
    if (!read_exact(b, 8)) return false;
    uint64_t n = 0;
    for (int i = 7; i >= 0; --i) n = (n << 8) | b[i];
    if (n > MAX_REC) return false;
    v.resize((size_t)n);
    return read_exact(v.data(), (size_t)n);
}
static void write_blob(const uint8_t *p, size_t n) {
    unsigned char b[8];
    uint64_t v = n;
    for (int i = 0; i < 8; ++i) { b[i] = (unsigned char)(v & 0xFF); v >>= 8; }
    fwrite(b, 1, 8, stdout);
    if (n) fwrite(p, 1, n, stdout);
}
static int sink(void *d, const uint8_t *b, size_t n) {
    auto *v = (std::vector<uint8_t> *)d;
    v->insert(v->end(), b, b + n);
    return (int)n;
}
static bool brunsli_pack(const JPEGData &jpg, std::vector<uint8_t> &out) {
    size_t cap = GetMaximumBrunsliEncodedSize(jpg);
    out.resize(cap);
    size_t len = cap;
    if (!BrunsliEncodeJpeg(jpg, out.data(), &len, true)) return false;
    out.resize(len);
    return true;
}
static bool brunsli_unpack(const uint8_t *p, size_t n, JPEGData *jpg) {
    if (BrunsliEstimateDecoderPeakMemoryUsage(p, n) > MAX_DECODE_MEM) return false;
    return BrunsliDecodeJpeg(p, n, jpg, true) == BRUNSLI_OK;
}

static bool decode(const std::vector<uint8_t> &in, std::vector<uint8_t> &out, int threads) {
    if (in.empty()) return false;
    JPEGData jpg;
    if (in[0] == 'B') {
        if (!brunsli_unpack(in.data() + 1, in.size() - 1, &jpg)) return false;
    } else if (in[0] == 'J') {
        if (in.size() < 6 || (in[1] != 1 && in[1] != 2)) return false;
        uint32_t ml = in[2] | (in[3] << 8) | (in[4] << 16) | ((uint32_t)in[5] << 24);
        if ((size_t)ml > in.size() - 6) return false;
        if (!brunsli_unpack(in.data() + 6, ml, &jpg)) return false;
        const uint8_t *p = in.data() + 6 + ml;
        size_t n = in.size() - 6 - ml;
        if (in[1] == 1) {                      /* frozen version-1 decoder */
            jcm1::Coder c(p, n);
            jcm1::Model m;
            if (!m.run(c, jpg) || c.overrun()) return false;
        } else {
            if (!jcm::decode_image(p, n, jpg, threads)) return false;
        }
    } else {
        return false;
    }
    out.clear();
    JPEGOutput o(sink, &out);
    return WriteJpeg(jpg, o);
}

static bool encode_brunsli(const std::vector<uint8_t> &in, const JPEGData &jpg,
                           std::vector<uint8_t> &out) {
    std::vector<uint8_t> b;
    if (!brunsli_pack(jpg, b)) return false;
    out.assign(1, 'B');
    out.insert(out.end(), b.begin(), b.end());
    std::vector<uint8_t> back;
    return decode(out, back, 1) && back == in;          /* prove it */
}

static int g_stripe_log2 = jcm::kStripeLog2Default;
static bool encode_jcm(const JPEGData &jpg, std::vector<uint8_t> &out, int threads) {
    JPEGData zero = jpg;
    for (auto &c : zero.components) std::fill(c.coeffs.begin(), c.coeffs.end(), 0);
    std::vector<uint8_t> meta;
    if (!brunsli_pack(zero, meta) || meta.size() > 0xFFFFFFFFu) return false;
    out.assign(1, 'J');
    out.push_back(JCM_VERSION);
    uint32_t ml = (uint32_t)meta.size();
    for (int i = 0; i < 4; ++i) out.push_back((uint8_t)(ml >> (8 * i)));
    out.insert(out.end(), meta.begin(), meta.end());
    std::vector<uint8_t> body;
    if (!jcm::encode_image(jpg, body, threads, g_stripe_log2)) return false;
    out.insert(out.end(), body.begin(), body.end());
    return true;
}

static bool process(char mode, const std::vector<uint8_t> &in, std::vector<uint8_t> &out, int threads) {
    try {
        if (mode == 'd') return decode(in, out, threads);
        JPEGData jpg;
        if (!ReadJpeg(in.data(), in.size(), JPEG_READ_ALL, &jpg)) return false;
        bool ok = false;
        if (mode == 'E' || mode == 'J') ok = encode_jcm(jpg, out, threads);
        if (mode == 'J') return ok;
        // jcm learns as it goes; on a small image it has had no time to,
        // and Brunsli's fixed model can win. Try both.
        if (!ok || in.size() < (64u << 10)) {
            std::vector<uint8_t> alt;
            if (encode_brunsli(in, jpg, alt) && (!ok || alt.size() < out.size())) {
                out.swap(alt);
                ok = true;
            }
        }
        return ok;
    } catch (...) {
        return false;
    }
}

static int thread_count() {
    const char *e = getenv("CRUSHJPG_THREADS");
    int t = e ? atoi(e) : (int)std::thread::hardware_concurrency();
    if (t < 1) t = 1;
    if (t > 8 && !e) t = 8;
    if (t > 64) t = 64;
    return t;
}

int main(int argc, char **argv) {
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    if (argc < 2) { fprintf(stderr, "usage: crushjpg e|E|J|d|v\n"); return 2; }
    char mode = argv[1][0];
    if (mode == 'v') { printf("%s\n", VERSION); return 0; }
    if (mode != 'e' && mode != 'E' && mode != 'J' && mode != 'd') return 2;
    if (argc > 2) {                     /* optional: stripe size, log2 blocks */
        g_stripe_log2 = atoi(argv[2]);
        if (g_stripe_log2 < jcm::kStripeLog2Min || g_stripe_log2 > jcm::kStripeLog2Max) return 2;
    }
    jcm1::init_tabs();          /* before any thread: v1 builds them lazily */
    const int T = thread_count();
    for (;;) {
        // Read a batch (everything, or as much as MAX_BATCH allows), process
        // its records in parallel, write the results in order, repeat.
        std::vector<std::vector<uint8_t>> in;
        uint64_t held = 0;
        bool eof = false;
        while (held < MAX_BATCH) {
            std::vector<uint8_t> v;
            if (!read_blob(v)) { eof = true; break; }
            held += v.size();
            in.push_back(std::move(v));
        }
        const int n = (int)in.size();
        std::vector<std::vector<uint8_t>> out(n);
        std::vector<char> ok(n, 0);
        const int workers = n < T ? n : T;
        const int inner = workers > 0 ? (T + workers - 1) / workers : 1;
        jcm::parallel_for(n, workers, [&](int i) {
            ok[i] = process(mode, in[i], out[i], inner);
            std::vector<uint8_t>().swap(in[i]);
        });
        for (int i = 0; i < n; ++i) {
            fputc(ok[i] ? 1 : 0, stdout);
            if (ok[i]) write_blob(out[i].data(), out[i].size());
        }
        fflush(stdout);
        if (eof) break;
    }
    return 0;
}
