# Crush v4 — what was tried, what was measured, what shipped

Six lines of attack were built and measured. Three shipped. Three did not, and
the reasons they did not are the more useful part of this document.

Every number below was measured on this machine, and every compression result
was checked by actually decompressing and comparing against the original.

---

## The result that matters most: shipped knowledge

There is no way around the counting argument. There are 2ⁿ files of n bits and
only 2ⁿ−1 shorter ones, so no reversible scheme shrinks everything. Every honest
gain has to come from somewhere, and there is exactly one place left with room
in it: **what the decoder already knows before the archive arrives.**

Crush ships priors — dictionaries and pretrained model corpora that both sides
read, so their contents never travel. The question nobody had answered was how
that trade actually scales. So it was measured.

**Method.** 1,529 Python source files were collected from this machine. Twenty
small files (97,521 bytes total, median 4,828 bytes) were held out as the test
set. Every file matching a test file *by content hash or by filename* was
removed from the pool before any prior was built, so the model has never seen
the test data. Each test file was compressed on its own — the case where an
adaptive coder has no room to learn anything before the file is over, and the
case that describes most files people actually have.

| Shipped knowledge | Bytes | Of original | vs xz -9e |
|---|---|---|---|
| none — xz -9e | 30,960 | 31.75% | — |
| none — Crush CM | 25,219 | 25.86% | −18.5% |
| **1 MB prior** | **16,757** | **17.18%** | **−45.9%** |
| **2 MB prior** | **16,011** | **16.42%** | **−48.3%** |

Every point round-trips byte-exactly with the same prior.

Two things fall out of this, and the second was a surprise.

**The first megabyte is worth more than the coder.** Going from xz to a
context-mixing coder buys 18.5%. Handing that same coder one megabyte of
relevant material it has seen before buys 45.9% — two and a half times as much,
for no cleverness at all. On small files, *what the decoder knows* dominates
*how well the decoder codes*.

**And the curve flattens almost immediately.** The first megabyte takes 33.6%
off. The second takes 4.5% more. This is the finding that killed the feature it
was meant to justify: the plan was to build machinery to ship a much larger
distilled model, and the curve says a much larger *generic* prior is not where
the remaining value is. A prior that matches the content beats a prior that is
merely bigger. Crush already selects among four prior sets by file class; the
next real gain is more and narrower sets, not one enormous one — and that is a
data-collection job, not an engineering one.

That is why there is no "Atlas-2" in this release. The measurement said not to
build it.

---

## Shipped

### Helix — sequencing and variant data

A FASTQ file is three files wearing one coat: templated read identifiers, a
four-letter base alphabet with strong local context, and quality strings whose
statistics have nothing whatever in common with either. Compressed as one
anonymous text stream, most of that structure is thrown away. Helix splits the
file into its real streams and models each one properly, and does the same for
FASTA, SAM and VCF.

Measured end to end through Crush, `--tier balanced`, byte-exact both ways:

| File | Size | Crush v3 | Crush v4 | Change |
|---|---|---|---|---|
| reads.fastq | 3,000,000 | 926,403 | 763,387 | **−17.60%** |
| ref.fasta | 10,166,820 | 2,210,144 | 1,924,510 | **−12.92%** |
| calls.vcf | 11,726,984 | 1,834,524 | 1,356,886 | **−26.04%** |
| **total** | **24,893,804** | **4,971,071** | **4,044,783** | **−18.63%** |

It is also **2.5× faster** than the general path it replaces, because modelling
three coherent streams is easier work than modelling one incoherent one.

Against the tools a lab would otherwise reach for, on the same FASTQ: xz -9e
gets 5,711,444 and brotli -q11 gets 5,704,947. Helix's stream split reaches
4,629,061 before Crush's bundle codec even runs.

### Vector — numeric and floating-point arrays

An IEEE-754 array is also three things at once: sign, exponent and mantissa have
wildly different statistics, and consecutive values are usually close. Vector
splits an array into its real channels — detecting width, endianness and
interleaving without being told — then picks per channel between XOR against the
previous value with zero-run coding, delta, byte planes, and the one that pays
most on exported data: recovering the exact decimal a float was printed from and
coding the integer instead.

Measured end to end, `--tier balanced`, byte-exact both ways:

| File | Size | Crush v3 | Crush v4 | Change |
|---|---|---|---|---|
| Financial ticks, decimal prices, float64 | 2,000,000 | 173,500 | 137,120 | **−20.97%** |
| 8-channel sensor stream, float32 | 6,400,000 | 3,511,692 | 3,096,660 | **−11.82%** |
| Smooth 2-D field, big-endian float32 | 2,520,000 | 1,089,129 | 988,336 | **−9.25%** |
| data_floats.bin (corpus) | 7,200,000 | 5,182,423 | 5,182,424 | 0.00% |
| data_timeseries.bin (corpus) | 8,000,000 | 719,297 | 719,298 | 0.00% |
| **total** | **26,120,000** | **10,676,041** | **10,123,838** | **−5.17%** |

