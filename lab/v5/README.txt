Round 10 research harnesses (Linux; paths inside are the session's, adjust)
==========================================================================

jcm_test.cc      Standalone JPEG harness for jcm: parses with Brunsli, codes the
                 coefficients with jcm, decodes, rebuilds and compares, and
                 prints per-decision byte costs when built with -DJCM_STATS.
                 Build (from this folder):
                   S=../../engine/crushjpg-src
                   g++ -std=c++14 -O2 -DJCM_STATS -I$S -I$S/brunsli/c/include \
                       -I$S/brunsli/c -I$S/brotli/c/include -o jcm_test jcm_test.cc \
                       <brunsli and brotli sources, as in crushjpg-src/build.sh>
                   ./jcm_test photos/*.jpg

fuzz_helpers.py  Mutation fuzzer for the helpers' decode paths (crushjpg d,
                 crushpf r with :raw framing). Build the helper with
                 -fsanitize=address,undefined -fno-sanitize=alignment first.
fuzz_pf.py       Structure-aware preflate fuzzer: keeps the framing valid and
                 mutates the correction record, or mutates raw deflate ('d').
survey2.py       Per-transform coverage survey: how many streams each unwrapper
                 rebuilt by zlib vs preflate, and the correction-record share.

Results of all of these are in BENCHMARKS.md (v5, round 10) and SECURITY.md.
