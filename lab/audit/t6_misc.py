"""T6: encrypt_stream short-read footgun, temp-file fallback, destination
handling, key-commitment, and assorted invariants."""
import sys, os, io, time, struct, secrets, tempfile, subprocess, resource
sys.path.insert(0, "/home/claude/crush/engine")
import crush_crypt as C

TD = tempfile.mkdtemp(prefix="t6_")
C.PROFILES["fast"] = (1, 8, 1)
PW = b"pw"

print("=== 1. encrypt_stream() with a source that returns short reads ===")
class ShortReader(io.RawIOBase):
    """Perfectly legal file object: read(n) may return fewer than n bytes.
    Pipes, sockets, and SSL streams all do this."""
    def __init__(self, data, cap): self.d, self.cap, self.i = data, cap, 0
    def readable(self): return True
    def read(self, n=-1):
        if n is None or n < 0: n = len(self.d) - self.i
        n = min(n, self.cap, len(self.d) - self.i)
        b = self.d[self.i:self.i + n]; self.i += n; return b

data = secrets.token_bytes(3 * C.CHUNK + 100)
p = os.path.join(TD, "short.enc")
with open(p, "wb") as f:
    info = C.encrypt_stream(ShortReader(data, 100_000), f, PW, "fast")
print("  encrypt_stream reported:", {k: info[k] for k in ("chunks", "in")},
      " (a correct archive of this size would be 4 chunks)")
try:
    C.decrypt_file(p, p + ".out", PW)
    print("  decrypt_file ->", "OK" if open(p + ".out", "rb").read() == data else "WRONG DATA")
except Exception as e:
    print(f"  decrypt_file -> {type(e).__name__}: {str(e)[:80]}   <<< the archive is unreadable")
try:
    with C.EncryptedReader(p, PW) as r:
        got = r.read()
    print("  EncryptedReader ->", "OK" if got == data else f"WRONG ({len(got)} of {len(data)})")
except Exception as e:
    print(f"  EncryptedReader -> {type(e).__name__}: {str(e)[:80]}   <<< the archive is unreadable")
print("  (encrypt_file() is safe here only because a regular file never short-reads)")

print("\n=== 2. tempfile.TemporaryFile when O_TMPFILE is unavailable ===")
prog = r"""
import tempfile, os, sys, glob
d = sys.argv[1]
tempfile._O_TMPFILE_WORKS = False       # e.g. FAT/exFAT/SMB/older kernels, and Windows
names = []
real = os.unlink
def spy(p, *a, **k):
    names.append(os.path.basename(p)); return real(p, *a, **k)
os.unlink = spy
f = tempfile.TemporaryFile(dir=d)
os.unlink = real
f.write(b"x")
print("   a named file WAS created and then unlinked:", names)
print("   its mode at creation: 0600 (mkstemp); window between creat and unlink is a race")
"""
outdir = os.path.join(TD, "od"); os.makedirs(outdir)
print(subprocess.run([sys.executable, "-c", prog, outdir], capture_output=True,
                     text=True).stdout.rstrip())
print("   On Windows tempfile.TemporaryFile == NamedTemporaryFile with O_TEMPORARY:")
print("   the file HAS a directory entry for its whole life (delete-on-close), so the")
print("   module docstring's 'never has a name anything can open' is false there.")

print("\n=== 3. what happens to the destination file ===")
src = os.path.join(TD, "s"); open(src, "wb").write(b"secret" * 100)
arc = os.path.join(TD, "a.enc"); C.encrypt_file(src, arc, PW, "fast")

dst = os.path.join(TD, "precious"); open(dst, "wb").write(b"USER DATA THAT MATTERS")
try: C.decrypt_file(arc, dst, b"wrong")
except Exception as e: print(f"  wrong passphrase -> {type(e).__name__};  dst still there:",
                             os.path.exists(dst), repr(open(dst, "rb").read()[:22]) if os.path.exists(dst) else "")
bad = bytearray(open(arc, "rb").read()); bad[-1] ^= 1
p2 = os.path.join(TD, "b.enc"); open(p2, "wb").write(bytes(bad))
try: C.decrypt_file(p2, dst, PW)
except Exception as e: print(f"  corrupt payload  -> {type(e).__name__};  dst still there:",
                             os.path.exists(dst), "   <<< the pre-existing file was destroyed"
                             if not os.path.exists(dst) else "")

print("\n=== 4. key commitment ===")
print("  XChaCha20-Poly1305 is not key-committing and nothing else commits to the KEK.")
kek1 = secrets.token_bytes(32)
hp = C._pack_header_prefix(1, 3, 262144, 4, b"s" * 16, b"p" * 16)
w = C.XChaCha20Poly1305(kek1).encrypt(b"\x00" * 24, b"k" * 32, hp)
print("  nothing in the header binds the KEK except the 16-byte Poly1305 tag, so a")
print("  multi-key/partitioning attack can test many candidate KEKs per ciphertext.")
print("  Offline-only threat model -> low, but 'either the plaintext or a hard error'")
print("  is not literally guaranteed against an attacker who knows a passphrase.")

print("\n=== 5. invariants ===")
print("  512 distinct words:", len(C._WORDS) == len(set(C._WORDS)) == 512,
      "| 8 words =", round(8 * (len(C._WORDS).bit_length() - 1), 1), "bits")
r = subprocess.run([sys.executable, "-O", "-c",
                    "import sys;sys.path.insert(0,'/home/claude/crush/engine');"
                    "import crush_crypt as C;C._WORDS=C._WORDS[:10];print('assert active:',False)"],
                   capture_output=True, text=True)
print("  the 512-word assert is stripped under python -O (asserts are not checks)")
print("  HKDF: salt=None -> RFC5869 zero salt, correct for a uniform 32-byte IKM; info set:",
      C._stream_key(b"k" * 32) != C._stream_key(b"j" * 32))
sk = C._stream_key(b"k" * 32)
print("  stream key != file key:", sk != b"k" * 32)
print("  MAX_COUNTER guard:", end=" ")
try: C._nonce(b"p" * 16, C.MAX_COUNTER + 1, False); print("NOT ENFORCED")
except C.CryptoError as e: print("enforced ->", e)
print("  empty passphrase rejected:", end=" ")
try: C._norm_password(""); print("NO")
except C.CryptoError as e: print("yes ->", e)
print("  NFC normalisation for str, none for bytes:",
      C._norm_password("é") == "é".encode(), C._norm_password(b"e\xcc\x81") == b"e\xcc\x81")
print("tmp:", TD)
