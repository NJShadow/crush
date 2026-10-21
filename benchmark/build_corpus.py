#!/usr/bin/env python3
import os as _os  # the Crush folder, wherever it is
CRUSH_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Assemble a representative real-world benchmark corpus for Crush."""
import os, io, glob, shutil, subprocess, random, json, csv, sqlite3, struct, wave
import numpy as np

C = CRUSH_ROOT + "/corpus"
os.makedirs(C, exist_ok=True)
random.seed(1337); np.random.seed(1337)

def w(name, data):
    p = os.path.join(C, name)
    with open(p, "wb") as f: f.write(data)
    print(f"  {name:38s} {len(data):>12,} B")
    return p

def cat_files(paths, limit):
    buf = bytearray()
    for p in paths:
        try:
            with open(p, "rb") as f: buf += f.read()
        except Exception: pass
        if len(buf) >= limit: break
    return bytes(buf[:limit])

print("== TEXT ==")
# English prose: package docs, changelogs, licenses, man pages
docs = sorted(glob.glob("/usr/share/doc/**/*", recursive=True))
prose = [p for p in docs if os.path.isfile(p) and not os.path.islink(p)
         and p.endswith(("copyright", "README", "NEWS", "changelog", ".txt", ".md"))]
random.shuffle(prose)
w("text_english.txt", cat_files(prose, 8_000_000))

# Source code: Python stdlib
py = [p for p in glob.glob("/usr/lib/python3.11/**/*.py", recursive=True) if os.path.isfile(p)]
random.shuffle(py)
w("code_python.py", cat_files(py, 6_000_000))

# C/C++ source headers
ch = glob.glob("/usr/include/**/*.h", recursive=True) + glob.glob("/usr/include/**/*.hpp", recursive=True)
random.shuffle(ch)
w("code_c.h", cat_files(ch, 4_000_000))

# XML / markup (TeX + LibreOffice config)
xmls = glob.glob("/usr/lib/libreoffice/**/*.xml", recursive=True) + glob.glob("/usr/share/**/*.xml", recursive=True)
random.shuffle(xmls)
w("data_xml.xml", cat_files(xmls, 4_000_000))

print("== STRUCTURED DATA ==")
# Realistic JSON records
recs = []
first = ["James","Mary","Robert","Patricia","John","Jennifer","Michael","Linda","David","Elizabeth","William","Barbara"]
last  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez","Hernandez"]
city  = ["Newark","Jersey City","Paterson","Elizabeth","Trenton","Camden","Clifton","Passaic","Union City"]
for i in range(60000):
    recs.append({"id": i, "uuid": f"{random.getrandbits(128):032x}",
                 "first_name": random.choice(first), "last_name": random.choice(last),
                 "email": f"user{i}@example.com", "city": random.choice(city),
                 "state": "NJ", "zip": f"0{random.randint(7000,8999)}",
                 "signup_ts": 1600000000 + i*37, "balance": round(random.uniform(0, 5000), 2),
                 "active": random.random() > 0.2, "tier": random.choice(["free","pro","team","enterprise"])})
w("data_records.json", json.dumps(recs, indent=2).encode())

sio = io.StringIO(); cw = csv.DictWriter(sio, fieldnames=list(recs[0].keys())); cw.writeheader(); cw.writerows(recs)
w("data_records.csv", sio.getvalue().encode())

# SQLite database
dbp = os.path.join(C, "data_database.sqlite")
if os.path.exists(dbp): os.remove(dbp)
con = sqlite3.connect(dbp); cur = con.cursor()
cur.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, uuid TEXT, first_name TEXT, last_name TEXT, email TEXT, city TEXT, state TEXT, zip TEXT, signup_ts INT, balance REAL, active INT, tier TEXT)")
cur.executemany("INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["id"],r["uuid"],r["first_name"],r["last_name"],r["email"],r["city"],r["state"],r["zip"],r["signup_ts"],r["balance"],int(r["active"]),r["tier"]) for r in recs])
con.commit(); con.close()
print(f"  {'data_database.sqlite':38s} {os.path.getsize(dbp):>12,} B")

# Scientific float array (genuinely high entropy, tests delta/shuffle filters)
arr = np.cumsum(np.random.randn(900000).astype(np.float64))
w("data_floats.bin", arr.tobytes())

# Sensor-style integer time series
ts = (np.cumsum(np.random.randint(-3, 4, 2_000_000)) + 32768).astype(np.int32)
w("data_timeseries.bin", ts.tobytes())

print("== IMAGES ==")
from PIL import Image
from skimage import data as skd
photos = []
for nm in ["hubble_deep_field", "retina", "astronaut", "coffee", "chelsea", "immunohistochemistry", "cat"]:
    try:
        a = getattr(skd, nm)()
        if a.ndim == 2: a = np.stack([a]*3, -1)
        if a.dtype != np.uint8:
            a = (255*(a-a.min())/max(1e-9, (a.max()-a.min()))).astype(np.uint8)
        photos.append((nm, Image.fromarray(a[..., :3])))
    except Exception as e:
        print("   skip", nm, e)

# Composite one large photographic sheet (real pixel content, no duplication)
big = Image.new("RGB", (1600, 1200))
x = y = rowh = 0
for nm, im in photos:
    if x + im.width > 1600: x = 0; y += rowh; rowh = 0
    if y >= 1200: break
    big.paste(im, (x, y)); x += im.width; rowh = max(rowh, im.height)
b = io.BytesIO(); big.save(b, "JPEG", quality=92, subsampling=0); w("photo_q92.jpg", b.getvalue())
b = io.BytesIO(); big.save(b, "JPEG", quality=75); w("photo_q75.jpg", b.getvalue())
b = io.BytesIO(); big.save(b, "PNG", compress_level=6); w("photo_lossless.png", b.getvalue())

# Screenshot-like PNG: a real matplotlib figure (flat colors, text, lines)
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
fig, axs = plt.subplots(2, 2, figsize=(12, 8), dpi=130)
t = np.linspace(0, 20, 800)
axs[0,0].plot(t, np.sin(t)*np.exp(-t/12)); axs[0,0].set_title("Damped oscillation"); axs[0,0].grid(True)
axs[0,1].bar(range(18), np.random.rand(18)*80); axs[0,1].set_title("Category totals")
axs[1,0].imshow(np.random.rand(60,60), cmap="viridis"); axs[1,0].set_title("Heatmap")
axs[1,1].hist(np.random.randn(9000), bins=70); axs[1,1].set_title("Distribution")
fig.tight_layout()
b = io.BytesIO(); fig.savefig(b, format="png"); w("screenshot_ui.png", b.getvalue()); plt.close(fig)

# 16-bit scientific TIFF-ish raw image
w("image_raw16.bin", (np.random.rand(1200,1200)*3000 + 20000).astype(np.uint16).tobytes())

print("== AUDIO ==")
snd = sorted(glob.glob("/usr/lib/libreoffice/share/gallery/sounds/*.wav"))
frames = []; params = None
for p in snd:
    try:
        with wave.open(p, "rb") as wf:
            if params is None: params = wf.getparams()
            if (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (params.nchannels, params.sampwidth, params.framerate):
                frames.append(wf.readframes(wf.getnframes()))
    except Exception: pass
if frames and params:
    raw = b"".join(frames)
    # real recorded audio, upsampled to 16-bit stereo 44.1k for a realistic music-file profile
    a = np.frombuffer(raw, dtype=np.uint8 if params.sampwidth == 1 else np.int16)
    if params.sampwidth == 1: a = ((a.astype(np.int16) - 128) * 256)
    a = a.astype(np.int16)
    reps = max(1, int(44100 * 2 * 60 / max(1, len(a))))
    # do NOT duplicate: instead resample-stretch the real material to 44.1k stereo
    idx = np.linspace(0, len(a)-1, min(len(a)*4, 44100*70)).astype(np.int64)
    mono = a[idx]
    left = mono.astype(np.float64)
    right = np.roll(left, 137) * 0.93 + np.random.randn(len(left)) * 12   # decorrelated channel
    st = np.empty(len(left)*2, dtype=np.int16)
    st[0::2] = np.clip(left, -32768, 32767).astype(np.int16)
    st[1::2] = np.clip(right, -32768, 32767).astype(np.int16)
    p = os.path.join(C, "audio_music.wav")
    with wave.open(p, "wb") as wf:
        wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(44100); wf.writeframes(st.tobytes())
    print(f"  {'audio_music.wav':38s} {os.path.getsize(p):>12,} B")
    subprocess.run(["ffmpeg","-y","-loglevel","quiet","-i",p,"-b:a","192k",os.path.join(C,"audio_music.mp3")])
    print(f"  {'audio_music.mp3':38s} {os.path.getsize(os.path.join(C,'audio_music.mp3')):>12,} B")

print("== BINARIES ==")
for src, dst in [("/tmp/winx/numpy-2.4.6-cp311-cp311-win_amd64/numpy/_core/_multiarray_umath.cp311-win_amd64.pyd", "binary_win_x64.pyd"),
                 ("/tmp/winx/pillow-12.3.0-cp311-cp311-win_amd64/PIL/_imaging.cp311-win_amd64.pyd", "binary_win_imaging.pyd"),
                 ("/tmp/winx/numpy-2.4.6-cp311-cp311-win_amd64/numpy.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll", "binary_win.dll")]:
    if os.path.exists(src):
        shutil.copy(src, os.path.join(C, dst))
        print(f"  {dst:38s} {os.path.getsize(os.path.join(C,dst)):>12,} B")
for src, dst in [("/usr/bin/pandoc", "binary_linux_elf")]:
    if os.path.exists(src):
        with open(src,"rb") as f: d = f.read(20_000_000)
        w(dst, d)

print("== CONTAINERS / DOCS ==")
shutil.copy("/tmp/winwheels/pillow-12.3.0-cp311-cp311-win_amd64.whl", os.path.join(C, "container_wheel.zip"))
print(f"  {'container_wheel.zip':38s} {os.path.getsize(os.path.join(C,'container_wheel.zip')):>12,} B")
fonts = sorted(glob.glob("/usr/share/fonts/truetype/**/*.ttf", recursive=True))
if fonts: shutil.copy(fonts[0], os.path.join(C, "font.ttf")); print(f"  font.ttf")

print("== INCOMPRESSIBLE CONTROL ==")
w("control_random.bin", os.urandom(4_000_000))
w("control_zeros.bin", b"\x00" * 4_000_000)

print("\nTotal corpus:", f"{sum(os.path.getsize(os.path.join(C,f)) for f in os.listdir(C)):,} bytes")
