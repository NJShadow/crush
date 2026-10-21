#!/usr/bin/env python3
import os as _os  # the Crush folder, wherever it is
CRUSH_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Assemble the final benchmark report data from every measurement taken."""
import os, sys, json, subprocess
sys.path.insert(0, CRUSH_ROOT + "/engine")
import crush_engine as ce

R = CRUSH_ROOT + "/results"
CORPUS = CRUSH_ROOT + "/corpus"

base = json.load(open(f"{R}/baseline.json"))
idx = ce.read_index(f"{R}/corpus_crush.crush")
crush_size = os.path.getsize(f"{R}/corpus_crush.crush")
total = base["corpus_total"]

solid = {}
for name, f in [("ZIP -9", "solid.zip"), ("tar + Zstandard -19", "solid.tar.zst"),
                ("tar + xz -9e", "solid.tar.xz"), ("7-Zip -mx=9 (solid)", "solid.7z")]:
    p = f"{R}/{f}"
    if os.path.exists(p):
        solid[name] = os.path.getsize(p)
solid["Crush (crush tier)"] = crush_size

# per-file: original size, class, best competitor, crush contribution
per_class = {}
for f in idx["files"]:
    c = per_class.setdefault(f["cls"], {"n": 0, "in": 0, "out": 0, "transforms": {},
                                        "codec": "", "exact": True, "files": []})
    c["n"] += 1; c["in"] += f["s"]; c["files"].append(f["p"])
    for st in f["t"]:
        c["transforms"][st["n"]] = c["transforms"].get(st["n"], 0) + 1
    if not f.get("ex", True):
        c["exact"] = False
for b in idx["bundles"]:
    c = per_class.setdefault(b["cls"], {"n": 0, "in": 0, "out": 0, "transforms": {},
                                        "codec": "", "exact": True, "files": []})
    c["out"] += b["clen"]
    c["codec"] = ce.CODECS[b["codec"]][2]
    c["trial"] = [{"codec": ce.CODECS[k][2], "size": v} for k, v in b.get("trial", [])]

# competitor totals restricted to the same files, from the per-file baseline
comp_names = ["zip -9", "zstd -19", "brotli -q11", "xz -9e", "7z -mx=9", "7z PPMd"]
for cls, c in per_class.items():
    for cn in comp_names:
        s = 0
        for fn in c["files"]:
            b = base["files"].get(os.path.basename(fn))
            if b and b.get(cn, {}).get("size"):
                s += b[cn]["size"]
        c.setdefault("competitors", {})[cn] = s

rows = []
for k, v in sorted(per_class.items(), key=lambda kv: -kv[1]["in"]):
    if not v["in"]:
        continue
    best_comp = min((s for s in v.get("competitors", {}).values() if s), default=0)
    rows.append({
        "cls": k, "label": ce.CLASS_LABELS.get(k, k), "n": v["n"],
        "in": v["in"], "out": v["out"], "pct": 100.0 * v["out"] / v["in"],
        "codec": v["codec"],
        "transforms": [t for t in v["transforms"]],
        "exact": v["exact"],
        "best_competitor": best_comp,
        "vs_best": (100.0 * (best_comp - v["out"]) / best_comp) if best_comp else 0.0,
        "competitors": v.get("competitors", {}),
        "trial": v.get("trial", []),
    })

out = {
    "corpus_total": total,
    "crush_total": crush_size,
    "crush_pct": 100.0 * crush_size / total,
    "solid": solid,
    "per_file_baseline": base["summary"],
    "rows": rows,
    "stats": idx["stats"],
    "entropy": json.load(open(f"{R}/entropy_wall.json")),
    "file_count": len(idx["files"]),
}
json.dump(out, open(f"{R}/report.json", "w"), indent=1)

print(f"CORPUS {total:,} bytes, {len(idx['files'])} files\n")
print(f"{'method':<26}{'size':>14}{'% of original':>16}")
for k, v in sorted(solid.items(), key=lambda kv: -kv[1]):
    print(f"{k:<26}{v:>14,}{100*v/total:>15.2f}%")
best = min(v for k, v in solid.items() if k != "Crush (crush tier)")
print(f"\nCrush is {100*(best-crush_size)/best:.1f}% smaller than the best competitor")
print(f"Crush is {100*(solid['ZIP -9']-crush_size)/solid['ZIP -9']:.1f}% smaller than ZIP\n")
print(f"{'file type':<26}{'n':>4}{'before':>14}{'after':>13}{'%':>8}{'best rival':>13}{'vs rival':>10}")
for r in rows:
    print(f"{r['label']:<26}{r['n']:>4}{r['in']:>14,}{r['out']:>13,}{r['pct']:>7.1f}%"
          f"{r['best_competitor']:>13,}{r['vs_best']:>9.1f}%")
