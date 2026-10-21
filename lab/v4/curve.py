"""The shipped-knowledge curve.

The one lever that can move compression by an order of magnitude is not a
cleverer coder - it is how much the DECODER already knows. Crush ships priors
today; this measures what happens as that shipped knowledge grows, on the case
where it matters most: small files, where an adaptive coder has no room to learn
anything before the file is over.

Strictly held out: the prior corpus and the test files are disjoint by content
hash, and the test files are removed from the pool before any prior is built.
"""
import hashlib, os, random, subprocess, sys, time, lzma, json
from pathlib import Path

CRUSHCM = "/home/claude/crush/engine/crushcm"
WORK = Path("/home/claude/crush/lab/v4/atlas2/work"); WORK.mkdir(parents=True, exist_ok=True)

def collect(roots, lo, hi):
    out = []
    for r in roots:
        for dp, _, fs in os.walk(r):
            for f in fs:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dp, f)
                try:
                    b = open(p, "rb").read()
                except OSError:
                    continue
                if lo <= len(b) <= hi:
                    out.append((p, b))
    return out

ROOTS = ["/usr/lib/python3.11", "/usr/lib/python3/dist-packages"]
print("collecting…", flush=True)
pool = collect(ROOTS, 300, 400_000)
random.Random(4).shuffle(pool)

# ---- test set: small files, the case shipped knowledge should transform ----
TEST_N = 20
test, seen = [], set()
for p, b in pool:
    if 800 <= len(b) <= 12000:
        h = hashlib.sha256(b).hexdigest()
        if h in seen:
            continue
        seen.add(h); test.append((p, b))
    if len(test) == TEST_N:
        break
test_hashes = {hashlib.sha256(b).hexdigest() for _, b in test}
test_names  = {os.path.basename(p) for p, _ in test}

# ---- prior pool: everything else, disjoint by content hash AND by name ----
prior_pool = []
for p, b in pool:
    h = hashlib.sha256(b).hexdigest()
    if h in test_hashes or os.path.basename(p) in test_names:
        continue
    prior_pool.append(b)

total_test = sum(len(b) for _, b in test)
print(f"test:  {len(test)} files, {total_test:,} B  "
      f"(median {sorted(len(b) for _,b in test)[len(test)//2]:,} B)")
print(f"prior pool: {len(prior_pool)} files, {sum(len(b) for b in prior_pool)/1e6:.1f} MB "
      f"(disjoint by content hash and filename)\n", flush=True)

def build_prior(nbytes):
    out, tot = [], 0
    for b in prior_pool:
        out.append(b); tot += len(b)
        if tot >= nbytes:
            break
    blob = b"\n".join(out)[:nbytes]
    p = WORK / f"prior_{nbytes}.bin"
    p.write_bytes(blob)
    return str(p), len(blob)

def crushcm_size(data, prior=None):
    i = WORK / "in.bin"; o = WORK / "out.bin"
    i.write_bytes(data)
    cmd = [CRUSHCM, "c", str(i), str(o)] + (["-p", prior] if prior else [])
    subprocess.run(cmd, check=True, capture_output=True)
    n = o.stat().st_size
    # prove it round-trips with the same prior
    d = WORK / "back.bin"
    subprocess.run([CRUSHCM, "d", str(o), str(d)] + (["-p", prior] if prior else []),
                   check=True, capture_output=True)
    assert d.read_bytes() == data, "ROUND-TRIP FAILED"
    return n

# ---- reference points that ship no knowledge -------------------------------
xz_tot   = sum(len(lzma.compress(b, preset=9 | lzma.PRESET_EXTREME)) for _, b in test)
print(f"{'shipped knowledge':>18}   {'bytes':>9}  {'of original':>11}  {'vs xz':>8}   {'sec':>6}")
print("-" * 66)
print(f"{'xz -9e (none)':>18}   {xz_tot:>9,}  {100*xz_tot/total_test:>10.2f}%  {'—':>8}")

rows = []
for mb in (0, 1, 2, 4, 8, 16):
    n = mb * 1024 * 1024
    prior = None
    if n:
        prior, real = build_prior(n)
    t0 = time.time()
    tot = sum(crushcm_size(b, prior) for _, b in test)
    dt = time.time() - t0
    label = "none (crushcm)" if mb == 0 else f"{mb} MB"
    print(f"{label:>18}   {tot:>9,}  {100*tot/total_test:>10.2f}%  "
          f"{100*(tot-xz_tot)/xz_tot:>7.1f}%  {dt:>6.1f}", flush=True)
    rows.append({"mb": mb, "bytes": tot, "pct": 100*tot/total_test, "sec": dt})

json.dump({"total_test": total_test, "xz": xz_tot, "rows": rows},
          open("/home/claude/crush/lab/v4/atlas2/curve.json", "w"), indent=1)
print(f"\noriginal: {total_test:,} B in {len(test)} files")
