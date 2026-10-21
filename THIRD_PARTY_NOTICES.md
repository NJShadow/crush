# Third-party components

Crush's own code is MIT-licensed (see `LICENSE`). These components are
bundled with it, unmodified unless noted, under their own licences. Each
licence text sits next to the component.

| Component | Where | Licence | Notes |
|---|---|---|---|
| zlib 1.3.1 | `engine/crushz/zlib/` (built into `crushz.dll`, `crushz.wasm`) | zlib | symbol-prefixed build, unmodified |
| preflate | `engine/crushpf-src/preflate/` (built into `crushpf.exe`, `crushpf.wasm`) | Apache-2.0 | **modified**: bounds checks marked `CRUSH PATCH` |
| Brunsli | `engine/crushjpg-src/brunsli/` (in `crushjpg.exe`, `crushjpg.wasm`) | MIT | |
| Brotli | `engine/crushjpg-src/brotli/`; `share/wasm-src/third_party.tar.xz` | MIT | |
| Zstandard | `share/wasm-src/third_party.tar.xz` (in `zstd.wasm`) | BSD-3-Clause (dual BSD/GPLv2; used under BSD) | decoder only |
| LZMA SDK | `share/wasm-src/third_party.tar.xz` (in `lzma.wasm`) | public domain | decoder only |
| bzip2 | `share/wasm-src/third_party.tar.xz` (in `bzip2.wasm`) | bzip2 licence (BSD-style) | |
| Monocypher 4.0.2 | `share/crypto/monocypher/` (in `crush_crypto.wasm`) | CC0-1.0 or BSD-2-Clause | |
| jsQR 1.4.0 | `channels/js/vendor/`, inlined in `channels/dist/` pages | Apache-2.0 | |
| QR Code generator (Nayuki) | `channels/js/vendor/qrcodegen.js` | MIT | |
| zxing-wasm 2.2.4 / zxing-cpp | `channels/js/vendor/`, inlined in `channels/dist/receive.html` | MIT / Apache-2.0 | |
| Learned code model | `priors/code.cnn` | derived from the Python 3.12 standard library, PSF-2.0 | notice: `priors/code.cnn.LICENSE.txt` |

## The shared priors (`priors/*.prime`, `priors/*.zdict`)

The priors are sample corpora that Crush's encoder and decoder both read, so
their contents never have to travel inside an archive. They were assembled
from files on a Debian-based Linux system: C headers (`code`), the
`/usr/share/doc/*/copyright` files (`text`), icon SVGs (`web`) and the
iso-codes JSON tables (`data`). Each of those files is open-source and carries
its own licence (GPL, LGPL, MIT, BSD, Apache and others, as stated inside
them). They are redistributed here verbatim as data, not linked into any
program. They are part of the archive format: an archive made with a prior can
only be opened with exactly that prior file, so they must not be edited.

## Python packages

Installed by `Crush.bat` from PyPI, not bundled: numpy, zstandard, brotli,
pyppmd, cryptography, argon2-cffi, pywebview, segno, reportlab, pillow.
Each keeps its own licence.
