"""Size cost of portable=True: same inputs, same tier, with and without."""
import os, sys, time, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "engine"))
import crush_engine as ce
C, C2, B = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "corpus", ""), os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "corpus2", ""), "/home/claude/bench/"
SETS = {"smallfiles(300 .py)": [C2 + "smallfiles"],
        "docs(xml+docx+pdf+txt)": [C + "data_xml.xml", C + "office_report.docx", B + "compat/lo_readme.pdf", B + "compat/readme.docx"],
        "one small .py": [C2 + "smallfiles/0002_core.py"]}
out = sys.argv[1]; tiers = sys.argv[2:] or ["crush", "singularity"]
os.makedirs(out, exist_ok=True)
for name, paths in SETS.items():
    for tier in tiers:
        row = {"set": name, "tier": tier}
        for port in (False, True):
            p = os.path.join(out, f"{abs(hash(name))%10000}-{tier}-{port}.crush")
            t0 = time.time()
            r = ce.compress(paths, p, tier=tier, portable=port)
            idx = ce.read_index(p)
            row["portable" if port else "normal"] = r["out"]
            row[("p" if port else "n") + "_codecs"] = sorted({b["codec"] for b in idx["bundles"]})
            row[("p" if port else "n") + "_s"] = round(time.time() - t0, 1)
            row["in"] = r["in"]
        row["cost_pct"] = round(100 * (row["portable"] / row["normal"] - 1), 2)
        print(json.dumps(row), flush=True)
