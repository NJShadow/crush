"""
gendata.py - constructed test material for the Crush v4 domain codecs.

Everything here is SYNTHETIC and is honestly labelled as such in RESULTS.md.
The generators are written to reproduce the *statistics* that make each domain
what it is (exponent clustering, autocorrelated quality scores, templated read
identifiers, sequencing coverage, decimal-valued prices), not to be pretty.

Files are kept under ~20 MB each; the machine has 2 shared cores.
"""
import os
import numpy as np

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT, exist_ok=True)


def w(name, b):
    p = os.path.join(OUT, name)
    with open(p, "wb") as f:
        f.write(b)
    print(f"  {name:28s} {len(b):>12,} B")
    return p


# ------------------------------------------------------------------ floats

def make_floats(seed=7):
    rng = np.random.default_rng(seed)

    # 1. smooth 2-D scientific field (a temperature/pressure field on a grid)
    H, Wd = 700, 900
    y = np.linspace(0, 6, H)[:, None]
    x = np.linspace(0, 8, Wd)[None, :]
    f = (280.0 + 12.0 * np.sin(x) * np.cos(0.7 * y)
         + 3.0 * np.exp(-((x - 4) ** 2 + (y - 3) ** 2) / 2.0)
         + 0.35 * np.sin(3.1 * x + 1.7 * y))
    f += 0.02 * rng.standard_normal((H, Wd))          # measurement noise
    w("field2d_f64.bin", f.astype("<f8").tobytes())
    w("field2d_be_f32.bin", f.astype(">f4").tobytes())

    # 2. noisy multi-channel sensor stream, float32, 8 interleaved channels
    N = 200000
    C = 8
    base = np.cumsum(rng.standard_normal((N, C)) * 0.02, axis=0)
    base += np.array([20.0, 1013.0, 55.0, 0.0, 3.3, -60.0, 9.81, 100.0])
    base[:, 3] = np.sin(np.arange(N) * 0.001)[:, None].repeat(1, 1)[:, 0] * 5
    base += rng.standard_normal((N, C)) * 0.005
    w("sensor_f32.bin", base.astype("<f4").tobytes())

    # 3. financial ticks: prices are decimals with 2 dp, sizes are integers
    T = 300000
    px = 100.0 + np.cumsum(rng.integers(-3, 4, T)) * 0.01
    px = np.round(px, 2)
    bid = np.round(px - 0.01, 2)
    ask = np.round(px + 0.01, 2)
    sz = rng.integers(1, 500, T).astype(np.float64) * 100
    tick = np.stack([px, bid, ask, sz], axis=1)
    w("ticks_f64.bin", tick.astype("<f8").tobytes())

    # 4. ML weight tensor, float32, roughly normal
    Wt = (rng.standard_normal(2000000) * 0.02).astype("<f4")
    w("weights_f32.bin", Wt.tobytes())

    # 5. edge cases: NaN payloads, -0.0, denormals, infinities
    n = 200000
    a = rng.standard_normal(n)
    u = a.view(np.uint64).copy()
    idx = rng.permutation(n)
    k = n // 20
    # quiet + signalling NaNs with distinct payloads
    u[idx[:k]] = 0x7FF8000000000000 | rng.integers(0, 1 << 40, k, dtype=np.uint64)
    u[idx[k:2 * k]] = 0xFFF0000000000001 + rng.integers(0, 1 << 30, k, dtype=np.uint64)
    u[idx[2 * k:3 * k]] = 0x8000000000000000          # -0.0
    u[idx[3 * k:4 * k]] = 0x0000000000000000          # +0.0
    u[idx[4 * k:5 * k]] = rng.integers(1, 1 << 52, k, dtype=np.uint64)   # denormals
    u[idx[5 * k:5 * k + 100]] = 0x7FF0000000000000    # +inf
    u[idx[5 * k + 100:5 * k + 200]] = 0xFFF0000000000000  # -inf
    w("edge_f64.bin", u.view("<f8").astype("<f8").tobytes())

    a32 = rng.standard_normal(400000).astype(np.float32)
    u32 = a32.view(np.uint32).copy()
    m = 400000
    i2 = rng.permutation(m)
    k2 = m // 20
    u32[i2[:k2]] = 0x7FC00000 | rng.integers(0, 1 << 20, k2, dtype=np.uint32)
    u32[i2[k2:2 * k2]] = 0x80000000
    u32[i2[2 * k2:3 * k2]] = rng.integers(1, 1 << 23, k2, dtype=np.uint32)
    u32[i2[3 * k2:3 * k2 + 50]] = 0x7F800000
    w("edge_f32.bin", u32.view("<f4").tobytes())

    # 6. float64 holding values that are exactly float32 (very common)
    v = (rng.standard_normal(700000).astype(np.float32)).astype(np.float64)
    w("widened_f64.bin", v.astype("<f8").tobytes())


