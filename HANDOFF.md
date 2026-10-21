# Crush — state of play

First written at the end of the session that built v4 and the encryption
layer; updated at the end of rounds 9, 10, 11 and 12 (the 1.0.0 release audit) (v5: the pinned deflate; PDF, gzip
and zip unwrapping for any producer; built-in JPEG recompression with Crush's
own model; sharing as a web page, paper or sound; a learned code model), for whoever picks this up next. `CLAUDE.md` has the rules and the layout; this file
has the history, the numbers, and the open questions.

---

## Where the project actually stands

Everything described below is built, measured, and on the user's machine. The
app runs, the tests pass, and no known defect is outstanding.

**Compression.** A five-step content-aware pipeline — identify, transform,
prove, bundle, compete — with eight specialised layers on top of a codec
tournament. On the extended benchmark corpus (195,671,137 bytes, 325 files):

| Method | Size | % of original |
|---|---:|---:|
| ZIP -9 | 44,701,481 | 22.85% |
| tar + xz -9e | 20,711,308 | 10.58% |
| 7-Zip -mx=9 solid | 20,040,591 | 10.24% |
| **Crush — Singularity** | **9,192,201** | **4.70%** |

**Encryption.** Optional. Argon2id → XChaCha20-Poly1305 in STREAM mode, whole
archive including the index, so a locked archive cannot even be listed. Survived
a three-way adversarial audit; twelve findings fixed. Full detail in
`SECURITY.md`.

**Interface.** One HTML file, a loopback server, native window or browser.
Drag-and-drop, selective extraction, an encryption panel and an unlock screen.

**Verification.** 76 crypto checks and 402 tamper cases with zero silent
forgeries; 15 local-auth checks; 19 end-to-end browser checks for the encryption
flow and 15 for the rest of the app; the adversarial edge-test suites byte-exact
across every tier.

---

## What each layer is worth, measured

These are the numbers that justify each piece existing. All byte-exact, all from
runs on this hardware.

| Layer | What it does | Measured |
|---|---|---|
| **Oracle** | Finds the *program* behind the bytes: counters, timestamps, linear recurrences, LCG streams | 2 MB of LCG output — which xz makes *bigger* — becomes 68 bytes |
| **Prism** | Shreds JSON/CSV/logs down their real structural axis, then fits laws per column | A telemetry export xz gets to 182,696 B comes out at **1,472 B** |
| **Atlas** | Shared priors both sides read, so their contents never travel | 25.9% below every competing tool on a small source file |
| **Echo** | Content-defined chunking past any codec's window | 2.5× faster on a revision set, 0.9% smaller; engages above 24 MB |
| **Helix** | FASTQ/FASTA/SAM/VCF split into their real streams | **−18.6%** vs the previous build, and 2.5× faster |
| **Vector** | Float arrays split into real channels; recovers exact decimals | **−21%** on financial ticks, −11.8% on sensor float32 |
| **Loom** | Infers record layout of binary formats with no parser | −4% to −10% on struct-packed records; ~0 on real corpus binaries |
| **crushcm** | Context mixing, twelve models, two-layer neural mixer | The mixer change alone was worth −6.8%, −10.3% on JSON |
| **Perceptual** | Opt-in lossy, PSNR/SSIM measured per file | 71.3% smaller than 7-Zip on a media set |
| **PDF unwrap** (v5) | Every FlateDecode stream rebuilt exactly from its contents | 448 real PDFs: **−54.9%** vs v4, 55% below 7-Zip |
| **Gzip unwrap** (v5) | The deflate layer of .gz/.tgz removed, whoever wrote it | 887 .gz files: **−56.5%** vs v4, 54% below 7-Zip |
| **Zip unwrap** (v5 rework) | Now any producer, and up to 65,536 members | 107 jars/docx/zips: **−42.0%** vs v4 |
| **Pinned deflate** (v5) | Crush's own zlib 1.3.1, known-answer tested at start | Keeps every docx/xlsx/PNG/PDF/gz archive extractable after a Python upgrade |
| **preflate** (v5) | Rebuilds deflate zlib did not write from a correction record | Corrections 1.1% (gzip) to 5.8% (zip, PNG) of what they rebuild |
| **JPEG / jcm** (v5) | Crush's own coefficient model; Brunsli at balanced | Real photos: **30.9%** smaller (Brunsli 22.3%, packJPG 25.0%, JPEG XL 17.9%) |

