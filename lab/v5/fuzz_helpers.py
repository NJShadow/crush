"""Mutation fuzzing of the helpers' DECODE paths - the side that reads data out
of an archive, which may come from someone else. Each case is run in its own
process under AddressSanitizer + UBSan with a timeout; any sanitizer report,
crash signal or hang is a finding."""
import os, random, struct, subprocess, sys, time

def recs(b):
    p, out = 0, []
    while p < len(b):
        s = b[p]; p += 1
        if s:
            n = struct.unpack_from("<Q", b, p)[0]; p += 8
            out.append(b[p:p + n]); p += n
        else:
            out.append(None)
    return out

def mutate(rng, d):
    d = bytearray(d)
    kind = rng.randrange(6)
    n = len(d)
    if kind == 0:                                   # flip bits
        for _ in range(rng.randint(1, 8)):
            i = rng.randrange(n); d[i] ^= 1 << rng.randrange(8)
    elif kind == 1:                                 # random bytes in a run
        i = rng.randrange(n); L = rng.randint(1, 64)
        d[i:i + L] = os.urandom(min(L, n - i))
    elif kind == 2:                                 # truncate
        d = d[:rng.randrange(1, n)]
    elif kind == 3:                                 # flip in the first 64 bytes (headers)
        for _ in range(rng.randint(1, 4)):
            i = rng.randrange(min(64, n)); d[i] = rng.randrange(256)
    elif kind == 4:                                 # duplicate a slice
        i = rng.randrange(n); j = rng.randrange(n)
        d = d[:i] + d[j:j + rng.randint(1, 256)] + d[i:]
    else:                                           # extreme values
        i = rng.randrange(n); d[i] = rng.choice([0, 0xFF, 0x7F, 0x80])
    return bytes(d)

def main(helper, mode, corpus_file, cases, seed):
    raw = mode.endswith(":raw")
    mode = mode.split(":")[0]
    rng = random.Random(seed)
    seeds = [r for r in recs(open(corpus_file, "rb").read()) if r]
    findings = 0; hangs = 0; t0 = time.time()
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:abort_on_error=0:exitcode=99",
               UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1:exitcode=98")
    for k in range(cases):
        d = mutate(rng, rng.choice(seeds))
        body = d if raw else struct.pack("<Q", len(d)) + d
        try:
            r = subprocess.run([helper, mode], input=body, capture_output=True, timeout=60, env=env)
        except subprocess.TimeoutExpired:
            hangs += 1
            open(f"/tmp/fuzz_hang_{mode}_{k}.bin", "wb").write(d)
            continue
        if r.returncode != 0:
            findings += 1
            open(f"/tmp/fuzz_crash_{mode}_{k}.bin", "wb").write(d)
            print("FINDING", k, r.returncode, r.stderr.decode(errors="replace")[:1500])
    print(f"{cases} cases, {findings} crashes/sanitizer reports, {hangs} hangs, {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
