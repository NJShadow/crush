"""T2: nonce discipline, authentication coverage, tamper/truncate/splice,
EncryptedReader arithmetic. Uses a small CHUNK and a cheap KDF for speed."""
import sys, os, io, struct, secrets, tempfile, collections
sys.path.insert(0, "/home/claude/crush/engine")
import crush_crypt as C

C.CHUNK = 1024                       # keep test data small
C.PROFILES["fast"] = (1, 8, 1)       # cheapest legal Argon2 for the test suite
PW = b"correct horse battery staple"
TD = tempfile.mkdtemp(prefix="t2_")

fails = []
def chk(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not ok: fails.append(name)

def mk(data, name, pw=PW):
    p = os.path.join(TD, name)
    src = p + ".pt"
    open(src, "wb").write(data)
    C.encrypt_file(src, p, pw, "fast")
    return p

def rt(path, pw=PW):
    out = path + ".out"
    C.decrypt_file(path, out, pw)
    return open(out, "rb").read()

# ------------------------------------------------------------------ round trip
for n in (0, 1, C.CHUNK - 1, C.CHUNK, C.CHUNK + 1, 3 * C.CHUNK, 3 * C.CHUNK + 7):
    d = secrets.token_bytes(n)
    p = mk(d, f"rt{n}")
    ok1 = rt(p) == d
    with C.EncryptedReader(p, PW) as r:
        ok2 = r.plaintext_size == n and r.read() == d
    chk(f"roundtrip n={n}", ok1 and ok2, f"decrypt_file={ok1} reader={ok2}")

# ------------------------------------------------------- nonce uniqueness audit
seen = collections.Counter()
real_enc = C.XChaCha20Poly1305.encrypt
def spy(self, nonce, data, aad):
    seen[(self._key, nonce)] += 1
    return real_enc(self, nonce, data, aad)
C.XChaCha20Poly1305.encrypt = spy
for i in range(6):                                   # same passphrase, 6 archives
    mk(secrets.token_bytes(3 * C.CHUNK + 5), f"nz{i}")
mk(b"", "nz_empty"); mk(b"x", "nz_one")
C.XChaCha20Poly1305.encrypt = real_enc
dup = [k for k, v in seen.items() if v > 1]
chk("no (key,nonce) pair repeated across 8 archives, same passphrase",
    not dup, f"{len(seen)} encryptions, {len(dup)} repeats")
wrapnonces = {n for (k, n), v in seen.items() if n == b"\x00" * 24}
kekkeys = {k for (k, n) in seen if n == b"\x00" * 24}
chk("all-zero wrap nonce used exactly once per distinct KEK",
    len(kekkeys) == 8 and all(seen[(k, b"\x00" * 24)] == 1 for k in kekkeys))

# same input encrypted twice -> different ciphertext
d = b"A" * 5000
a, b = mk(d, "same1"), mk(d, "same2")
chk("same plaintext twice -> different ciphertext",
    open(a, "rb").read() != open(b, "rb").read())

# ---------------------------------------------------------- header is auth'd
base = mk(secrets.token_bytes(2 * C.CHUNK), "hdr")
raw = bytearray(open(base, "rb").read())
def try_hdr(mut, label, expect):
    b = bytearray(raw); mut(b)
    p = os.path.join(TD, "hdrmut"); open(p, "wb").write(bytes(b))
    try:
        C.decrypt_file(p, p + ".o", PW); got = "SUCCESS"
    except Exception as e:
        got = type(e).__name__
    chk(f"header tamper: {label} -> {got}", got == expect, f"expected {expect}")

try_hdr(lambda b: b.__setitem__(9, 2), "suite 1->2", "CryptoError")     # scrypt path
try_hdr(lambda b: b.__setitem__(8, 2), "version 1->2", "CryptoError")
try_hdr(lambda b: b.__setitem__(13, b[13] ^ 1), "kdf t", "WrongPassword")
try_hdr(lambda b: b.__setitem__(17, b[17] ^ 1), "kdf m", "WrongPassword")
try_hdr(lambda b: b.__setitem__(21, b[21] ^ 1), "kdf p", "WrongPassword")
try_hdr(lambda b: b.__setitem__(25, b[25] ^ 1), "salt", "WrongPassword")
try_hdr(lambda b: b.__setitem__(40, b[40] ^ 1), "stream prefix", "WrongPassword")
try_hdr(lambda b: b.__setitem__(60, b[60] ^ 1), "wrapped key", "WrongPassword")
try_hdr(lambda b: b.__setitem__(0, 0x58), "magic", "NotEncrypted")

# ------------------------------------------------- payload tamper / truncate
enc = C.CHUNK + C.TAG_LEN
p5 = mk(secrets.token_bytes(5 * C.CHUNK), "p5")       # 5 chunks
raw5 = open(p5, "rb").read()
def payload_case(newbytes, label, reader_reads_all=True):
    p = os.path.join(TD, "pl"); open(p, "wb").write(newbytes)
    try:
        C.decrypt_file(p, p + ".o", PW); df = "SUCCESS"
    except Exception as e: df = type(e).__name__
    try:
        with C.EncryptedReader(p, PW) as r:
            r.read() if reader_reads_all else None
        rd = "SUCCESS"
    except Exception as e: rd = type(e).__name__
    print(f"     {label:<44} decrypt_file={df:<15} reader.read()={rd}")
    return df, rd

H = C.HEADER_LEN
print("  payload attacks (both code paths):")
cases = {
 "truncate last chunk entirely":      raw5[:H + 4 * enc],
 "truncate 1 byte off the end":       raw5[:-1],
 "truncate mid-chunk (half chunk 4)": raw5[:H + 4 * enc + enc // 2],
 "append 1 byte":                     raw5 + b"\x00",
 "append a whole bogus chunk":        raw5 + b"\x00" * enc,
 "flip a bit in chunk 2":             raw5[:H + 2 * enc + 5] + bytes([raw5[H + 2 * enc + 5] ^ 1]) + raw5[H + 2 * enc + 6:],
 "swap chunks 1 and 3":               raw5[:H + enc] + raw5[H + 3 * enc:H + 4 * enc] + raw5[H + 2 * enc:H + 3 * enc] + raw5[H + enc:H + 2 * enc] + raw5[H + 4 * enc:],
 "duplicate chunk 1 over chunk 2":    raw5[:H + 2 * enc] + raw5[H + enc:H + 2 * enc] + raw5[H + 3 * enc:],
 "drop chunk 2":                      raw5[:H + 2 * enc] + raw5[H + 3 * enc:],
 "header only (no payload)":          raw5[:H],
}
res = {}
for label, data in cases.items():
    res[label] = payload_case(data, label)
for label in cases:
    df, rd = res[label]
    chk(f"payload: {label}", df != "SUCCESS" and rd != "SUCCESS", f"{df}/{rd}")

# ---------------------------------------------------------- cross-archive splice
A = mk(secrets.token_bytes(4 * C.CHUNK), "spA")
B = mk(secrets.token_bytes(4 * C.CHUNK), "spB")
ra, rb = open(A, "rb").read(), open(B, "rb").read()
spliced = ra[:H + 2 * enc] + rb[H + 2 * enc:]
p = os.path.join(TD, "splice"); open(p, "wb").write(spliced)
try:
    C.decrypt_file(p, p + ".o", PW); got = "SUCCESS"
except Exception as e: got = type(e).__name__
chk("splice chunks from another archive w/ SAME passphrase", got != "SUCCESS", got)
# whole-header swap
p = os.path.join(TD, "hdrswap"); open(p, "wb").write(rb[:H] + ra[H:])
try:
    C.decrypt_file(p, p + ".o", PW); got = "SUCCESS"
except Exception as e: got = type(e).__name__
chk("swap whole header between two archives", got != "SUCCESS", got)

# ------------------------------------ THE reader hole: unread chunks unchecked
p10 = mk(bytes(10 * C.CHUNK), "p10")
raw10 = bytearray(open(p10, "rb").read())
raw10[H + 5 * enc + 100] ^= 0xFF                      # smash chunk 5
p = os.path.join(TD, "unread"); open(p, "wb").write(bytes(raw10))
with C.EncryptedReader(p, PW) as r:
    a = r.read(10)                                    # touches chunk 0 only
    r.seek(9 * C.CHUNK); b = r.read(10)               # touches chunk 9 only
chk("EncryptedReader hands back data from a file with a forged chunk it never read",
    a == bytes(10) and b == bytes(10), "no error raised")
try:
    with C.EncryptedReader(p, PW) as r: r.verify()
    v = "SUCCESS"
except Exception as e: v = type(e).__name__
chk("...but verify() does catch it", v == "CorruptArchive", v)

# ----------------------------------------------- crafted sizes / arithmetic
def reader_on(nbytes_payload, label):
    p = os.path.join(TD, "sz")
    open(p, "wb").write(raw5[:H] + bytes(nbytes_payload))
    try:
        r = C.EncryptedReader(p, PW)
        out = (r._n, r._size); r.close(); return out
    except Exception as e:
        return type(e).__name__ + ": " + str(e)[:50]
print("  crafted ciphertext lengths -> (n_chunks, claimed plaintext size):")
for n in (0, 1, 15, 16, 17, enc - 1, enc, enc + 1, enc + 15, enc + 16, 2 * enc + 16):
    print(f"     payload={n:<8} -> {reader_on(n, n)}")

print()
print("RESULT:", "all pass" if not fails else f"{len(fails)} FAILURES: " + "; ".join(fails))
print("tmp:", TD)
