/* ==========================================================================
 *  crushpf - rebuild deflate streams that zlib did not write
 *  --------------------------------------------------------------------------
 *  Crush's pinned zlib (crushz) can rebuild a deflate stream only if zlib made
 *  it. GNU gzip, Chrome, Microsoft Office, Adobe, 7-Zip and others use their
 *  own deflate, so for those Crush uses preflate (Dirk Steinke, Apache-2.0,
 *  vendored unmodified in preflate/): it re-parses the stream with a model of
 *  how deflate encoders decide, and records only where the real stream
 *  departed from the model. Decompressed data + that small correction record
 *  rebuilds the original bytes exactly.
 *
 *  Runs as a separate process on purpose. The input at compression time is
 *  whatever file the user drops in, and at extraction the correction record
 *  comes out of an archive - either could be hostile. If preflate ever
 *  crashes on one, the worst outcome is that one stream is left compressed
 *  (or one file reported), never that Crush itself goes down.
 *
 *  Protocol (binary, stdin -> stdout, little-endian u64 lengths):
 *    "crushpf d" : in  = repeat { u64 n, n bytes of raw deflate }
 *                  out = repeat { u8 ok, [u64 n, unpacked] [u64 m, correction] }
 *    "crushpf r" : in  = repeat { u64 m, correction, u64 n, unpacked }
 *                  out = repeat { u8 ok, [u64 n, deflate] }
 *    "crushpf v" : prints the version string
 *  Every "d" result is re-encoded and compared before it is reported ok, so
 *  the helper never hands back a record it has not proven.
 * ========================================================================== */
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>
#include "preflate/preflate.h"
#ifdef _WIN32
#include <io.h>
#include <fcntl.h>
#endif

static const char *VERSION = "crushpf 1 (preflate 0.3.5)";
static const uint64_t MAX_REC = 1ull << 30;   /* refuse absurd lengths */

static bool read_exact(void *p, size_t n) {
    return n == 0 || fread(p, 1, n, stdin) == n;
}
static bool read_u64(uint64_t &v) {
    unsigned char b[8];
    if (!read_exact(b, 8)) return false;
    v = 0;
    for (int i = 7; i >= 0; --i) v = (v << 8) | b[i];
    return true;
}
static bool read_blob(std::vector<unsigned char> &v) {
    uint64_t n;
    if (!read_u64(n) || n > MAX_REC) return false;
    v.resize((size_t)n);
    return read_exact(v.data(), (size_t)n);
}
static void write_u64(uint64_t v) {
    unsigned char b[8];
    for (int i = 0; i < 8; ++i) { b[i] = (unsigned char)(v & 0xFF); v >>= 8; }
    fwrite(b, 1, 8, stdout);
}
static void write_blob(const std::vector<unsigned char> &v) {
    write_u64(v.size());
    if (!v.empty()) fwrite(v.data(), 1, v.size(), stdout);
}

int main(int argc, char **argv) {
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    if (argc < 2) { fprintf(stderr, "usage: crushpf d|r|v\n"); return 2; }
    char mode = argv[1][0];
    if (mode == 'v') { printf("%s\n", VERSION); return 0; }
    if (mode == 'd') {
        std::vector<unsigned char> in;
        while (read_blob(in)) {
            std::vector<unsigned char> un, diff, back;
            bool ok = false;
            try {
                ok = preflate_decode(un, diff, in)
                     && preflate_reencode(back, diff, un) && back == in;
            } catch (...) { ok = false; }
            fputc(ok ? 1 : 0, stdout);
            if (ok) { write_blob(un); write_blob(diff); }
            fflush(stdout);
        }
        return 0;
    }
    if (mode == 'r') {
        std::vector<unsigned char> diff, un;
        while (read_blob(diff)) {
            if (!read_blob(un)) return 3;
            std::vector<unsigned char> out;
            bool ok = false;
            try { ok = preflate_reencode(out, diff, un); } catch (...) { ok = false; }
            fputc(ok ? 1 : 0, stdout);
            if (ok) write_blob(out);
            fflush(stdout);
        }
        return 0;
    }
    return 2;
}
