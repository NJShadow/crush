"""Large archives for the size-limit test: N MB of incompressible files
(10 MB each, random bytes) at the quick tier, so the archive is ~N MB."""
import hashlib, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "engine"))
import crush_engine as ce
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
for mb in map(int, sys.argv[2:]):
    d = os.path.join(out, f"src{mb}"); os.makedirs(d, exist_ok=True)
    files = {}
    for i in range(mb // 10):
        p = os.path.join(d, f"part{i:03d}.bin")
        if not os.path.exists(p):
            with open(p, "wb") as f: f.write(os.urandom(10 << 20))
    arch = os.path.join(out, f"big{mb}.crush")
    r = ce.compress([d], arch, tier="quick")
    exp = {rel: {"sha": hashlib.sha256(open(fp, "rb").read()).hexdigest(), "size": os.path.getsize(fp)} for fp, rel in ce.collect([d])}
    json.dump({"files": exp}, open(arch[:-6] + ".json", "w"))
    print(mb, r["out"], flush=True)
