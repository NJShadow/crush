#!/bin/sh
# Builds crushnn: crushcm's context mixer plus Crush's integer-only byte
# transformer (nn.h) as extra mixer inputs. -O3 matters for speed (it
# vectorises the int16 dot products); output is bit-identical at any -O level
# and across Linux / Windows (checked under Wine, see HANDOFF.md round 11).
#   sh build.sh        -> ../crushnn and ../crushnn.exe
#   sh build.sh asan   -> ../crushnn_asan (for fuzzing)
set -e
cd "$(dirname "$0")"
if [ "$1" = "asan" ]; then
  gcc -O1 -g -fsanitize=address,undefined -fno-sanitize=alignment -o ../crushnn_asan crushnn.c
  exit 0
fi
gcc -O3 -o ../crushnn crushnn.c
if command -v x86_64-w64-mingw32-gcc-posix >/dev/null; then
  x86_64-w64-mingw32-gcc-posix -O3 -static -o ../crushnn.exe crushnn.c
fi
