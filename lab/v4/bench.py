"""
bench.py - the full comparison for the Crush v4 domain codecs.

Every row is verified by ACTUALLY INVERTING and byte-comparing against the
input.  A row that does not reproduce its input exactly is reported FAIL and
its size is not to be believed.

Columns
    xz -9e / zstd -19 / brotli -q 11   the external CLI tools, as asked
    crush-num                          engine.ByteTranspose + engine.tournament
    crush-txt                          engine.tournament alone (no transform),
                                       the general-purpose Crush path
    v4-domain                          this component's transform, then the
                                       same engine tournament
    v4-domain+                         genomics only: the same split with a
                                       codec chosen per stream

Usage:  python3 bench.py [--quick] [--only float|genome|edge]
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/home/claude/crush/engine")

import numpy as np
import crush_engine as ce
import floatcodec as fcm
import genome as gm

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CORPUS = "/home/claude/crush/corpus"
CORPUS2 = "/home/claude/crush/corpus2"

FLOAT_CASES = [
    ("field2d_f64",     f"{DATA}/field2d_f64.bin",   "smooth 2-D field, float64 (constructed)"),
    ("field2d_be_f32",  f"{DATA}/field2d_be_f32.bin", "same field, BIG-endian float32 (constructed)"),
    ("sensor_f32",      f"{DATA}/sensor_f32.bin",    "8-channel interleaved sensor stream, float32 (constructed)"),
    ("ticks_f64",       f"{DATA}/ticks_f64.bin",     "financial ticks, decimal prices, float64 (constructed)"),
    ("weights_f32",     f"{DATA}/weights_f32.bin",   "ML weight tensor, N(0,0.02), float32 (constructed)"),
    ("widened_f64",     f"{DATA}/widened_f64.bin",   "float32 values stored as float64 (constructed)"),
    ("edge_f64",        f"{DATA}/edge_f64.bin",      "NaN payloads / -0.0 / denormals / inf, float64"),
    ("edge_f32",        f"{DATA}/edge_f32.bin",      "NaN payloads / -0.0 / denormals / inf, float32"),
    ("data_floats",     f"{CORPUS}/data_floats.bin", "REAL corpus file: float64 random walk"),
    ("data_timeseries", f"{CORPUS}/data_timeseries.bin", "REAL corpus file: int32 random walk"),
]

GENOME_CASES = [
    ("reads.fastq",     f"{DATA}/reads.fastq",  "75k x 100bp Illumina-style FASTQ (constructed)"),
    ("ref.fasta",       f"{DATA}/ref.fasta",    "10 Mbp soft-masked reference FASTA (constructed)"),
    ("calls.vcf",       f"{DATA}/calls.vcf",    "200k-variant VCF (constructed)"),
    ("telemetry.csv",   f"{CORPUS2}/struct/telemetry.csv", "REAL corpus file: sensor CSV"),
    ("data_records.csv", f"{CORPUS}/data_records.csv", "REAL corpus file: record CSV (CRLF)"),
]


# ------------------------------------------------------------------- tools

def _run_tool(cmd, data, verify):
    with tempfile.TemporaryDirectory() as td:
        i = os.path.join(td, "in")
        with open(i, "wb") as f:
            f.write(data)
        t0 = time.time()
        r = subprocess.run(cmd + [i], capture_output=True)
        t1 = time.time()
        if r.returncode != 0:
            return None
        out = r.stdout
        ok = verify(out) == data
        return len(out), t1 - t0, ok


def base_xz(d):
    import lzma
    return _run_tool(["xz", "-9e", "-T1", "-c"], d, lambda b: lzma.decompress(b))


def base_zstd(d):
    import zstandard as zstd
    return _run_tool(["zstd", "-19", "--long=27", "-q", "-c"], d,
                     lambda b: zstd.ZstdDecompressor().decompress(b, max_output_size=1 << 31))


def base_brotli(d):
    import brotli
    return _run_tool(["brotli", "-q", "11", "-w", "24", "-c"], d,
                     lambda b: brotli.decompress(b))


# ------------------------------------------------------- engine tournament

def engine_pack(payload, cls, tier="crush"):
    cid, blob, prior = ce.tournament(payload, cls, tier)
    return cid, blob


def engine_unpack(cid, blob):
    return ce.CODECS[cid][1](blob, None)


# --------------------------------------------------------------- one case

def bench_float(name, path, note, tier="crush"):
    d = open(path, "rb").read()
    rows = []

    for label, fn in (("xz -9e", base_xz), ("zstd -19", base_zstd),
                      ("brotli -q11", base_brotli)):
        r = fn(d)
        rows.append((label, r[0], r[1], None, r[2]) if r else (label, None, 0, None, False))

    # ---- existing Crush numeric path
    t0 = time.time()
    res = ce.ByteTranspose.forward(d)
    pay = res[0] if res else d
    aux = res[1] if res else None
    cid, blob = engine_pack(pay, "numeric", tier)
    t1 = time.time()
    t2 = time.time()
    back = engine_unpack(cid, blob)
    if aux is not None:
        back = ce.ByteTranspose.inverse(back, aux)
    t3 = time.time()
    rows.append((f"crush-num[{cid}]", len(blob), t1 - t0, t3 - t2, back == d))

    # ---- v4 domain codec
    t0 = time.time()
    r = fcm.FloatArray.forward(d)
    used = r is not None
    pay = r[0] if used else d
    aux = r[1] if used else None
    cid2, blob2 = engine_pack(pay, "numeric", tier)
    t1 = time.time()
    t2 = time.time()
    back = engine_unpack(cid2, blob2)
    if used:
        back = fcm.FloatArray.inverse(back, aux)
    t3 = time.time()
    rows.append((f"v4-float[{cid2}{'' if used else ',declined'}]",
                 len(blob2), t1 - t0, t3 - t2, back == d))
    return d, rows


def bench_genome(name, path, note, tier="crush"):
    d = open(path, "rb").read()
    rows = []
    for label, fn in (("xz -9e", base_xz), ("zstd -19", base_zstd),
                      ("brotli -q11", base_brotli)):
        r = fn(d)
        rows.append((label, r[0], r[1], None, r[2]) if r else (label, None, 0, None, False))

    # ---- general-purpose Crush (tournament, no domain transform)
    t0 = time.time()
    cid, blob = engine_pack(d, "text", tier)
    t1 = time.time()
    t2 = time.time()
    back = engine_unpack(cid, blob)
    t3 = time.time()
    rows.append((f"crush-txt[{cid}]", len(blob), t1 - t0, t3 - t2, back == d))

    # ---- v4 transform + the same tournament
    t0 = time.time()
    r = gm.Genome.forward(d, path=path)
    used = r is not None
    pay = r[0] if used else d
    aux = r[1] if used else None
    cid2, blob2 = engine_pack(pay, "text", tier)
    t1 = time.time()
    t2 = time.time()
    back = engine_unpack(cid2, blob2)
    if used:
        back = gm.Genome.inverse(back, aux)
    t3 = time.time()
    rows.append((f"v4-split[{cid2}{'' if used else ',declined'}]",
                 len(blob2), t1 - t0, t3 - t2, back == d))

    # ---- v4 split + per-stream codecs
    t0 = time.time()
    blob3 = gm.pack(d, path=path)
    t1 = time.time()
    if blob3 is not None:
        t2 = time.time()
        back = gm.unpack(blob3)
        t3 = time.time()
        rows.append(("v4-perstream", len(blob3), t1 - t0, t3 - t2, back == d))
    return d, rows


# ------------------------------------------------------------- edge tests

def edge_tests():
    """The float paths must be exact on every IEEE-754 oddity, not merely on
    well-behaved data.  Each of these is round-tripped through the real
    transform and compared as BIT PATTERNS, so -0.0 != 0.0 and every NaN
    payload must survive."""
    out = []
    rng = np.random.default_rng(3)

    def case(nm, u64):
        u = np.asarray(u64, dtype=np.uint64)
        raw = u.tobytes()
        if len(raw) < 4096:
            reps = (4096 // len(raw)) + 2
            u = np.tile(u, reps)
            raw = u.tobytes()
        r = fcm.FloatArray.forward(raw)
        if r is None:
            out.append((nm, len(raw), "declined", True))
            return
        back = fcm.FloatArray.inverse(r[0], r[1])
        ok = back == raw
        # and check the bit patterns explicitly
        if ok:
            bu = np.frombuffer(back, dtype="<u8")
            ok = bool(np.array_equal(bu, u))
        out.append((nm, len(raw), "transformed", ok))

    case("all -0.0", np.full(600, 0x8000000000000000, dtype=np.uint64))
    case("+0.0 / -0.0 alternating",
         np.where(np.arange(600) % 2, np.uint64(0x8000000000000000), np.uint64(0)))
    case("quiet NaN, distinct payloads",
         np.uint64(0x7FF8000000000000) + rng.integers(0, 1 << 45, 600, dtype=np.uint64))
    case("signalling NaN, distinct payloads",
         np.uint64(0x7FF0000000000001) + rng.integers(0, 1 << 45, 600, dtype=np.uint64))
    case("negative NaN payloads",
         np.uint64(0xFFF8000000000000) + rng.integers(0, 1 << 45, 600, dtype=np.uint64))
    case("denormals (smallest)", np.arange(1, 601, dtype=np.uint64))
    case("denormals random", rng.integers(1, 1 << 52, 600, dtype=np.uint64))
    case("+inf / -inf", np.where(np.arange(600) % 2,
                                 np.uint64(0x7FF0000000000000),
                                 np.uint64(0xFFF0000000000000)))
    case("mixed pathological",
         np.concatenate([
             np.full(150, 0x8000000000000000, dtype=np.uint64),
             np.uint64(0x7FF8000000000000) + rng.integers(0, 1 << 40, 150, dtype=np.uint64),
             rng.integers(1, 1 << 52, 150, dtype=np.uint64),
             np.full(150, 0x7FF0000000000000, dtype=np.uint64)]))
    case("decimal-looking with -0.0 mixed in",
         np.concatenate([
             (np.round(100 + np.arange(500) * 0.01, 2)).astype("<f8").view(np.uint64),
             np.full(100, 0x8000000000000000, dtype=np.uint64)]))

    # float32 versions
    def case32(nm, u32):
        u = np.asarray(u32, dtype=np.uint32)
        raw = u.tobytes()
        if len(raw) < 4096:
            u = np.tile(u, (4096 // len(raw)) + 2)
            raw = u.tobytes()
        r = fcm.FloatArray.forward(raw)
        if r is None:
            out.append((nm, len(raw), "declined", True))
            return
        back = fcm.FloatArray.inverse(r[0], r[1])
        ok = back == raw and np.array_equal(np.frombuffer(back, "<u4"), u)
        out.append((nm, len(raw), "transformed", bool(ok)))

    case32("f32 -0.0", np.full(1200, 0x80000000, dtype=np.uint32))
    case32("f32 NaN payloads",
           np.uint32(0x7FC00000) + rng.integers(0, 1 << 21, 1200, dtype=np.uint32))
    case32("f32 denormals", np.arange(1, 1201, dtype=np.uint32))

    # exhaustive-ish sweep: every float32 bit pattern in a strided sample
    step = 9973
    u = np.arange(0, 1 << 32, step, dtype=np.uint64).astype(np.uint32)
    raw = u.tobytes()
    r = fcm.FloatArray.forward(raw)
    if r is None:
        out.append(("f32 strided sweep of the whole 2^32 space", len(raw), "declined", True))
    else:
        back = fcm.FloatArray.inverse(r[0], r[1])
        out.append(("f32 strided sweep of the whole 2^32 space", len(raw),
                    "transformed", back == raw))

    # random 64-bit patterns: every class at once, nothing well-behaved
    u = rng.integers(0, 1 << 63, 200000, dtype=np.uint64) * 2 + rng.integers(0, 2, 200000, dtype=np.uint64)
    raw = u.tobytes()
    r = fcm.FloatArray.forward(raw)
    if r is None:
        out.append(("uniform random 64-bit patterns", len(raw), "declined", True))
    else:
        back = fcm.FloatArray.inverse(r[0], r[1])
        out.append(("uniform random 64-bit patterns", len(raw), "transformed", back == raw))
    return out


def genome_edge_tests():
    """Structural oddities the FASTQ/FASTA/tabular parsers have to survive."""
    out = []

    def case(nm, data, path=""):
        try:
            r = gm.Genome.forward(data, path=path)
        except Exception:
            out.append((nm, len(data), "EXCEPTION", False))
            return
        if r is None:
            out.append((nm, len(data), "declined", True))
            return
        try:
            back = gm.Genome.inverse(r[0], r[1])
        except Exception:
            out.append((nm, len(data), "EXCEPTION", False))
            return
        out.append((nm, len(data), "transformed", back == data))

    def fq(n, idf, seqf, qf, plus=b"+", tail=True):
        b = bytearray()
        for i in range(n):
            b += b"@" + idf(i) + b"\n" + seqf(i) + b"\n" + plus + b"\n" + qf(i) + b"\n"
        if not tail:
            b = b[:-1]
        return bytes(b)

    rng = np.random.default_rng(5)
    S = lambda i: bytes(rng.choice(list(b"ACGT"), 100).tolist())
    Q = lambda i: bytes(rng.integers(35, 74, 100).tolist())

    case("fastq, no trailing newline",
         fq(3000, lambda i: b"read%d" % i, S, Q, tail=False), "x.fastq")
    case("fastq, variable read lengths",
         fq(3000, lambda i: b"read%d" % i,
            lambda i: bytes(rng.choice(list(b"ACGT"), 50 + i % 40).tolist()),
            lambda i: bytes(rng.integers(35, 74, 50 + i % 40).tolist())), "x.fastq")
    case("fastq, non-empty + lines",
         fq(3000, lambda i: b"read%d" % i, S, Q, plus=b"+read"), "x.fastq")
    case("fastq, N-heavy reads",
         fq(3000, lambda i: b"read%d" % i,
            lambda i: b"N" * 100, Q), "x.fastq")
    case("fastq, non-templated identifiers",
         fq(3000, lambda i: b"".join(bytes([rng.integers(97, 123)]) for _ in range(8)),
            S, Q), "x.fastq")
    case("fastq, CRLF line endings",
         fq(3000, lambda i: b"read%d" % i, S, Q).replace(b"\n", b"\r\n"), "x.fastq")
    case("fastq, identifiers with leading-zero fields",
         fq(3000, lambda i: b"R%08d:%03d" % (i, i % 999), S, Q), "x.fastq")
    case("fastq, empty read (zero-length seq+qual)",
         fq(3000, lambda i: b"read%d" % i,
            lambda i: b"" if i % 500 == 0 else S(i),
            lambda i: b"" if i % 500 == 0 else Q(i)), "x.fastq")

    fa = b">c1 x\n" + b"\n".join(b"ACGTNNacgt" * 6 for _ in range(2000)) + b"\n"
    case("fasta, mixed case + N", fa, "x.fasta")
    case("fasta, no trailing newline", fa[:-1], "x.fasta")
    case("fasta, ragged line lengths",
         b">c1\n" + b"\n".join(b"ACGT" * ((i % 7) + 1) for i in range(4000)) + b"\n",
         "x.fasta")
    case("fasta, protein sequence (not ACGT)",
         b">p1\n" + b"\n".join(b"MKVLATTLLA" * 6 for _ in range(2000)) + b"\n",
         "x.fasta")

    csv = b"a,b,c\n" + b"".join(b"%d,%d.%02d,text%d\n" % (i, i, i % 100, i % 7)
                                for i in range(4000))
    case("csv, mixed int/decimal/text columns", csv, "x.csv")
    case("csv, ragged rows",
         b"a,b,c\n" + b"".join((b"%d,%d\n" if i % 3 else b"%d,%d,%d\n") %
                               ((i, i) if i % 3 else (i, i, i))
                               for i in range(4000)), "x.csv")
    case("csv, quoted fields containing the delimiter",
         b"a,b\n" + b"".join(b'%d,"x,y,z"\n' % i for i in range(4000)), "x.csv")
    case("csv, negative zero decimals",
         b"a,b\n" + b"".join(b"%d,-0.00\n" % i for i in range(4000)), "x.csv")
    case("tsv, leading-zero integer column",
         b"a\tb\n" + b"".join(b"%08d\t%d\n" % (i, i) for i in range(4000)), "x.tsv")
    return out


# ------------------------------------------------------------------- main

def fmt(n):
    return f"{n:,}" if n is not None else "n/a"


def main():
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    tier = "crush"

    print("=" * 108)
    print("CRUSH v4 DOMAIN CODECS - benchmark")
    print("=" * 108)

    if only in (None, "float"):
        print("\n### 1. Floating-point / numeric arrays\n")
        for name, path, note in FLOAT_CASES:
            if not os.path.exists(path):
                print(f"-- {name}: MISSING {path}")
                continue
            try:
                d, rows = bench_float(name, path, note, tier)
            except Exception:
                traceback.print_exc()
                continue
            n = len(d)
            print(f"\n{name}  ({fmt(n)} B) - {note}")
            print(f"  {'codec':26s} {'bytes':>12s} {'ratio':>7s} {'enc MB/s':>9s} "
                  f"{'dec MB/s':>9s}  exact")
            best = min((r[1] for r in rows if r[1]), default=None)
            for lab, sz, te, td, ok in rows:
                if sz is None:
                    print(f"  {lab:26s} {'FAILED':>12s}")
                    continue
                mark = " *" if sz == best else "  "
                ds = f"{n/1e6/td:9.1f}" if td else f"{'':9s}"
                print(f"  {lab:26s} {fmt(sz):>12s} {n/sz:7.2f} {n/1e6/max(te,1e-9):9.1f} "
                      f"{ds}  {'PASS' if ok else 'FAIL'}{mark}")

    if only in (None, "genome"):
        print("\n\n### 2. Genomic / line-oriented scientific data\n")
        for name, path, note in GENOME_CASES:
            if not os.path.exists(path):
                print(f"-- {name}: MISSING {path}")
                continue
            try:
                d, rows = bench_genome(name, path, note, tier)
            except Exception:
                traceback.print_exc()
                continue
            n = len(d)
            print(f"\n{name}  ({fmt(n)} B) - {note}")
            print(f"  {'codec':26s} {'bytes':>12s} {'ratio':>7s} {'enc MB/s':>9s} "
                  f"{'dec MB/s':>9s}  exact")
            best = min((r[1] for r in rows if r[1]), default=None)
            for lab, sz, te, td, ok in rows:
                if sz is None:
                    print(f"  {lab:26s} {'FAILED':>12s}")
                    continue
                mark = " *" if sz == best else "  "
                ds = f"{n/1e6/td:9.1f}" if td else f"{'':9s}"
                print(f"  {lab:26s} {fmt(sz):>12s} {n/sz:7.2f} {n/1e6/max(te,1e-9):9.1f} "
                      f"{ds}  {'PASS' if ok else 'FAIL'}{mark}")

    if only in (None, "edge"):
        print("\n\n### 3. Edge cases (bit-pattern exactness)\n")
        allok = True
        for nm, sz, what, ok in edge_tests():
            allok &= ok
            print(f"  {nm:46s} {fmt(sz):>10s}  {what:12s}  {'PASS' if ok else 'FAIL'}")
        print("\n### 4. Genomic structural edge cases\n")
        for nm, sz, what, ok in genome_edge_tests():
            allok &= ok
            print(f"  {nm:46s} {fmt(sz):>10s}  {what:12s}  {'PASS' if ok else 'FAIL'}")
        print(f"\n  ALL EDGE CASES: {'PASS' if allok else 'FAIL'}")


if __name__ == "__main__":
    main()
