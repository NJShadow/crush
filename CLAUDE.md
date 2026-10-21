# Crush — working notes for Claude

You are picking up a project that is already built, measured and shipped. This
file is the context a previous session held in conversation. Read it before
changing anything.

**Crush** is a lossless, content-aware file compressor with an optional
encryption layer and a desktop interface. It runs on the user's Windows machine
(`Python 3.13`). Archives use the `.crush` extension.

The user is **Mike**. He is not a programmer by trade but follows the work
closely, reads the reports, and notices when a claim is not backed by a number.
He values ambition, and he values honesty about results more.

---

## Hard rules

These are not style preferences. Breaking any of them is a defect.

1. **Nothing ships without a byte-exact round-trip.** Every transform is
   inverted and compared against its input *before* it is accepted, per file, at
   compression time (`apply_transforms` in `engine/crush_engine.py`). A
   transform that does not reproduce its input exactly is discarded and the file
   falls back. Do not add a transform that skips this.
2. **Never inflate.** If a codec or transform would make something larger,
   store it plainly instead. Random data comes out the same size plus a header,
   and that is the correct answer.
3. **Measure, never estimate.** Every number in `README.md`, `BENCHMARKS.md`,
   `V4-RESULTS.md` and `SECURITY.md` was produced by a run on real data. If you
   cannot measure something, say so instead of estimating it.
4. **Report negative results.** Three substantial pieces of work were built,
   measured, found not to pay, and documented as rejected. That is the norm
   here, not a failure. See `HANDOFF.md`.
5. **Never weaken the encryption to make something easier.** If a change would
   touch `engine/crush_crypt.py`, re-run `tests/test_crypt.py` and read
   `SECURITY.md` first. The format has already survived a three-way adversarial
   audit; twelve findings were fixed. Do not regress them.
6. **A passphrase never reaches the log, a URL, a traceback, a temp file, or
   `ps`.** This was audited specifically. Keep it that way.

---

## Layout

```
Crush/
  CLAUDE.md            this file
  HANDOFF.md           state of play: what shipped, what was rejected, what is open
  README.md            the user-facing manual
  SECURITY.md          encryption design, threat model, and the audit
  BENCHMARKS.md        every measured compression result
  V4-RESULTS.md        the v4 research round, including what did not work

  app.py               desktop host: loopback HTTP server + native window
  ui/index.html        the entire interface, one file, no build step
  engine/
    crush_engine.py    the core: sniffing, transforms, bundling, tournament, container
    crush_crypt.py     encryption: Argon2id + XChaCha20-Poly1305 STREAM
    crush_oracle.py    Oracle - program synthesis
    crush_prism.py     Prism - structural shredding of text formats
    crush_echo.py      Echo - content-defined chunking / dedup
    crush_vector.py    Vector - float and numeric array codec
    crush_genome.py    Helix - FASTQ / FASTA / SAM / VCF
    crush_loom.py      Loom - structure discovery in unknown binaries
    crush_perceptual.py  the opt-in lossy tier
    crush_zpin.py      the pinned deflate: every "rebuild the original deflate
                       bytes" transform goes through this, never Python's zlib
    crushz.dll         zlib 1.3.1 built for Windows x64 (source + build.sh in crushz/)
    crushpf.exe        preflate helper: rebuilds deflate zlib did not write
                       (source, CRUSH PATCHes and build.sh in crushpf-src/)
    crushjpg.exe       JPEG helper: Brunsli + jcm, Crush's own coefficient model
                       (source and build.sh in crushjpg-src/; lab/jpegcm/ has
                       the research harness)
    crush_jpeg.py      drives crushjpg
    crushcm.c / .exe   context-mixing bit predictor (C, prebuilt for Windows x64)
    crushnn(.exe)      crushcm + an integer-only byte transformer (codec crushnn_m,
                       Singularity, code bundles <= 256 KB); source in crushnn-src/
  priors/              shared dictionaries and pretrained model corpora (Atlas);
                       code.cnn is the learned model - pinned by SHA-256, part of
                       the format, never retrain in place
  share/               self-opening HTML archives (reader JS, WASM decoders built
                       from engine sources by wasm-src/build.sh, make_share.py)
  channels/            paper / sound / QR stream / link transports + receiver pages
  tools/               optional accelerators the user can drop in (cjxl, flac)

  tests/               the verification suites - run these
  benchmark/           corpus builders and the older benchmark harness
  lab/                 research code, including the rejected experiments
  Claude outputs/      screenshots the user keeps; do not touch
  *.bat                Windows launchers
```

