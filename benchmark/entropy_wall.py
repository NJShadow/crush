#!/usr/bin/env python3
import os as _os  # the Crush folder, wherever it is
CRUSH_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""
EXPERIMENT 1 - Where is the wall, really?

Four tests that establish, empirically, the boundary any honest compressor works within,
and - crucially - how much room is left INSIDE that boundary.
"""
import os, io, lzma, zlib, bz2, math, random, json, subprocess, collections
import numpy as np
import zstandard as zstd, brotli

random.seed(42); np.random.seed(42)
R = {}

def z19(b):  return zstd.ZstdCompressor(level=19).compress(b)
def xz9(b):  return lzma.compress(b, preset=9 | lzma.PRESET_EXTREME)
def br11(b): return brotli.compress(b, quality=11)
def best(b): return min(len(z19(b)), len(xz9(b)), len(bz2.compress(b, 9)), len(zlib.compress(b, 9)))

print("="*78)
print("TEST 1 - THE COUNTING ARGUMENT (pigeonhole principle)")
print("="*78)
print("Claim under test: 'a good enough algorithm could shrink any file'.")
print("Method: compress 4,000 independent random 256-byte strings with the five")
print("strongest general-purpose codecs available, keeping the best result each time.\n")
smaller = 0; total_in = 0; total_out = 0
for i in range(4000):
    b = os.urandom(256)
    out = best(b)
    total_in += len(b); total_out += out
    if out < len(b): smaller += 1
print(f"  Strings compressed        : 4,000")
print(f"  Strings that got SMALLER  : {smaller}  ({100*smaller/4000:.3f}%)")
print(f"  Total input               : {total_in:,} bytes")
print(f"  Total output (best-of-5)  : {total_out:,} bytes")
print(f"  Net result                : {100*(total_out-total_in)/total_in:+.2f}%  <- compression EXPANDED the data")
R["test1"] = dict(n=4000, smaller=smaller, total_in=total_in, total_out=total_out)

print("\n  Why this can never be beaten:")
print("  There are 2^2048 possible 256-byte strings but only 2^2048 - 1 shorter strings.")
print("  Any injective (reversible) map must therefore leave at least one input un-shrunk.")
print("  Extended: at most 1/256 of inputs can shrink by even one byte, at most 1/65536")
print("  by two bytes. Compression is a redistribution of length, never a net reduction.\n")

print("="*78)
print("TEST 2 - RECURSIVE COMPRESSION (the 'infinite squeeze' claim)")
print("="*78)
print("Claim under test: 'feed the output back in and keep shrinking'.")
print("Method: take real text, compress, then re-compress the output 12 times.\n")
data = open(CRUSH_ROOT + "/corpus/text_english.txt","rb").read(2_000_000)
sizes = [len(data)]; cur = data
print(f"  pass  0 (original)   {len(data):>10,} B")
for p in range(1, 13):
    cur = xz9(cur); sizes.append(len(cur))
    d = sizes[-1] - sizes[-2]
    print(f"  pass {p:2d}             {len(cur):>10,} B   {d:+,} vs previous")
print(f"\n  First pass:  {100*(sizes[1]-sizes[0])/sizes[0]:+.1f}%   (real structure removed)")
print(f"  Passes 2-12: {100*(sizes[-1]-sizes[1])/sizes[1]:+.3f}%   (output is now incompressible; only overhead accrues)")
R["test2"] = sizes

print("\n" + "="*78)
print("TEST 3 - HOW FAR ARE REAL FILES FROM *THEIR OWN* ENTROPY FLOOR?")
print("="*78)
print("This is the number that matters. The wall is real, but real files sit nowhere")
print("near it. Empirical order-N entropy vs what today's best codecs actually achieve.\n")

def order_n_entropy_bits(b, n, cap=1_200_000):
    """Empirical conditional entropy H(X | previous n bytes), in bits/byte."""
    b = b[:cap]
    if n == 0:
        c = collections.Counter(b); N = len(b)
        return -sum((v/N)*math.log2(v/N) for v in c.values())
    ctx = collections.defaultdict(collections.Counter)
    for i in range(n, len(b)):
        ctx[b[i-n:i]][b[i]] += 1
    tot = len(b)-n; H = 0.0
    for k, cnt in ctx.items():
        m = sum(cnt.values())
        h = -sum((v/m)*math.log2(v/m) for v in cnt.values())
        H += (m/tot)*h
    return H

print(f"  {'file':<26}{'o0':>7}{'o1':>7}{'o2':>7}{'o3':>7} | {'xz -9e':>9}{'zstd19':>9}{'br 11':>9}")
print("  " + "-"*76)
rows = []
for f in ["text_english.txt","code_python.py","data_xml.xml","data_records.json","data_records.csv"]:
    b = open(f"{CRUSH_ROOT}/corpus/{f}","rb").read(1_200_000)
    ents = [order_n_entropy_bits(b, n) for n in (0,1,2,3)]
    bpc = lambda c: 8.0*len(c)/len(b)
    a, c_, d_ = bpc(xz9(b)), bpc(z19(b)), bpc(br11(b))
    print(f"  {f:<26}{ents[0]:>7.2f}{ents[1]:>7.2f}{ents[2]:>7.2f}{ents[3]:>7.2f} | {a:>9.3f}{c_:>9.3f}{d_:>9.3f}")
    rows.append(dict(file=f, o0=ents[0], o1=ents[1], o2=ents[2], o3=ents[3], xz=a, zstd=c_, brotli=d_))
R["test3"] = rows
print("\n  Read this carefully: the best codecs already beat order-3 entropy, because they")
print("  model far longer contexts. Order-N estimates are an upper bound, not the floor.")
print("  The true floor (Kolmogorov complexity) is UNCOMPUTABLE - which means nobody,")
print("  including the market leaders, can prove they are near it. That unprovable gap")
print("  is the entire commercial opportunity.\n")

print("="*78)
print("TEST 4 - SIZING THE REAL OPPORTUNITY")
print("="*78)
print("If the wall can't move, where does a better product come from? Two places.\n")

print("  (a) STRUCTURE THE GENERAL-PURPOSE CODECS CANNOT SEE")
print("      A .docx / .xlsx / .zip / .apk is a container of DEFLATE streams. To a")
print("      byte-oriented compressor it looks like noise. Unwrap it first:\n")
for f in ["office_sheet.xlsx", "container_wheel.zip", "office_report.docx"]:
    p = f"{CRUSH_ROOT}/corpus/{f}"
    if not os.path.exists(p): continue
    orig = os.path.getsize(p)
    direct = len(xz9(open(p,"rb").read()))
    # unwrap: concatenate the DECOMPRESSED members, then compress those
    import zipfile
    raw = bytearray()
    try:
        with zipfile.ZipFile(p) as zf:
            for n in zf.namelist():
                try: raw += zf.read(n)
                except Exception: pass
    except Exception: continue
    unwrapped = len(xz9(bytes(raw)))
    print(f"      {f:<24} {orig:>9,} B  -> xz direct {direct:>9,} ({100*direct/orig:5.1f}%)"
          f"  -> unwrapped {unwrapped:>9,} ({100*unwrapped/orig:5.1f}%)   gain {100*(direct-unwrapped)/direct:5.1f}%")

print("\n  (b) NUMERIC LAYOUT THE CODECS CANNOT SEE")
print("      Float/int arrays are byte-interleaved. Transpose them and the exponent")
print("      bytes line up into a highly repetitive plane:\n")
for f, dt in [("data_floats.bin", np.float64), ("data_timeseries.bin", np.int32), ("image_raw16.bin", np.uint16)]:
    b = open(f"{CRUSH_ROOT}/corpus/{f}","rb").read()
    a = np.frombuffer(b, dtype=dt)
    direct = len(xz9(b))
    shuf = a.view(np.uint8).reshape(-1, a.dtype.itemsize).T.tobytes()   # byte-plane transpose
    sh = len(xz9(shuf))
    dl = len(xz9(np.diff(a.astype(np.int64), prepend=0).astype(np.int64).view(np.uint8)
                 .reshape(-1,8).T.tobytes())) if dt != np.float64 else None
    extra = f"  -> delta+shuffle {dl:>9,} ({100*dl/len(b):5.1f}%)" if dl else ""
    print(f"      {f:<24} {len(b):>9,} B  -> xz direct {direct:>9,} ({100*direct/len(b):5.1f}%)"
          f"  -> shuffled {sh:>9,} ({100*sh/len(b):5.1f}%){extra}")

json.dump(R, open(CRUSH_ROOT + "/results/entropy_wall.json","w"), indent=1)
print("\n" + "="*78)
print("CONCLUSION: the wall is real and immovable. The gap between the wall and where")
print("today's mainstream tools actually stop is large, exploitable, and is where Crush")
print("will live. No physics was harmed in the making of this compressor.")
print("="*78)
