#!/usr/bin/env python3
import os as _os  # the Crush folder, wherever it is
CRUSH_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Baseline: what the market leaders actually achieve on the Crush corpus."""
import os, subprocess, time, json, shutil, tempfile

CORPUS = CRUSH_ROOT + "/corpus"
OUT = CRUSH_ROOT + "/results"
os.makedirs(OUT, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="crushbase_")

TOOLS = {
    "zip -9":        lambda i, o: ["zip", "-9", "-q", "-j", o, i],
    "gzip -9":       None,
    "bzip2 -9":      None,
    "zstd -19":      lambda i, o: ["zstd", "-19", "-q", "-f", "-o", o, i],
    "brotli -q11":   lambda i, o: ["brotli", "-q", "11", "-f", "-o", o, i],
    "xz -9e":        None,
    "7z -mx=9":      lambda i, o: ["7z", "a", "-t7z", "-mx=9", "-mmt=2", "-bso0", "-bsp0", o, i],
    "7z PPMd":       lambda i, o: ["7z", "a", "-t7z", "-m0=PPMd:mem=256m:o=16", "-mx=9", "-bso0", "-bsp0", o, i],
    "7z LZMA2+BCJ2": lambda i, o: ["7z", "a", "-t7z", "-mx=9", "-m0=BCJ2", "-m1=LZMA2:d64m", "-mmt=2", "-bso0", "-bsp0", o, i],
}

def run_size(cmd, outpath):
    if os.path.exists(outpath): os.remove(outpath)
    t = time.time()
    r = subprocess.run(cmd, capture_output=True)
    dt = time.time() - t
    if not os.path.exists(outpath): return None, dt
    return os.path.getsize(outpath), dt

def main():
    files = sorted(f for f in os.listdir(CORPUS) if os.path.isfile(os.path.join(CORPUS, f)))
    results = {}
    names = ["zip -9", "zstd -19", "brotli -q11", "xz -9e", "7z -mx=9", "7z PPMd", "7z LZMA2+BCJ2"]
    print(f"{'file':<26}{'orig':>12}" + "".join(f"{n:>15}" for n in names))
    print("-" * (38 + 15*len(names)))
    for f in files:
        src = os.path.join(CORPUS, f)
        orig = os.path.getsize(src)
        row = {"orig": orig}
        cells = []
        for n in names:
            o = os.path.join(TMP, f + {"zip -9": ".zip", "zstd -19": ".zst", "brotli -q11": ".br",
                                       "xz -9e": ".xz"}.get(n, ".7z"))
            if n == "xz -9e":
                if os.path.exists(o): os.remove(o)
                t = time.time()
                subprocess.run(f"xz -9e -T2 -c '{src}' > '{o}'", shell=True, capture_output=True)
                dt = time.time()-t
                sz = os.path.getsize(o) if os.path.exists(o) else None
            else:
                sz, dt = run_size(TOOLS[n](src, o), o)
            row[n] = {"size": sz, "sec": round(dt, 2)}
            cells.append(f"{(100*sz/orig if sz else 0):>13.1f}%" if sz else f"{'--':>14}")
            if os.path.exists(o): os.remove(o)
        results[f] = row
        print(f"{f:<26}{orig:>12,}" + "".join(f"{c:>15}" for c in cells))

    print("\n" + "="*100)
    tot = sum(r["orig"] for r in results.values())
    print(f"{'TOTAL CORPUS':<26}{tot:>12,}")
    summary = {}
    for n in names:
        s = sum(r[n]["size"] for r in results.values() if r[n]["size"])
        t = sum(r[n]["sec"] for r in results.values())
        summary[n] = {"total": s, "pct": round(100*s/tot, 2), "sec": round(t, 1)}
        print(f"  {n:<20} {s:>12,}  {100*s/tot:6.2f}% of original   {t:7.1f}s")
    json.dump({"files": results, "summary": summary, "corpus_total": tot},
              open(os.path.join(OUT, "baseline.json"), "w"), indent=1)
    shutil.rmtree(TMP, ignore_errors=True)

if __name__ == "__main__":
    main()
