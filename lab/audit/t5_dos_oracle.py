"""T5: pre-authentication DoS through _sanity_kdf, the suite-byte switch,
parameter combinations that escape as non-CryptoError, and error/timing oracles."""
import sys, os, time, struct, secrets, tempfile, subprocess, resource
sys.path.insert(0, "/home/claude/crush/engine")
import crush_crypt as C

TD = tempfile.mkdtemp(prefix="t5_")
print("=== 1. what does _sanity_kdf let through? ===")
worst = [
    ("argon2id", 1, 32, 4 * 1024 * 1024, 64),
    ("argon2id", 1, 32, 4 * 1024 * 1024, 1),
    ("scrypt  ", 2, 32, 1 << 22, 64),
    ("scrypt  ", 2, 32, 1 << 22, 1),
]
for name, suite, t, m, p in worst:
    try:
        C._sanity_kdf(suite, t, m, p); ok = "ACCEPTED"
    except C.CryptoError as e:
        ok = "rejected: " + str(e)[:40]
    if suite == 1:
        mem = m * 1024; work = f"{t} passes over {mem/2**30:.1f} GiB"
    else:
        mem = 128 * m * t; work = f"N*r*p = {m*t*p:,} core-ops over {mem/2**30:.1f} GiB"
    print(f"  {name} t={t:<3} m={m:<9} p={p:<3} -> {ok:<9}  memory {mem/2**30:6.2f} GiB  ({work})")

print("\n  the scrypt maxmem ceiling the code computes for itself:")
for m, t in ((1 << 22, 32), (1 << 20, 8)):
    print(f"    N={m:<9} r={t:<3} -> maxmem={2*m*t*128 + (1<<26):,} bytes "
          f"({(2*m*t*128 + (1<<26))/2**30:.1f} GiB) - permits {128*m*t/2**30:.1f} GiB of real use")

print("\n=== 2. measured cost, extrapolated ===")
from argon2.low_level import Type, hash_secret_raw
for m in (65536, 262144):                                    # 64 MiB, 256 MiB
    t0 = time.time(); hash_secret_raw(secret=b"x", salt=b"y" * 16, time_cost=1,
                                      memory_cost=m, parallelism=1, hash_len=32, type=Type.ID)
    d = time.time() - t0
    scale = (4 * 1024 * 1024 / m) * 32
    print(f"  argon2id t=1 m={m//1024}MiB p=1 : {d:.2f}s  ->  t=32 m=4GiB ~= {d*scale/60:.0f} min "
          f"and {4:.0f} GiB resident")
import hashlib
t0 = time.time(); hashlib.scrypt(b"x", salt=b"y" * 16, n=1 << 16, r=8, p=1, dklen=32,
                                 maxmem=2 * (1 << 16) * 8 * 128 + (1 << 26))
d = time.time() - t0
print(f"  scrypt N=2^16 r=8 p=1     : {d:.2f}s  ->  N=2^22 r=32 p=64 ~= "
      f"{d*(2**22/2**16)*(32/8)*64/3600:.0f} h and 16 GiB resident")

print("\n=== 3. one byte of an intercepted header turns a victim's unlock into an OOM ===")
src = os.path.join(TD, "p"); open(src, "wb").write(b"hello")
arc = os.path.join(TD, "a.enc")
C.PROFILES["fast"] = (1, 8, 1)
C.encrypt_file(src, arc, "pw", "fast")
raw = bytearray(open(arc, "rb").read())
raw[9] = C.SUITE_SCRYPT_XCHACHA                       # suite 1 -> 2
struct.pack_into(">III", raw, 10, 32, 1 << 22, 64)    # r=32, N=2^22, p=64
hostile = os.path.join(TD, "hostile.enc"); open(hostile, "wb").write(bytes(raw))
print("  header_info() (what the UI shows, no validation at all):", C.header_info(hostile))
prog = f"""
import sys, resource, time; sys.path.insert(0,'/home/claude/crush/engine')
resource.setrlimit(resource.RLIMIT_AS, (1<<30, 1<<30))   # pretend 1 GiB is all we have
import crush_crypt as C
t0=time.time()
try: C.decrypt_file({hostile!r}, {os.path.join(TD,'o')!r}, 'pw')
except BaseException as e: print('   ->', type(e).__name__, str(e)[:90], f'after {{time.time()-t0:.1f}}s')
"""
cp = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=300)
print(cp.stdout.rstrip() or cp.stderr.strip()[-300:])
print("  (the exception is NOT a CryptoError, so callers that catch CryptoError crash)")

