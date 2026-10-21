#!/usr/bin/env python3
import os as _os  # the Crush folder, wherever it is
CRUSH_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""
Adversarial tests for the v2 layers.

Each new layer is a new way to be wrong. Oracle can mistake a near-generator
for a generator. Prism can mis-parse structure and reassemble it differently.
Echo can collide chunks. Perceptual can be handed something it must refuse.
Everything here is designed to trip one of them.
"""
import os, sys, json, random, shutil, tempfile, struct, hashlib
sys.path.insert(0, CRUSH_ROOT + "/engine")
import numpy as np
import crush_engine as ce
from crush_oracle import Oracle
from crush_prism import Prism

random.seed(2026); np.random.seed(2026)
ROOT = tempfile.mkdtemp(prefix="crush_edge2_")
SRC = os.path.join(ROOT, "src"); os.makedirs(SRC)

def w(rel, data):
    p = os.path.join(SRC, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)

print("=" * 78)
print("PART 1 - ORACLE must not hallucinate a generator")
print("=" * 78)
cases = []
# sequences that start like a generator and then break
a = np.arange(400000, dtype="<u4"); a[399999] = 7
cases.append(("arithmetic that breaks on the LAST value", a.tobytes()))
a = np.arange(400000, dtype="<u4"); a[200000] += 1
cases.append(("arithmetic with one corrupted value mid-way", a.tobytes()))
fib = [1, 1]
for i in range(200000): fib.append((fib[-1] + fib[-2]) % (2 ** 32))
fib[150000] ^= 0x40
cases.append(("Fibonacci with one flipped bit", np.array(fib, dtype="<u4").tobytes()))
blk = os.urandom(4096)
cases.append(("periodic except for the final block", blk * 300 + os.urandom(4096)))
cases.append(("almost-constant (one byte differs)", b"\xAA" * 500000 + b"\xAB" + b"\xAA" * 500000))
cases.append(("real text", open(CRUSH_ROOT + "/corpus/text_english.txt", "rb").read(500000)))
cases.append(("random", os.urandom(500000)))
bad = 0
for name, data in cases:
    r = Oracle.forward(data)
    if r is None:
        print(f"  {name:<46} correctly found no program")
    else:
        back = Oracle.inverse(r[0], r[1])
        ok = back == data
        print(f"  {name:<46} program found, regenerates exactly: {ok}")
        if not ok:
            bad += 1; print("     *** ORACLE PRODUCED A WRONG PROGRAM ***")
print(f"\n  wrong programs: {bad}")

print("\n" + "=" * 78)
print("PART 2 - PRISM must reassemble byte-for-byte, however ugly the input")
print("=" * 78)
pcases = [
    ("JSON, compact, no whitespace", json.dumps([{"a": i, "b": "x" * (i % 7)} for i in range(4000)],
                                                separators=(",", ":")).encode()),
    ("JSON, pretty, 4-space indent", json.dumps([{"a": i, "b": None, "c": True} for i in range(3000)],
                                                indent=4).encode()),
    ("JSON with unicode escapes", json.dumps([{"n": "café 中文 \U0001f600", "i": i}
                                              for i in range(3000)]).encode()),
    ("JSON with escaped quotes/backslashes", json.dumps([{"s": 'he said "hi"\\n\\path', "i": i}
                                                         for i in range(3000)]).encode()),
    ("JSON deeply nested", json.dumps({"a": {"b": {"c": {"d": [{"e": i} for i in range(2000)]}}}}).encode()),
    ("JSON, trailing whitespace + newline", (json.dumps([{"i": i} for i in range(3000)]) + "   \n\n").encode()),
    ("CSV with CRLF line endings", b"\r\n".join(b"%d,alpha,beta,%d" % (i, i * 3) for i in range(5000)) + b"\r\n"),
    ("CSV with ragged rows", b"\n".join(
        (b"%d,a,b,c" % i) if i % 50 else (b"%d,only,two" % i) for i in range(5000)) + b"\n"),
    ("CSV with empty fields", b"\n".join(b"%d,,,,x" % i for i in range(5000)) + b"\n"),
    ("CSV with quoted commas inside fields", b"\n".join(
        b'%d,"a,b,c",plain,%d' % (i, i) for i in range(5000)) + b"\n"),
    ("CSV, no trailing newline", b"\n".join(b"%d,a,b" % i for i in range(5000))),
    ("CSV with a UTF-8 BOM", b"\xef\xbb\xbf" + b"\n".join(b"%d,a,b" % i for i in range(5000)) + b"\n"),
    ("CSV with zero-padded numeric column", b"\n".join(b"%08d,a,b" % i for i in range(5000)) + b"\n"),
    ("CSV with huge integers", b"\n".join(b"%d,a,b" % (10 ** 18 + i) for i in range(5000)) + b"\n"),
    ("CSV with negative + leading-zero mix", b"\n".join(
        b"%s,a,b" % (b"-0%d" % i if i % 3 == 0 else b"%d" % -i) for i in range(5000)) + b"\n"),
    ("log lines with embedded tabs", b"\n".join(b"a\tb\t%d\tc" % i for i in range(6000)) + b"\n"),
    ("XML with CDATA and comments", (b"<r>" + b"".join(
        b"<i id='%d'><![CDATA[x<y&z]]><!--c--></i>" % i for i in range(4000)) + b"</r>")),
    ("truncated JSON", json.dumps([{"i": i} for i in range(3000)]).encode()[:-40]),
    ("JSON with NaN-ish tokens (invalid)", b"[" + b",".join(b"NaN" for _ in range(3000)) + b"]"),
    ("binary that looks delimited", os.urandom(200000).replace(b"\x00", b",")),
]
bad2 = 0
for name, data in pcases:
    try:
        r = Prism.forward(data)
    except Exception as e:
        print(f"  {name:<44} raised {type(e).__name__} (rejected)"); continue
    if r is None:
        print(f"  {name:<44} declined")
        continue
    p, aux = r
    try:
        back = Prism.inverse(p, aux)
    except Exception as e:
        print(f"  {name:<44} *** INVERSE RAISED {type(e).__name__} ***"); bad2 += 1; continue
    ok = back == data
    print(f"  {name:<44} split, exact={ok}   {Prism.describe(aux)}")
    if not ok:
        bad2 += 1; print("     *** PRISM LOST BYTES ***")
print(f"\n  reassembly failures: {bad2}")

print("\n" + "=" * 78)
print("PART 3 - full archive roundtrip over everything above")
print("=" * 78)
for i, (name, data) in enumerate(cases):
    w(f"oracle/{i:02d}.bin", data)
for i, (name, data) in enumerate(pcases):
    w(f"prism/{i:02d}.dat", data)
# plus the v1 hostile set, condensed
w("misc/empty.bin", b"")
w("misc/one.bin", b"\x00")
w("misc/unicode_é中\U0001f600.txt", "ünicöde\n".encode() * 400)
w("misc/random.bin", os.urandom(1_000_000))
w("misc/zeros.bin", b"\x00" * 1_000_000)
dup = os.urandom(300_000)
for k in range(5):
    w(f"misc/dup{k}.bin", dup)
base = bytearray(open(CRUSH_ROOT + "/corpus/text_english.txt", "rb").read(1_500_000))
for v in range(4):
    for _ in range(8):
        q = random.randrange(0, len(base) - 100); base[q:q] = os.urandom(50)
    w(f"misc/rev{v}.txt", bytes(base))

nf = sum(len(fs) for _, _, fs in os.walk(SRC))
nb = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(SRC) for f in fs)
print(f"  {nf} files, {nb:,} bytes\n")

results = []
for tier, harder, perc in [("quick", False, None), ("balanced", False, None),
                           ("crush", True, None), ("singularity", True, None),
                           ("crush", True, {"photo": 1.0, "audio": "128k"})]:
    tag = f"{tier}{'+harder' if harder else ''}{'+perceptual' if perc else ''}"
    arc = os.path.join(ROOT, f"t_{tag}.crush"); dst = os.path.join(ROOT, f"x_{tag}")
    shutil.rmtree(dst, ignore_errors=True)
    try:
        r = ce.compress([SRC], arc, tier, harder=harder, perceptual=perc)
        x = ce.extract(arc, dst)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"  {tag:<26} *** EXCEPTION {e}"); results.append((tag, False)); continue
    mism, missing = [], []
    for d, _, fs in os.walk(SRC):
        for f in fs:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, os.path.dirname(SRC))
            q = os.path.join(dst, rel)
            if not os.path.exists(q):
                missing.append(rel); continue
            if open(p, "rb").read() != open(q, "rb").read():
                mism.append(rel)
    ok = not mism and not missing and not x["failed"]
    print(f"  {tag:<26} {r['in']:>11,} -> {r['out']:>9,} ({100*r['ratio']:5.2f}%) "
          f"{r['seconds']:6.1f}s   {'ALL BYTE-EXACT' if ok else 'PROBLEMS'}")
    if mism:    print(f"      mismatched: {mism[:5]}")
    if missing: print(f"      missing:    {missing[:5]}")
    if x["failed"]: print(f"      failed verify: {x['failed'][:5]}")
    results.append((tag, ok))

print(f"\n  {sum(1 for _, o in results if o)}/{len(results)} configurations fully byte-exact")
print(f"  Oracle wrong programs: {bad}   Prism reassembly failures: {bad2}")
shutil.rmtree(ROOT, ignore_errors=True)
