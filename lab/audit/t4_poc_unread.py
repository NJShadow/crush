"""T4: proof of concept - chunks that crush_engine never reads are never
authenticated, so an attacker can forge them and every operation still reports
success.  Uses several bundles so `--only` really skips regions."""
import sys, os, secrets, tempfile
sys.path.insert(0, "/home/claude/crush/engine")
import crush_crypt as C
import crush_engine as E

C.PROFILES["fast"] = C.PROFILES["balanced"] = (1, 8, 1)   # cheap KDF for testing
E.BUNDLE_CAP = 200_000                                    # force one bundle per file
PW = "correct horse battery staple"
TD = tempfile.mkdtemp(prefix="t4_")
enc = C.CHUNK + C.TAG_LEN

src = os.path.join(TD, "src"); os.makedirs(src)
for i in range(8):
    open(os.path.join(src, f"f{i}.bin"), "wb").write(secrets.token_bytes(300_000))
arc = os.path.join(TD, "a.crush")
E.compress([src], arc, "quick", password=PW, kdf_profile="fast")

touched = set()
orig = C.EncryptedReader._chunk
C.EncryptedReader._chunk = lambda self, i: (touched.add(i), orig(self, i))[1]

def cov(fn, label):
    touched.clear(); fn()
    rd = C.EncryptedReader(arc, PW); n = rd._n; rd.close()
    miss = sorted(set(range(n)) - touched)
    print(f"  {label:<32} {len(touched)}/{n} chunks authenticated; UNCHECKED {miss}")
    return miss

m_idx = cov(lambda: E.read_index(arc, PW), "read_index / `crush l`")
m_all = cov(lambda: E.extract(arc, os.path.join(TD, "xa"), password=PW), "extract (full)")
m_sel = cov(lambda: E.extract(arc, os.path.join(TD, "xs"), select=["src/f0.bin"],
                              password=PW), "extract --only src/f0.bin")
C.EncryptedReader._chunk = orig
print()

def forge(chunks, name):
    raw = bytearray(open(arc, "rb").read())
    for i in chunks:
        o = C.HEADER_LEN + i * enc
        for j in range(o, min(o + enc, len(raw))): raw[j] ^= 0xA5
    p = os.path.join(TD, name); open(p, "wb").write(bytes(raw)); return p

if m_sel:
    bad = forge(m_sel, "forged_sel.crush")
    print(f"  forged chunks {m_sel} (every chunk `--only` skips), then:")
    try:
        r = E.extract(bad, os.path.join(TD, "x1"), select=["src/f0.bin"], password=PW)
        print(f"    x --only src/f0.bin  -> written={r['written']} verified={r['verified']} "
              f"failed={r['failed']}   <<< NO ERROR")
    except Exception as e:
        print("    x --only ->", type(e).__name__, e)
    try:
        idx = E.read_index(bad, PW)
        print(f"    l                    -> listed {len(idx['files'])} files   <<< NO ERROR")
    except Exception as e:
        print("    l ->", type(e).__name__, e)
    try:
        r = E.extract(bad, os.path.join(TD, "x2"), password=PW)
        print(f"    x (full)             -> written={r['written']} failed={len(r['failed'])}")
    except Exception as e:
        print("    x (full)             ->", type(e).__name__, str(e)[:80])
else:
    print("  (partial extract happened to touch every chunk here)")

print()
bad2 = forge(m_idx, "forged_idx.crush")
print(f"  forged chunks {m_idx} (every chunk `l` skips), then:")
try:
    idx = E.read_index(bad2, PW)
    print(f"    l -> listed {len(idx['files'])} files, total_in={idx['total_in']:,}   <<< NO ERROR")
except Exception as e:
    print("    l ->", type(e).__name__, e)

print()
print("  verify() is the documented mitigation.  Call sites in the whole tree:")
os.system("grep -rn '\\.verify(' /home/claude/crush/engine /home/claude/crush/app "
          "--include='*.py' | grep -v verify_content | grep -v crush_crypt.py || echo '    (none)'")
print("tmp:", TD)
