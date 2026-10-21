"""Structure-aware fuzzing of crushpf. 'r' cases keep the framing valid and
mutate the correction record (mostly) or the unpacked data, so the preflate
reencoder itself is exercised; 'd' cases mutate raw deflate streams, which
is what a hostile file dropped in for compression looks like."""
import os, random, struct, subprocess, sys, time
sys.path.insert(0, "/home/claude/lab")
from fuzz_helpers import mutate

def load_pairs():
    b = open("/tmp/pf_lin.out", "rb").read(); p = 0; pairs = []
    while p < len(b):
        p += 1
        n = struct.unpack_from("<Q", b, p)[0]; p += 8; un = b[p:p + n]; p += n
        m = struct.unpack_from("<Q", b, p)[0]; p += 8; d = b[p:p + m]; p += m
        if len(un) < 300000:
            pairs.append((d, un))
    return pairs

def load_streams():
    b = open("/tmp/pf_in.bin", "rb").read(); p = 0; out = []
    while p < len(b):
        n = struct.unpack_from("<Q", b, p)[0]; p += 8
        if n < 150000: out.append(b[p:p + n])
        p += n
    return out

def run(helper, mode, cases, seed):
    rng = random.Random(seed)
    pairs, streams = load_pairs(), load_streams()
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:exitcode=99",
               UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1:exitcode=98")
    bad = hangs = 0; t0 = time.time()
    for k in range(cases):
        if mode == "r":
            d, un = rng.choice(pairs)
            if rng.random() < 0.85: d = mutate(rng, d)
            else: un = mutate(rng, un)
            body = struct.pack("<Q", len(d)) + d + struct.pack("<Q", len(un)) + un
        else:
            s = mutate(rng, rng.choice(streams))
            body = struct.pack("<Q", len(s)) + s
        try:
            r = subprocess.run([helper, mode], input=body, capture_output=True, timeout=60, env=env)
        except subprocess.TimeoutExpired:
            hangs += 1; open(f"/tmp/pfz_hang_{mode}_{k}.bin", "wb").write(body); continue
        if r.returncode != 0:
            bad += 1; open(f"/tmp/pfz_crash_{mode}_{k}.bin", "wb").write(body)
            print("FINDING", k, r.returncode, r.stderr.decode(errors="replace")[:600], flush=True)
    print(f"{mode}: {cases} cases, {bad} crashes/sanitizer reports, {hangs} hangs, {time.time()-t0:.0f}s", flush=True)

if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
