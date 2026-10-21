# Known-answer vectors from the libraries Crush itself uses (argon2-cffi,
# cryptography via crush_crypt), for the WASM/JS side to reproduce.
import json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "engine"))
import crush_crypt as cc
from argon2.low_level import hash_secret_raw, Type
rng = random.Random(1234)
out = {"argon2": [], "xchacha": [], "hkdf": []}
for k in range(40):
    t = rng.choice([1, 2, 3])
    p = rng.choice([1, 2, 3, 4, 8])
    m = max(8 * p, rng.choice([8, 16, 33, 64, 256, 1000, 4096, 8 * p]))
    pw = os.urandom(rng.choice([1, 5, 32, 100]))
    salt = os.urandom(16)
    h = hash_secret_raw(secret=pw, salt=salt, time_cost=t, memory_cost=m, parallelism=p,
                        hash_len=32, type=Type.ID)
    out["argon2"].append([pw.hex(), salt.hex(), t, m, p, h.hex()])
# the real profiles too (fast and balanced)
for (t, m, p) in [cc.PROFILES["fast"], cc.PROFILES["balanced"]]:
    pw, salt = "correct horse été".encode(), os.urandom(16)
    out["argon2"].append([pw.hex(), salt.hex(), t, m, p,
        hash_secret_raw(secret=pw, salt=salt, time_cost=t, memory_cost=m, parallelism=p,
                        hash_len=32, type=Type.ID).hex()])
for k in range(300):
    key, nonce = os.urandom(32), os.urandom(24)
    msg = os.urandom(rng.choice([0, 1, 15, 16, 17, 63, 64, 65, 1000, 262144]))
    ad = os.urandom(rng.choice([0, 1, 86])) if k % 2 else b""
    ct = cc.XChaCha20Poly1305(key).encrypt(nonce, msg, ad or None)
    out["xchacha"].append([key.hex(), nonce.hex(), ad.hex(), msg.hex(), ct.hex()])
for k in range(50):
    ikm = os.urandom(rng.choice([1, 32, 100]))
    out["hkdf"].append([ikm.hex(), cc._commitment(ikm).hex() if len(ikm) else "", cc._stream_key(ikm).hex()])
json.dump(out, open(sys.argv[1], "w"))
print("vectors:", {k: len(v) for k, v in out.items()})
