# Crush — measured results

Every number came from a run on this machine. Nothing is estimated. Every
archive was extracted again and compared byte-for-byte.

---

## Headline

**Extended corpus — 195,671,137 bytes, 325 files**

| Method | Size | % of original |
|---|---:|---:|
| ZIP -9 | 44,701,481 | 22.85% |
| tar + Zstandard -19 | 23,046,134 | 11.78% |
| tar + xz -9e | 20,711,308 | 10.58% |
| 7-Zip -mx=9 solid | 20,040,591 | 10.24% |
| Crush v2 (previous best) | 9,713,594 | 4.96% |
| **Crush v3 — Crush tier** | **9,507,791** | **4.86%** |
| **Crush v3 — Singularity** | **9,192,201** | **4.70%** |

> **54.1% smaller than 7-Zip** at maximum setting — 2.18× its compression.
> **79.4% smaller than ZIP.** 325 of 325 files verified byte-exact.

On the original v1 corpus (unchanged, so the earlier numbers stay comparable):
**35,458,084 — 15.1% smaller than 7-Zip**, up from v1's 10.8% and v2's 14.4%.

---

## What changed in v3

Three changes survived measurement. Several did not, and those are below too.

### 1 · A two-layer neural mixer — **−6.78%** on the context-mixing engine

`crushcm` combined its 12 model predictions with a single logistic mixer. It now
uses eight first-layer mixers, each selecting its weights from a *different*
view of the context — previous byte × match strength, partial byte, bit
position, an order-2 hash, a sparse hash, order-3, and **column-within-line** —
feeding a second-layer mixer. Each layer trains on its own local error.

| Test file | before | after | change |
|---|---:|---:|---:|
| English text, 300 KB | 45,669 | 43,893 | −3.89% |
| Python source, 200 KB | 37,263 | 35,502 | −4.73% |
| Windows binary, 100 KB | 28,455 | 27,105 | −4.74% |
| JSON records, 2 MB | 172,337 | 154,646 | **−10.26%** |
| XML, 1 MB | 81,636 | 76,057 | −6.83% |
| **Total** | **415,360** | **387,203** | **−6.78%** |

Held-out Python file with shared-prior priming: 7,725 → 7,276 (−5.81%). Random
data still expands by only 0.39%. Cost: **1.75× slower**.

The single most valuable new view was **column-within-line** — knowing where in
a record you are. It alone moved JSON by −5.7%.

**Clean negative worth stating:** feeding layer 2 linear probabilities instead
of stretched log-odds was **5.7% worse**, wiping out the entire two-layer gain.
The stretch/squash pair is not incidental plumbing; it *is* the network's
nonlinearity, and layer 2 has to live in logit space.

### 2 · Two real bugs in the byte-plane transpose

**The delta was sign-extended instead of zigzagged.** A delta of −1 became
`0xFFFFFFFF`, filling the three high byte-planes with `0xFF` every time a
sequence ticked downward — precisely the planes transposition exists to make
constant.

**8-byte delta mode threw instead of compressing.** `OverflowError: Python int
too large to convert to C long`. The transform-verification gate caught the
exception and silently rejected the transform, so no archive was ever corrupted
— but the mode never once ran in v1 or v2.

| Data | lzma -9e | transpose only | + zigzag delta |
|---|---:|---:|---:|
| Random walk, int32 | 556,604 | 520,132 | **374,804** |
| Timestamps, int64 | 508,896 | 300,452 | **224,444** |
| Sensor readings, uint16 | 682,988 | 648,988 | **474,488** |

Now **30–56% under lzma** on numeric series. All 16 width × delta combinations
round-trip.

### 3 · Column law fitting in Prism — up to **124×**

Prism already split structured text into columns. It now also asks whether a
column *is a law*: an exact integer polynomial, or a cycle. A telemetry export:

| | Size |
|---|---:|
| lzma -9e | 182,696 |
| Prism v2 (columns only) | 34,660 |
| **Prism v3 (columns + laws)** | **1,472** |

**This one took three attempts to get right, and the failures are instructive.**

- **A CSV header row broke every fit.** With `ts` sitting atop a column of
  integers, no column parsed as numeric or cyclic and every law silently failed.
  Holding back *one* leading value rescued the entire file.
- **Selecting by raw size picked the wrong candidate**, costing 11% on a server
  log. A delta-varint column is larger raw than a cyclic one but can beat it
  after entropy coding.
- **Probing with one codec was flatly wrong.** On that log, lzma **-9e** made the
  law payload 11% *worse* while lzma **-6**, zstd-19 and the text-tuned LZMA
  filter all made it better — and the tournament's actual winner was 2.5%
  *better* with laws on. The gate now probes with two dissimilar codecs.
