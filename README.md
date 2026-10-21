# Crush

Content-aware file compression. Files go in, a `.crush` archive comes out, and
every byte is provably recoverable.

**What it does.** Crush identifies what each file really is, applies a
reversible transform that exposes its structure (unpacking the compression
inside PDFs, Office files, PNGs and zips; re-coding JPEGs; finding the program
behind generated data), proves the transform is exact, then lets every
suitable codec compete and keeps the smallest. Optional encryption
(Argon2id + XChaCha20-Poly1305), and archives can travel as a self-opening web
page, a paper backup or sound. Every file is checked against its SHA-256 when it
comes back out.

**Requirements.** Windows 10 or 11 (x64) with Python 3.11 or newer
([python.org](https://www.python.org/downloads/) — tick *"Add python.exe to
PATH"*). The engine also runs on Linux and macOS from the command line once
the native helpers are built (`sh engine/crushz/build.sh`, and the `build.sh`
in `engine/crushpf-src`, `engine/crushjpg-src` and `engine/crushnn-src`;
`gcc -O3 -o engine/crushcm engine/crushcm.c`).

**Licence.** MIT for Crush itself; bundled components keep their own licences
(`THIRD_PARTY_NOTICES.md`).

**Status.** Every number in this repository was measured; see `BENCHMARKS.md`
for the results and `HANDOFF.md` for what was tried and rejected. Contributors
using Claude: `CLAUDE.md` carries the architecture, the rules and how to run
every test.

---

## Run it

**Double-click `Crush.bat`.**

It checks everything it needs *before* opening a window, installs the five
Python packages on first run, and starts the app. If Python isn't on the PC it
will tell you where to get it (tick *"Add python.exe to PATH"* on the
installer's first screen).

If anything goes wrong you get a message you can read, and every run writes
`crush-debug.log` next to this file.

### One path from the interface to the engine

Crush runs a small server on `127.0.0.1` and the interface talks to it — in the
native window and in the browser alike. The native window is a frame around the
same page your browser would get.

That is a deliberate change. The earlier build used pywebview's JavaScript
bridge for the native window, which is injected *after* the page starts running.
For a moment `window.pywebview.api` exists but has no methods on it, and a call
made in that moment failed with **`api(...).ping is not a function`** — in a
window that then never recovered. Defining the interface's connection inside the
page itself makes that impossible rather than unlikely. The server is bound to
the loopback address only and every request carries a token generated fresh for
that run.

### If the window still opens but never comes alive

Use **`Crush (Browser Mode).bat`**. Same app, same engine, drawn in your normal
web browser.

The native window frame is drawn by **WebView2**, reached through a chain of
Windows-specific packages (`pywebview` → `pythonnet` → `clr-loader`). On a very
new Python — 3.13 and up — those sometimes install without fully working. Crush
checks for that before opening a window and switches by itself, and falls back
anyway if the window has not loaded in 25 seconds; the launcher is there if you
want to force it.

**`Crush (Diagnose).bat`** runs the engine with no interface at all and prints
what it finds. If that says `roundtrip BYTE-EXACT`, the engine is healthy and
anything still wrong is only the window.

### A standalone Crush.exe

Double-click `Build-Crush-exe.bat`. It produces `dist\Crush.exe`, which needs no
Python. Copy the `priors` and `tools` folders next to it before running. (The
script was updated for this release but has not been rebuilt and tested on
Windows yet; `Crush.bat` is the supported way to run Crush.)

> If `dist\Crush.exe` already exists from an earlier build, it is **out of date**
> — it predates these fixes and will still hang. Delete it and rebuild, or just
> use `Crush.bat`.

---

## Putting files in

Drop files or a folder onto the window and they go straight into the queue. A
drop never opens a file dialog — the dialog is what the **Choose files** and
**Choose folder** buttons are for.

Getting there takes two routes, because a web view is not allowed to tell a page
where a dropped file lives on disk:

- **Native window.** Windows hands the real paths to Crush directly. Nothing is
  copied; the files are read where they already sit, and a dropped folder comes
  through whole.
- **Browser, or an older pywebview.** The page reads the drop itself — walking
  into subfolders so a dropped folder keeps its shape — and hands the contents
  to Crush through the local server. It shows progress while it does.

The first route is tried first, and the second starts only if it stays silent,
so nothing is ever ingested twice.

## Locking an archive

Encryption is off by default. Turn it on in the **Encryption** panel, type a
passphrase twice, and the whole archive — contents, file names, sizes,
timestamps, folder structure, even how many files there are — is encrypted. A
locked archive cannot be listed without the passphrase, only unlocked.

**Argon2id** turns the passphrase into a key, deliberately slowly and using a
lot of memory, so that guessing is expensive for someone who has your archive
and unlimited time. **XChaCha20-Poly1305** does the encryption, in 256 KiB
chunks under the STREAM construction — the same arrangement `age` uses — which
is what makes truncating, reordering, duplicating or splicing chunks fail rather
than silently succeed.

| Key stretching | Time per attempt | Memory per attempt |
|---|---|---|
| Fast | ~0.3 s | 64 MB |
| **Balanced** (default) | ~1 s | 256 MB |
| Paranoid | ~3 s | 1 GB |

The **Suggest** button generates eight words from a 512-word list. That is
exactly 9 bits per word and exactly 72 bits in total — a number you can check
rather than trust, and out of reach of any offline attack that exists once
Argon2id is charging an attacker 256 MB per guess.

> **There is no recovery.** Not by us, not by you. A forgotten passphrase is a
> deleted archive. That is the property you are buying; write the passphrase
> down somewhere safe before you rely on it.

**What is hidden:** every file name, size, timestamp and byte, the folder
structure, and the number of files.

**What is not:** that the file is a Crush archive, the format version, the key
stretching cost, and the archive's total size. Because Crush compresses before
it encrypts — the only order that works, since ciphertext does not compress —
the length of the output reveals roughly how well your data compressed. This is
the same effect behind CRIME and BREACH, it is inherent to compressing before
encrypting, and no archive format avoids it.

From the command line, `--ask` prompts twice rather than putting the passphrase
in the process list where other users can read it:

```
python engine\crush_engine.py c archive.crush <files> --ask --kdf balanced
python engine\crush_engine.py x archive.crush dest          (prompts)
set CRUSH_PASSWORD=...                                      (or use the environment)
```

See **SECURITY.md** for the full design, the threat model, and the results of
the audit it was put through.

## Taking files out

Open a `.crush` archive and you get its full contents listed, with a filter box.

- **Extract everything** restores the whole archive.
- Tick individual files and **Extract selected** restores just those, into the
  folder you choose. Only the bundles those files live in are decoded, so
  pulling one file out of a large archive doesn't cost a full restore.

Deduplication is handled properly. If a file you picked is stored as a duplicate
of a file you didn't pick, Crush decodes the one that holds the bytes and writes
your file from them in memory — you get what you asked for and nothing else.

### How Crush keeps your data safe

- **Nothing is replaced without asking.** If any file Crush is about to restore
  already exists in the folder you chose, nothing is written and you are asked
  whether to replace them or pick another folder (`--overwrite` on the command
  line).
- **Every restored file is checked against the SHA-256 recorded when it was
  packed**, before it is given its name. A file that fails the check is never
  written; it is listed with the reason, and every other file is still
  restored. In archives made by this version the table of contents carries a
  checksum too, so a damaged archive is refused rather than read wrongly.
- **Nothing is ever half-written under a real name.** Archives and restored
  files are written under a temporary name and renamed only when complete. If
  Crush is closed or crashes, or the disk fills, mid-way, an existing archive
  or file of the same name is left exactly as it was. Archives, and restored
  files over 1 MB, are also flushed to disk before the rename, so they survive
  a power cut as well.
- **Compressing never touches your files**, and refuses to write over anything
  that is not a Crush archive (so `crush c notes.txt todo.txt`, with the output
  name forgotten, fails instead of destroying notes.txt).
- **Problems are refused, not skipped**: a folder that cannot be read, a path
  that does not exist, or two inputs that would be stored under the same name
  stop the run with a message, before anything is written.
- **Any file size.** Files over 256 MB are streamed in chunks, so a 3 GB video
  needs a few megabytes of memory; large folders spill to a temporary file
  instead of filling RAM.

### What an archive keeps

| Kept | Not kept |
|---|---|
| every file's exact bytes and its path inside the folder | empty folders |
| modification time | creation time, folder times |
| Unix permission bits (restored on Linux and macOS) | Windows attributes (read-only, hidden), owners and ACLs |
| | symbolic links and junctions (skipped and counted, not followed) |
| | NTFS alternate data streams |

On Windows, an archive made elsewhere can hold names Windows cannot write
faithfully (`CON`, `aux.c`, names ending in a dot or space, or containing
`: * ? " < > |`). Those files are reported as not restored, with the reason,
rather than written under a changed name.

From the command line:

```
python engine\crush_engine.py l archive.crush
python engine\crush_engine.py x archive.crush dest --only "src/notes.txt" "src/img/logo.png"
python engine\crush_engine.py x archive.crush dest --overwrite      (replace existing files)
```

Exit codes: `0` everything restored and verified; `1` the archive could not be
read (a clear message says why); `2` some files were not restored (each is
listed); `3` files already exist and `--overwrite` was not given (nothing was
written).

---

## The layers

Crush v1 was a router: identify each file, apply a reversible transform that
exposes its real content, run a tournament of codecs. That is the section below.
v2 added five layers that attack compression from angles the mainstream tools do
not have at all; v4 adds three more.

**Oracle — program synthesis.** Instead of asking what earlier part of the file
this part resembles, it asks what *program* would produce these bytes. Counters,
timestamps, linear recurrences, congruential (LCG) streams, repeated structure.
When it finds one, two million bytes of statistically-random LCG output — which
xz makes *bigger* — become 68 bytes. Every candidate is regenerated and
byte-compared before it is accepted, so it never guesses.

**Prism — structural shredding.** The redundancy in JSON, CSV and logs runs down
the columns, not along the rows. Prism takes such files apart along their real
structural axis so every field compresses against itself — and then asks whether
a column *is* a law: an exact integer polynomial, or a cycle. A telemetry export
that xz reduces to 182,696 bytes comes out at **1,472** — 124× smaller. A
structured server log came out 90.7% smaller than xz managed.

**Atlas — shared priors.** Trained dictionaries and pretrained context-model
corpora ship with Crush. Encoder and decoder both read them, so what they
contain never travels in the archive. On a small source file this lands 25.9%
below every competing tool. It is the same trade a neural compressor makes with
its weights, at a thousandth of the cost.

**Echo — content-defined chunking.** Whole-archive deduplication past any
codec's window. In practice a speed feature more than a size one: 2.5× faster
on a large revision set, for 0.9% smaller output. It engages above 24 MB.

**Helix — sequencing and variant data.** A FASTQ file is three files wearing one
coat: templated read identifiers, a four-letter base alphabet, and quality
strings whose statistics have nothing in common with either. Helix splits it
into its real streams and models each properly, and does the same for FASTA, SAM
and VCF. **18.6% smaller than Crush v3** across a FASTQ/FASTA/VCF set, and 2.5×
faster, because three coherent streams are easier work than one incoherent one.

**Vector — numeric and floating-point arrays.** Sign, exponent and mantissa have
wildly different statistics, and consecutive values are usually close. Vector
finds an array's real width, endianness and interleaving without being told,
then picks per channel between XOR-against-previous with zero-run coding, delta,
byte planes, and — the one that pays most on exported data — recovering the exact
decimal a float was printed from and coding the integer instead. Up to **21% off
financial tick data** and 12% off multi-channel sensor float32. NaN payloads,
negative zero and denormals all come back bit-for-bit.

**Loom — structure discovery.** For a binary format nobody wrote a parser for,
Loom infers the record stride from the data, types each column, regroups columns
that are byte planes of one wider value, and codes each field on its own terms.
4–10% on struct-packed record files. Crush tier and above, on files over 256 KB.

**Perceptual — opt-in, measured.** Off by default. Photos and audio re-coded
with PSNR and SSIM measured per file against the original, and anything that
misses the quality floor kept losslessly instead. On a media set this reached
**71.3% smaller than 7-Zip**.

**Unwrapping PDF, gzip and zip — v5.** Most of a PDF, a .docx or a .tar.gz is
deflate-compressed, and deflate is weak, so the real contents are hidden from
every stronger codec. Crush decompresses each stream, stores the decompressed
contents, and keeps just enough to rebuild the original compressed bytes
exactly: the zlib settings when zlib wrote it, or — for GNU gzip, Chrome,
Microsoft Office, 7-Zip and the rest — a small correction record from
*preflate*, typically 1–6% of the stream. On 448 real PDFs that is **55% smaller
than 7-Zip**; on 887 gzip files, 54% smaller; on 107 jars, docx and zips, 56%
smaller.

**JPEG, built in.** A JPEG's picture is Huffman-coded DCT coefficients, and
Huffman coding is weak. Crush re-codes the same coefficients and rebuilds the
original file bit for bit. The balanced tier uses Brunsli; the Crush tier and
above use **jcm**, Crush's own context-mixing model, which predicts each block's
edges — value and sign — from its neighbours and its DC from edge continuity.
On 59 real photos from a Downloads folder: **30.9% smaller**, against 22.3% for
Brunsli, 25.0% for packJPG and 17.9% for JPEG XL. It is about 0.5 MB/s, which
is why it waits for the Crush tier. Photos inside PDFs and Office files are
recompressed the same way.

**A deflate that cannot drift.** Rebuilding those original bytes — and the ones
inside every .docx, .xlsx and PNG — means running deflate again at extraction,
and it has to be the identical program. Python 3.14 on Windows switched to a
different zlib that produces different bytes, which would have left archives
unable to verify after a Python upgrade. Crush now carries its own copy of zlib
1.3.1 (`engine/crushz.dll`) and checks it against reference output every time it
starts. If it is ever missing, Crush still restores every other file and says
exactly what is needed for the rest.

See BENCHMARKS.md for every figure and how it was measured.

---

## What makes it different

Zip, 7-Zip and RAR treat every file as an anonymous stream of bytes. That is the
whole opportunity: a `.docx` is a Zip container full of separately-deflated XML,
a JPEG is arithmetic-coded DCT coefficients, a `.wav` is a correlated waveform,
a `.exe` is machine code full of absolute jump addresses. A byte-oriented
compressor sees none of that structure and leaves most of it on the table.

Crush works in five steps.

**1 · Identify.** Every file is sniffed by structure, not just extension.

**2 · Transform.** A reversible transform exposes the real content:

| File type | What Crush does |
|---|---|
| Zip, DOCX, XLSX, PPTX, JAR, APK, EPUB, WHL | Unwraps the container, re-compresses the contents that were hiding inside it, and recompresses any JPEGs in it |
| PNG | Unwraps the deflate stream back to raw filtered scanlines |
| PDF | Unwraps every compressed stream — page text, fonts, images — and recompresses the JPEG photos inside |
| .gz, .tgz, .svgz | Unwraps the gzip layer, whoever wrote it (GNU gzip, Python, git...), and re-compresses the contents |
| JPEG | Re-codes the photo's own coefficients with a far stronger model; the original file comes back bit for bit. Built in |
| WAV / PCM audio | Inter-channel decorrelation plus a per-block polynomial predictor |
| EXE, DLL, ELF | x86 branch-address filter, turning scattered jump targets into repeats |
| Float / integer arrays | Byte-plane transposition, optionally after delta coding |
| FASTQ, FASTA, SAM, VCF | Split into identifiers, bases and quality strings, each modelled separately |
| Float and integer arrays | Split into real channels, then XOR / delta / planes / exact-decimal per channel |
| MP3, MP4, JPEG-in-archive, other archives | Recognised as already compressed and **stored**, never uselessly re-processed |

**3 · Prove it.** Every transform is inverted and byte-compared against the
original *before anything is written*. A transform that does not reproduce the
input exactly is discarded and the file falls back to plain compression. Crush
cannot ship a transform it has not already proven correct on that exact file.

**4 · Bundle.** Files of the same kind are compressed together in one solid
stream, so redundancy *between* files is found too. Byte-identical files are
stored once.

**5 · Compete.** Each bundle is compressed by every codec that suits it — LZMA2
with the appropriate filter, Brotli, Zstandard, PPMd, and the Crush
context-mixing engine — and the smallest output wins. Crush never writes a
result larger than storing the data plainly.

---

## The four effort tiers

| Tier | What it does |
|---|---|
| **Quick** | Zstandard only. Seconds, not minutes. |
| **Balanced** | Default. Zstandard-19 and LZMA2 with filters compete. |
| **Crush** | The full tournament — adds Brotli-11, PPMd, and every filter variant. |
| **Obsession** | Adds `crushcm`, the bundled context-mixing engine. |
| **Singularity** | Everything, including shared-prior priming. Slowest, smallest. |

`crushcm` predicts the next *bit* from an ensemble of twelve models — context
orders 0/1/2/3/4/6/8/12, a word model, two sparse models, and a match model.
As of v3 they are combined by a **two-layer neural mixer**: eight first-layer
mixers, each selecting weights from a different view of the context (previous
byte, partial byte, bit position, an order-2 hash, a sparse hash, order-3, and
column-within-line), feeding a second-layer mixer, then four SSE stages. That
change alone was worth **−6.8%**, and −10.3% on JSON. It is included, pre-built,
as `engine\crushcm.exe`.

---

## Crush Harder

Zip-family files have a second, larger prize behind a small catch.

Unwrapping a container byte-exactly requires reproducing the exact deflate
output of whichever tool built it — which only works when that tool used a
stock zlib. When it didn't, Crush can still get the win by **rebuilding** the
container: every file inside is extracted, stored, and re-packed on restore.

Turn on **Crush Harder** and Crush will do that where the exact path fails. It
typically saves another 30–50% on Office documents and Zip archives.

- Every file *inside* the container comes back **bit-for-bit identical**, and
  each one is verified against its SHA-256 on extraction.
- The container's own packing bytes may differ from the original's.

That is why it is off by default, labelled *content-exact* rather than
*byte-exact* in the results table, and marked per-file when you open an archive.

---

## Optional accelerators

Crush needs nothing installed — JPEG recompression is now built in. Two
optional tools can be dropped into the `tools\` folder — see `tools\README.txt`:

- **`cjxl.exe` + `djxl.exe`** — JPEG XL's lossless JPEG re-coding then competes
  with Crush's own and the smaller wins (Crush's has been smaller on every set
  measured), and the opt-in perceptual photo tier can run.
- **`flac.exe`** — competes against Crush's own audio transform; the smaller
  one wins. Crush usually wins, so this is genuinely optional.

The **Engine** tab shows what is active on your machine.

---

## Sending an archive anywhere

After compressing, the **Send it anywhere** panel turns the archive into
something that travels without Crush at the other end. Each one carries the
archive unchanged and checks its SHA-256 before giving anything back.

- **Web page (.html).** One file that opens itself in any modern browser —
  offline, nothing to install, nothing uploaded. It lists the files, checks
  every one, and saves them singly or as a .zip. A locked archive asks for its
  passphrase in the page; the cryptography is the same as the app's. To make
  this possible Crush re-packs with only the layers a browser can decode
  (usually costing nothing; up to ~5% at Singularity on source code).
- **Paper backup (.pdf).** Pages of QR codes to print and file away. Restore by
  photographing or scanning the pages into `channels/dist/receive.html` (any
  browser with a camera) or on the command line. Codes can be read in any
  order, and a lost page can be missing: any K of the N codes restore it. Good
  for small archives — about 4–6 KB per page.
- **Sound (.wav).** Play it from one device into another's microphone, opened
  in `channels/dist/audio.html`. About 130 bytes a second, so for keys, notes
  and small files.

Also on the command line (`channels/README.txt`): an animated **QR video or
endless QR stream** you can point a phone at (fountain-coded, so it can join
late and miss frames; ~6 KB/s), and a **link** with the archive inside the
URL itself (fine up to a few KB for messengers).

These were tested in Chromium with simulated cameras, printers and rooms, not
yet with real ones, and not yet in Safari or Firefox.

---

## Command line

```
python engine\crush_engine.py c archive.crush <files or folders> --tier singularity [--harder]
python engine\crush_engine.py c archive.crush <folder> --tier crush --photo 0.75 --audio 128k
python engine\crush_engine.py x archive.crush <destination folder>
python engine\crush_engine.py x archive.crush <destination folder> --only <stored paths>
python engine\crush_engine.py l archive.crush
python engine\crush_engine.py c archive.crush <files> --share        (also writes archive.html)
python share\tools\make_share.py archive.crush                      (page from an existing archive)
cd channels\py & python -m crushchan.paper make ..\..\archive.crush backup.pdf
cd channels\py & python -m crushchan.audio encode ..\..\archive.crush archive.wav
```

---

## What Crush will not claim

No compressor can shrink every input. There are 2ⁿ files of n bits and only
2ⁿ−1 shorter ones, so any reversible scheme that shrinks some files must
enlarge others — this is arithmetic, not an engineering limit. It also means
recursive compression is impossible: re-compressing your own output cannot keep
shrinking it, or 2ⁿ files would map into 256.

Crush therefore never inflates a file — it stores it instead — and never
reports a saving it has not measured. Random data comes out the same size it
went in, plus a header. That is the correct answer.

Oracle is not an exception to this. It does not compress random data; it
recognises data that was never random to begin with, because a short program
made it. An LCG stream looks like noise to every statistical test a compressor
applies, but its Kolmogorov complexity is 68 bytes, and that is what Crush
stores. The counting argument is untouched: for every file Oracle shrinks by a
factor of thirty thousand, there are vastly more files it cannot help at all,
and it says so rather than guessing.

The perceptual tier is a different kind of exception. It is not bounded by the
counting argument because it does not preserve the input — and that is exactly
why it is off by default, labelled per file, and made to show its measurements.

---

## Files

```
Crush.bat              run it
Build-Crush-exe.bat    build a standalone Crush.exe
app.py                 desktop host (loopback server + native window)
ui\index.html          the interface
engine\crush_engine.py the engine: detection, transforms, container, tournament
engine\crushcm.c       the context-mixing compressor, source
engine\crushcm.exe     the same, pre-built for Windows x64
engine\crush_oracle.py program synthesis
engine\crush_prism.py  structural decomposition
engine\crush_echo.py   content-defined chunking
engine\crush_perceptual.py  the opt-in perceptual tier
engine\crush_zpin.py   the pinned deflate: loads crushz and checks it
engine\crushz.dll      zlib 1.3.1, pre-built for Windows x64 (source in engine\crushz\)
engine\crushpf.exe     preflate: rebuilds deflate that zlib did not write (source in engine\crushpf-src\)
engine\crushjpg.exe    JPEG recompression: Brunsli + jcm (source in engine\crushjpg-src\)
engine\crush_jpeg.py   drives crushjpg
engine\crushnn.exe     the learned code model's coder (source in engine\crushnn-src\)
priors\                shared dictionaries and prime corpora (Atlas); code.cnn = the learned code model
share\                 self-opening web pages: reader, WebAssembly decoders, make_share.py
channels\              paper backup, sound, QR stream and link: encoders and receiver pages
tools\                 drop optional accelerators here
```