# ----------------------------------------------------------------- genomics

BASES = np.frombuffer(b"ACGT", dtype=np.uint8)


def _markov_genome(rng, n, order=3):
    """A sequence with realistic local structure: a random order-k Markov
    chain over ACGT with a GC bias, plus interspersed repeat families."""
    k = 4 ** order
    P = rng.dirichlet(np.array([0.9, 0.6, 0.6, 0.9]) * 1.4, size=k)
    Pc = np.cumsum(P, axis=1)
    seq = np.empty(n, dtype=np.uint8)
    ctx = 0
    step = 1 << 20
    pos = 0
    while pos < n:
        m = min(step, n - pos)
        r = rng.random(m)
        out = np.empty(m, dtype=np.uint8)
        c = ctx
        # this loop is the honest cost of a real Markov chain; kept to 1 MB blocks
        cs = Pc
        for i in range(m):
            row = cs[c]
            s = 0
            x = r[i]
            if x > row[0]:
                s = 1
                if x > row[1]:
                    s = 2
                    if x > row[2]:
                        s = 3
            out[i] = s
            c = ((c << 2) | s) & (k - 1)
        ctx = c
        seq[pos:pos + m] = out
        pos += m
    # repeat families (Alu-like): copy a 300 bp element to ~8% of the genome
    for _ in range(3):
        el = seq[rng.integers(0, n - 300):][:300].copy()
        cnt = int(n * 0.03 / 300)
        for _ in range(cnt):
            p = int(rng.integers(0, n - 300))
            mut = el.copy()
            nm = rng.integers(0, 20)
            if nm:
                mp = rng.integers(0, 300, nm)
                mut[mp] = rng.integers(0, 4, nm).astype(np.uint8)
            seq[p:p + 300] = mut
    return seq


def make_fasta(seed=11, n=10_000_000, line=60):
    rng = np.random.default_rng(seed)
    seq = _markov_genome(rng, n)
    chars = BASES[seq]
    # soft-masked (lowercase) repeat regions - present in every real reference
    mask = np.zeros(n, dtype=bool)
    for _ in range(400):
        p = int(rng.integers(0, n - 5000))
        L = int(rng.integers(200, 5000))
        mask[p:p + L] = True
    chars = np.where(mask, chars + 32, chars)
    # runs of N (assembly gaps)
    for _ in range(60):
        p = int(rng.integers(0, n - 20000))
        L = int(rng.integers(100, 20000))
        chars[p:p + L] = ord("N")

    out = bytearray()
    per_rec = n // 4
    for c in range(4):
        s = chars[c * per_rec:(c + 1) * per_rec]
        out += (f">chr{c+1} synthetic contig length={len(s)}\n").encode()
        for i in range(0, len(s), line):
            out += s[i:i + line].tobytes()
            out += b"\n"
    w("ref.fasta", bytes(out))
    return seq


