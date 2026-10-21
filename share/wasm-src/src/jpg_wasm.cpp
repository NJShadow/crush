/* crushjpg 'd' mode (Brunsli and jcm decode) for WebAssembly.
 *
 * Includes the vendored engine/crushjpg-src/crushjpg.cc unchanged, with its
 * main() renamed out of the way, and exports its decode(): packed record
 * ('B' + Brunsli, or 'J' + jcm) -> the original JPEG bytes. The native
 * helper's 1 GB record limit and 2 GB decoder-memory estimate are kept.
 * No C++ exceptions in WebAssembly here: where the native helper would catch,
 * the module traps, and the JavaScript side reports failure for that image
 * and discards the instance. */
#include <cstdlib>
#include <cstring>
/* crushjpg.cc's main() wraps each record in try/catch; main() is not used
   here, but it must compile without exception support */
#define main crushjpg_native_main
#define try if (true)
#define catch(...) if (false)
#include "crushjpg.cc"
#undef catch
#undef try
#undef main

#define EXPORT(name) __attribute__((export_name(name)))
extern "C" {
EXPORT("malloc") void *x_malloc(size_t n) { return malloc(n); }
EXPORT("free") void x_free(void *p) { free(p); }

EXPORT("jpg_decode")
int jpg_decode(const unsigned char *in, size_t n_in, unsigned char **out, size_t *outlen)
{
    *out = nullptr; *outlen = 0;
    if (n_in > MAX_REC) return -1;
    std::vector<uint8_t> v(in, in + n_in), o;
    if (!decode(v, o, 1)) return -2;
    unsigned char *b = (unsigned char *)malloc(o.size() ? o.size() : 1);
    if (!b) return -3;
    if (!o.empty()) memcpy(b, o.data(), o.size());
    *out = b; *outlen = o.size();
    return 0;
}
EXPORT("jpg_version") const char *jpg_version(void) { return VERSION; }
}
