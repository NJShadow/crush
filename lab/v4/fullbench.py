"""v3 vs v4 on a whole corpus, as one archive - the regression check that
matters: nothing anywhere may get bigger or stop round-tripping."""
import subprocess, sys, tempfile, time, os
from pathlib import Path

V3 = "/mnt/user-data/outputs/Crush/engine/crush_engine.py"
V4 = "/home/claude/crush/engine/crush_engine.py"
SRC, TIER = sys.argv[1], sys.argv[2]
raw = sum(os.path.getsize(os.path.join(dp, f))
          for dp, _, fs in os.walk(SRC) for f in fs)

def run(engine):
    with tempfile.TemporaryDirectory() as td:
        arc = Path(td) / "a.crush"
        t0 = time.time()
        subprocess.run([sys.executable, engine, "c", str(arc), SRC, "--tier", TIER],
                       check=True, capture_output=True)
        enc = time.time() - t0
        out = Path(td) / "o"
        subprocess.run([sys.executable, engine, "x", str(arc), str(out)],
                       check=True, capture_output=True)
        base, bad = Path(SRC).name, []
        for dp, _, fs in os.walk(SRC):
            for f in fs:
                p = Path(dp) / f
                q = out / base / p.relative_to(SRC)
                if not q.exists() or q.read_bytes() != p.read_bytes():
                    bad.append(str(p))
        return arc.stat().st_size, enc, bad

a, ta, ba = run(V3)
b, tb, bb = run(V4)
print(f"{SRC}  tier={TIER}  raw {raw:,}")
print(f"  v3  {a:>12,}  {100*a/raw:6.2f}%  {ta:7.1f}s  "
      f"{'BYTE-EXACT' if not ba else 'FAIL ' + str(ba[:3])}")
print(f"  v4  {b:>12,}  {100*b/raw:6.2f}%  {tb:7.1f}s  "
      f"{'BYTE-EXACT' if not bb else 'FAIL ' + str(bb[:3])}")
print(f"  change {100*(b-a)/a:+.2f}%")