`Crush.bat` is how the user starts the app. `Crush (Browser Mode).bat` is the
fallback. `Crush (Diagnose).bat` runs the engine with no interface at all.

---

## How to run and test

Everything below runs from the project root. There is no build step and no
package manifest beyond the launchers.

```
python engine/crush_engine.py c out.crush <paths> --tier singularity [--harder]
python engine/crush_engine.py x out.crush <dest> [--only "path/one.txt" ...]
python engine/crush_engine.py l out.crush
python app.py --selftest          # engine health, no interface
python app.py --browser           # the app, in the default browser
```

Encryption adds `--ask` (prompts twice), `-k PASS`, `CRUSH_PASSWORD`, and
`--kdf fast|balanced|paranoid`.

**The test suites, in the order worth running:**

| Command | What it covers | Time |
|---|---|---|
| `python app.py --selftest` | engine loads, round-trips byte-exact | seconds |
| `python tests/test_crypt.py --skip-slow` | 79 checks, 402 tamper cases, RFC vectors | ~2 min |
| `python tests/test_unwrap.py` | pinned deflate, preflate, PDF/gzip/zip/PNG unwrapping, JPEG, hostile inputs, old-archive compatibility | ~2 min |
| `python tests/test_localauth.py` | the loopback server's auth model | ~30 s |
| `python tests/e2e_crypt.py` | the whole encryption flow in a real browser | ~2 min |
| `python tests/e2e_app.py` | drag-drop, compression, selective extraction | ~2 min |
| `python benchmark/edge_tests.py` | adversarial inputs across tiers | ~1 min |
| `python benchmark/edge_tests2.py` | Prism edge cases + full archive round-trip | ~3 min |
| `python tests/test_safety.py` | data safety: no overwrite without asking, atomic writes, collisions, damaged/hostile archives, big-file streaming, CLI exit codes | ~1 min |
| `python tests/test_nn.py` | learned code model: pinned model, known-answer test, hostile streams, archive round trip | ~2 min |
| `sh channels/run_tests.sh` | channels: Python/JS packet identity, hostile packets, audio, camera, paper, video | ~10 min |
| `sh share/tests/run_all.sh` | share pages: crypto KATs, transforms, codecs, Chromium end to end, tamper, fuzz | ~15 min |

The browser tests need Playwright and Chromium. `tests/test_crypt.py` writes its
own `tests/RESULTS.md` as it runs, so that table can never drift from the code.

The benchmark corpora are **not** in this folder — they are ~316 MB of generated
data. Rebuild them with `python benchmark/build_corpus.py` and
`python benchmark/build_corpus2.py` before running the older benchmark scripts.

**Dependencies:** `numpy zstandard brotli pyppmd cryptography argon2-cffi`, plus
`pywebview` for the native window. `Crush.bat` installs them on first run.
Playwright is only needed for the browser tests.

---

## How the compressor is put together

Five steps, and the order matters:

1. **Identify** — `sniff()` classifies by structure, not just extension.
2. **Transform** — a reversible transform exposes the real content. Oracle runs
   first (if a generator exists, nothing downstream has work left); perceptual
   before the lossless media transforms; Prism and Loom last, on whatever is
   still a byte stream.
3. **Prove it** — invert and byte-compare before writing anything.
4. **Bundle** — files of the same class are compressed together in one solid
   stream, so between-file redundancy is found; identical files stored once.
5. **Compete** — every codec that suits the bundle runs, smallest wins.

