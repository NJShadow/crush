Optional accelerators
=====================
Crush works with nothing in this folder. JPEG recompression is now built in
(engine/crushjpg.exe), so nothing here is needed for photos any more.

  cjxl.exe, djxl.exe   JPEG XL reference tools. Optional: if present, JPEG XL's
                       lossless JPEG re-coding competes with Crush's own, and
                       the smaller result wins. On the photos measured so far
                       Crush's own was smaller (see BENCHMARKS.md).
                       Also used by the opt-in perceptual (lossy) photo tier.
                       Get them from: https://github.com/libjxl/libjxl/releases

  flac.exe             The FLAC reference encoder. Crush has its own audio
                       transform and usually beats FLAC, but with flac.exe
                       present the two compete and the smaller one wins.
                       Get it from: https://xiph.org/flac/download.html

Nothing here is required and nothing phones home.
