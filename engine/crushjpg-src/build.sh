#!/bin/sh
# Builds crushjpg (Brunsli + jcm JPEG recompression helper) for Linux and,
# cross-compiled, Windows x64. The coefficient model is integer-only; the two
# builds are checked against each other under Wine (see REPORT.md).
#   sh build.sh          -> ../crushjpg and ../crushjpg.exe
#   sh build.sh asan     -> ../crushjpg_asan (AddressSanitizer + UBSan, for fuzzing)
set -e
cd "$(dirname "$0")"
B=brunsli/c; BR=brotli/c
CSRC="$(ls $BR/common/*.c $BR/dec/*.c $BR/enc/*.c | tr '\n' ' ')"
CCSRC="crushjpg.cc $(ls $B/common/*.cc $B/dec/*.cc $B/enc/*.cc | tr '\n' ' ')"
INC="-I$B/include -I$B -I$BR/include"
build() {  # $1 = C compiler, $2 = C++ compiler, $3 = output, $4 = extra link flags, $5 = extra compile flags
  rm -rf .obj && mkdir .obj
  for f in $CSRC; do $1 -O2 $5 $INC -c $f -o .obj/$(echo $f | tr '/' '_').o; done
  $2 -std=c++14 -O2 $5 $INC -o $3 $CCSRC .obj/*.o $4
  rm -rf .obj
}
if [ "$1" = "asan" ]; then
  build gcc g++ ../crushjpg_asan "-pthread -fsanitize=address,undefined -fno-sanitize=alignment" "-g -fsanitize=address,undefined -fno-sanitize=alignment -fno-omit-frame-pointer"
  exit 0
fi
build gcc g++ ../crushjpg "-pthread" ""
if command -v x86_64-w64-mingw32-g++-posix >/dev/null; then
  build x86_64-w64-mingw32-gcc-posix x86_64-w64-mingw32-g++-posix ../crushjpg.exe "-static" ""
fi