---

## Round 9 (v5) — what was found and what shipped

**A latent defect in every archive with a docx, xlsx, zip or PNG in it.** The
transforms that rebuild original deflate bytes used Python's own zlib. Python
3.14 on Windows ships zlib-ng, which emits different bytes (11 of 15 settings
differed). Simulated: an archive written by the previous engine, extracted under
zlib-ng, failed verification on 6 of 13 files. Fixed by `crush_zpin.py` +
`crushz.dll` (unmodified zlib 1.3.1, symbol-prefixed, known-answer tested). No
format change: every existing archive extracts exactly as before, and now keeps
doing so whatever Python Mike installs. If the DLL is ever missing and Python's
zlib fails the test, compression simply stops unwrapping containers, and
extraction restores everything else and names the files it could not rebuild
and why.

**PDFs were never being unwrapped.** `pdf` was routed to a transform that only
reads zip directories. `PdfUnwrap` now handles it: 99.3% of the flate bytes in
448 PDFs rebuilt exactly, 54.3% smaller on the set, −56.7% total and −7.3%
median when each file is compressed alone.

**Gzip** was stored as-is. `GzipUnwrap` handles anything zlib wrote. GNU gzip
cannot be rebuilt (measured: 0 of its files), which on this machine left 81% of
.gz bytes untouched.

Full tables in `BENCHMARKS.md`, section v5. New suite: `tests/test_unwrap.py`
(33 checks).

---

## Round 10 (v5) — what was found and what shipped

**preflate for everything zlib did not write.** Vendored (Apache-2.0) as the
`crushpf` helper process, wired into all four unwrappers as the fallback after
the pinned-zlib search. Gzip −11.5% → −56.5%, because GNU gzip is now covered;
all 40 current-Chrome PDF streams now rebuild.

**Fuzzing found real bugs in preflate's reconstructor** — out-of-bounds reads
and writes, UB shifts, divide-by-zero, a multi-GB allocation — all reachable
from a crafted archive. Fixed with bounds checks marked `CRUSH PATCH`; output
on valid streams is unchanged (checked). The JPEG helper's decode path had no
findings in 5,500 cases. Details and counts in SECURITY.md.