- **The first fully-integrated v3 was WORSE than v2** — 9,826,988 against
  9,713,594. A law-fitted payload is tiny and statistically unlike its
  neighbours; left in the same solid bundle it flipped which codec won for
  *everyone else*, costing 89 KB to save 33 KB. Law-fitted files now get their
  own bundle. That single change was worth **3.3%** of the whole archive.

---

## Program synthesis, expanded — and mostly parked

A research pass extended Oracle with six new generator families. The synthetic
results are spectacular and the honest real-data result is not.

**Shipped:** xorshift32/64, xorshift128, splitmix64 and geometric streams (lzma
*expands* these; the program is ~130 bytes), and residual mode, which turns
Oracle's all-or-nothing rejection into a partial win.

**Parked:** 2D grid fitting fired on **zero** real files. Per-field law discovery
inside detected record structures contributed 0–9% of its own win on real bytes
and twice made the result *worse* than plain transposition — the useful half was
the transposition, which is 20-year-old technology.

**The number that matters:** across 38 inputs, 22 named adversarial near-misses,
240 randomised fuzz mutations, 24 real files and 163 real source files —
**0 wrong programs.** The verification gate is load-bearing, not decorative: it
rejected **612 of 637** proposed laws, and **370 of 370** on the adversarial set.

---

## Where Crush still loses

- **A single file under ~10 KB.** ~150 bytes of container metadata outweighs the
  gain; about 2% behind a raw 7-Zip PPMd stream, though 16.6% ahead of ZIP.
- **Anything at its entropy floor.** MP3, video, 7z/xz/rar archives, encrypted
  data. Crush stores them. Brotli beat Crush's pick on an MP3 by 0.5%. (Gzip,
  zip, PNG, PDF and JPEG are no longer on this list - see v5.)
- **Echo, on ratio.** 0.9% smaller, 2.5× faster. It is a speed feature.
- **Marginal law fits.** The 5% margin gives up ~8% on two files to avoid a 5%
  bundle-level loss on a third.

---

## Correctness

| Suite | Result |
|---|---|
| Extended corpus, Crush tier | 325 / 325 byte-exact |
| Extended corpus, Singularity | 325 / 325 byte-exact |
| Adversarial v2 — 41 hostile files × 5 configurations | **5 / 5** byte-exact |
| Adversarial v1 — 429 files, 16 hostile cases × 4 configurations | **4 / 4** byte-exact |
| Oracle wrong programs | **0** |
| Prism reassembly failures | **0** |
| 2 MB random data | +377 bytes (+0.019%) — header only |

**Bugs the guarantee has now caught, cumulatively:** a `pyppmd` library fault
that would have produced unrecoverable archives; silent deflate-matching
failure on non-stock zlib; a 454-byte index that turned a 15% win into a loss;
perceptual entries failing verification; a sign-extended delta; an
`OverflowError` that disabled a whole transform mode; and a bundling interaction
that made the first v3 slower *and* larger than v2.

Every one was found before an archive left the machine, because transforms must
invert and byte-compare before acceptance and codecs must decode their own
output before they can win.

---

## v4 — the new layers

Three layers were added in v4. They do not change the corpus above, because it
contains no sequencing data and its numeric files are random walks — and that is
reported here rather than hidden:

**Whole corpus regression, `--tier balanced`, 133,497,698 bytes, 27 files**

| Version | Size | % of original | Time | Round-trip |
|---|---:|---:|---:|---|
| Crush v3 | 41,103,780 | 30.79% | 352.8 s | BYTE-EXACT |
| Crush v4 | 41,103,779 | 30.79% | 348.9 s | BYTE-EXACT |

One byte. The v4 layers decline on every file in this corpus, which is what they
are supposed to do when they have nothing to offer. Where they do have something
to offer:

**Helix — sequencing and variant data, `--tier balanced`**

| File | Size | v3 | v4 | Change |
|---|---:|---:|---:|---:|
| reads.fastq | 3,000,000 | 926,403 | 763,387 | −17.60% |
| ref.fasta | 10,166,820 | 2,210,144 | 1,924,510 | −12.92% |
| calls.vcf | 11,726,984 | 1,834,524 | 1,356,886 | −26.04% |
| **total** | **24,893,804** | **4,971,071** | **4,044,783** | **−18.63%** |

Also 2.5× faster than the path it replaces. For scale, on the same FASTQ:
xz -9e 5,711,444 · zstd -19 5,859,226 · brotli -q11 5,704,947.

**Vector — numeric and floating-point arrays, `--tier balanced`**

| File | Size | v3 | v4 | Change |
|---|---:|---:|---:|---:|
| Financial ticks, decimal prices, float64 | 2,000,000 | 173,500 | 137,120 | −20.97% |
| 8-channel sensor stream, float32 | 6,400,000 | 3,511,692 | 3,096,660 | −11.82% |
| Smooth 2-D field, big-endian float32 | 2,520,000 | 1,089,129 | 988,336 | −9.25% |
| data_floats.bin (corpus) | 7,200,000 | 5,182,423 | 5,182,424 | 0.00% |
| data_timeseries.bin (corpus) | 8,000,000 | 719,297 | 719,298 | 0.00% |

