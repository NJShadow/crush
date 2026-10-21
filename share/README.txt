Crush share pages (round 11)
============================

A share page is ONE .html file that contains a Crush archive plus everything
needed to open it: the WebAssembly decoders it uses, the reader, and (for an
encrypted archive) Argon2id + XChaCha20-Poly1305. Anyone can open it in a
modern browser - offline, nothing installed, nothing uploaded - see the file
list, have every file checked against its SHA-256, and save one file or all
of them (as a .zip).

Make one
  python engine/crush_engine.py c --share OUT.crush FILES...     (writes OUT.html too)
  python share/tools/make_share.py ARCHIVE.crush [-o OUT.html] [--encoding base64]
  From Python: crush_engine.compress(..., portable=True), then
               crush_engine.make_share_page(archive)

--share implies --portable: only transforms and codecs that have byte-exact
browser decoders are used. Cost measured in round 11: ~0 at the Crush tier,
+5.3% at Singularity on 300 .py files (no shared priors, no learned model).
JPEG XL, FLAC, zip recomposition, the perceptual tiers and the learned code
model (crushnn_m) are never portable; make_share refuses an archive that uses
them unless allow_partial=True, and then the page names each file it cannot
rebuild and why.

Encrypted archives: set CRUSH_PASSWORD when calling make_share so it can look
inside and embed only the decoders needed (otherwise all are embedded). The
page asks for the passphrase; a wrong one is refused and nothing is shown.

Layout
  src/          the reader (JavaScript, ASCII only - the page is windows-1252)
  codecs/       the WASM decoders + crush_codecs.js; CODECS.lock = SHA-256s
  crypto/       crush_crypto.wasm, its shim.c and Monocypher 4.0.2
  wasm-src/     sources + build.sh that rebuild codecs/ bit-for-bit from the
                engine's own C/C++ sources (checked: identical hashes)
  tools/        make_share.py, share_build.py
  tests/        run_all.sh (~15 min): crypto KATs, transform/codec vectors,
                whole archives in Node, headless-Chromium end to end, tamper,
                fuzz. Needs node, playwright, and work/arch from
                tests/build_archives.py.

Limits (measured, headless Chromium): 500 MB archive opens and checks in
~23 s using ~3.3 GB of browser memory; 1 GB not tested. Only Chromium was
tested (no Safari/Firefox/real phones). Needs DecompressionStream
(Chrome 80+, Firefox 113+, Safari 16.4+) and WebAssembly. Email previewers do
not run JavaScript: save the file, then open it.