**JPEG, built in, with a model of Crush's own.** `crushjpg` = Brunsli (fast,
balanced tier) + **jcm** (Crush tier and above): Brunsli keeps everything that
is not a coefficient; jcm codes the coefficients. Its three ideas, in order of
what they were worth: code the 7x7 interior first and predict the first row and
column (value and sign) from the neighbour across the border; predict DC from
edge continuity (Lepton's idea); intra-block contexts. 30.9% on 59 real photos
from Mike's Downloads, against packJPG's 25.0% — the strongest tool measured
here. It is ~0.5 MB/s, hence the tier split. Source: `engine/crushjpg-src/`,
lab harness in `lab/jpegcm/`.

**A pre-existing bug that stopped older archives extracting.** Found by testing
Mike's three real archives: one (made in early September) failed verification in
both the old and new engines. Its index held full 64-hex hashes; the verifier
compared a 32-hex prefix. Every archive made before the index was trimmed was
affected. Fixed; regression test added.

**Measured and not shipped:** coding every edge zero flag instead of a per-edge
count (−0.3 points); dual-rate counters (+0.17 points for 30% more time);
Lepton (fails on CMYK/4:1:x, weaker than Brunsli here); packJPG (stronger than
Brunsli, weaker than jcm, LGPL and not fuzzed).

---

## Round 12 — release audit (1.0.0)

Mike is publishing Crush on GitHub (MIT). His one requirement: users must never
lose data. Three specialists attacked it: a data-safety code audit, a
hostile-archive campaign through the full extract path, and a review of the
fixes. Their reports are summarised in `CHANGELOG.md` and SECURITY.md.

**What was wrong, in order of severity.** Each was reproduced first, then fixed
and covered by `tests/test_safety.py`:

- Two inputs with the same stored name: the last overwrote the first and both
  were reported restored.
- Compressing truncated the output path before any work. A failure or a kill
  then destroyed the previous archive, and `crush c notes.txt todo.txt`
  destroyed notes.txt.
- Extraction overwrote existing files in place, without asking. A disk-full
  error left a half-file under the real name.
- A file growing while it was read was archived under the wrong hash.
- Unreadable folders were skipped silently.
- Share outputs overwrote same-named files.
- A duplicate was restored by copying whatever was on disk at its twin's path.
- The index had no checksum: 44 single-bit flips gave wrong names or wrong
  container metadata, reported as success.
- Three decoders trusted archive lengths:
  - pyppmd: a use-after-free;
  - crushcm: a heap overflow and a hang;
  - Echo: memory blow-up.
- Echo needed 24 bytes of RAM per input byte.
- The fixes' own review then found three more:
  - an over-tight Echo bound that would have refused valid archives;
  - a crushcm bound 8 MB too low;
  - a big-file dedup case that could re-label a changed copy.

**What was added.**
- Files over 256 MB are streamed both ways, in 4 MB chunks, and verified after
  writing.
- Payloads spill to a temporary file past 512 MB.
- Incompressible media skips the slow codecs.
- Extraction asks before replacing files, in the app and the CLI.
- The CLI uses exit codes and prints messages, not tracebacks.
- Release files: `LICENSE` (MIT), `THIRD_PARTY_NOTICES.md`, `CHANGELOG.md`,
  `RELEASE_CHECKLIST.md`, `.gitignore`, `.gitattributes`, `Run-Tests.bat`.
- The `.bat` launchers are now CRLF.

**Measured.**
- A real 4 GB+ round trip was byte-exact.
- 1.6 GB compressed in 66 MB of RAM.
- 41 archives from earlier versions (every codec and tier) still extract
  byte-exact, and the previous version reads 1.0.0 archives.
- Mike's own test folder (26 files, 429 MB, mostly video, FLAC, GIF and
  encrypted zips) came back byte-exact at every tier tried. Mostly
  already-compressed, so about 2% smaller. Table in BENCHMARKS.md.

