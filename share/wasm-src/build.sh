#!/bin/sh
# Rebuilds the share page's WebAssembly decoders into ../codecs/ from the very
# C/C++ sources the native helpers use (../../engine/) plus the third-party
# decoders in third_party.tar.xz (zstd, LZMA SDK, Brotli, bzip2).
# Needs: pip install ziglang. Then re-run ../tests (see ../README.txt) before
# trusting the new build, and refresh ../codecs/CODECS.lock.
# Each module is a wasm32-wasi "reactor" (no main, exports its functions); the
# only WASI calls left are stderr writes and exit, which crush_codecs.js stubs.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ENG="$HERE/../../engine"
OUT="$HERE/../codecs"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
tar -C "$T" -xJf "$HERE/third_party.tar.xz"
cd "$HERE"
ZCC="python3 -m ziglang cc -target wasm32-wasi -O2 -mexec-model=reactor"
ZXX="python3 -m ziglang c++ -target wasm32-wasi -O2 -std=c++14 -fno-exceptions -mexec-model=reactor"
mkdir -p "$T/obj"

echo "crushcm";  mkdir -p "$T/crushcm" && cp "$ENG/crushcm.c" "$T/crushcm/"
$ZCC -I"$T/crushcm" src/cm_wasm.c -o "$OUT/crushcm.wasm" -s -Wl,-z,stack-size=1048576

echo "crushz";   Z="$T/zlib"           # engine's pinned zlib 1.3.1 + its inflate files
mkdir -p "$Z" && cp "$ENG"/crushz/zlib/* "$T"/zlib-inflate/* "$Z/"
$ZCC -DZ_PREFIX -I"$ENG/crushz" -I"$Z" src/z_wasm.c $Z/adler32.c $Z/crc32.c $Z/deflate.c \
    $Z/trees.c $Z/zutil.c $Z/inflate.c $Z/inftrees.c $Z/inffast.c -o "$OUT/crushz.wasm" -s

echo "zstd";     Z="$T/zstd/lib"
$ZCC -DZSTD_DISABLE_ASM -DZSTD_LEGACY_SUPPORT=0 -DDEBUGLEVEL=0 -I$Z -I$Z/common \
    src/zstd_wasm.c $Z/common/*.c $Z/decompress/*.c -o "$OUT/zstd.wasm" -s

echo "lzma";     L="$T/lzma-sdk/C"
$ZCC -I$L src/lzma_wasm.c $L/LzmaDec.c $L/Lzma2Dec.c $L/Bra86.c $L/Delta.c $L/Ppmd7.c \
    $L/Ppmd7Dec.c -o "$OUT/lzma.wasm" -s

echo "brotli";   B="$T/brotli/c"
$ZCC -I$B/include src/brotli_wasm.c $B/common/*.c $B/dec/*.c -o "$OUT/brotli.wasm" -s

echo "bzip2";    B="$T/bzip2"
$ZCC -DBZ_NO_STDIO -I$B src/bzip2_wasm.c $B/bzlib.c $B/decompress.c $B/huffman.c \
    $B/crctable.c $B/randtable.c $B/compress.c $B/blocksort.c -o "$OUT/bzip2.wasm" -s

echo "crushpf";  P="$ENG/crushpf-src/preflate"
$ZXX -Isrc/nothreads -I$P src/pf_wasm.cpp $P/*.cpp $P/support/*.cpp \
    -o "$OUT/crushpf.wasm" -s -Wl,-z,stack-size=4194304

echo "crushjpg"; J="$ENG/crushjpg-src"; B=$J/brunsli/c; BR=$J/brotli/c
INC="-I$J -I$B/include -I$B -I$BR/include"
for f in $BR/common/*.c $BR/dec/*.c; do
  $ZCC $INC -c $f -o "$T/obj/$(basename $f).o"
done
$ZXX -Isrc/nothreads_jpg $INC src/jpg_wasm.cpp $B/common/*.cc $B/dec/*.cc $B/enc/*.cc "$T"/obj/*.o \
    -o "$OUT/crushjpg.wasm" -s -Wl,-z,stack-size=8388608

cd "$OUT" && sha256sum crush_codecs.js *.wasm > CODECS.lock && cat CODECS.lock
