#!/bin/sh
# Builds crush_crypto.wasm (Argon2id + XChaCha20-Poly1305 for the share page)
# from Monocypher 4.0.2 (monocypher/, CC0 / BSD-2, see LICENCE.md) + shim.c.
# Needs: pip install ziglang.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
MC="$HERE/monocypher"
python3 -m ziglang cc -target wasm32-freestanding -O3 -flto -fno-stack-protector \
  -I"$MC" "$HERE/shim.c" "$MC/monocypher.c" \
  -nostdlib -Wl,--no-entry -Wl,--export-memory -Wl,--max-memory=4294901760 \
  -Wl,--initial-memory=1048576 -Wl,-z,stack-size=262144 -Wl,--strip-all \
  -o "$HERE/crush_crypto.wasm"
sha256sum "$HERE/crush_crypto.wasm"
