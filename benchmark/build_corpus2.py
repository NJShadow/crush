#!/usr/bin/env python3
import os as _os  # the Crush folder, wherever it is
CRUSH_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""
The EXTENDED corpus.

The v1 corpus stays untouched so the earlier numbers remain comparable. This
adds the kinds of file the new layers exist for - and, deliberately, several
that they cannot help with at all, so the average is not flattered.
"""
import os, io, glob, json, random, shutil, subprocess, csv, sqlite3
import numpy as np

C = CRUSH_ROOT + "/corpus2"
shutil.rmtree(C, ignore_errors=True)
os.makedirs(C)
random.seed(4242); np.random.seed(4242)

def w(name, data):
    p = os.path.join(C, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)
    print(f"  {name:<40} {len(data):>12,} B")

print("== GENERATOR-BACKED DATA (Oracle) ==")
w("gen/auto_increment_ids.bin", np.arange(1_500_000, dtype="<u4").tobytes())
w("gen/event_timestamps.bin",
  (1735689600 + np.arange(800_000, dtype="<i8") * 30).tobytes())
a, c, m = 6364136223846793005, 1442695040888963407, 1 << 64
x = 88172645463325252
vals = []
for _ in range(400_000):
    vals.append(x); x = (a * x + c) % m
w("gen/prng_stream.bin", np.array(vals, dtype="<u8").tobytes())
w("gen/zero_padding.bin", b"\x00" * 3_000_000)
tmpl = os.urandom(2048)
w("gen/repeated_block.bin", (tmpl * 1200))

print("\n== STRUCTURED TEXT (Prism) ==")
svc = ["api", "auth", "billing", "search", "media", "notify"]
lvl = ["INFO", "INFO", "INFO", "WARN", "ERROR", "DEBUG"]
lines = []
for i in range(400_000):
    lines.append(
        f"2026-{1+i%12:02d}-{1+i%28:02d}T{i%24:02d}:{(i*7)%60:02d}:{(i*13)%60:02d}.{i%1000:03d}Z "
        f"{lvl[i%6]} svc={svc[i%6]}-{i%9} trace={i:012d} req_id={(i*2654435761)%(1<<32):010d} "
        f"lat={2+(i*37)%1800}ms status={200 if i%23 else 503} bytes={512+(i*97)%64000} "
        f"path=/v2/{svc[i%6]}/{i%400} user=u{i%50000}")
w("struct/server.log", ("\n".join(lines) + "\n").encode())

recs = []
first = ["James","Mary","Robert","Patricia","John","Jennifer","Michael","Linda","David","Elizabeth"]
last = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez"]
for i in range(120_000):
    recs.append({"id": i, "account": f"ACCT-{i:09d}",
                 "first_name": random.choice(first), "last_name": random.choice(last),
                 "email": f"user{i}@example.com",
                 "created_at": f"2026-{1+i%12:02d}-{1+i%28:02d}T00:00:00Z",
                 "balance_cents": (i * 7919) % 5_000_000,
                 "currency": "USD", "tier": ["free","pro","team","enterprise"][i % 4],
                 "active": bool(i % 5), "region": ["us-east","us-west","eu","apac"][i % 4],
                 "score": round((i % 1000) / 10.0, 1)})
w("struct/accounts.json", json.dumps(recs, indent=2).encode())
sio = io.StringIO(); cw = csv.DictWriter(sio, fieldnames=list(recs[0].keys()))
cw.writeheader(); cw.writerows(recs)
w("struct/accounts.csv", sio.getvalue().encode())

# telemetry: many numeric columns, a real analytics export
rows = ["ts,device,temp_c,humidity,pressure_hpa,battery_mv,rssi,packets"]
for i in range(300_000):
    rows.append(f"{1735689600+i*15},dev-{i%800:04d},{18+(i%140)/10:.1f},"
                f"{30+(i%500)/10:.1f},{980+(i%400)/10:.1f},{3300-(i%900)},"
                f"{-40-(i%50)},{i%65536}")
w("struct/telemetry.csv", ("\n".join(rows) + "\n").encode())

print("\n== DOCUMENT REVISIONS (Echo) ==")
base = open(CRUSH_ROOT + "/corpus/text_english.txt", "rb").read(3_000_000)
cur = bytearray(base)
for v in range(1, 13):
    for _ in range(14):
        p = random.randrange(0, len(cur) - 200)
        cur[p:p] = ("\n\n[revision %d note] " % v).encode() + os.urandom(40).hex().encode()
    w(f"revisions/report_v{v:02d}.txt", bytes(cur))

print("\n== MANY SMALL FILES (Atlas) ==")
py = [p for p in glob.glob("/usr/lib/python3/dist-packages/**/*.py", recursive=True)
      if os.path.isfile(p)]
random.shuffle(py)
n = 0
for p in py:
    try:
        d = open(p, "rb").read()
    except Exception:
        continue
    if not (500 < len(d) < 40000):
        continue
    w(f"smallfiles/{n:04d}_{os.path.basename(p)}", d)
    n += 1
    if n >= 300:
        break

print("\n== NEGATIVE CONTROLS (must not improve) ==")
w("control/random.bin", os.urandom(3_000_000))
w("control/encrypted_like.bin", os.urandom(2_000_000))
shutil.copy(CRUSH_ROOT + "/corpus/audio_music.mp3", os.path.join(C, "control/music.mp3"))
shutil.copy(CRUSH_ROOT + "/corpus/binary_win_x64.pyd", os.path.join(C, "control/native.pyd"))
print(f"  control/music.mp3, control/native.pyd copied")

tot = sum(os.path.getsize(os.path.join(d, f))
          for d, _, fs in os.walk(C) for f in fs)
nf = sum(len(fs) for _, _, fs in os.walk(C))
print(f"\nEXTENDED CORPUS: {nf} files, {tot:,} bytes")