def make_fastq(seed=13, nreads=75000, rlen=100, genome=None):
    rng = np.random.default_rng(seed)
    if genome is None:
        genome = _markov_genome(rng, 2_000_000)
    G = genome.size

    # --- read positions (sequencing coverage => genuine cross-read redundancy)
    pos = rng.integers(0, G - rlen, nreads)
    idx = pos[:, None] + np.arange(rlen)[None, :]
    reads = genome[idx]
    rc = rng.random(nreads) < 0.5                      # half on the minus strand
    revd = 3 - reads[rc][:, ::-1]
    reads[rc] = revd

    # --- quality scores: declining with cycle, autocorrelated, skewed
    cycle = np.arange(rlen)
    base_q = 38.0 - 0.055 * cycle - 0.0009 * cycle ** 2
    ar = np.empty((nreads, rlen), dtype=np.float32)
    e = rng.standard_normal((nreads, rlen)).astype(np.float32) * 2.2
    read_off = rng.standard_normal(nreads).astype(np.float32) * 2.0
    prev = np.zeros(nreads, dtype=np.float32)
    for c in range(rlen):
        prev = 0.82 * prev + e[:, c]                   # AR(1), rho = 0.82
        ar[:, c] = prev
    q = base_q[None, :] + ar + read_off[:, None]
    # a fraction of reads go bad partway through (adapter / bubble)
    bad = rng.random(nreads) < 0.05
    cut = rng.integers(rlen // 3, rlen, nreads)
    dropmask = (np.arange(rlen)[None, :] >= cut[:, None]) & bad[:, None]
    q = np.where(dropmask, q - 22.0, q)
    q = np.clip(np.rint(q), 2, 41).astype(np.uint8)

    # --- sequencing errors, more likely where quality is low
    perr = 10.0 ** (-q.astype(np.float32) / 10.0)
    err = rng.random((nreads, rlen)) < perr
    ne = int(err.sum())
    if ne:
        reads[err] = (reads[err] + rng.integers(1, 4, ne).astype(np.uint8)) & 3
    # a few no-calls
    ncall = rng.random((nreads, rlen)) < 0.0006
    seq = BASES[reads]
    seq[ncall] = ord("N")
    q[ncall] = 2

    quals = (q + 33).astype(np.uint8)

    # --- identifiers: Illumina template
    tile = rng.integers(1101, 1125, nreads)
    xs = rng.integers(1000, 32000, nreads)
    ys = np.sort(rng.integers(1000, 32000, nreads))    # y is nearly sorted in real runs
    lane = rng.integers(1, 5, nreads)

    out = bytearray()
    nl = b"\n"
    for i in range(nreads):
        out += (b"@SIM-A00123:41:HGVKJDSXX:%d:%d:%d:%d 1:N:0:ATTCAGAA+GGCTCTGA\n"
                % (lane[i], tile[i], xs[i], ys[i]))
        out += seq[i].tobytes(); out += nl
        out += b"+\n"
        out += quals[i].tobytes(); out += nl
    w("reads.fastq", bytes(out))


def make_vcf(seed=17, nvar=200000):
    rng = np.random.default_rng(seed)
    pos = np.cumsum(rng.integers(1, 900, nvar))
    ref = BASES[rng.integers(0, 4, nvar)]
    alt = BASES[rng.integers(0, 4, nvar)]
    qual = np.round(rng.gamma(4, 40, nvar), 1)
    dp = rng.poisson(35, nvar) + 1
    af = np.round(rng.beta(1.2, 3, nvar), 3)
    gts = np.array([b"0/1", b"1/1", b"0/0", b"1/2"], dtype="S3")
    gt = gts[rng.choice(4, nvar, p=[0.55, 0.3, 0.1, 0.05])]
    out = bytearray(b"##fileformat=VCFv4.2\n##source=synthetic\n"
                    b"##contig=<ID=chr1,length=248956422>\n"
                    b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE1\n")
    for i in range(nvar):
        out += (b"chr1\t%d\t.\t%c\t%c\t%.1f\tPASS\tDP=%d;AF=%.3f\tGT:DP\t%s:%d\n"
                % (pos[i], ref[i], alt[i], qual[i], dp[i], af[i], gt[i], dp[i]))
    w("calls.vcf", bytes(out))


if __name__ == "__main__":
    print("floats:")
    make_floats()
    print("genomics:")
    g = make_fasta()
    make_fastq(genome=g[:3_000_000])
    make_vcf()
