"""Builds (original, payload, aux) triples for every portable transform, from
real files and from inputs constructed to reach each code path. The JS port
must invert every payload back to the original, byte for byte.

    python3 tests/gen_transform_vectors.py OUTDIR
"""
import json, os, random, struct, sys, glob, hashlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "engine"))
import numpy as np
import crush_engine as ce
import crush_vector as cv
import crush_genome as cg
import crush_loom as cl
from crush_oracle import Oracle
from crush_prism import Prism

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
rng = random.Random(7)
nrng = np.random.default_rng(7)
manifest = []
stats = {}

def add(name, tname, data, res):
    if res is None:
        print("  (not applicable)", name, tname); return False
    payload, aux = res
    T = ce.TRANSFORMS[tname]
    if T.inverse(payload, aux) != data:
        print("  PYTHON INVERSE MISMATCH", name); return False
    k = f"{len(manifest):04d}"
    open(os.path.join(OUT, k + ".in"), "wb").write(data)
    open(os.path.join(OUT, k + ".pl"), "wb").write(payload)
    manifest.append({"k": k, "name": name, "t": tname,
                     "aux": json.dumps(aux, separators=(",", ":"))})
    stats[tname] = stats.get(tname, 0) + 1
    return True

def tf(name, tname, data, **kw):
    T = ce.TRANSFORMS[tname]
    try:
        res = T.forward(data, **kw)
    except Exception as e:
        print("  forward raised", name, e); res = None
    return add(name, tname, data, res)

def head(p, n):
    with open(p, "rb") as f:
        return f.read(n)

C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "corpus", "")
C2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "corpus2", "")

# ---------------- Oracle ----------------
for dt in ["<u1", "<u2", "<u4", "<u8", "<i2", "<i4", "<i8", ">u2", ">u4"]:
    w = np.dtype(dt).itemsize
    n = 20000
    i = np.arange(n, dtype=object)
    for kind, vals in [
        ("poly1", [7 + 3 * x for x in range(n)]),
        ("poly3", [5 - 2 * x + 3 * x * x - x ** 3 for x in range(n)]),
        ("modlin", [(11 + 7 * x) % 1000 for x in range(n)]),
        ("lin2", None), ("lcg", None)]:
        if kind == "lin2":
            v = [1, 1]
            for _ in range(n - 2):
                v.append(v[-1] + v[-2] + 3)
            vals = v
        if kind == "lcg":
            x, vals = 12345, []
            for _ in range(n):
                vals.append(x); x = (1103515245 * x + 12345) % (1 << (8 * w))
        mask = (1 << (8 * w)) - 1
        u = [v & mask for v in vals]
        data = np.array(u, dtype=np.uint64).astype(f"<u{w}" if dt[0] == "<" else f">u{w}").tobytes() \
            if w < 8 else b"".join(struct.pack("<Q" if dt[0] == "<" else ">Q", x) for x in u)
        tf(f"oracle {kind} {dt}", "oracle", data + b"t" * (w - 1))
data = b"\x00" * 50000; tf("oracle const", "oracle", data)
data = b"abcdefg123" * 9000; tf("oracle period", "oracle", data)
data = b"".join(b"row %d id=%07d t=%d\n" % (k, 3 * k + 1, 1000 + k * k) for k in range(3000))
tf("oracle template", "oracle", data)
data = b"".join(b"%d,%d\n" % (10 ** 20 + k * 10 ** 18, 5 * k) for k in range(2000))
tf("oracle template bigint", "oracle", data)
for name in ("auto_increment_ids.bin", "event_timestamps.bin", "prng_stream.bin", "repeated_block.bin", "zero_padding.bin"):
    tf("oracle corpus2/gen/" + name, "oracle", open(C2 + "gen/" + name, "rb").read())

for dt in ("<u4", "<i8", ">u4", "<u2"):
    tf("oracle modlin real " + dt, "oracle", np.array([(11 + 7 * x) % 300 for x in range(1100)], dtype=dt).tobytes())