Adding a transform means: a class with `name`, `forward(data) -> (payload, aux)
| None`, and `inverse(payload, aux) -> bytes`; registering it in `TRANSFORMS`;
and adding it to a stage in `TRANSFORM_PLAN`. Alternatives inside one stage
compete. The framework handles verification and the never-inflate guard — a
transform returning `None` means "not applicable", which is always safe.

Tiers are `quick`, `balanced` (default), `crush`, `obsession`, `singularity`.
Expensive stages (Prism, Loom) are gated to `crush` and above via the `deep`
flag. Loom additionally skips files under 256 KB, because its analysis pass
costs more than it saves on small inputs.

---

## How the app is put together

A loopback HTTP server runs in **both** modes and the native window is a frame
around the same page a browser would get. This is deliberate and hard-won: the
earlier build used pywebview's injected JavaScript bridge, which arrives *after*
the page starts running, and calling it a moment early produced
`api(...).ping is not a function` in a window that never recovered. The page now
defines its connection to Python synchronously, in the page itself.

Three layers of local authentication, all of which an audit attacked:

- a **launch key** in the URL the window is opened with, without which the page
  is not served at all (the page carries the API token, so serving it to anyone
  hands them the API — an auditor demonstrated exactly that);
- a per-run **API token** on every request, compared in constant time;
- **Origin** checked against this server's own origin, and **Host** pinned to
  loopback, which is what stops CSRF and DNS rebinding.

Note the subtlety that already bit once: browsers send `Origin` on *same-origin*
POSTs too, so refusing every request that carries one refuses the app itself.
Compare against the server's own origin, never reject outright.

Drag-and-drop has two routes because a web view is not allowed to tell a page
where a dropped file lives: native path capture via pywebview (zero copy), and
an upload fallback that walks directory entries. The native route is tried first
and the fallback starts only if it stays silent, so nothing is ingested twice.
**A drop must never open a file dialog** — the user was explicit about this.

---

## Conventions

- **The interface is one file.** `ui/index.html` holds all HTML, CSS and JS. No
  framework, no build. Keep it that way.
- **Prose in the product explains itself.** Comments and user-facing text say
  *why*, and state limitations plainly rather than burying them. When something
  cannot be done, the document says so in the same voice as the wins.
- **Em dashes and plain language** in user-facing text; no marketing register.
- The user reads the reports. Tables with real numbers land better than
  adjectives.
- When a measurement kills a planned feature, say so and do not build it. That
  has happened twice here and both times it was the right call.

---

## Gotchas that have already cost time

- **The project lives in two folders on the owner's PC**: the working folder
  and the GitHub repository folder (created for 1.0.0). Every update goes to
  both, and both are checked after writing.
- **Verify what actually landed on the user's machine.** A commit once reported
  success while writing a stale copy, and the user spent a round re-reporting a
  bug that had been "fixed". After writing files, stage them back and compare
  checksums.
- **`assert` is stripped by `python -O`.** Invariants that matter raise instead.
- **Argon2 memory is charged per attempt**, so `--kdf paranoid` needs 1 GB free.
  Header parameters are bounded before use precisely so a hostile archive cannot
  make the machine allocate more.
- **Encrypted archives cannot be listed without the passphrase.** Anything that
  calls `read_index` on a finished archive must pass the passphrase through —
  forgetting this once made every encrypted compression report as a failure.
- **`crushcm.exe` is prebuilt for Windows x64.** There is no Linux binary in
  this folder; build from `crushcm.c` with
  `gcc -O2 -o crushcm crushcm.c -lm` if you need one.
- **Never call `zlib.compressobj` to rebuild stored deflate bytes.** Python's
  zlib is not a stable program: 3.14 on Windows is zlib-ng, which emits
  different bytes. Use `_redeflate` / `_deflate_with` (they go through
  `crush_zpin`). The index into `zpin.PARAMS` is part of the archive format -
  append to that list, never reorder it. Decompression through Python's zlib is
  fine; inflate is fully specified.
