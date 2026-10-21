"""v3 vs v4, file by file, with the same tier and a byte-exact check on both."""
import os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

V3 = "/mnt/user-data/outputs/Crush/engine/crush_engine.py"
V4 = "/home/claude/crush/engine/crush_engine.py"
TIER = sys.argv[1] if len(sys.argv) > 1 else "balanced"
FILES = sys.argv[2:]

def run(engine, src, tier):
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "in"; d.mkdir()
        shutil.copy(src, d / Path(src).name)
        arc = Path(td) / "a.crush"
        t0 = time.time()
        subprocess.run([sys.executable, engine, "c", str(arc), str(d), "--tier", tier],
                       check=True, capture_output=True)
        enc = time.time() - t0
        out = Path(td) / "out"
        subprocess.run([sys.executable, engine, "x", str(arc), str(out)],
                       check=True, capture_output=True)
        back = out / "in" / Path(src).name
        ok = back.read_bytes() == Path(src).read_bytes()
        return arc.stat().st_size, enc, ok

print(f"tier={TIER}")
print(f"{'file':<26}{'bytes':>12}{'v3':>12}{'v4':>12}{'change':>9}  {'v3 s':>6}{'v4 s':>7}  exact")
print("-" * 92)
tv3 = tv4 = traw = 0
for f in FILES:
    raw = os.path.getsize(f)
    a, ta, oka = run(V3, f, TIER)
    b, tb, okb = run(V4, f, TIER)
    tv3 += a; tv4 += b; traw += raw
    print(f"{Path(f).name:<26}{raw:>12,}{a:>12,}{b:>12,}{100*(b-a)/a:>8.2f}%  "
          f"{ta:>6.1f}{tb:>7.1f}  {'PASS' if oka and okb else 'FAIL'}", flush=True)
print("-" * 92)
print(f"{'TOTAL':<26}{traw:>12,}{tv3:>12,}{tv4:>12,}{100*(tv4-tv3)/tv3:>8.2f}%")
