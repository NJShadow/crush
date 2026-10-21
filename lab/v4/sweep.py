#!/usr/bin/env python3
"""Quick hyper-parameter sweep for the recurrent mixer, on small corpus slices."""
import os, subprocess, sys, time

D = "/home/claude/crush/lab/v4/mixer"
FILES = ["text", "json", "elf", "py"]
REF = {}

def build(name, defs, src="crushcm2.c", opt="-O2"):
    exe = f"{D}/tmp/sw_{name}"
    cmd = ["gcc", opt, "-Wno-misleading-indentation"] + [f"-D{k}={v}" for k, v in defs.items()] + \
          ["-o", exe, f"{D}/{src}", "-lm"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        print(f"{name}: BUILD FAIL\n{r.stderr.decode()[:800]}"); return None
    return exe

def measure(exe):
    res, tsum = {}, 0.0
    for f in FILES:
        src, out = f"{D}/tmp/q_{f}", f"{D}/tmp/sw.out"
        t0 = time.time(); subprocess.run([exe, "c", src, out], check=True); t1 = time.time()
        res[f] = os.path.getsize(out); tsum += t1 - t0
    return res, tsum

def report(name, res, tsum):
    tot = sum(res.values()); reft = sum(REF[f] for f in FILES)
    parts = " ".join(f"{f}:{res[f]}({(res[f]-REF[f])/REF[f]*100:+.2f}%)" for f in FILES)
    print(f"{name:34s} TOT={tot} ({(tot-reft)/reft*100:+.3f}%)  {tsum:5.1f}s  {parts}", flush=True)
    return tot

def main():
    global REF
    ref = build("ref", {}, src="crushcm_base_ref.c")
    REF, t = measure(ref)
    print(f"{'BASELINE (current crushcm)':34s} TOT={sum(REF.values())} ( +0.000%)  {t:5.1f}s  " +
          " ".join(f"{f}:{REF[f]}" for f in FILES), flush=True)
    for spec in CONFIGS:
        name, defs = spec
        safe = "".join(ch if ch.isalnum() else "_" for ch in name)
        exe = build(safe, defs)
        if not exe: continue
        res, tsum = measure(exe)
        report(name, res, tsum)
        os.remove(exe)

CONFIGS = []
if __name__ == "__main__":
    import json as _j
    CONFIGS = _j.loads(sys.argv[1]) if len(sys.argv) > 1 else []
    CONFIGS = [(c[0], c[1]) for c in CONFIGS]
    main()