NaN payloads, negative zero, denormals and infinities were verified to reproduce
bit-for-bit, including the payload bits inside NaNs.

**Loom — structure discovery, `--tier crush`**

| File | Size | v3 | v4 | Change |
|---|---:|---:|---:|---:|
| Struct-packed record file, big-endian | 3,360,000 | 731,055 | 699,537 | −4.31% |
| Mixed int/float sensor log with padding | 7,200,100 | 3,184,365 | 2,853,456 | −10.39% |
| font.ttf | 84,492 | 35,725 | 35,726 | 0.00% |

Both winning files are constructed. On every real binary in the corpus — SQLite,
a Windows DLL, a Python extension, a TrueType font — Loom declines.

---

## v4 — the shipped-knowledge curve

The measurement that should shape the next release. 20 small Python files
(97,521 bytes, median 4,828) held out from a 1,529-file pool, disjoint from
every prior by content hash *and* filename, each compressed on its own.

| Shipped knowledge | Bytes | Of original | vs xz -9e |
|---|---:|---:|---:|
| none — xz -9e | 30,960 | 31.75% | — |
| none — Crush CM | 25,219 | 25.86% | −18.5% |
| 1 MB prior | 16,757 | 17.18% | **−45.9%** |
| 2 MB prior | 16,011 | 16.42% | −48.3% |

The first megabyte of shipped knowledge is worth two and a half times the entire
step from xz to context mixing. The second megabyte is worth 4.5%. Bigger
generic priors are not where the remaining value is; narrower ones are.

---

## v4 — measured and rejected

**Recurrent (LSTM) mixer.** Correct — round-trip exact, repeat compression
bit-identical, `-O0` and `-O2` builds byte-identical, so there is no
floating-point drift. −1.72% on a 4-file sweep. But 179,435 → 176,344 bytes cost
7.9 s → 80.2 s, roughly 10× the time on the slowest component in the product
(0.13 MB/s → 0.012 MB/s). Not shipped.

**Weave — similarity ordering and cross-file delta.** MinHash similarity
ordering improved adjacent-pair similarity from 11.3 to 14.5 on a 90-file
template document set and moved the compressed size from 160,556 to 160,508
bytes — 0.03%. Explicit cross-file delta against a cluster reference produced
246,796 bytes, over 50% *worse*. A solid stream with a 64 MB dictionary has
already found the redundancy a delta would encode, and delta-coding destroys the
literal repetition the stream was exploiting. Not shipped.

---

## v5 — PDF and gzip, and a deflate that cannot drift

### A latent defect, found and fixed first

The container transforms (`deflate_unwrap` for zip/docx, `png_unwrap`) store
the *decompressed* contents plus the deflate settings that rebuild the original
bytes, and extraction re-runs deflate. They used Python's own `zlib` for that.
Python 3.14 on Windows ships **zlib-ng** instead of zlib, and zlib-ng emits
different bytes for the same settings: **11 of 15** level/strategy pairs
differed on a 216,030-byte input. So an archive written under Python 3.13 would
have failed its checksum on extraction after a Python upgrade.

Reproduced, not inferred. A 13-file archive (docx, odt, xlsx, zip, PNG, PDF,
gzip) was written by the previous engine, then extracted with Python's
compressor swapped for zlib-ng:

| Engine at extraction | Python's deflate | Result |
|---|---|---|
| previous | zlib 1.3 | 13 / 13 byte-exact |
| previous | zlib-ng | **6 files FAILED VERIFICATION** (every docx, odt, xlsx and PNG) |
| this one | zlib-ng | 13 / 13 byte-exact |
| this one, pinned library deleted | zlib-ng | the other 7 restored; the 6 reported with the reason and the fix |

The fix is `engine/crush_zpin.py` plus `engine/crushz.dll`: Crush's own copy of
unmodified zlib 1.3.1, compiled with a symbol prefix so it can never be confused
with any zlib Python has loaded. Any deflate — pinned or Python's — must pass a
known-answer test against reference output before it is trusted.

| Check | Result |
|---|---|
| pinned 1.3.1 vs Python's zlib 1.3, 405 settings × 2 wrappers × 7 inputs | 5,670 compared, **0 differ** |
| Windows `crushz.dll` (run under Wine) vs Linux build, 810 outputs | **identical** |
| zlib-ng against the known-answer test | **rejected** |
| settings index for every existing archive | unchanged — old archives extract as before |

### PDF: the unwrapper that was not running

