#!/bin/sh
# Builds crushpf (preflate helper) for Linux and, cross-compiled, Windows x64.
# preflate is integer-only, so both builds must produce identical records;
# tests/test_unwrap.py and the Wine check in HANDOFF.md verify that.
set -e
cd "$(dirname "$0")"
SRC="crushpf.cpp $(ls preflate/*.cpp preflate/support/*.cpp | tr '\n' ' ')"
FLAGS="-std=c++14 -O2 -Ipreflate"
g++ $FLAGS -o ../crushpf $SRC -lpthread
if command -v x86_64-w64-mingw32-g++-posix >/dev/null; then
  x86_64-w64-mingw32-g++-posix $FLAGS -static -o ../crushpf.exe $SRC
fi