- **`crushz.dll` is cross-built here** with `x86_64-w64-mingw32-gcc` (see
  `engine/crushz/build.sh`) and checked under Wine against the Linux build.
  Rebuild both if you touch the C; the known-answer digest in `crush_zpin.py`
  must not change. Only the Windows builds ship in this folder - on Linux, run
  `sh engine/crushz/build.sh`, `sh engine/crushpf-src/build.sh` and
  `sh engine/crushjpg-src/build.sh` before testing (and build crushcm as
  below), or those transforms quietly switch themselves off.
- **The native helpers (`crushpf`, `crushjpg`) are separate processes on
  purpose** - they parse archive data that may come from someone else. Keep them
  out of process, keep lengths bounded, and after changing either, rebuild the
  ASan build and re-run `lab/fuzz_pf.py` / `lab/fuzz_helpers.py` (see
  SECURITY.md). Any change to jcm's contexts or constants changes the format:
  bump `JCM_VERSION` in `crushjpg.cc` and keep the old decoder.
- **Build the helpers with the `build.sh` next to each source folder**; they
  cross-compile Windows `.exe`s with `x86_64-w64-mingw32-g*-posix` and need
  Wine to check them. After any rebuild, compare Linux and Windows output on the
  same inputs - identical bytes, both directions - before shipping.
- **The device commit tool re-encodes anything that looks like a JPEG** on its
  way to the user's machine, whatever the extension (a 4,022-byte test JPEG
  arrived as 9,793 bytes, as `.jpg` and as `.jpg.bin`). Staging *from* the
  machine is byte-exact. Test JPEGs therefore live in
  `tests/data/jpeg_fixtures.b64`. Two stray re-encoded copies
  (`tests/data/*.jpg`, `*.jpg.bin`) are unused and can be deleted.
- **Never rename a directory your shell is sitting in, and never `pkill -f`
  a pattern that appears in your own command line.** Both happened this round:
  the first left every shell command failing with ENOTDIR (recovered only by a
  WorktreeCreate hook trick), the second killed the command that issued it.
- **The share page decodes everything the engine writes, so a format change is
  two changes.** A new codec, transform or jcm version needs its WASM decoder
  rebuilt (`share/wasm-src/build.sh`, then the share tests) or it must stay out
  of `PORTABLE_CODECS` / `PORTABLE_TRANSFORMS`. Round 11 caught this: jcm v2
  would have produced pages that could not open their JPEGs.
- **`crushnn` must be built with -O3** (3.4x faster than -O2; output identical).
  Any change to `nn.h`, `crushnn.c` or `priors/code.cnn` changes the format: new
  stream magic and a new model name, keep the old ones, re-pin the KAT in
  `tests/test_nn.py`, and check it under Wine.
- **The priors overlap some benchmark files** (`code.prime` holds 5 stdlib test
  files; `text.prime` shares its source with `text_english.txt`). Use the
  third-party Python set or new data when measuring anything prior-related.
- **Extraction refuses to replace existing files** unless `overwrite=True`
  (CLI `--overwrite`, UI "Replace them"). A test that extracts twice into one
  folder must pass it. Every write goes through `_write_restored` /
  `_restore_big` (scratch name, verify, rename) - never `open(final, "wb")`.
- **Files over `BIG_FILE` (256 MB) never pass through memory**: `_write_big` /
  `_restore_big` stream them in 4 MB chunks, in a bundle of their own marked
  `"stream": True` (still an ordinary store/zstd bundle to older readers).
  Tests shrink `BIG_FILE` to 1 MB to exercise this quickly.
- **Every length from an archive is untrusted.** `_bundle_raw` checks the codec
  headers (ppmd, crushcm, Echo) against the index before a decoder runs; add the
  same for any new codec whose decoder trusts a length.
- **`.bat` files must be CRLF** (`.gitattributes` enforces it); cmd.exe
  mis-reads `goto` labels in LF-only files.
- The singularity tier has a large fixed overhead — about 80–120 s regardless of
  input size. Do not benchmark it on tiny files and conclude something is wrong.

---

## Where to look next

`HANDOFF.md` has the current state, every measured result, the three pieces of
work that were built and rejected with the numbers that killed them, the known
limitations, and the candidate next moves. Read it before proposing new work, so
you do not rebuild something that has already been measured and found wanting.