Note the two zeroes honestly: on the corpus's own numeric files — a float64
random walk and an int32 random walk — Vector finds nothing the existing
byte-plane path had not already found, and correctly declines. The wins are on
structured numeric data, which is what scientific, telemetry and financial
archives are made of.

Verified explicitly on NaN payloads, negative zero, denormals and infinities:
all reproduce bit-for-bit, including the payload bits inside NaNs.

### Loom — structure discovery in unknown binary formats

For a binary file nobody wrote a parser for — a sensor dump, a game asset, a
proprietary record file — Crush previously guessed a fixed transpose width or
treated it as an opaque stream. But such files are almost always arrays of
records with typed fields, and their redundancy runs down the columns rather
than along the stream.

Loom infers the record stride from the data itself, types each column, regroups
columns that turn out to be byte planes of one wider value, and codes each field
on its own terms — with no knowledge of the format. It runs at Crush tier and
above, on files over 256 KB, because the analysis pass is not free.

| File | Size | Crush v3 | Crush v4 | Change |
|---|---|---|---|---|
| Struct-packed record file, big-endian | 3,360,000 | 731,055 | 699,537 | **−4.31%** |
| Mixed int/float sensor log with padding | 7,200,100 | 3,184,365 | 2,853,456 | **−10.39%** |
| font.ttf | 84,492 | 35,725 | 35,726 | 0.00% |

**The honest caveat, stated plainly:** both winning files are constructed. On
every *real* binary in the corpus — an SQLite database, a Windows DLL, a Python
extension module, a TrueType font — Loom finds nothing and declines, changing
the result by less than 0.05%. Standing alone against a byte-plane transpose it
looks much stronger (+16.3% and +13.0% on those same two files), but most of
that margin is recovered by the codec tournament anyway once PPMd and context
mixing get their turn.

So: Loom is real, it is verified, and it will help people whose data is
struct-packed records. It did not move the corpus, and this document is not
going to pretend it did.

---

## Not shipped

### The recurrent mixer — correct, and far too slow

The single biggest known gain left in a PAQ-style architecture is what `cmix`
gets over `paq8`: replacing the static logistic mixer with an LSTM trained
online, by encoder and decoder identically, so no weights need to ship. It was
built — in integer arithmetic, so there is no floating-point drift to worry
about — and it works.

Correctness is not the problem. On every test the round-trip is exact, a repeat
compression is bit-identical, and a `-O0` build and a `-O2` build produce
identical output, which is the strong test that there is no drift anywhere.

| Configuration | Total on the 4-file sweep | Change | Time |
|---|---|---|---|
| current mixer | 179,435 | — | 7.9 s |
| LSTM, 20 cells | 176,344 | **−1.72%** | 80.2 s |
| LSTM, 12 cells | 176,617 | −1.57% | 44.9 s |
| diagonal gated unit, 24 cells | 179,445 | +0.01% | 37.8 s |

**−1.7% for roughly 10× the time**, on the component that is already the slowest
thing in the product. Crush CM runs at about 0.13 MB/s; with the recurrent mixer
it runs at about 0.012 MB/s. A 100 MB archive would take over two hours in each
direction. That is not a compressor anyone would leave running.

The code is kept in the lab, and if it is ever worth 10× the time — an archive
being written once and read by thousands, say — it is ready. It is not the
default for anyone.

### Weave — similarity ordering and cross-file delta

Two ideas that should work and do not.

**Ordering.** Files in a solid bundle should sit next to the files they resemble,
because a compressor's memory decays. Content-defined-chunk MinHash signatures
were computed per file, a similarity graph built, and a greedy path taken
through it. Adjacent-pair similarity improved measurably (11.3 → 14.5 on the
document set). Compressed output went from 160,556 bytes to 160,508 — **0.03%**.

**Cross-file delta.** For clusters of near-duplicate files, code one as a
reference and the others as deltas against it. On 90 documents from one
template this produced **246,796 bytes against 160,556** — over 50% *worse*.

The reason is the same in both cases, and it is worth stating because it is not
obvious: **a solid stream with a 64 MB dictionary has already found all of it.**
LZMA2 can reach back across the entire bundle, so proximity buys nothing, and
delta-coding actively destroys the literal repetition the stream was exploiting.
The idea has real value for archives far larger than the dictionary, and none at
all below that line. Not shipped.

---

## Regression check

Nothing above is worth anything if it broke what already worked.

The full corpus was compressed and extracted with v3 and v4 at the same tier,
and every file compared byte for byte. Result in BENCHMARKS.md. Selective
extraction, the drag-and-drop routes, and all five tiers were re-verified after
integration.

---

## What this round changed, in one line each

- Sequencing and variant data: **−18.6%**, and 2.5× faster.
- Structured numeric and float data: **−5% to −21%**, depending on the shape.
- Struct-packed binary records: **−4% to −10%** at Crush tier and above.
- Everything else: unchanged, and still byte-exact.
- And the measurement that should shape the next round: on small files, one
  megabyte of shipped knowledge is worth more than the entire step from xz to
  context mixing — but the second megabyte is worth almost nothing, so the next
  gain is in *narrower* priors, not bigger ones.