`pdf` was routed to `deflate_unwrap`, which only finds streams through a zip
central directory. A PDF has none, so every PDF went through untouched. The new
`pdf_unwrap` finds each FlateDecode stream, proves which pinned deflate setting
rebuilds it byte for byte, and swaps in the decompressed bytes. Everything
between streams is carried verbatim.

**How often real producers can be rebuilt** — 437 PDFs found on this machine
(TeX, Ghostscript, cairo, macOS, Skia, Ipe, matplotlib and others) plus 11
generated here:

| Producer | Files | Flate streams | Rebuilt | Of flate bytes |
|---|---:|---:|---:|---:|
| pdfTeX / MiKTeX / LuaTeX / xdvipdfmx | 183 | 1,595 | 1,435 | 99.9% |
| Ghostscript (GPL, GNU, AFPL, Aladdin) | 132 | 1,707 | 1,687 | 99.9% |
| cairo | 85 | 293 | 292 | 100.0% |
| no /Producer entry | 21 | 7,593 | 7,538 | 99.8% |
| LibreOffice, wkhtmltopdf (Qt), pikepdf, qpdf, img2pdf | 7 | 114 | 114 | 100.0% |
| matplotlib | 3 | 23 | 23 | 100.0% |
| mbt, Ipe, macOS Quartz, Distiller, PSNormalizer | 11 | 477 | 477 | 100.0% |
| Skia, older builds | 3 | 5 | 5 | 100.0% |
| **Chrome / Skia, current build** | 2 | 40 | **0** | **0.0%** |
| ReportLab (ASCII85 around Flate) | 1 | 0 | 0 | — |
| **all 448** | **448** | **11,847** | **11,571** | **99.3%** |

Chrome's zlib is Chromium's fork, whose output diverges from reference zlib
within the first six bytes of every stream; older Skia builds on this machine
did rebuild. Microsoft Office and Adobe Acrobat PDFs were not available here and
are **not measured**. *(Round 10: all 40 Chrome streams are now rebuilt through
preflate, and 20 "Microsoft: Print To PDF" files from the user's Downloads were
measured - their streams are mostly zlib-exact; the rest go through preflate.)*

**Compression — the same 448 PDFs, 34,400,586 bytes (424 distinct)**

| Method | Size | % of original | Time | Round-trip |
|---|---:|---:|---:|---|
| ZIP -9 | 27,149,331 | 78.92% | | |
| tar + xz -9e | 22,802,716 | 66.29% | | |
| 7-Zip -mx=9 solid | 22,796,224 | 66.27% | | |
| Crush previous, balanced | 22,798,127 | 66.27% | 50.4 s | BYTE-EXACT |
| **Crush now, balanced** | **10,411,346** | **30.27%** | 128.8 s | BYTE-EXACT |
| Crush previous, crush tier | 22,651,733 | 65.85% | 236.9 s | BYTE-EXACT |
| **Crush now, crush tier** | **10,356,223** | **30.10%** | 403.3 s | BYTE-EXACT |
| Crush previous, singularity | 22,651,777 | 65.85% | 1,302.5 s | BYTE-EXACT |
| **Crush now, singularity** | **10,060,970** | **29.25%** | 2,271.4 s | BYTE-EXACT |

**54.3% smaller than both 7-Zip and the previous Crush** at the default tier, at
2.6× the compression time. Singularity takes it to **55.9% below 7-Zip**, but its
context-mixing winner also has to run at extraction: 451.8 s to extract, against
5.2 s for the balanced archive. For PDFs, balanced is the sensible default.

That set is kind to the change in one way worth stating: many of these PDFs
embed the same fonts, and once the streams are unwrapped the solid bundle finds
that sharing. So each PDF was also compressed **on its own** — every 8th
distinct system PDF by size, plus the 11 generated ones, 63 files, 10,442,537
bytes, `--tier balanced`:

| | Previous | Now | Change |
|---|---:|---:|---:|
| total | 7,277,902 | 3,151,773 | **−56.7%** |
| median file | | | −7.3% |
| LibreOffice exports (2) | 186,300 | 130,616 | −29.9% |
| wkhtmltopdf (2) | 138,299 | 82,507 | −40.3% |
| matplotlib, 6 charts | 70,490 | 41,781 | −40.7% |
| img2pdf scan (Flate image) | 657,071 | 422,005 | −35.8% |
| Chrome (2) | 169,578 | 169,578 | 0.0% |
| worst case | 3 tiny PDFs (1.7–2.3 KB) | | **+2 to +8 bytes** |

The median is small because most system PDFs are tiny font samples whose
streams are a few hundred bytes. The three files that got a few bytes worse are
the recipe costing more than it saved; nothing got larger than the input.

### gzip: works for zlib producers, not for GNU gzip

*Superseded in round 10: GNU gzip's output is now rebuilt through preflate, and
the gzip set went from −11.5% to −56.5%. Kept as measured at the time.*

