"""T7: engine-level tamper matrix - what each CLI operation does to a modified
encrypted archive."""
import sys, os, secrets, tempfile
sys.path.insert(0, "/home/claude/crush/engine")
import crush_crypt as C
import crush_engine as E

C.PROFILES["fast"] = C.PROFILES["balanced"] = (1, 8, 1)
E.BUNDLE_CAP = 200_000
PW = "pw"
TD = tempfile.mkdtemp(prefix="t7_")
enc = C.CHUNK + C.TAG_LEN
H = C.HEADER_LEN

src = os.path.join(TD, "src"); os.makedirs(src)
for i in range(8):
    open(os.path.join(src, f"f{i}.bin"), "wb").write(secrets.token_bytes(300_000))
arc = os.path.join(TD, "a.crush")
E.compress([src], arc, "quick", password=PW, kdf_profile="fast")
raw = open(arc, "rb").read()
n = (len(raw) - H) // enc + (1 if (len(raw) - H) % enc else 0)
print(f"  {len(raw):,} bytes, {n} ciphertext chunks\n")

def run(label, data):
    p = os.path.join(TD, "t.crush"); open(p, "wb").write(data)
    row = []
    for name, fn in (("l", lambda: f"{len(E.read_index(p, PW)['files'])} files"),
                     ("x --only f0", lambda: (lambda r: f"w={r['written']} f={len(r['failed'])}")(
                         E.extract(p, tempfile.mkdtemp(dir=TD), select=["src/f0.bin"], password=PW))),
                     ("x (full)", lambda: (lambda r: f"w={r['written']} f={len(r['failed'])}")(
                         E.extract(p, tempfile.mkdtemp(dir=TD), password=PW)))):
        try: row.append(f"{name}={fn()}")
        except Exception as e: row.append(f"{name}={type(e).__name__}")
    print(f"  {label:<38} " + "  ".join(f"{c:<26}" for c in row))

print(f"  {'archive':<38} {'list':<28}{'partial extract':<28}{'full extract'}")
run("untouched", raw)
run("append 1 byte", raw + b"\x00")
run("append one whole bogus chunk", raw + bytes(enc))
run("truncate last chunk", raw[:H + (n - 1) * enc])
run("truncate 1 byte", raw[:-1])
run("forge chunk 0 (header+index area)", raw[:H + 40] + bytes(1) + raw[H + 41:])
run("forge chunk 3 (a middle bundle)", raw[:H + 3 * enc + 40] + bytes(1) + raw[H + 3 * enc + 41:])
run("delete chunk 3", raw[:H + 3 * enc] + raw[H + 4 * enc:])
run("swap chunks 3 and 5",
    raw[:H + 3 * enc] + raw[H + 5 * enc:H + 6 * enc] + raw[H + 4 * enc:H + 5 * enc]
    + raw[H + 3 * enc:H + 4 * enc] + raw[H + 6 * enc:])
run("forge every chunk except 0 and last",
    raw[:H + enc] + bytes(len(raw) - H - enc - (len(raw) - H) % enc or 0)
    + raw[H + enc + (len(raw) - H - enc - (len(raw) - H) % enc or 0):])
print("\ntmp:", TD)