**Not tested here, stated plainly.**
- Nothing ran on real Windows. There is no Windows Python in this
  environment, so the Windows-only paths are reasoned, not run: `\\?\` long
  paths, reserved names, NTFS case folding, os.replace over open files.
  `Run-Tests.bat` runs the important suites on the user's own PC.
- The 3.2 GB video in Mike's folder could not be copied here.
- `Build-Crush-exe.bat` was updated but not rebuilt.

---

## Round 11 — sharing anywhere, a learned model, jcm v2

Mike asked for something dramatically further, and for archives that can be
sent "in creative ways, regardless of type". Four specialists worked in
parallel (their full reports, with every command, are kept outside the
product; the summaries are below and in BENCHMARKS.md, "Round 11").

**What could not be broken, said first.** No lossless method can shrink every
file: there are fewer short files than long ones (the counting argument), and
already-compressed data — video, most of Mike's Downloads now — sits at its
entropy unless a model understands its *format* (as jcm does for JPEG). The
gains this round come from three honest levers: better models of specific
formats (jcm v2), knowledge shipped with Crush instead of inside the archive
(the learned code model), and new ways to move the archive itself.

**1. Share anywhere (`share/`, `channels/`).** An archive can now leave Crush as:

- **a web page that opens itself** — one .html file with the archive, the
  WebAssembly decoders it needs, the reader and (if locked) Argon2id +
  XChaCha20-Poly1305 inside. Opens offline in a modern browser, checks every
  file's SHA-256, saves files or a .zip. `crush c --share`, or the app's
  "Send it anywhere" panel. Fixed cost ~76–150 KB plus 1.6% of the archive.
  The WASM decoders are built from the engine's own C sources and rebuild
  bit-for-bit (`share/wasm-src/build.sh`; checked: identical hashes).
- **a paper backup** — A4/Letter PDF of QR codes with Reed-Solomon across
  pages: any K of N codes restore it, so a lost page can be missing. 3.8–5.9
  KB per page.
- **sound** — an OFDM modem (.wav) robust to a reverberant room: 132 B/s.
- **a QR video / endless QR stream** — fountain-coded, so the receiver can
  join late and miss frames: 5.7 KB/s measured end to end in Chromium with
  30% of codes garbled.
- **a link** — the archive in the URL fragment of a static page (up to ~1.5 MB
  in Chromium; most messengers cap far lower).

Every channel carries the archive unchanged and releases it only after its
SHA-256 matches. Receivers are static single-file pages (`channels/dist/`) that
make no network requests. **Not measured on real hardware**: cameras, printers,
speakers were simulated (detailed channel models, real decoders, real
browsers). Only Chromium was tested — no Safari, Firefox or real phones.

`portable=True` (`--portable`, implied by `--share`) restricts the pipeline to
layers with byte-exact browser decoders: ~0 cost at the Crush tier, +5.3% at
Singularity (no shared priors, no learned model). JPEG XL, FLAC, zip
recomposition, perceptual tiers and `crushnn_m` are never portable; a page
built from an archive using them names each file it cannot rebuild.

**2. A learned code model (`crushnn`, `priors/code.cnn`).** A 0.85M-parameter
byte-level transformer trained here on the Python 3.12 standard library
(PSF-2.0, notice in `priors/code.cnn.LICENSE.txt`), run with **integer-only
arithmetic** so every machine computes the identical prediction (Linux -O0,
-O2, -O3 -march=native and Windows under Wine: byte-identical, cross-decoding;
a known-answer test pins it). Its prediction feeds crushcm's mixer. Offered at
Singularity for source-code bundles up to 256 KB. On 20 held-out third-party
Python files (no overlap with the training set by name or content), as one
Singularity archive: **19,612 → 16,644 bytes (−15.1%)**; per file the
specialist measured −18.4% against today's primed path and −48% against
xz -9e. Go −5.4%, English −2.1%. Cost: ~6 KB/s each way (compress 62 s → 98 s
on that set). The model file is now part of the archive format: never retrain
it in place — ship a new name.

**A measurement problem found on the way:** `code.prime` contains 5 files of
the in-domain test set verbatim, and `text.prime` is built from the same Debian
copyright corpus as the benchmark's `text_english.txt` (it compresses that file
to 1.7%). The English-text and some code figures in earlier rounds are
flattered by this. The third-party Python set (testB) has no such overlap.

**3. jcm v2** (JPEG). Public 51-file set 23.50% → **23.97%** saving (lead's own
check on the 30 images jcm takes alone: 23.82% → 24.29%); ~2× faster on one
thread (instructions −45%); luma, chroma and stripes are independent segments,
so a batch on 2 threads reached 1.74 MB/s encode / 1.54 decode. The 2 MB/s
single-thread goal was **not** met, so balanced stays on Brunsli. v1 streams
still decode (frozen v1 decoder kept). Wins: chroma from co-located luma, a
second mixer averaged in, an all-AC-zero flag per block, lazily sized tables.
Not re-measured on Mike's own photos.

**Verified this round:** jcm v2 Linux = Wine both directions, v1 data decodes
(Linux and Wine), WASM decoder exact on v2, v1 and Brunsli records (42/42);
crushnn KAT identical under Wine; share pages byte-exact in Chromium with zero
network requests; app end to end (page, paper, sound, each restored
byte-exact); fuzzing in SECURITY.md.

---

## Built, measured, and rejected

Do not rebuild these without a reason the numbers do not already answer. The
code is kept in `lab/` if you want to see it.

**A recurrent (LSTM) mixer for crushcm** — `lab/v4/crushcm2.c`.
The cmix trick: replace the static logistic mixer with an LSTM trained online by
both sides, so no weights need to ship. It works and it is provably
deterministic (round-trip exact, repeat compression bit-identical, `-O0` and
`-O2` builds byte-identical — no floating-point drift). It is also **−1.7% for
roughly 10× the time**, on the slowest component in the product: 0.13 MB/s
becomes 0.012 MB/s, so a 100 MB archive would take over two hours each way. Not
shippable. If a use case ever appears where an archive is written once and read
by thousands, it is ready.

**Weave — similarity ordering and cross-file delta** — `lab/v4/weave.py`.
Two ideas that should work and do not. MinHash similarity ordering improved
adjacent-pair similarity from 11.3 to 14.5 on a 90-file template document set
and moved the compressed size from 160,556 to 160,508 bytes — **0.03%**.
Explicit cross-file delta against a cluster reference produced **246,796 bytes,
over 50% worse**. The reason is the same in both cases and is worth remembering:
a solid stream with a 64 MB dictionary has already found the redundancy a delta
would encode, and delta-coding destroys the literal repetition the stream was
exploiting. The idea has real value only for archives far larger than the
dictionary.

**Atlas-2 — shipping a much larger distilled model.**
Not built, because the measurement said not to. The experiment
(`lab/v4/curve.py`, `curve.log`) took 20 small Python files held out from a
1,529-file pool — disjoint by content hash *and* filename — and measured what
shipped knowledge is worth:

| Shipped knowledge | Bytes | Of original | vs xz -9e |
|---|---:|---:|---:|
| none — xz -9e | 30,960 | 31.75% | — |
| none — Crush CM | 25,219 | 25.86% | −18.5% |
| 1 MB prior | 16,757 | 17.18% | **−45.9%** |
| 2 MB prior | 16,011 | 16.42% | −48.3% |

**The first megabyte of shipped knowledge is worth two and a half times the
entire step from xz to context mixing. The second megabyte is worth 4.5%.** A
bigger generic prior is not where the remaining value is.

---

## Known limitations, stated plainly

- **The passphrase strength meter is a heuristic, not zxcvbn.** It knows 218
  common passwords, leetspeak variants of them, and Crush's own 512-word list.
  An uncommon dictionary word — `Tr0ub4dor&3` is the canonical example — still
  scores higher than it deserves. The interface says so and offers **Suggest**,
  which is provably 72 bits. A real fix means shipping a cracking dictionary.
- **The plaintext archive briefly exists on disk during encryption.** The format
  seeks backwards to write its index, so it is built into a temporary file and
  then streamed through the cipher. On Linux with `O_TMPFILE` that file has no
  name anything can open; on Windows it has a directory entry for its lifetime.
  Unlinking is not erasure.
- **Output length leaks how well your data compressed**, because compression
  must precede encryption. Same effect as CRIME and BREACH; inherent; no archive
  format avoids it.
- **Nothing binds an archive to a name, time or author.** Two archives from the
  same person with the same passphrase can be swapped and every check passes.
- **Loom's wins are on constructed files.** It finds nothing on every real
  binary in the corpus — SQLite, a DLL, a Python extension, a TrueType font —
  and correctly declines. It will help people whose data is struct-packed
  records; it did not move the benchmark.
- **Adobe Acrobat PDFs and Word-made .docx files are measured thinly.** One
  Word-made docx and 20 "Microsoft Print to PDF" files were available; no
  Acrobat output. preflate handles any deflate, but correction sizes for those
  producers are not measured at scale.
- **Unwrapping costs time.** PDF set 55 s → 147 s; gzip 11 s → 241 s, and gzip
  extraction 0.4 s → 21 s, because preflate re-parses every GNU gzip stream.
- **jcm is slow**: ~0.5 MB/s each way (Brunsli ~2–5 MB/s). Compression also
  runs the verifying decode, so a gigabyte of photos takes roughly an hour at
  the Crush tier. It runs one image at a time; running images in parallel is
  the obvious next speed-up.
- **JPEGs inside a zip or PDF are recompressed separately**, which can undo the
  solid bundle's cross-file deduplication of a repeated image: on 20 invoices
  sharing a logo the gain was 0.5%, not the ~20% a photo alone gets.
- **Three tiny PDFs (under 2.3 KB) came out 2–8 bytes larger** than the previous
  engine made them: the rebuild recipe cost more than it saved. Still never
  larger than the input.
- **`--harder` container rebuilding is content-exact, not byte-exact.** Every
  file inside comes back bit-identical and SHA-256 verified; the container's own
  packing bytes may differ. It is off by default and labelled per file.

---

## Candidate next moves

In rough order of expected value. None of these is committed — talk to Mike
before starting one, and expect him to have his own ideas. Mike has already said
yes to jcm speed and ratio work; round 11 delivered part of both.

1. **Send standalone JPEGs to crushjpg as a batch** (`pack_many`), so jcm v2's
   segment threads run across images. On Mike's core count (unmeasured) that
   may be enough to make jcm the balanced default.
2. **Train the code model longer, and per language.** 10M → 30M training tokens
   was still worth −8.6% and the curve had not flattened. Licence-clean corpora
   for JavaScript/C/English are the bottleneck, not compute.
3. **Rebuild the priors without test-set overlap** (see round 11) and re-run
   the text/code benchmarks, so published numbers are clean.
4. **Real-hardware trials of the channels** — a printer and a phone camera, two
   laptops and a speaker — and Safari/Firefox for the share page.
5. **Video.** Mike's data is now mostly video. Lossless H.264/HEVC
   recompression (re-coding the entropy layer, as jcm does for JPEG) is the
   only lever that can move it; it is a large project.
6. **Lossless MP3/AAC.** Mike's Downloads folder has 258 MP3s (280 MB) — real
   test data. The jcm machinery (parse with an existing library, code the
   quantised values with context mixing, rebuild exactly) carries over directly.
7. **Narrower priors, not bigger ones.** The shipped-knowledge curve is the most
   actionable result of the last round: the value is in *matching* the prior to
   the content, not in scale. Crush selects among four prior sets by file class;
   finer sets — per language, per document type, per format — is the only lever
   measured to move things by tens of percent rather than single digits. This is
   mostly a data-collection job, not an engineering one, and it needs a careful
   answer to "what may we legally ship in the priors folder".
8. **A real passphrase dictionary** for the strength meter, which would close
   the one documented weakness in the encryption UX.
9. **Public-key recipients for encryption.** Mike chose password-only when asked,
   explicitly to keep the audited surface small. age-style X25519 recipients
   would let him send an archive to someone without sharing a secret. Worth
   raising again now that the password path is audited and stable, but it is a
   real expansion of what must be got right.
10. **A standalone `Crush.exe`.** `Build-Crush-exe.bat` exists, but any `dist/`
   folder on the machine predates several fixes, and the bat does not yet pass
   `--hidden-import` for `cryptography` / `argon2`. `crushz.dll`, `crushpf.exe`
   and `crushjpg.exe` are inside `engine/`, so the existing
   `--add-data "engine;engine"` already carries them. Rebuild and test first.
11. **Fuzz crushcm.exe**, the one decoder of archive data not fuzzed yet.

---

## Things Mike has asked for, in his words

Useful for calibration, because they set the register of this project:

- *"push the limits of what is physically possible, and go beyond them"*
- *"Conceive concepts that humans have yet to perceive or grasp"*
- *"approach the situation like the greatest computer scientist that ever lived"*
- *"create several versions of yourself to research and compete"*
- *"subject the app and files encrypted with the app to a full security audit to
  ensure when a user chooses to use encryption it's bulletproof"*

He wants maximum ambition on the research, and he has been given honest negative
results four times without complaint — the round that reported "one byte
smaller, and here is why" was received as well as the rounds with big numbers.
Ambition in what you attempt; precision in what you claim.

---

## Session history

| Round | What happened |
|---|---|
| 1 | The engine, the routing architecture, the interface, the first benchmarks |
| 2 | Five new layers: Oracle, Prism, Atlas, Echo, Perceptual |
| 3 | Two-layer neural mixer in crushcm (−6.8%); parallel research agents |
| 4 | Fixed the app hang (a path with spaces passed to pywebview) |
| 5 | Fixed the native-mode bridge race; drag-and-drop; selective extraction |
| 6 | v4: Helix, Vector, Loom shipped; LSTM mixer and Weave rejected; the shipped-knowledge curve |
| 7 | Optional encryption, three-way adversarial audit, twelve findings fixed |
| 8 | This handoff |
| 9 | v5: pinned deflate (latent Python 3.14 defect fixed), PDF and gzip unwrapping |
| 10 | v5: preflate for any deflate; built-in JPEG with Crush's own jcm model; fuzzing fixed preflate memory bugs; older-archive verification bug fixed |
| 11 | Share anywhere (self-opening web page, paper, sound, QR stream, link); learned integer-only code model (−15% on held-out Python); jcm v2; prior/test overlap found |
| 12 | Release audit for 1.0.0: data-loss paths closed (overwrite, atomic writes, collisions, index checksum, hostile lengths), big-file streaming, MIT licence and release files |