`gzip_unwrap` handles `.gz`, `.tgz`, `.svgz` and multi-member files; headers and
CRC trailers are carried verbatim. Previously every gzip file was stored as an
existing archive.

| Producer | Rebuilt |
|---|---|
| Python `gzip`, `git archive`, PyPI source tarballs (3) | **all** |
| GNU gzip `-6` / `-9`, and so `tar czf` | **none** |
| a GitHub release tarball (zlib-1.3.1.tar.gz) | no |
| 879 `.gz` files already on this machine | 555 files, 18.9% of their bytes |

GNU gzip's deflate is an older sibling of zlib's and makes different block
decisions, so it never matches. That is the biggest gap left, and it is the same
gap as Chrome's PDFs (see HANDOFF.md). A GNU-gzip miss costs a full recompress
per candidate, so gzip only tries the 20 settings real zlib producers use.

**887 gzip files, 24,416,032 bytes, `--tier balanced`**

| Method | Size | Time | Round-trip |
|---|---:|---:|---|
| ZIP -9 | 22,621,858 | | |
| tar + xz -9e | 17,998,268 | | |
| 7-Zip -mx=9 solid | 17,873,614 | | |
| Crush previous | 18,748,277 | 9.0 s | BYTE-EXACT |
| **Crush now** | **16,599,239** | 78.0 s | BYTE-EXACT |

**−11.5%** against the previous Crush and **−7.1%** against 7-Zip, from
unwrapping only 22.9% of the bytes. The time goes on the misses: 324 GNU gzip
files each fail 20 full recompressions (38.7 s in total).

### Nothing else moved

| Check | Result |
|---|---|
| Whole corpus regression, balanced, 109,955,101 bytes | 26,881,703 → 26,881,704 — **one byte**, BYTE-EXACT |
| Adversarial v1, 4 configurations | **4 / 4** byte-exact |
| Adversarial v2, 41 hostile files × 5 configurations | **5 / 5** byte-exact |
| `tests/test_unwrap.py` (new: KAT, hostile PDF and gzip, bombs, whole archive) | **33 / 33** |
| `tests/test_crypt.py --skip-slow` | 76 pass, 0 fail, 2 known warnings — unchanged |
| `tests/test_localauth.py`, `e2e_app.py`, `e2e_crypt.py` | all pass |

---

## v5, round 10 — any deflate, any JPEG

Two gaps the first v5 round left open: deflate streams that zlib did not write
(GNU gzip, Chrome, Office, 7-Zip...) and JPEG photos, which Crush previously
recompressed only if `cjxl` had been installed by hand.

### Rebuilding deflate that zlib did not write

When no pinned-zlib setting reproduces a stream, Crush now runs **preflate**
(Dirk Steinke, Apache-2.0, vendored in `engine/crushpf-src/`, patched as
described in SECURITY.md). It re-parses the stream with a model of how deflate
encoders decide and stores only where the real stream departed from the model.
It runs in a separate process (`crushpf.exe`) and every record is proven before
use. It applies to zip/docx/xlsx, PDF, PNG and gzip alike.

| Set | Streams rebuilt by zlib | Streams rebuilt by preflate | Correction record, as a share of what it rebuilt |
|---|---:|---:|---:|
| 887 .gz files | 560 | 326 | **1.10%** |
| 448 PDFs | 11,571 | 95 (incl. all 40 current-Chrome streams) | 2.76% |
| 107 zip-family files (jars, docx, pptx, wheels, zips) | 19,753 | 592 | 5.79% |
| 119 PNGs | 74 | 45 | 5.78% |

Before this, 324 of the 887 gzip files could not be unwrapped at all; now 886
can (the last is declined as not worth it). The zip-family budget also rose from
4,000 to 65,536 members per file, because the C matcher made per-member cost
small — five large files (three `.jar`s, two icon-set `.zip`s) that were
previously skipped now unwrap in 1–3 s each.

### JPEG: built in, and a model of Crush's own

`crushjpg.exe` recompresses the JPEG's own coefficients and rebuilds the file
bit for bit. Two codecs share one parser (Brunsli's):

- **Brunsli** (Google, MIT) — fast; used at the balanced tier.
- **jcm** — Crush's own context-mixing coefficient model, written this round;
  used at Crush tier and above. Brunsli stores everything that is not a
  coefficient (the file is Brunsli-encoded with every coefficient zeroed, a few
  hundred bytes); jcm codes the coefficients: each block's 7×7 interior first,
  then its first row and column *predicted in value and sign* from the
  neighbouring block across the shared border, then DC predicted so the block's
  edges continue its neighbours' (Lepton's idea), every binary decision mixed
  from several adaptive contexts and refined by an APM. Integer-only.

Standalone, every file round-tripped and compared. "Real photos" are 59 JPEGs
from the user's Downloads folder (social-media and web images, 10,118,180
bytes); the "test set" is 51 JPEGs from public sample images and encoders run
here (4,927,408 bytes). Only totals are reported for the real photos.

