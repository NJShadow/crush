"""T3: crush_engine integration - which ciphertext chunks does each operation
actually authenticate?  Plus the compress() temp-file path."""
import sys, os, json, struct, secrets, tempfile, shutil, traceback
sys.path.insert(0, "/home/claude/crush/engine")
import crush_crypt as C
import crush_engine as E

C.PROFILES["balanced"] = (1, 8, 1)     # cheap KDF for the test suite only
C.PROFILES["fast"] = (1, 8, 1)
PW = "correct horse battery staple"
TD = tempfile.mkdtemp(prefix="t3_")

fails = []
def chk(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not ok: fails.append(name)

# ---- build an archive whose PLAINTEXT spans several 256 KiB chunks -----------
src = os.path.join(TD, "src"); os.makedirs(src)
for i in range(6):
    open(os.path.join(src, f"f{i}.bin"), "wb").write(secrets.token_bytes(300_000))
arc = os.path.join(TD, "a.crush")
r = E.compress([src], arc, "quick", password=PW, kdf_profile="fast")
print(f"  archive: plaintext ~{r['in']:,}B -> ciphertext {os.path.getsize(arc):,}B")

# ---- instrument EncryptedReader._chunk to record chunk coverage --------------
touched = set()
orig = C.EncryptedReader._chunk
def spy(self, i):
    touched.add(i); return orig(self, i)
C.EncryptedReader._chunk = spy

def coverage(fn, label):
    touched.clear()
    fn()
    rd = C.EncryptedReader(arc, PW); n = rd._n; rd.close()
    miss = sorted(set(range(n)) - touched)
    print(f"  {label:<34} authenticated {len(touched)}/{n} chunks; never checked: {miss}")
    return n, sorted(touched), miss

n_idx, t_idx, m_idx = coverage(lambda: E.read_index(arc, PW), "read_index / `crush l`")
d1 = os.path.join(TD, "x1")
n_all, t_all, m_all = coverage(lambda: E.extract(arc, d1, password=PW), "extract (full)")
d2 = os.path.join(TD, "x2")
n_sel, t_sel, m_sel = coverage(
    lambda: E.extract(arc, d2, select=["src/f0.bin"], password=PW), "extract --only src/f0.bin")

chk("full extract authenticates every chunk", not m_all, str(m_all))
chk("read_index leaves chunks unauthenticated", bool(m_idx), str(m_idx))
chk("partial extract leaves chunks unauthenticated", bool(m_sel), str(m_sel))
C.EncryptedReader._chunk = orig

# ---- exploit: forge the chunks nobody reads ---------------------------------
raw = bytearray(open(arc, "rb").read())
enc = C.CHUNK + C.TAG_LEN
for i in m_sel:                                     # smash every unread chunk
    off = C.HEADER_LEN + i * enc
    for j in range(off, min(off + enc, len(raw))):
        raw[j] ^= 0xA5
bad = os.path.join(TD, "forged.crush")
open(bad, "wb").write(bytes(raw))

d3 = os.path.join(TD, "x3")
try:
    rr = E.extract(bad, d3, select=["src/f0.bin"], password=PW)
    ok = (rr["written"] == 1 and not rr["failed"])
except Exception as e:
    rr, ok = type(e).__name__ + str(e), False
chk("PoC: `crush x --only` reports full success on a forged archive", ok, str(rr))
try:
    E.read_index(bad, PW); li = "SUCCESS"
except Exception as e: li = type(e).__name__
chk("PoC: `crush l` lists a forged archive without complaint", li == "SUCCESS", li)
d4 = os.path.join(TD, "x4")
try:
    rr2 = E.extract(bad, d4, password=PW); full = f"written={rr2['written']} failed={len(rr2['failed'])}"
except Exception as e: full = type(e).__name__
print(f"     (a FULL extract of the same forged archive: {full})")

# ---- does the CLI signal failure? -------------------------------------------
import subprocess
cp = subprocess.run([sys.executable, "-c", f"""
import sys; sys.path.insert(0,'/home/claude/crush/engine')
import crush_crypt as C; C.PROFILES['fast']=(1,8,1); C.PROFILES['balanced']=(1,8,1)
import crush_engine as E
r = E.extract({bad!r}, {os.path.join(TD,'x5')!r}, password={PW!r})
print('written', r['written'], 'failed', len(r['failed']))
"""], capture_output=True, text=True)
print("     full-extract-of-forged returncode:", cp.returncode, cp.stdout.strip(), cp.stderr.strip()[-200:])

# ---- GUI path: compress(password) then read_index(out) with no password ------
try:
    E.read_index(arc)          # exactly what app.py:_report does after compressing
    got = "SUCCESS"
except Exception as e:
    got = f"{type(e).__name__}: {e}"
chk("app.py _report() can read back an encrypted archive it just made",
    got == "SUCCESS", got)

# ---- temp-file behaviour in compress() --------------------------------------
outdir = os.path.join(TD, "outdir"); os.makedirs(outdir)
before = set(os.listdir(outdir))
seen_names, seen_modes = [], []
real_tf = E.tempfile.TemporaryFile
def tf_spy(*a, **k):
    f = real_tf(*a, **k)
    try:
        st = os.fstat(f.fileno()); seen_modes.append(oct(st.st_mode & 0o777))
        seen_names.append(os.readlink(f"/proc/self/fd/{f.fileno()}"))
    except Exception as e:
        seen_names.append("?" + str(e))
    seen_names.append(sorted(set(os.listdir(outdir)) - before))
    return f
E.tempfile.TemporaryFile = tf_spy
o2 = os.path.join(outdir, "b.crush")
E.compress([src], o2, "quick", password=PW, kdf_profile="fast")
E.tempfile.TemporaryFile = real_tf
print("  temp file fd path:", seen_names[0])
print("  new names in outdir while temp file open:", seen_names[1])
print("  temp file mode:", seen_modes)
print("  output mode:", oct(os.stat(o2).st_mode & 0o777))
print("  unencrypted output mode:",
      (E.compress([src], os.path.join(outdir, "c.crush"), "quick"),
       oct(os.stat(os.path.join(outdir, "c.crush")).st_mode & 0o777))[1])

# ---- read-only output directory / failure leaves what behind? ---------------
ro = os.path.join(TD, "ro"); os.makedirs(ro); os.chmod(ro, 0o500)
try:
    E.compress([src], os.path.join(ro, "d.crush"), "quick", password=PW, kdf_profile="fast")
    res = "SUCCESS"
except Exception as e:
    res = f"{type(e).__name__}: {str(e)[:70]}"
print("  compress into read-only outdir:", res, "| leftovers:", os.listdir(ro))
os.chmod(ro, 0o700)

print()
print("RESULT:", "all pass" if not fails else f"{len(fails)} FAILURES: " + "; ".join(fails))
print("tmp:", TD)