tf("oracle modlin negative c0 <i8", "oracle", np.array([(-50 + 7 * x) % 300 for x in range(1100)], dtype="<i8").tobytes())
for prog in ({"k": "poly", "dt": "<u8", "c": [2 ** 62 - 5, 3 * 2 ** 40, 7], "n": 3000},
             {"k": "poly", "dt": "<i8", "c": [-(2 ** 62), -12345, 99, -1], "n": 3000},
             {"k": "poly", "dt": "<i4", "c": [2 ** 31 - 7, 3], "n": 3000},
             {"k": "linrec", "dt": "<i8", "a": [3, -2, 2 ** 40], "i": [2 ** 62, -(2 ** 61)], "n": 3000},
             {"k": "lcg", "dt": "<i8", "a": 6364136223846793005, "c": 1442695040888963407, "s": -77, "n": 3000},
             {"k": "modlin", "dt": "<u1", "c0": 5, "step": 3, "M": 251, "n": 9000}):
    try:
        data = Oracle.generate(dict(prog, _tail=b"zz"))
    except Exception as e:
        print("  python cannot generate", prog["k"], prog["dt"], e); continue
    add("oracle handmade " + prog["k"] + " " + prog["dt"], "oracle", data,
        (b"zz", {"p": json.dumps(dict(prog, _tail_n=2), separators=(",", ":"))}))
# ---------------- Prism ----------------
for p, n in [(C + "data_records.json", 3 << 20), (C + "data_records.csv", 3 << 20), (C + "data_xml.xml", 2 << 20),
             (C2 + "struct/accounts.csv", 2 << 20), (C2 + "struct/accounts.json", 2 << 20),
             (C2 + "struct/server.log", 2 << 20), (C2 + "struct/telemetry.csv", 2 << 20)]:
    d = head(p, n)
    # cut at a line end so the splitter sees whole rows
    d = d[:d.rfind(b"\n") + 1] if b"\n" in d else d
    tf("prism " + os.path.basename(p), "prism", d)
rows = [b"ts,dev,val,big,neg,dec"]
for k in range(4000):
    rows.append(b"%d,%d,%d,%d,%d,%d.%02d" % (1600000000 + 15 * k, k % 800, (k * k) % 977,
                (2 ** 62) + k * (2 ** 40) * (1 if k % 2 else -1), -5 * k, k, k % 100))
tf("prism synthetic laws csv", "prism", b"\r\n".join(rows) + b"\r\n")
import json as _j
objs = [{"id": k, "t": 1700000000 + 60 * k, "cyc": ["a", "b", "c"][k % 3], "x": 9223372036854775807 - k,
         "y": -9223372036854775807 + k * 3, "s": "v%d" % (k % 7), "f": k / 8} for k in range(3000)]
tf("prism synthetic json extremes", "prism", _j.dumps(objs, indent=1).encode())
tf("prism json compact", "prism", _j.dumps(objs, separators=(",", ":")).encode())

# ---------------- transpose ----------------
for w in (2, 4, 8):
    for walk in ("walk", "ramp"):
        n = 300000 // w
        if walk == "walk":
            v = np.cumsum(nrng.integers(-3, 4, n)).astype(np.int64) + 1000
        else:
            v = np.arange(n, dtype=np.int64) * 3 + (nrng.integers(0, 2, n))
        data = v.astype(f"<i{w}").tobytes() + b"xyz"
        tf(f"transpose {walk} w{w}", "transpose", data)
tf("transpose data_timeseries", "transpose", head(C + "data_timeseries.bin", 4 << 20))
tf("transpose image_raw16", "transpose", open(C + "image_raw16.bin", "rb").read())
# archives written before the zigzag fix: delta without zigzag
for w in (2, 4, 8):
    n = 80000 // w
    v = (np.arange(n, dtype=np.uint64) * np.uint64(7)).astype(f"<u{w}")
    dt = np.dtype(f"<u{w}")
    d = np.empty_like(v); d[0] = v[0]; d[1:] = v[1:] - v[:-1]
    payload = d.view(np.uint8).reshape(-1, w).T.tobytes() + b"!"
    data = v.tobytes() + b"!"
    add(f"transpose legacy no-zigzag w{w}", "transpose", data, (payload, {"w": w, "d": True, "n": len(data)}))