| Codec | Real photos | Test set | Notes |
|---|---:|---:|---|
| JPEG XL, effort 9 (libjxl via pillow-jxl) | 17.91% | 18.67% | 6 test files failed; output not round-trip verified here |
| Lepton (Dropbox) | — | 18.93% | failed on 5 files (CMYK, 4:1:0, 4:1:1) |
| Brunsli | 22.32% | 19.77% | |
| packJPG 2.5 | 24.97% | 20.12% | LGPL; 1 real file failed |
| **jcm** | **30.91%** | **23.50%** | this round |

**Where jcm's bytes go** (real photos, 6.96 MB, measured before the final
context pruning): first rows and columns 3.15 MB, 7×7 interiors 2.62 MB,
interior non-zero counts 0.44 MB, DC 0.75 MB. Moving DC from
LOCO-I prediction to edge-continuity prediction took DC from 1.05 MB to 0.75 MB;
coding the interior first and predicting the edges took the total from 26.8% to
30.4%. Negative results kept: coding every edge coefficient's zero flag instead
of a per-edge count was 0.3 points worse; a second, fast-adapting counter set
gained 0.17 points for 30% more time and was dropped; counter limits 30–1000 and
mixer rates 2–30 were all within 0.3 points of the chosen values.

**Speed**, measured here: Brunsli ~2.3 MB/s including its self-check; jcm about
0.5 MB/s each way. That is why jcm is reserved for the Crush tier and above. The
Windows builds were checked under Wine to produce byte-identical output to the
Linux builds on all 110 test JPEGs, both directions.

**In the archive**, `--tier balanced` unless stated:

| Set | Bytes | 7-Zip -mx=9 | Crush v4 | Crush now | vs v4 |
|---|---:|---:|---:|---:|---:|
| JPEG test set | 4,927,408 | 4,757,108 | 4,740,014 | **3,913,203** | −17.4% |
| JPEG test set, Crush tier | | | 4,715,110 | **3,715,784** | −21.2% |
| Real photos | 10,118,180 | | 9,859,033 | **7,862,748** | −20.2% |
| Real photos, Crush tier | | | 9,782,682 | **6,993,182** | −28.5% |

JPEGs embedded in other files are recompressed too, as a second stage after the
container unwrap (`jpeg_embedded`): photos inside PDFs, and inside .docx/.pptx
once the zip layer is open. On 25 real PDFs from the Downloads folder (20 of them
invoices sharing one logo) this was worth only 0.5%, because the solid bundle
had already removed the repeated image; it costs 12 s on that set.

### Everything this round, in the archive

`--tier balanced`, every archive extracted and compared byte for byte.