print("\n=== 4. parameter combos that escape as non-CryptoError ===")
for label, suite, t, m, p in (("argon2 m=8 p=64 (m < 8*p)", 1, 1, 8, 64),
                              ("argon2 m=8  p=1", 1, 1, 8, 1),
                              ("scrypt N=2 r=32 p=64", 2, 32, 2, 64),
                              ("scrypt N=2^22 r=1 p=64", 2, 1, 1 << 22, 64)):
    try:
        C._sanity_kdf(suite, t, m, p)
    except C.CryptoError as e:
        print(f"  {label:<28} rejected by _sanity_kdf"); continue
    try:
        C._derive_kek(b"pw", b"s" * 16, suite, t, m, p)
        print(f"  {label:<28} ACCEPTED and derived a key")
    except Exception as e:
        cls = type(e).__name__
        flag = "" if isinstance(e, C.CryptoError) else "   <<< escapes CryptoError"
        print(f"  {label:<28} ACCEPTED, then {cls}: {str(e)[:60]}{flag}")

print("\n=== 5. wrong passphrase vs damaged header: distinguishable? ===")
C.PROFILES["fast"] = (1, 65536, 1)
good = os.path.join(TD, "g.enc"); C.encrypt_file(src, good, "hunter2hunter2", "fast")
g = bytearray(open(good, "rb").read())
def timeit(path, pw, n=5):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        try: C.decrypt_file(path, os.path.join(TD, "oo"), pw); r = "SUCCESS"
        except Exception as e: r = type(e).__name__ + ": " + str(e)[:48]
        ts.append(time.perf_counter() - t0)
    return sorted(ts)[n // 2], r
for label, path, pw in (
        ("right passphrase          ", good, "hunter2hunter2"),
        ("wrong passphrase          ", good, "hunter2hunter3"),
        ("wrong passphrase, 1 char  ", good, "a"),
        ("wrong passphrase, 4 KiB   ", good, "z" * 4096)):
    med, r = timeit(path, pw)
    print(f"  {label} {med*1000:8.1f} ms  {r}")
for label, mut in (("corrupt salt              ", lambda b: b.__setitem__(25, b[25] ^ 1)),
                   ("corrupt wrapped key       ", lambda b: b.__setitem__(70, b[70] ^ 1)),
                   ("corrupt stream prefix     ", lambda b: b.__setitem__(45, b[45] ^ 1))):
    bb = bytearray(g); mut(bb)
    p = os.path.join(TD, "mm"); open(p, "wb").write(bytes(bb))
    med, r = timeit(p, "hunter2hunter2")
    print(f"  {label} {med*1000:8.1f} ms  {r}")

print("\n=== 6. does any message leak key material or the passphrase? ===")
pwd = "S3cret-Passphrase-Do-Not-Leak"
bb = bytearray(g); bb[70] ^= 1
p = os.path.join(TD, "leak"); open(p, "wb").write(bytes(bb))
import traceback as _tb
for fn in (lambda: C.decrypt_file(p, os.path.join(TD, "z"), pwd),
           lambda: C.EncryptedReader(p, pwd),
           lambda: C.decrypt_file(hostile, os.path.join(TD, "z"), pwd)):
    try: fn()
    except BaseException as e:
        txt = _tb.format_exc()
        print(f"  {type(e).__name__}: {str(e)[:70]!r}  passphrase in traceback: {pwd in txt}")
print("tmp:", TD)
