#!/bin/sh
# Builds Crush's pinned deflate library for Linux and (cross-compiled) Windows.
# Z_PREFIX renames every zlib symbol (deflate -> z_deflate) and
# -fvisibility=hidden keeps them private, so this copy can never be mixed up
# with whatever zlib Python itself has loaded.
set -e
cd "$(dirname "$0")"
SRC="crushz.c zlib/adler32.c zlib/crc32.c zlib/deflate.c zlib/trees.c zlib/zutil.c"
FLAGS="-O2 -DZ_PREFIX -fvisibility=hidden -Izlib"
gcc $FLAGS -shared -fPIC -o ../libcrushz.so $SRC
if command -v x86_64-w64-mingw32-gcc >/dev/null; then
  x86_64-w64-mingw32-gcc $FLAGS -shared -static-libgcc -o ../crushz.dll $SRC
fi
