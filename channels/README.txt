Crush channels (round 11): move an archive without file transfer
================================================================

Every channel carries the .crush archive unchanged, split into packets with a
CRC-32 each, and releases it only once its SHA-256 matches. Receivers are
static single-file pages in dist/ that make no network requests.

From channels/py:
  python -m crushchan.paper  make    A.crush backup.pdf [--paper letter --parity 0.6]
  python -m crushchan.paper  restore out.crush photos-or-scans... | --pdf backup.pdf
  python -m crushchan.audio  encode  A.crush out.wav [--mode echo|robust|fast]
  python -m crushchan.audio  decode  in.wav out.crush
  python -m crushchan.stream make    A.crush out.mp4|out.gif   (needs ffmpeg for mp4)
  python -m crushchan.stream decode  in.mp4 out.crush
  python -m crushchan.link   make    A.crush --base https://your.site/link.html

Pages (dist/, built from js/ by build.py):
  receive.html       camera / photos / scans -> archive (zxing-cpp in WASM, 1.5 MB)
  receive-lite.html  the same with jsQR, 177 KB (cannot read paper photos well)
  stream-send.html   shows an endless fountain-coded QR stream of a chosen archive
  audio.html         send or receive by sound
  link.html          opens an archive carried in its own #fragment

Measured (simulated cameras/rooms, real decoders, Chromium): QR stream 5.7 KB/s
end to end with 30% of codes garbled; paper 3.8-5.9 KB per A4 page, any K of N
codes restore (use --parity 0.6+ if every page may be damaged); sound 132 B/s,
decoding at 3 dB SNR in a 0.6 s reverberant room; link up to ~1.5 MB in
Chromium (messengers: Telegram 4,096 chars, Discord 2,000 - not tested).
A link's fragment is never sent to the server hosting link.html, but anyone who
sees the link sees the archive: encrypt it if that matters.

Python needs: segno, reportlab, pillow, numpy; restoring from photos also needs
pyzbar (and pypdfium2 for --pdf). Tests: sh run_tests.sh (~10 min).
Licences: jsQR Apache-2.0, Nayuki QR generator MIT, zxing-wasm MIT
(zxing-cpp Apache-2.0); see js/vendor/.