# ---------------- audio_predict ----------------
tf("audio_predict corpus wav", "audio_predict", head(C + "audio_music.wav", 3 << 20))
import wave, io
for ch in (1, 2):
    b = io.BytesIO()
    with wave.open(b, "wb") as wv:
        wv.setnchannels(ch); wv.setsampwidth(2); wv.setframerate(44100)
        t = np.arange(50000)
        s = (30000 * np.sin(t / 30.0) + nrng.integers(-200, 200, t.size)).astype(np.int16)
        if ch == 2:
            s = np.stack([s, (s // 2 + 7000).astype(np.int16)], axis=1)
        wv.writeframes(s.tobytes())
    tf(f"audio_predict synth ch{ch}", "audio_predict", b.getvalue() + b"LIST\x04\x00\x00\x00abcd")

# ---------------- floatarray ----------------
def fa(name, data):
    tf("floatarray " + name, "floatarray", data)
fa("data_floats.bin", head(C + "data_floats.bin", 4 << 20))
fa("data_timeseries.bin", head(C + "data_timeseries.bin", 4 << 20))
prices = np.round(4000 + np.cumsum(nrng.normal(0, 1, 200000)), 2)
fa("prices f64 decimal", prices.astype("<f8").tobytes())
fa("prices f32 decimal", prices.astype("<f4").tobytes())
p3 = np.round(nrng.normal(0, 50, 100000), 3); p3[::997] = np.nan; p3[5] = np.inf; p3[9] = -0.0; p3[11] = 1e-310
fa("decimal f64 with nan inf denormal", p3.astype("<f8").tobytes())
s32 = (np.sin(np.arange(200000) / 50.0) * 100).astype("<f4")
fa("sensor f32", s32.tobytes())
w64 = s32.astype("<f8"); w64[::1000] = 0.1; w64[7] = np.float64(np.float32(np.nan)); w64[13] = 3e-40
fa("narrow f64 from f32", w64.tobytes())
nanpay = np.frombuffer(struct.pack("<I", 0x7FC12345) * 1, dtype="<f4")
w64b = s32.astype("<f8").copy(); w64b[100] = np.float64(nanpay[0]); w64b[200] = np.float64(np.float32(1e-41))
fa("narrow f64 nan payload + subnormal", w64b.tobytes())
q = (np.round(nrng.normal(0, 1, 300000) * 64) / 64).astype("<f8")
fa("quantised f64 (trunc)", q.tobytes())
img = (np.add.outer(np.arange(512), np.arange(600)) * 0.25 + nrng.normal(0, 0.01, (512, 600))).astype("<f4")
fa("2d field f32 (lorenzo)", img.tobytes())
img64 = np.add.outer(np.sin(np.arange(700) / 40.0), np.cos(np.arange(500) / 30.0)).astype("<f8")
fa("2d field f64", img64.tobytes())
inter = np.stack([np.cumsum(nrng.normal(0, 1, 60000)), np.arange(60000) * 0.5, np.sin(np.arange(60000) / 9)], axis=1)
fa("3-channel interleaved f64", inter.astype("<f8").tobytes())
fa("3-channel interleaved f64 big-endian", inter.astype(">f8").tobytes())
fa("3-channel interleaved f32 + tail", inter.astype("<f4").tobytes() + b"\x01\x02\x03")
xs = nrng.normal(0, 1, 200000).astype("<f8"); xs = np.sort(xs)
fa("sorted f64 (xork/odelta)", xs.tobytes())
# force each simple mode directly through the channel coder
u64 = np.ascontiguousarray(img64.reshape(-1)).view("<u8")
for mo in (cv.M_RAW, cv.M_SPLIT, cv.M_XOR1, cv.M_XORK, cv.M_ODELTA1, cv.M_ODELTA2, cv.M_IDELTA1,
           cv.M_IDELTA2, cv.M_SEM, cv.M_LORENZO):
    for W, arr in ((8, u64[:50001]), (4, img.reshape(-1)[:50001].view("<u4"))):
        dt = np.dtype(f"<u{W}")
        try:
            s, c, x = cv._enc_body(np.ascontiguousarray(arr), mo, dt, 500 if mo == cv.M_LORENZO else 0)
        except Exception as e:
            print("enc", mo, W, e); continue
        chan = cv._pack_channel(mo, s, c, x)
        hdr = bytearray(cv.MAGIC); hdr.append(W); hdr.append(0)
        cv._put_varint(hdr, 1); cv._put_varint(hdr, arr.size); cv._put_varint(hdr, arr.size * W)
        payload = bytes(hdr) + cv.pack_streams([b"", b""]) + chan
        add(f"floatarray forced mode {mo} W{W}", "floatarray", arr.tobytes(), (payload, {"n": arr.size * W, "v": 1}))
for d in range(0, 10):
    vals = np.round(nrng.normal(0, 1000, 30000), d)
    for W, fdt in ((8, "<f8"), (4, "<f4")):
        a = vals.astype(fdt).view(f"<u{W}")
        dt = np.dtype(f"<u{W}")
        b = cv._decimal_encode(a, dt, d, cv.M_IDELTA1, 0)
        if b is None:
            continue
        hdr = bytearray(cv.MAGIC); hdr.append(W); hdr.append(0)
        cv._put_varint(hdr, 1); cv._put_varint(hdr, a.size); cv._put_varint(hdr, a.size * W)
        payload = bytes(hdr) + cv.pack_streams([b"", b""]) + b
        add(f"floatarray forced decimal d={d} W{W}", "floatarray", a.tobytes(), (payload, {"n": a.size * W, "v": 1}))

# ---------------- genome ----------------
def fastq(n, L, tmpl=True, var=False, plus_copy=False, lower=False, crlf=False, ragged=False):
    out = []
    for k in range(n):
        ln = L if not var else L - (k % 13)
        seq = "".join(rng.choice("ACGT" if k % 50 else "ACGTN") for _ in range(ln))
        q = "".join(chr(33 + min(40, max(2, 38 - j // 5 + rng.randint(-3, 3)))) for j in range(ln))
        rid = f"@SRR{1000 + k}.{k * 3} HWI:{k % 8}:{k * 7 % 1000}/1" if tmpl else f"@read_{rng.random():.6f}"
        out.append(f"{rid}\n{seq}\n+{rid[1:] if plus_copy else ''}\n{q}\n")
    s = "".join(out)
    if ragged:
        s += "@partial\nACG"
    if crlf:
        s = s.replace("\n", "\r\n")
    return s.encode()
for kw in [dict(), dict(var=True), dict(tmpl=False), dict(plus_copy=True), dict(crlf=True), dict(ragged=True)]:
    tf("genome fastq " + str(kw), "genome", fastq(800, 100, **kw))
def fasta(nseq, crlf=False):
    out = []
    for s in range(nseq):
        out.append(f">chr{s} synthetic sequence {s}\n")
        seq = "".join(rng.choice("ACGT" if (k // 500) % 3 else "acgtn") for k in range(5000 + 37 * s))
        for i in range(0, len(seq), 60):
            out.append(seq[i:i + 60] + "\n")
    s = "".join(out)
    return (s.replace("\n", "\r\n") if crlf else s).encode()
tf("genome fasta", "genome", fasta(12))
tf("genome fasta crlf", "genome", fasta(5, True))
vcf = ["##fileformat=VCFv4.2", "##source=synthetic", "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"]
for k in range(3000):
    vcf.append(f"chr{1 + k // 1000}\t{10000 + k * 37}\trs{k}\t{rng.choice('ACGT')}\t{rng.choice('ACGT')}\t{rng.randint(10, 99)}.{rng.randint(0, 9)}\tPASS\tDP={rng.randint(1, 80)}" + ("\tEXTRA" if k % 17 == 0 else ""))
tf("genome vcf", "genome", ("\n".join(vcf) + "\n").encode(), path="x.vcf")
csv = ["a,b,c,d"] + [f"{k},{-k * 3},{k * 0.25:.3f},{(k * 7919) % 1000:04d}" for k in range(5000)]
tf("genome csv", "genome", ("\n".join(csv)).encode(), path="x.csv")

# ---------------- loom ----------------
recs = b"".join(struct.pack("<IHhdB3s", 1000 + k, k % 50, -(k % 30000), k * 0.5, k % 3, b"abc") for k in range(40000))
tf("loom struct records", "loom", b"HDR!" * 10 + recs + b"TRAILER")
recs = b"".join(struct.pack(">QIf", 10 ** 12 + 977 * k, (k * 31) % 65536, k / 3) for k in range(30000))
tf("loom struct big-endian", "loom", recs)
d = open(C + "binary_linux_elf", "rb").read()[:2 << 20]
tf("loom elf (likely declines)", "loom", d)
d = head(C + "data_database.sqlite", 2 << 20); tf("loom sqlite", "loom", d)
lines = b"".join(b"%08d|%s|%d\x00" % (k, b"x" * (k % 37), k * k % 1000) for k in range(30000))
tf("loom vlen", "loom", lines)
# force each field mode through the stride plan directly
data = recs
for fields in ([(8, 1, 0), (4, 1, 1), (4, 2, 0)], [(8, 0, 0), (4, 1, 0), (4, 1, 0)],
               [(2, 1, 1), (2, 1, 0), (4, 1, 1), (4, 2, 0), (1, 0, 0), (1, 2, 0), (2, 1, 0)],
               [(8, 1, 1), (8, 1, 0)]):
    S = sum(f[0] for f in fields)
    R = len(data) // S
    plan = {"kind": "stride", "S": S, "B": 5, "E": 5 + (R - 1) * S, "R": R - 1, "fields": fields}
    add(f"loom forced fields {fields}", "loom", data, cl._apply_stride_plan(data, plan))
for align in (0, 1):
    plan = cl._build_vlen_plan(lines, 0, align)
    add(f"loom forced vlen align={align}", "loom", lines + b"tail", cl._apply_vlen_plan(lines + b"tail", plan))

json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w"))
print("cases:", len(manifest), stats)
