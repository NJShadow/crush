#!/usr/bin/env python3
import os as _os  # the Crush folder, wherever it is
CRUSH_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Edge-case and adversarial testing for the Crush container.

Every case here is compressed, extracted and compared byte-for-byte. Anything
that does not survive is a bug, not a curiosity.
"""
import os, sys, shutil, random, subprocess, hashlib, tempfile, json
sys.path.insert(0, CRUSH_ROOT + "/engine")
import crush_engine as ce

random.seed(99)
ROOT = tempfile.mkdtemp(prefix="crush_edge_")
SRC = os.path.join(ROOT, "src")
os.makedirs(SRC)

def w(rel, data):
    p = os.path.join(SRC, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)

print("Building adversarial input set...")
cases = []

w("empty.bin", b"");                                        cases.append("zero-length file")
w("one_byte.bin", b"\x00");                                 cases.append("single NUL byte")
w("two_bytes.txt", b"hi");                                  cases.append("two bytes")
w("all_same.bin", b"\xAA" * 3_000_000);                     cases.append("3 MB of one repeated byte")
w("random.bin", os.urandom(2_000_000));                     cases.append("2 MB incompressible random")
w("high_entropy_text.txt", os.urandom(500_000).hex().encode()); cases.append("hex-encoded random")

# names that break naive path handling
w("unicode_éà中文_\U0001f600.txt", "Ünïcödé ✓ 中文 🎉\n".encode() * 500)
cases.append("unicode filename incl. emoji")
w("spaces and (parens) & symbols #1.txt", b"x" * 10000);    cases.append("spaces and symbols in name")
w("a" * 180 + ".txt", b"long name" * 100);                  cases.append("180-character filename")
w("deep/" + "/".join(f"lvl{i}" for i in range(14)) + "/buried.txt", b"deep" * 2000)
cases.append("14 levels of nesting")

# duplicates - should be stored once
dup = os.urandom(400_000)
for i in range(6):
    w(f"dups/copy{i}.bin", dup)
cases.append("6 byte-identical copies (dedup)")

# near-duplicates - solid bundling should find the overlap
base = os.urandom(300_000)
for i in range(4):
    w(f"near/v{i}.bin", base[:290_000] + os.urandom(10_000))
cases.append("4 near-identical files (solid bundling)")

# malformed / truncated structures that must not crash the transforms
good_png = open(CRUSH_ROOT + "/corpus/screenshot_ui.png", "rb").read()
w("broken/truncated.png", good_png[:len(good_png)//3])
w("broken/png_header_only.png", good_png[:8] + b"\x00" * 200)
w("broken/fake.jpg", b"\xff\xd8\xff" + os.urandom(50_000))
w("broken/truncated.zip", open(CRUSH_ROOT + "/corpus/office_report.docx", "rb").read()[:5000])
w("broken/empty.zip", b"PK\x05\x06" + b"\x00" * 18)
w("broken/fake.wav", b"RIFF" + b"\x00" * 4 + b"WAVE" + os.urandom(30_000))
w("broken/zero_wav.wav", b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x02\x00"
                         b"\x44\xac\x00\x00\x10\xb1\x02\x00\x04\x00\x10\x00data\x00\x00\x00\x00")
cases.append("6 truncated / malformed media and container files")

# a .crush inside a .crush
sub = os.path.join(ROOT, "inner.crush")
ce.compress([os.path.join(SRC, "all_same.bin")], sub, "quick")
shutil.copy(sub, os.path.join(SRC, "nested.crush"))
cases.append("a .crush archive as input")

# tiny files en masse - index overhead check
for i in range(400):
    w(f"many/f{i:04d}.txt", f"file number {i}\n".encode() * random.randint(1, 30))
cases.append("400 tiny text files")

# binary with embedded deflate-looking noise
w("tricky/pk_noise.bin", b"".join(b"PK\x03\x04" + os.urandom(60) for _ in range(4000)))
cases.append("data full of fake ZIP signatures")

total_files = sum(len(f) for _, _, f in os.walk(SRC))
total_bytes = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(SRC) for f in fs)
print(f"  {total_files} files, {total_bytes:,} bytes across {len(cases)} adversarial cases\n")

results = []
for tier, harder in [("quick", False), ("balanced", False), ("crush", False), ("crush", True)]:
    arc = os.path.join(ROOT, f"t_{tier}_{harder}.crush")
    dst = os.path.join(ROOT, f"x_{tier}_{harder}")
    shutil.rmtree(dst, ignore_errors=True)
    try:
        r = ce.compress([SRC], arc, tier, harder=harder)
        x = ce.extract(arc, dst)
    except Exception as e:
        print(f"  {tier:<10} harder={harder!s:<6} *** EXCEPTION: {e}")
        results.append({"tier": tier, "harder": harder, "ok": False, "err": str(e)})
        continue

    # exhaustive comparison
    mism, missing, extra = [], [], []
    src_rel = set()
    for d, _, fs in os.walk(SRC):
        for f in fs:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, os.path.dirname(SRC))
            src_rel.add(rel)
            q = os.path.join(dst, rel)
            if not os.path.exists(q):
                missing.append(rel); continue
            if open(p, "rb").read() != open(q, "rb").read():
                mism.append(rel)
    out_rel = set()
    for d, _, fs in os.walk(dst):
        for f in fs:
            out_rel.add(os.path.relpath(os.path.join(d, f), dst))
    extra = sorted(out_rel - src_rel)

    ok = not mism and not missing and not extra and not x["failed"]
    print(f"  {tier:<10} harder={harder!s:<6} {r['in']:>11,} -> {r['out']:>10,} "
          f"({100*r['ratio']:5.1f}%)  {r['seconds']:6.1f}s   "
          f"{'ALL BYTE-EXACT' if ok else 'PROBLEMS'}")
    if mism:    print(f"      mismatched: {mism[:6]}")
    if missing: print(f"      missing:    {missing[:6]}")
    if extra:   print(f"      unexpected: {extra[:6]}")
    if x["failed"]: print(f"      failed verify: {x['failed'][:6]}")
    results.append({"tier": tier, "harder": harder, "ok": ok, "in": r["in"], "out": r["out"],
                    "sec": r["seconds"], "dedup": r["stats"].get("dedup_files", 0)})

print("\n--- never-inflate check (the guarantee that matters on bad input) ---")
for nm in ["random.bin", "nested.crush", "high_entropy_text.txt"]:
    p = os.path.join(SRC, nm)
    a = os.path.join(ROOT, "single.crush")
    r = ce.compress([p], a, "crush")
    over = r["out"] - r["in"]
    print(f"  {nm:<26} {r['in']:>10,} -> {r['out']:>10,}  overhead {over:+,} bytes "
          f"({100*over/max(1,r['in']):+.3f}%)")

json.dump(results, open(CRUSH_ROOT + "/results/edge_tests.json", "w"), indent=1)
print(f"\n{sum(1 for r in results if r.get('ok'))}/{len(results)} configurations fully byte-exact")
shutil.rmtree(ROOT, ignore_errors=True)
