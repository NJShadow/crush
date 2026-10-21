"""T1: verify _hchacha20 against RFC draft-irtf-cfrg-xchacha-03 and against an
independent pure-Python ChaCha20 reference over random inputs."""
import sys, os, struct, secrets
sys.path.insert(0, "/home/claude/crush/engine")
import crush_crypt as C

MASK = 0xFFFFFFFF
def rotl(v, n): return ((v << n) | (v >> (32 - n))) & MASK

def qr(x, a, b, c, d):
    x[a] = (x[a] + x[b]) & MASK; x[d] = rotl(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & MASK; x[b] = rotl(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & MASK; x[d] = rotl(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & MASK; x[b] = rotl(x[b] ^ x[c], 7)

def chacha_state(key, nonce16):
    return ([0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]
            + list(struct.unpack("<8I", key)) + list(struct.unpack("<4I", nonce16)))

def chacha_rounds(st):
    x = list(st)
    for _ in range(10):
        qr(x, 0, 4, 8, 12); qr(x, 1, 5, 9, 13); qr(x, 2, 6, 10, 14); qr(x, 3, 7, 11, 15)
        qr(x, 0, 5, 10, 15); qr(x, 1, 6, 11, 12); qr(x, 2, 7, 8, 13); qr(x, 3, 4, 9, 14)
    return x

def ref_hchacha20(key, nonce16):
    x = chacha_rounds(chacha_state(key, nonce16))
    return struct.pack("<8I", *([x[i] for i in (0, 1, 2, 3)] + [x[i] for i in (12, 13, 14, 15)]))

def ref_block(key, nonce16):
    st = chacha_state(key, nonce16)
    x = chacha_rounds(st)
    return struct.pack("<16I", *[(x[i] + st[i]) & MASK for i in range(16)])

fails = []
def chk(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (("  " + extra) if extra else ""))
    if not ok: fails.append(name)

# ---- RFC draft-irtf-cfrg-xchacha-03 2.2.1 ----
key = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
n16 = bytes.fromhex("000000090000004a0000000031415927")
want = bytes.fromhex("82413b4227b27bfed30e42508a877d73a0f9e4d58a74a853c12ec41326d3ecdc")
chk("2.2.1 HChaCha20 vector", C._hchacha20(key, n16) == want, C._hchacha20(key, n16).hex())
chk("2.2.1 vs pure-python ref", ref_hchacha20(key, n16) == want)

# ---- A.3.1 XChaCha20-Poly1305 AEAD vector ----
pt = bytes.fromhex(
 "4c616469657320616e642047656e746c656d656e206f662074686520636c617373206f66202739"
 "393a204966204920636f756c64206f6666657220796f75206f6e6c79206f6e652074697020666f"
 "7220746865206675747572652c2073756e73637265656e20776f756c642062652069742e")
aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
k   = bytes.fromhex("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
n24 = bytes.fromhex("404142434445464748494a4b4c4d4e4f5051525354555657")
ct  = bytes.fromhex(
 "bd6d179d3e83d43b9576579493c0e939572a1700252bfaccbed2902c21396cbb731c7f1b0b4aa6"
 "440bf3a82f4eda7e39ae64c6708c54c216cb96b72e1213b4522f8c9ba40db5d945b11b69b982c1"
 "bb9e3f3fac2bc369488f76b2383565d3fff921f9664c97637da9768812f615c68b13b52e")
tag = bytes.fromhex("c0875924c1c7987947deafd8780acf49")
got = C.XChaCha20Poly1305(k).encrypt(n24, pt, aad)
chk("A.3.1 AEAD encrypt", got == ct + tag, got[-16:].hex())
chk("A.3.1 AEAD decrypt", C.XChaCha20Poly1305(k).decrypt(n24, ct + tag, aad) == pt)

# ---- does cryptography's ChaCha20 lay the 16-byte nonce out as words 12..15? ----
bad = 0
for _ in range(300):
    kk = secrets.token_bytes(32); nn = secrets.token_bytes(16)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    e = Cipher(algorithms.ChaCha20(kk, nn), mode=None).encryptor()
    blk = e.update(b"\x00" * 64) + e.finalize()
    if blk != ref_block(kk, nn): bad += 1
chk("cryptography ChaCha20 nonce16 == state words 12..15 (300 random)", bad == 0, f"{bad} mismatches")

# ---- edge nonces: counter word at max, all-ones, all-zero, boundary values ----
edge = [b"\x00" * 16, b"\xff" * 16,
        b"\xff\xff\xff\xff" + b"\x00" * 12,          # counter word 12 = 0xFFFFFFFF
        b"\xff\xff\xff\xff\xff\xff\xff\xff" + b"\x00" * 8,   # 64-bit counter = max
        b"\x00" * 12 + b"\xff\xff\xff\xff",
        b"\x01" + b"\x00" * 15]
bad = 0
for nn in edge:
    for kk in (b"\x00" * 32, b"\xff" * 32, secrets.token_bytes(32)):
        if C._hchacha20(kk, nn) != ref_hchacha20(kk, nn):
            bad += 1; print("   edge mismatch", nn.hex(), kk.hex())
chk("HChaCha20 on counter-overflow / edge nonces", bad == 0)

# ---- random differential vs reference ----
bad = 0
for _ in range(500):
    kk = secrets.token_bytes(32); nn = secrets.token_bytes(16)
    if C._hchacha20(kk, nn) != ref_hchacha20(kk, nn): bad += 1
chk("HChaCha20 vs reference (500 random)", bad == 0, f"{bad} mismatches")

# ---- the actual nonces the STREAM construction uses ----
# nonce24 = prefix16 || counter7 || final1 -> subkey from prefix, inner nonce 0^4||ctr||fin
xk = secrets.token_bytes(32); pref = secrets.token_bytes(16)
inner = set()
for ctr in (0, 1, 2, 255, 256, 1 << 40, C.MAX_COUNTER):
    for fin in (False, True):
        n = C._nonce(pref, ctr, fin)
        assert len(n) == 24
        inner.add((C._hchacha20(xk, n[:16]), b"\x00" * 4 + n[16:]))
chk("STREAM inner nonces distinct", len(inner) == 14, f"{len(inner)} distinct")
chk("STREAM subkey constant across chunks", len({s for s, _ in inner}) == 1)

print()
print("RESULT:", "all pass" if not fails else "FAILURES: " + ", ".join(fails))