| Set | Files | Bytes | 7-Zip -mx=9 | Crush v4 | Crush now | vs v4 |
|---|---:|---:|---:|---:|---:|---:|
| Zip family (jar, whl, docx, pptx, zip) | 107 | 87,518,571 | 59,037,883 | 44,767,957 | **25,965,541** | **−42.0%** |
| Gzip | 887 | 24,416,032 | 17,873,614 | 18,748,277 | **8,163,246** | **−56.5%** |
| PDF (public) | 448 | 34,400,586 | 22,796,224 | 22,798,127 | **10,289,915** | **−54.9%** |
| PDF (user's Downloads) | 25 | 14,287,535 | | 4,072,689 | **2,942,624** | −27.7% |
| PNG | 119 | 8,377,272 | 7,309,064 | 6,456,777 | **6,357,915** | −1.5% |
| JPEG test set | 51 | 4,927,408 | 4,757,108 | 4,740,014 | **3,913,203** | −17.4% |
| Whole-corpus regression | 17 | 109,955,101 | | 26,881,703 | **26,761,605** | −0.45% |

Gzip went from −11.5% (first v5 round) to −56.5% entirely because GNU gzip's
output can now be rebuilt. The PNG set also compressed **3× faster** (437 s →
147 s): the old code searched zlib settings in Python; the pinned C matcher
abandons each wrong guess at its first differing byte.

Costs, stated: the zip set took 407 s against 348 s, gzip 241 s against 11 s, PDF
147 s against 55 s; extraction is slower wherever a stream is rebuilt (gzip 0.4 s
→ 21 s). The whole-corpus regression moved by −0.45%, from its two JPEGs and its
PNGs.

### A second latent defect: older archives would not extract

While checking compatibility against three real archives in the user's
Downloads folder, one — a 92 MB video archive made in early September — **failed
verification with both the previous engine and this one**, and a file that
fails verification is not written out. The data was intact: its index stored
the full 64-hex SHA-256, from before the index was trimmed to 32 hex digits,
and the check compared a 32-character prefix against the 64-character value.
Every file in every archive written before that change would have failed the
same way. The check now compares at the stored length (never fewer than 32
digits); the archive extracts and its SHA-256 matches in full.
`tests/test_unwrap.py` now builds an archive in the old style and checks it.

### Correctness this round

| Check | Result |
|---|---|
| `tests/test_unwrap.py` | **54 / 54** |
| Adversarial v1 (4 configurations) and v2 (41 hostile files × 5 configurations) | **4 / 4** and **5 / 5** byte-exact |
| `test_crypt.py --skip-slow`, `test_localauth.py`, `e2e_app.py`, `e2e_crypt.py` | 76 pass / 0 fail / 2 known warnings; all pass; all pass; all pass |
| Archives written by the previous engine, extracted by this one (incl. under zlib-ng) | byte-exact |
| The user's three existing archives | all extract; all verify |
| Windows helpers vs Linux helpers under Wine (crushz, crushpf, crushjpg) | byte-identical, both directions |
| Fuzzing (ASan + UBSan), after fixes — see SECURITY.md | preflate 10,000 + 2,000 + 1,500 cases, JPEG 7,500 cases: **0 findings** |

---

## Round 11 — sharing anywhere, a learned code model, jcm v2

2 shared CPU cores; timings indicative. Every archive extracted and compared.

### The learned code model (`crushnn_m`)

20 third-party Python files (112,119 bytes) from installed packages, none of
them in the training set (checked by content hash and file name) and none in
`code.prime`. One archive, `--tier singularity`:

| | Archive | vs before |
|---|---:|---:|
| Singularity without the model | 19,612 | — |
| **Singularity with the model** | **16,644** | **−15.1%** |

Compress 62 s → 98 s; extract 18.6 s. The specialist's per-file numbers
(each file alone, codec stream only): xz -9e 30,908; crushcm 25,400; crushcm +
`code.prime` 19,716; **crushnn 16,083** (−18.4%, −48.0% vs xz). Larger files:
pandas `series.py` −12.0%, matplotlib `_axes.py` −11.9%, Go `server.go`
−5.4% (no Go in training), 1 MB English −2.1%, all against crushcm +
the matching shipped prior. Integer quantisation costs 0.6% against the float
model. Speed ~6 KB/s each way at -O3.

**Prior/test overlap found.** `code.prime` contains 5 of the 20 in-domain
stdlib test files verbatim; `text.prime` is built from the same Debian copyright
text as `corpus/text_english.txt` and compresses it to 1.7%. Prior-assisted
figures on those files in earlier sections are flattered by it.

### jcm v2

| Set | v1 | v2 |
|---|---:|---:|
| Public JPEG set (51 files), saving | 23.50% | **23.97%** |
| Lead's re-check: the 30 images jcm accepts alone | 23.82% | **24.29%** |
| Lepton's camera photos (8–22 MP) | 25.55% | **25.59%** |
| One thread, encode / decode | ~0.5 MB/s | ~1.0 / 0.96 MB/s |
| Batch on 2 threads | 0.57 / 0.50 MB/s | 1.74 / 1.54 MB/s |

Instructions on a 5-file subset: 7.89 G → 4.31 G. Ideas measured and dropped:
an APM-selected mixer (−0.09 pt), a second APM (−0.03), cheap interior-sign
coding (−0.02 to −0.15; interior signs cost 0.99 bit each), smaller stripes for
more threads (−0.09 to −0.31 on large photos).

### Share pages and portability

| | Bytes |
|---|---:|
| Whole corpus (18 files), `--tier crush` | 109,946,909 → 24,922,203 |
| the same with `--portable` | 24,922,190 (no cost) |
| its share page (`--share`) | 25,687,168 (+3.1%: decoders + escaping) |

The page opened in headless Chromium, rebuilt and verified all 18 files in
13.1 s, saved them as a .zip byte-exact, with zero network requests. Measured
by the specialist: Singularity on 300 .py files costs +5.3% when portable (no
shared priors); fixed page cost 76–150 KB; 500 MB archive opens and checks in
23 s using 3.3 GB of browser memory.

### Channels (simulated hardware, real decoders and browser)

| Channel | Measured |
|---|---|
| QR stream, V20-L, 10 codes/s | 124,585 B in 21.9 s (5.7 KB/s) with 30% of codes garbled; 100/100 completions at 10, 30 and 50% loss |
| Paper, A4, 12 codes per page | 3.8 KB/page (any 25 of 48) to 5.9 KB/page (any 200 of 252); 12/12 codes per page from scans and 12 MP photos |
| Sound, "echo" mode | 132 B/s; every frame decoded at 3 dB SNR in a 0.6 s reverberant room, resampled, with clock drift |
| Link | 1.5 MB archive in a 2,000,094-character URL opened in Chromium |

### Correctness this round

| Check | Result |
|---|---|
| `tests/test_unwrap.py` (jcm v2 helpers) | 54 / 54 |
| `tests/test_nn.py` (new) | 11 / 11 |
| `test_crypt.py`, `test_localauth.py`, `e2e_crypt.py` | 77 pass / 0 fail / 3 known warnings; all pass; all pass |
| `tests/e2e_app.py` (now incl. web page, paper and sound, each restored byte-exact) | all pass |
| Edge tests v1 / v2 | 4 / 4 and 5 / 5 configurations byte-exact |
| `channels/run_tests.sh` | all pass |
| share: transforms / codecs / crypto vectors through JS+WASM | 150/150; all codec cases; all KATs |
| crushjpg v2 Linux vs Wine; v1 records decoded by v2 (Linux, Wine) | identical; 30/30 both |
| crushjpg.wasm on v2, v1 and Brunsli records | 42 / 42 |
| crushnn known-answer test under Wine; WASM rebuild reproducible | identical; all 8 module hashes identical |
| Fuzzing (ASan + UBSan) | jcm v2 13,000 + v1 2,000; crushnn 250: 0 findings |

---

## 1.0.0 release audit (round 12)

### The owner's own test folder

26 files, 429,198,811 bytes: videos (mkv, mp4, webm), FLAC, animated GIFs,
JPEGs, AES-encrypted zip archives and a text file. Each run compressed the
folder, extracted it into a fresh folder and compared every SHA-256.

| Tier | Archive | % of original | Compress | Extract | Byte-exact |
|---|---:|---:|---:|---:|---|
| quick | 423,225,374 | 98.61% | 75 s | 3 s | 26 / 26 |
| balanced | 421,439,783 | 98.19% | 280 s | 4 s | 26 / 26 |
| balanced, encrypted | 421,465,639 | 98.20% | 343 s | 10 s | 26 / 26 |
| crush (before the media probe) | 420,096,576 | 97.88% | 2,624 s | 11 s | 26 / 26 |
| **crush (1.0.0)** | **420,480,078** | **97.97%** | **1,554 s** | 12 s | 26 / 26 |
| singularity | 421,724,678 | 98.26% | 600 s | 53 s | 26 / 26 |

This is what already-compressed data looks like: the JPEGs shrink (jcm),
everything else is at or near its entropy, and 2% is the honest result. The
encrypted zips are declined by the zip unwrapper as they should be (their
content is ciphertext). The 1.0.0 media probe skips zstd-22/LZMA/Brotli on
video and audio bundles whose samples do not compress at all. At the Crush
tier it cut 18 minutes for 0.09% of size. Animated GIFs still get the full
field, because LZMA finds 0.3–0.6% in their LZW data. The singularity run used
an earlier version of the probe that included GIFs; it is shown as measured.

### Large files

| Case | Result |
|---|---|
| 1.6 GB (two 629 MB random files, one a copy, plus 347 MB of text), quick | 66 MB peak RAM; byte-exact; dedup stored the copy once |
| the same, balanced, encrypted | 134 MB peak RAM; byte-exact |
| the same archive read by the previous engine | byte-exact |
| 4 GiB + 12,345 B video plus a 300 MB disk image (index past 4 GiB) | byte-exact, compress 67 s, extract 26 s |
| a sparse 1 GB image, streamed | peak 54 MB |

Before 1.0.0 every input byte was held in RAM until the codec stage, and Echo
needed 24 bytes of RAM per byte. A 200 MB bundle was an out-of-memory kill on
8 GB.

### Hostile archives, through the whole extract path

| | Before fixes | After |
|---|---:|---:|
| random mutations (truncation, flips, swaps, garbage) | 9,612 cases: 44 silent (index), 0 wrong data | 10,212 cases: **0 silent** |
| codec-header sweep | 3,001 cases: 1 crash (pyppmd) | 0 |
| "valid but lying" index edits | 328 cases: 72 wrong/renamed outputs | refused (index checksum) |
| encrypted archives | 1,480 cases: 0 | 0 |
| CLI on mutants | 77 / 113 raw tracebacks | 0 in the re-run sample (one line + exit code) |
| SIGKILL during compress / extract | broken archive or half file under the real name | the previous file is left untouched |

### Compatibility

41 archives written by the two earlier engines (every codec forced, four
tiers, Echo, encrypted) extract byte-exact with 1.0.0. Both earlier engines
extract 1.0.0 archives, including streamed big files and checksummed indexes.

---

## Reproducing

```
python build_corpus.py     python build_corpus2.py
python entropy_wall.py     python baseline.py       python make_report.py
python edge_tests.py       python edge_tests2.py
```

2 CPU cores, 7 GB RAM, Python 3.11, zlib 1.3, 7-Zip 16.02, xz 5.4,
Zstandard 1.5.5, Brotli 1.1.0, libjxl, FLAC, ffmpeg.
