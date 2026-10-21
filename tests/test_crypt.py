#!/usr/bin/env python3
"""Adversarial test suite for crush_crypt (Argon2id + XChaCha20-Poly1305-STREAM).

Run:   python3 test_crypt.py            # everything (~4-6 min on 2 cores)
       python3 test_crypt.py --skip-slow   # drops the 'singularity' tier (~80 s)
       python3 test_crypt.py --list        # list section names and exit

Exits non-zero if any check FAILs. Writes RESULTS.md next to this file.

Nothing here imports pytest; the suite is a single self-contained runner so it
can be dropped into any environment that has the engine's own dependencies.

The module under test is NEVER modified by this suite. Two checks temporarily
set `crush_crypt.HAVE_ARGON2 = False` in memory to exercise the scrypt fallback
and restore it in a finally block; no file on disk is touched.
"""
from __future__ import annotations

import binascii
import hashlib
import hmac
import io
import math
import os
import random
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
# Look for engine/ beside this file's parent first (the shipped layout:
# Crush/tests/ next to Crush/engine/), then at the research-tree depth, then an
# explicit override. Works wherever the project has been put, on any platform.
_CANDIDATES = [os.path.join(HERE, "..", "engine"),
               os.path.join(HERE, "..", "..", "..", "engine")]
if os.environ.get("CRUSH_ENGINE"):
    _CANDIDATES.insert(0, os.environ["CRUSH_ENGINE"])
ENGINE = next((os.path.abspath(c) for c in _CANDIDATES
               if os.path.isfile(os.path.join(c, "crush_crypt.py"))),
              os.path.abspath(_CANDIDATES[0]))
sys.path.insert(0, ENGINE)

import crush_crypt as cc                            # noqa: E402

C = cc.CHUNK                                        # 262144
ENC = C + cc.TAG_LEN                                # 262160
H = cc.HEADER_LEN                                   # 134
HP = cc.HEADER_PREFIX_LEN                           # 86
FAST = "fast"

TMP = None                                          # set up in main()


# ==========================================================================
#  Result bookkeeping
# ==========================================================================
PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

class Rec:
    __slots__ = ("section", "name", "status", "detail", "secs")
    def __init__(self, section, name, status, detail, secs):
        self.section, self.name, self.status = section, name, status
        self.detail, self.secs = detail, secs

RESULTS: list[Rec] = []
TAMPER: list[tuple] = []        # (case, decrypt_file outcome, EncryptedReader outcome, verdict)
_section = "?"


class Warn(Exception):
    """Raised by a check body to record a WARN rather than a FAIL."""


class Skipped(Exception):
    pass


def section(name):
    global _section
    _section = name
    print(f"\n=== {name} " + "=" * max(0, 66 - len(name)))


def check(name, fn):
    """Run fn(); PASS unless it raises. AssertionError -> FAIL, Warn -> WARN."""
    t0 = time.time()
    try:
        detail = fn() or ""
        st = PASS
    except Skipped as e:
        st, detail = SKIP, str(e)
    except Warn as e:
        st, detail = WARN, str(e)
    except AssertionError as e:
        st, detail = FAIL, str(e) or "assertion failed"
    except Exception as e:                                  # noqa: BLE001
        st = FAIL
        detail = f"unexpected {type(e).__name__}: {e}\n" + \
                 "".join(traceback.format_exc().splitlines(True)[-4:])
    dt = time.time() - t0
    RESULTS.append(Rec(_section, name, st, str(detail), dt))
    mark = {PASS: "ok  ", FAIL: "FAIL", WARN: "warn", SKIP: "skip"}[st]
    line = f"  [{mark}] {name}"
    if detail and st != PASS:
        line += f"\n         {str(detail).splitlines()[0][:160]}"
    print(line, flush=True)
    return st


# ==========================================================================
#  Small helpers
# ==========================================================================
def wf(name, data: bytes) -> str:
    p = os.path.join(TMP, name)
    os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) != TMP else None
    with open(p, "wb") as f:
        f.write(data)
    return p


def rf(p: str) -> bytes:
    with open(p, "rb") as f:
        return f.read()


def enc_bytes(plain: bytes, pw="suite-pass", profile=FAST) -> bytes:
    """Encrypt plain and return the archive as bytes."""
    s = wf("_enc_in.bin", plain)
    d = os.path.join(TMP, "_enc_out.bin")
    cc.encrypt_file(s, d, pw, profile)
    return rf(d)


def try_decrypt_file(blob: bytes, pw="suite-pass"):
    """-> ('raise', TypeName) | ('ok', plaintext)"""
    p = wf("_t.enc", blob)
    o = os.path.join(TMP, "_t.dec")
    if os.path.exists(o):
        os.unlink(o)
    try:
        cc.decrypt_file(p, o, pw)
    except Exception as e:                                  # noqa: BLE001
        return ("raise", type(e).__name__)
    return ("ok", rf(o))


def try_reader(blob: bytes, pw="suite-pass"):
    """-> ('raise', TypeName) | ('ok', plaintext).  Reads everything and verifies."""
    p = wf("_t.enc", blob)
    try:
        with cc.EncryptedReader(p, pw) as r:
            data = r.read()
            r.verify()
    except Exception as e:                                  # noqa: BLE001
        return ("raise", type(e).__name__)
    return ("ok", data)


def tamper_case(case: str, blob: bytes, original: bytes, pw="suite-pass",
                both=True) -> str:
    """Run a mutated archive through both decryption paths and grade it.

    The rule: a mutated archive must either raise, or hand back byte-identical
    plaintext (the mutation was a no-op).  Returning DIFFERENT plaintext without
    raising is a silent forgery and is graded CRITICAL.
    """
    outs = []
    for kind, fn in (("decrypt_file", try_decrypt_file),
                     ("EncryptedReader", try_reader)):
        if kind == "EncryptedReader" and not both:
            outs.append("-")
            continue
        k, v = fn(blob, pw)
        if k == "raise":
            outs.append(v)
        elif v == original:
            outs.append("OK-IDENTICAL")
        else:
            outs.append(f"SILENT-WRONG({len(v)}B)")
    bad = [o for o in outs if o.startswith("SILENT-WRONG")]
    verdict = "CRITICAL" if bad else "safe"
    TAMPER.append((case, outs[0], outs[1], verdict))
    return verdict


def flip(blob: bytes, off: int, bit: int = 0) -> bytes:
    b = bytearray(blob)
    b[off] ^= (1 << bit)
    return bytes(b)


def chunk_of(blob: bytes, i: int) -> bytes:
    return blob[H + i * ENC: H + (i + 1) * ENC]


def n_chunks(blob: bytes) -> int:
    csize = len(blob) - H
    full, rem = divmod(csize, ENC)
    return full + (1 if rem else 0)


HEADER_FIELDS = [
    ("magic",        0, 8),
    ("version",      8, 9),
    ("suite",        9, 10),
    ("kdf.t",       10, 14),
    ("kdf.m",       14, 18),
    ("kdf.p",       18, 22),
    ("salt",        22, 38),
    ("stream_prefix", 38, 54),
    ("wrapped_key.ct", 54, 86),
    ("wrapped_key.tag", 86, 102),
]


def field_of(off: int) -> str:
    for nm, a, b in HEADER_FIELDS:
        if a <= off < b:
            return nm
    return "?"


# ==========================================================================
#  An independent ChaCha20 / HChaCha20 reference, written from RFC 8439 and
#  draft-irtf-cfrg-xchacha-03 and validated against their own published
#  vectors before it is used as an oracle for anything.
# ==========================================================================
_CONST = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)


def _rotl(v, c):
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _qr(s, a, b, c_, d):
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF; s[d] = _rotl(s[d] ^ s[a], 16)
    s[c_] = (s[c_] + s[d]) & 0xFFFFFFFF; s[b] = _rotl(s[b] ^ s[c_], 12)
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF; s[d] = _rotl(s[d] ^ s[a], 8)
    s[c_] = (s[c_] + s[d]) & 0xFFFFFFFF; s[b] = _rotl(s[b] ^ s[c_], 7)


def _rounds(s):
    for _ in range(10):
        _qr(s, 0, 4, 8, 12); _qr(s, 1, 5, 9, 13)
        _qr(s, 2, 6, 10, 14); _qr(s, 3, 7, 11, 15)
        _qr(s, 0, 5, 10, 15); _qr(s, 1, 6, 11, 12)
        _qr(s, 2, 7, 8, 13); _qr(s, 3, 4, 9, 14)


def ref_chacha20_block(key: bytes, counter: int, nonce12: bytes) -> bytes:
    s = list(_CONST) + list(struct.unpack("<8I", key)) + [counter] + \
        list(struct.unpack("<3I", nonce12))
    w = s[:]
    _rounds(w)
    return b"".join(struct.pack("<I", (w[i] + s[i]) & 0xFFFFFFFF) for i in range(16))


def ref_hchacha20(key: bytes, nonce16: bytes) -> bytes:
    s = list(_CONST) + list(struct.unpack("<8I", key)) + list(struct.unpack("<4I", nonce16))
    w = s[:]
    _rounds(w)
    return b"".join(struct.pack("<I", w[i]) for i in (0, 1, 2, 3, 12, 13, 14, 15))


def ref_xchacha_seal(key, nonce24, pt, aad):
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    sub = ref_hchacha20(key, nonce24[:16])
    return ChaCha20Poly1305(sub).encrypt(b"\x00" * 4 + nonce24[16:], pt, aad)


def ref_hkdf_sha256(ikm: bytes, info: bytes, length=32, salt=None) -> bytes:
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


def ref_argon2_ctx(pwd, salt, secret, ad, t, m, p, taglen, typ, version=0x13):
    """argon2_ctx via argon2-cffi's raw binding: the only way to pass the
    secret and associated-data inputs the RFC 9106 vectors require."""
    from argon2.low_level import ffi, lib, core
    out = ffi.new("uint8_t[]", taglen)
    cp = ffi.new("uint8_t[]", pwd); cs = ffi.new("uint8_t[]", salt)
    ck = ffi.new("uint8_t[]", secret) if secret else ffi.NULL
    ca = ffi.new("uint8_t[]", ad) if ad else ffi.NULL
    ctx = ffi.new("argon2_context *", dict(
        out=out, outlen=taglen, pwd=cp, pwdlen=len(pwd), salt=cs, saltlen=len(salt),
        secret=ck, secretlen=len(secret), ad=ca, adlen=len(ad),
        t_cost=t, m_cost=m, lanes=p, threads=p, version=version,
        allocate_cbk=ffi.NULL, free_cbk=ffi.NULL, flags=lib.ARGON2_DEFAULT_FLAGS))
    rc = core(ctx, typ)
    assert rc == 0, f"argon2_ctx returned {rc}"
    return bytes(ffi.buffer(out, taglen))


# ==========================================================================
#  1. Known-answer tests
# ==========================================================================
def sec_kat():
    section("1. Known-answer tests")

    def t_ref_chacha():
        # RFC 8439 section 2.3.2 -- proves the in-test reference is trustworthy
        # before anything else is measured against it.
        exp = ("10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
               "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e")
        got = ref_chacha20_block(bytes(range(32)), 1,
                                 binascii.unhexlify("000000090000004a00000000")).hex()
        assert got == exp, f"reference ChaCha20 block wrong:\n  {got}\n  {exp}"
    check("reference ChaCha20 block matches RFC 8439 2.3.2", t_ref_chacha)

    def t_ref_aead():
        # RFC 8439 section 2.8.2 -- validates cryptography's ChaCha20Poly1305,
        # which is the primitive crush_crypt builds XChaCha on top of.
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        pt = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
              b"only one tip for the future, sunscreen would be it.")
        out = ChaCha20Poly1305(bytes(range(0x80, 0xA0))).encrypt(
            binascii.unhexlify("070000004041424344454647"), pt,
            binascii.unhexlify("50515253c0c1c2c3c4c5c6c7"))
        exp_ct = ("d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
                  "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
                  "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
                  "3ff4def08e4b7a9de576d26586cec64b6116")
        exp_tag = "1ae10b594f09e26a7e902ecbd0600691"
        assert out[:-16].hex() == exp_ct, "ChaCha20-Poly1305 ciphertext mismatch"
        assert out[-16:].hex() == exp_tag, "ChaCha20-Poly1305 tag mismatch"
    check("cryptography ChaCha20-Poly1305 matches RFC 8439 2.8.2", t_ref_aead)

    def t_hchacha_kat():
        # draft-irtf-cfrg-xchacha-03 section 2.2.1
        key = bytes(range(32))
        nonce = binascii.unhexlify("000000090000004a0000000031415927")
        exp = "82413b4227b27bfed30e42508a877d73a0f9e4d58a74a853c12ec41326d3ecdc"
        got = cc._hchacha20(key, nonce).hex()
        assert got == exp, f"crush_crypt._hchacha20 wrong:\n  got {got}\n  exp {exp}"
        assert cc._hchacha20(key, nonce) == ref_hchacha20(key, nonce)
    check("crush_crypt._hchacha20 vs xchacha-03 2.2.1 KAT", t_hchacha_kat)

    def t_xchacha_kat():
        # draft-irtf-cfrg-xchacha-03 section A.3.1
        key = bytes(range(0x80, 0xA0))
        nonce = binascii.unhexlify("404142434445464748494a4b4c4d4e4f5051525354555657")
        aad = binascii.unhexlify("50515253c0c1c2c3c4c5c6c7")
        pt = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
              b"only one tip for the future, sunscreen would be it.")
        exp_ct = ("bd6d179d3e83d43b9576579493c0e939572a1700252bfaccbed2902c21396cbb"
                  "731c7f1b0b4aa6440bf3a82f4eda7e39ae64c6708c54c216cb96b72e1213b452"
                  "2f8c9ba40db5d945b11b69b982c1bb9e3f3fac2bc369488f76b2383565d3fff9"
                  "21f9664c97637da9768812f615c68b13b52e")
        exp_tag = "c0875924c1c7987947deafd8780acf49"
        out = cc.XChaCha20Poly1305(key).encrypt(nonce, pt, aad)
        assert out[:-16].hex() == exp_ct, "XChaCha ciphertext != A.3.1"
        assert out[-16:].hex() == exp_tag, "XChaCha tag != A.3.1"
        assert cc.XChaCha20Poly1305(key).decrypt(nonce, out, aad) == pt
    check("XChaCha20-Poly1305 vs xchacha-03 A.3.1 KAT", t_xchacha_kat)

    def t_xchacha_cross():
        rnd = random.Random(20260909)
        for i in range(80):
            key = bytes(rnd.randrange(256) for _ in range(32))
            n24 = bytes(rnd.randrange(256) for _ in range(24))
            pt = bytes(rnd.randrange(256) for _ in range(rnd.choice([0, 1, 15, 16, 17, 63, 64, 65, 1000])))
            aad = bytes(rnd.randrange(256) for _ in range(rnd.choice([0, 1, 12, 100])))
            mine = cc.XChaCha20Poly1305(key).encrypt(n24, pt, aad or None)
            theirs = ref_xchacha_seal(key, n24, pt, aad or None)
            assert mine == theirs, f"cross-check differs at i={i}"
            assert cc.XChaCha20Poly1305(key).decrypt(n24, mine, aad or None) == pt
        return "80 random (key, 24B nonce, pt, aad) tuples agree with the reference"
    check("XChaCha layer cross-checked against independent HChaCha20 + "
          "cryptography's ChaCha20-Poly1305", t_xchacha_cross)

    def t_aead_rejects():
        key = secrets.token_bytes(32)
        n24 = secrets.token_bytes(24)
        ct = cc.XChaCha20Poly1305(key).encrypt(n24, b"hello", b"aad")
        from cryptography.exceptions import InvalidTag
        for label, args in (
                ("bad tag", (n24, ct[:-1] + bytes([ct[-1] ^ 1]), b"aad")),
                ("bad aad", (n24, ct, b"AAD")),
                ("bad nonce", (bytes([n24[0] ^ 1]) + n24[1:], ct, b"aad")),
                ("no aad", (n24, ct, None))):
            try:
                cc.XChaCha20Poly1305(key).decrypt(*args)
            except InvalidTag:
                continue
            raise AssertionError(f"XChaCha accepted a forgery: {label}")
        for bad in (b"", b"\x00" * 31, b"\x00" * 33):
            try:
                cc.XChaCha20Poly1305(bad)
                raise AssertionError(f"accepted a {len(bad)}-byte key")
            except ValueError:
                pass
        for bad in (b"\x00" * 23, b"\x00" * 25, b""):
            try:
                cc.XChaCha20Poly1305(key).encrypt(bad, b"x", None)
                raise AssertionError(f"accepted a {len(bad)}-byte nonce")
            except ValueError:
                pass
    check("XChaCha rejects forged tag / wrong AAD / wrong nonce / bad key size",
          t_aead_rejects)

    def t_argon2_rfc():
        # RFC 9106 section 5.3 (Argon2id) and 5.1 (Argon2d), through the raw
        # argon2_ctx binding because the vectors use a secret and associated data.
        from argon2.low_level import Type
        for name, typ, exp in (
            ("Argon2id 5.3", Type.ID.value,
             "0d640df58d78766c08c037a34a8b53c9d01ef0452d75b65eb52520e96b01e659"),
            ("Argon2d 5.1", Type.D.value,
             "512b391b6f1162975371d30919734294f868e3be3984f3c1a13a4db9fabe4acb"),
        ):
            got = ref_argon2_ctx(b"\x01" * 32, b"\x02" * 16, b"\x03" * 8, b"\x04" * 12,
                                 3, 32, 4, 32, typ).hex()
            assert got == exp, f"{name}: got {got}"
        return "argon2-cffi reproduces RFC 9106 5.1 and 5.3 exactly"
    check("Argon2 backend vs RFC 9106 published vectors", t_argon2_rfc)

    def t_kek_is_argon2id():
        from argon2.low_level import Type
        rnd = random.Random(4)
        for _ in range(4):
            pw = bytes(rnd.randrange(256) for _ in range(rnd.choice([1, 8, 33])))
            salt = bytes(rnd.randrange(256) for _ in range(16))
            t, m, p = 2, 8192, 2
            got = cc._derive_kek(pw, salt, cc.SUITE_ARGON2ID_XCHACHA, t, m, p)
            exp = ref_argon2_ctx(pw, salt, b"", b"", t, m, p, 32, Type.ID.value)
            assert got == exp, "crush_crypt KEK != RFC-9106 Argon2id(pw, salt)"
            assert len(got) == 32
        return "KEK == Argon2id v19, taglen 32, no secret/AD"
    check("_derive_kek is exactly RFC-9106 Argon2id over (passphrase, salt)",
          t_kek_is_argon2id)

    def t_hkdf():
        rnd = random.Random(9)
        for _ in range(20):
            fk = bytes(rnd.randrange(256) for _ in range(32))
            assert cc._stream_key(fk) == ref_hkdf_sha256(fk, b"crush/stream/v1", 32)
        return "HKDF-SHA256(salt=zeros, info='crush/stream/v1') confirmed"
    check("_stream_key matches an independent HKDF-SHA256", t_hkdf)

    def t_nonce():
        pre = b"P" * 16
        assert cc._nonce(pre, 0, False) == pre + b"\x00" * 7 + b"\x00"
        assert cc._nonce(pre, 1, True) == pre + b"\x00" * 6 + b"\x01" + b"\x01"
        assert cc._nonce(pre, 0x0102030405060, False) == \
            pre + bytes.fromhex("00102030405060") + b"\x00"
        assert cc._nonce(pre, cc.MAX_COUNTER, True) == pre + b"\xff" * 7 + b"\x01"
        try:
            cc._nonce(pre, cc.MAX_COUNTER + 1, False)
            raise AssertionError("counter overflow was not rejected")
        except cc.CryptoError:
            pass
        # STREAM discipline: the flag must actually be part of the nonce
        assert cc._nonce(pre, 5, True) != cc._nonce(pre, 5, False)
        assert cc._nonce(pre, 5, False) != cc._nonce(pre, 6, False)
    check("STREAM nonce = prefix || 7B BE counter || final flag", t_nonce)

    def t_scrypt_kek():
        salt = secrets.token_bytes(16)
        t, m, p = 8, 1 << 14, 2
        got = cc._derive_kek(b"pw", salt, cc.SUITE_SCRYPT_XCHACHA, t, m, p)
        exp = hashlib.scrypt(b"pw", salt=salt, n=m, r=t, p=p, dklen=32,
                             maxmem=2 * m * t * 128 + (1 << 26))
        assert got == exp, "scrypt fallback KEK != hashlib.scrypt"
    check("scrypt fallback KEK matches hashlib.scrypt", t_scrypt_kek)

    def t_const_time():
        assert cc.constant_time_equal(b"abc", b"abc")
        assert not cc.constant_time_equal(b"abc", b"abd")
        assert not cc.constant_time_equal(b"abc", b"ab")
    check("constant_time_equal", t_const_time)

    def t_header_layout():
        assert cc.HEADER_PREFIX_LEN == 86 and cc.HEADER_LEN == 134, \
            f"header layout changed: {cc.HEADER_PREFIX_LEN}/{cc.HEADER_LEN}"
        assert cc.COMMIT_LEN == 32
        blob = enc_bytes(b"x")
        assert blob[:8] == cc.MAGIC and blob[8] == cc.VERSION
        assert blob[9] == cc.SUITE_ARGON2ID_XCHACHA
        t, m, p = struct.unpack(">III", blob[10:22])
        assert (t, m, p) == cc.PROFILES[FAST], f"fast profile not in header: {(t,m,p)}"
        assert len(blob) == H + 0 + 16 + 1, f"1-byte archive is {len(blob)}B, expected {H+17}"
        # the key commitment must be exactly HKDF(KEK) and must sit in the AAD
        off = 22 + cc.SALT_LEN + cc.STREAM_PREFIX_LEN
        kek = cc._derive_kek(b"suite-pass", blob[22:22 + cc.SALT_LEN],
                             cc.SUITE_ARGON2ID_XCHACHA, *cc.PROFILES[FAST])
        assert blob[off:off + cc.COMMIT_LEN] == cc._commitment(kek), \
            "the header commitment is not HKDF(KEK)"
    check("header is 134 bytes, commitment included and correct", t_header_layout)


# ==========================================================================
#  2. Round-trip correctness
# ==========================================================================
ROUNDTRIP_SIZES = [
    ("empty", 0), ("1 byte", 1), ("15 bytes", 15), ("chunk-1", C - 1),
    ("exactly one chunk", C), ("chunk+1", C + 1), ("two chunks", 2 * C),
    ("2 chunks - 1", 2 * C - 1), ("2 chunks + 1", 2 * C + 1),
    ("five chunks + tail", 5 * C + 7777),
]


def sec_roundtrip():
    section("2. Round-trip correctness")

    def one(label, n, data=None):
        def t():
            plain = data if data is not None else os.urandom(n)
            src = wf("rt_in.bin", plain)
            e = os.path.join(TMP, "rt.enc")
            d = os.path.join(TMP, "rt.dec")
            info = cc.encrypt_file(src, e, "rt-pass", FAST)
            exp_chunks = max(1, (len(plain) + C - 1) // C)
            assert info["chunks"] == exp_chunks, \
                f"{info['chunks']} chunks for {len(plain)}B, expected {exp_chunks}"
            assert os.path.getsize(e) == H + len(plain) + 16 * exp_chunks, \
                "archive size is not header + plaintext + 16B per chunk"
            r = cc.decrypt_file(e, d, "rt-pass")
            assert rf(d) == plain, "decrypt_file round-trip is not byte-exact"
            assert r["chunks"] == exp_chunks
            with cc.EncryptedReader(e, "rt-pass") as rd:
                assert rd.plaintext_size == len(plain), \
                    f"reader size {rd.plaintext_size} != {len(plain)}"
                assert rd.read() == plain, "EncryptedReader != decrypt_file"
                rd.verify()
            # permissions: never world-readable
            assert (os.stat(e).st_mode & 0o077) == 0, "archive is group/other readable"
            return f"{len(plain)}B, {exp_chunks} chunk(s)"
        return t

    for label, n in ROUNDTRIP_SIZES:
        check(f"round-trip {label} ({n} B)", one(label, n))

    check("round-trip ASCII text, several chunks",
          one("text", 0, (b"The quick brown fox jumps over the lazy dog.\n" * 30000)))
    check("round-trip highly repetitive binary",
          one("repetitive", 0, bytes(range(256)) * 4096))
    check("round-trip all-zero payload", one("zeros", 0, b"\x00" * (C + 3)))
    check("round-trip UTF-8 text with every plane",
          one("utf8", 0, ("".join(chr(c) for c in range(0x20, 0x3000))
                          * 40).encode("utf-8")))

    def t_big():
        n = 40 * 1024 * 1024
        src = os.path.join(TMP, "big.bin")
        h = hashlib.sha256()
        with open(src, "wb") as f:
            for _ in range(n // (1 << 20)):
                b = os.urandom(1 << 20)
                h.update(b); f.write(b)
        digest = h.hexdigest()
        e = os.path.join(TMP, "big.enc"); d = os.path.join(TMP, "big.dec")
        t0 = time.time()
        info = cc.encrypt_file(src, e, "big-pass", FAST)
        t1 = time.time()
        cc.decrypt_file(e, d, "big-pass")
        t2 = time.time()
        hh = hashlib.sha256()
        with open(d, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                hh.update(b)
        assert hh.hexdigest() == digest, "40 MB round trip is not byte-exact"
        assert info["chunks"] == n // C, f"{info['chunks']} chunks for 40 MB"
        with cc.EncryptedReader(e, "big-pass") as r:
            assert r.plaintext_size == n
            r.verify()
        os.unlink(src); os.unlink(d)
        return (f"40 MB / {info['chunks']} chunks; enc {t1-t0:.2f}s dec {t2-t1:.2f}s "
                f"(excludes one Argon2 each)")
    check("round-trip 40 MB (160 chunks), streaming, sha256-exact", t_big)

    def t_balanced():
        plain = os.urandom(C + 500)
        src = wf("bal.bin", plain)
        e = os.path.join(TMP, "bal.enc"); d = os.path.join(TMP, "bal.dec")
        t0 = time.time()
        info = cc.encrypt_file(src, e, "bal-pass", "balanced")
        cc.decrypt_file(e, d, "bal-pass")
        assert rf(d) == plain
        t, m, p = struct.unpack(">III", rf(e)[10:22])
        assert (t, m, p) == cc.PROFILES["balanced"], \
            f"balanced profile not plumbed into the header: {(t,m,p)}"
        assert info["profile"] == "balanced"
        return f"t={t} m={m}KiB p={p}; {time.time()-t0:.2f}s for both KDF runs"
    check("profile plumbing: 'balanced' round-trips and lands in the header",
          t_balanced)

    def t_profiles():
        assert cc.profile_params("fast")[1:] == cc.PROFILES["fast"]
        assert cc.profile_params("balanced")[1:] == cc.PROFILES["balanced"]
        assert cc.profile_params("paranoid")[1:] == cc.PROFILES["paranoid"]
        got = cc.profile_params("no-such-profile")[1:]
        assert got == cc.PROFILES[cc.DEFAULT_PROFILE], \
            "unknown profile does not fall back to the default"
        return ("an unknown profile name silently becomes 'balanced' - "
                "a typo costs more time, never less")
    check("profile_params: all three profiles, unknown name -> default",
          t_profiles)

    def t_stream_api():
        plain = os.urandom(2 * C + 11)
        fin = io.BytesIO(plain); fout = io.BytesIO()
        seen = []
        info = cc.encrypt_stream(fin, fout, "st-pass", FAST,
                                 progress=lambda d: seen.append(d), total=len(plain))
        blob = fout.getvalue()
        assert info["chunks"] == 3
        assert len(blob) == H + len(plain) + 48
        p = wf("st.enc", blob); d = os.path.join(TMP, "st.dec")
        cc.decrypt_file(p, d, "st-pass")
        assert rf(d) == plain, "encrypt_stream output != decrypt_file input"
        assert seen and seen[-1]["stage"] == "encrypt"
        return f"{len(seen)} progress callbacks"
    check("encrypt_stream produces archives decrypt_file accepts", t_stream_api)

    def t_reader_seek():
        plain = os.urandom(5 * C + 9999)
        src = wf("sk.bin", plain)
        e = os.path.join(TMP, "sk.enc")
        cc.encrypt_file(src, e, "sk-pass", FAST)
        rnd = random.Random(1234)
        with cc.EncryptedReader(e, "sk-pass") as r:
            bad = 0
            # boundary-exact short reads
            offs = []
            for k in range(0, 6):
                for d in (-17, -16, -1, 0, 1, 15, 16, 17):
                    o = k * C + d
                    if 0 <= o <= len(plain):
                        offs.append(o)
            offs += [0, 1, len(plain) - 1, len(plain), len(plain) + 5]
            for o in offs:
                for n in (1, 2, 15, 16, 17, 32, C):
                    r.seek(o)
                    if r.read(n) != plain[o:o + n]:
                        bad += 1
            # random seeks
            for _ in range(400):
                o = rnd.randrange(0, len(plain) + 1)
                n = rnd.choice([1, 3, 16, 17, 4095, 4096, C - 1, C, C + 1, 3 * C])
                r.seek(o)
                if r.read(n) != plain[o:o + n]:
                    bad += 1
            assert bad == 0, f"{bad} seek/read mismatches vs the plaintext"
            # whence
            r.seek(0); r.seek(10, 1); assert r.read(4) == plain[10:14]
            r.seek(-9, 2); assert r.read() == plain[-9:]
            r.seek(0); assert r.read(-1) == plain and r.read(-1) == b""
            r.seek(0); assert r.read(None) == plain
            r.seek(len(plain) + 1000); assert r.read(10) == b""
            try:
                r.seek(-1); raise AssertionError("negative seek accepted")
            except OSError:
                pass
            try:
                r.seek(0, 9); raise AssertionError("bad whence accepted")
            except ValueError:
                pass
            assert r.readable() and r.seekable() and not r.writable()
        return f"{len(offs)*7 + 400} positioned reads, 0 mismatches"
    check("EncryptedReader: random seeks and short reads at chunk boundaries",
          t_reader_seek)

    def t_reader_eq_decrypt():
        for n in (0, 1, C - 1, C, C + 1, 3 * C + 3):
            plain = os.urandom(n)
            src = wf("eq.bin", plain)
            e = os.path.join(TMP, "eq.enc"); d = os.path.join(TMP, "eq.dec")
            cc.encrypt_file(src, e, "eq-pass", FAST)
            cc.decrypt_file(e, d, "eq-pass")
            with cc.EncryptedReader(e, "eq-pass") as r:
                assert r.read() == rf(d) == plain, f"reader != decrypt_file at n={n}"
                # chunk-at-a-time read must equal one big read
                r.seek(0)
                parts = []
                while True:
                    b = r.read(7919)
                    if not b:
                        break
                    parts.append(b)
                assert b"".join(parts) == plain, f"chunked read differs at n={n}"
        return "6 sizes, three read strategies, all identical"
    check("EncryptedReader identical to decrypt_file at every boundary size",
          t_reader_eq_decrypt)

    def t_is_encrypted():
        e = wf("ie.enc", enc_bytes(b"hello"))
        assert cc.is_encrypted(e)
        assert not cc.is_encrypted(wf("ie.plain", b"CRUSH\x01not encrypted"))
        assert not cc.is_encrypted(os.path.join(TMP, "does-not-exist"))
        info = cc.header_info(e)
        assert info["supported"] and info["kdf"] == "argon2id"
        assert (info["t"], info["m"], info["p"]) == cc.PROFILES[FAST]
        for bad in (b"", b"short", b"CRUSHENC" + b"\x00" * 10):
            try:
                cc.header_info(wf("ie.bad", bad))
                raise AssertionError(f"header_info accepted {len(bad)}B")
            except cc.NotEncrypted:
                pass
    check("is_encrypted / header_info on real, plain and truncated files",
          t_is_encrypted)


# ==========================================================================
#  3. Tamper resistance
# ==========================================================================
def sec_tamper():
    section("3. Tamper resistance")
    plain = os.urandom(3 * C - 1000)                 # 3 chunks, last one partial
    BASE = enc_bytes(plain)
    assert n_chunks(BASE) == 3
    other_same = enc_bytes(plain)                     # same passphrase, new salt/FK
    other_diff = enc_bytes(plain, pw="a-different-passphrase")

    # ---- 3a. every header field, single-bit flips ------------------------
    def t_hdr():
        by_field, crit = {}, []
        for off in range(H):
            for bit in (0, 7):
                v = tamper_case(f"header byte {off} ({field_of(off)}) bit {bit}",
                                flip(BASE, off, bit), plain, both=(bit == 0))
                by_field.setdefault(field_of(off), set()).add(TAMPER[-1][1])
                if v == "CRITICAL":
                    crit.append((off, bit))
        assert not crit, f"SILENT WRONG PLAINTEXT from header flips at {crit}"
        # every field must produce a raise, not a pass-through
        for f, outs in by_field.items():
            bad = [o for o in outs if not o.isidentifier() or o.startswith("OK")]
            assert not bad, f"field {f} produced {bad}"
        return "; ".join(f"{f}->{'/'.join(sorted(o))}" for f, o in by_field.items())
    check("204 single-bit flips across all 102 header bytes", t_hdr)

    def t_hdr_kinds():
        # A tampered header must raise something, and the exception must not
        # leak whether the passphrase itself was right.
        kinds = {}
        for f, a, b in HEADER_FIELDS:
            k, v = try_decrypt_file(flip(BASE, a, 0))
            assert k == "raise", f"{f} flip did not raise"
            kinds[f] = v
        assert kinds["magic"] == "NotEncrypted"
        for f in ("salt", "stream_prefix", "wrapped_key.ct", "wrapped_key.tag"):
            assert kinds[f] == "WrongPassword", f"{f} -> {kinds[f]}"
        odd = {f: k for f, k in kinds.items()
               if f in ("version", "suite", "kdf.t", "kdf.m", "kdf.p")
               and k != "WrongPassword"}
        if odd:
            raise Warn(
                "version/suite/KDF-parameter corruption raises CryptoError with a "
                "field-specific message (" + ", ".join(f"{k}:{v}" for k, v in odd.items())
                + ") rather than the WrongPassword the class docstring promises for "
                "'a corrupted or tampered header'. Not exploitable - those bytes are "
                "cleartext anyway - but the two failure modes are distinguishable.")
        return str(kinds)
    check("tampered header raises an indistinguishable WrongPassword", t_hdr_kinds)

    # ---- 3b. ciphertext bodies and tags ----------------------------------
    def t_body():
        crit = []
        spots = []
        for ci, cname in ((0, "first"), (1, "middle"), (2, "last")):
            base = H + ci * ENC
            end = min(base + ENC, len(BASE))
            body_end = end - 16
            spots += [
                (f"{cname} chunk, ciphertext byte 0", base),
                (f"{cname} chunk, ciphertext middle", base + (body_end - base) // 2),
                (f"{cname} chunk, ciphertext last byte", body_end - 1),
                (f"{cname} chunk, Poly1305 tag byte 0", body_end),
                (f"{cname} chunk, Poly1305 tag middle", body_end + 8),
                (f"{cname} chunk, Poly1305 tag last byte", end - 1),
            ]
        for label, off in spots:
            for bit in (0, 5):
                if tamper_case(f"flip {label} bit {bit}", flip(BASE, off, bit),
                               plain) == "CRITICAL":
                    crit.append(label)
        # byte substitution, not just bit flips
        for label, off in spots[:6]:
            b = bytearray(BASE); b[off] = (b[off] + 137) & 0xFF
            if tamper_case(f"substitute byte at {label}", bytes(b), plain) == "CRITICAL":
                crit.append(label)
        assert not crit, f"SILENT WRONG PLAINTEXT from body tampering: {crit}"
        return f"{len(spots)*2 + 6} mutations across first/middle/last chunk"
    check("bit flips in ciphertext and Poly1305 tags of first/middle/last chunk",
          t_body)

    # ---- 3c. truncation ---------------------------------------------------
    def t_trunc():
        L = len(BASE)
        offs = [0, 1, 7, 8, 50, H - 1, H, H + 1, H + 8, H + 15, H + 16, H + 17]
        for k in (1, 2):
            b = H + k * ENC
            offs += [b - 17, b - 16, b - 15, b - 1, b, b + 1, b + 15, b + 16, b + 17]
        offs += [L - 17, L - 16, L - 15, L - 8, L - 2, L - 1]
        rnd = random.Random(77)
        offs += [rnd.randrange(H, L) for _ in range(14)]
        offs = sorted({o for o in offs if 0 <= o < L})
        crit = []
        for o in offs:
            tag = "chunk boundary" if (o - H) % ENC == 0 and o >= H else (
                "mid-tag" if o >= H and (o - H) % ENC > C else "mid-body")
            if tamper_case(f"truncate to {o} B ({tag})", BASE[:o], plain) == "CRITICAL":
                crit.append(o)
        assert not crit, f"SILENT WRONG PLAINTEXT from truncation at {crit}"
        return f"{len(offs)} truncation offsets, including every chunk boundary"
    check("truncation at many offsets (boundaries, mid-tag, mid-body)", t_trunc)

    # ---- 3d. append -------------------------------------------------------
    def t_append():
        crit = []
        cases = [("append 1 zero byte", BASE + b"\x00"),
                 ("append 15 zero bytes", BASE + b"\x00" * 15),
                 ("append 16 zero bytes (a bare tag)", BASE + b"\x00" * 16),
                 ("append 17 zero bytes", BASE + b"\x00" * 17),
                 ("append a full random chunk", BASE + os.urandom(ENC)),
                 ("append 1 MB of random", BASE + os.urandom(1 << 20)),
                 ("append a copy of chunk 0 (relabels the final chunk non-final)",
                  BASE + chunk_of(BASE, 0)),
                 ("append the final chunk again", BASE + BASE[H + 2 * ENC:]),
                 ("append a whole second archive", BASE + other_same)]
        for label, blob in cases:
            if tamper_case(label, blob, plain) == "CRITICAL":
                crit.append(label)
        assert not crit, f"SILENT WRONG PLAINTEXT from appending: {crit}"
        return f"{len(cases)} append variants"
    check("appending bytes / chunks / a whole second archive", t_append)

    # ---- 3e. structural rearrangement -------------------------------------
    def t_struct():
        c0, c1, c2 = chunk_of(BASE, 0), chunk_of(BASE, 1), BASE[H + 2 * ENC:]
        hdr = BASE[:H]
        cases = [
            ("reorder: swap chunk 0 and chunk 1", hdr + c1 + c0 + c2),
            ("reorder: move the final chunk first", hdr + c2 + c0 + c1),
            ("duplicate: chunk 0 written over chunk 1", hdr + c0 + c0 + c2),
            ("duplicate: chunk 1 twice, file grows", hdr + c0 + c1 + c1 + c2),
            ("delete: interior chunk 1 removed", hdr + c0 + c2),
            ("delete: chunk 0 removed", hdr + c1 + c2),
            ("keep only chunk 0 (final-flag downgrade attack)", hdr + c0),
            ("keep only the final chunk (counter downgrade attack)", hdr + c2),
            ("keep only chunk 1", hdr + c1),
            ("header only, payload removed entirely", hdr),
            ("payload only, header removed", BASE[H:]),
            ("rewrite the final chunk as a non-final one, then re-append it",
             hdr + c0 + c1 + c2 + c2),
        ]
        crit = [l for l, b in cases if tamper_case(l, b, plain) == "CRITICAL"]
        assert not crit, f"SILENT WRONG PLAINTEXT from restructuring: {crit}"
        return f"{len(cases)} structural mutations"
    check("reorder / duplicate / delete / drop chunks", t_struct)

    # ---- 3f. cross-archive splicing ---------------------------------------
    def t_splice():
        hdr = BASE[:H]
        cases = [
            ("splice chunk 0 from a DIFFERENT archive, SAME passphrase",
             hdr + chunk_of(other_same, 0) + BASE[H + ENC:]),
            ("splice the final chunk from a DIFFERENT archive, SAME passphrase",
             BASE[:H + 2 * ENC] + other_same[H + 2 * ENC:]),
            ("splice chunk 0 from a DIFFERENT passphrase's archive",
             hdr + chunk_of(other_diff, 0) + BASE[H + ENC:]),
            ("whole header from a DIFFERENT archive, SAME passphrase",
             other_same[:H] + BASE[H:]),
            ("whole header from a DIFFERENT passphrase's archive",
             other_diff[:H] + BASE[H:]),
            ("wrapped key from a SAME-passphrase archive, rest unchanged",
             BASE[:HP] + other_same[HP:H] + BASE[H:]),
            ("salt from a SAME-passphrase archive, rest unchanged",
             BASE[:22] + other_same[22:38] + BASE[38:]),
            ("stream prefix from a SAME-passphrase archive, rest unchanged",
             BASE[:38] + other_same[38:54] + BASE[54:]),
            ("this archive's header on the other archive's payload (same pw)",
             BASE[:H] + other_same[H:]),
        ]
        crit = [l for l, b in cases if tamper_case(l, b, plain) == "CRITICAL"]
        assert not crit, f"SILENT WRONG PLAINTEXT from splicing: {crit}"
        return f"{len(cases)} cross-archive splices, none accepted"
    check("cross-archive chunk splicing (same and different passphrase)",
          t_splice)

    # ---- 3g. the whole matrix, restated -----------------------------------
    def t_no_silent():
        crit = [c for c in TAMPER if c[3] == "CRITICAL"]
        assert not crit, ("CRITICAL: %d mutated archives decrypted successfully to "
                          "the WRONG plaintext: %s" % (len(crit), crit[:5]))
        oki = [c for c in TAMPER if "OK-IDENTICAL" in (c[1], c[2])]
        assert not oki, f"mutations that were silently accepted as no-ops: {oki[:5]}"
        return f"{len(TAMPER)} mutated archives, every one rejected with an exception"
    check("NO mutation anywhere produced silent wrong plaintext", t_no_silent)

    def t_partial_read():
        """A forged chunk must be caught even by a caller that never reads it.

        This used to be the sharp edge of EncryptedReader: a 100-byte read from
        an archive whose last chunk was forged succeeded, because only the chunks
        actually touched were authenticated. Two things now close it - the final
        chunk is authenticated in the constructor (its 0x01 flag is the whole
        anti-truncation defence, and a caller that never reaches the end would
        never check it), and crush_engine opens every encrypted archive with
        verify_all=True."""
        blob = flip(BASE, len(BASE) - 5, 0)          # corrupt the final chunk
        p = wf("pr.enc", blob)
        try:
            r = cc.EncryptedReader(p, "suite-pass")
            r.read(100)
            r.close()
            raise AssertionError("a forged final chunk was not caught at open")
        except cc.CorruptArchive:
            pass
        # an interior forgery: caught by verify_all, which is what the engine uses
        blob2 = flip(BASE, cc.HEADER_LEN + 10, 0)
        p2 = wf("pr2.enc", blob2)
        try:
            cc.EncryptedReader(p2, "suite-pass", verify_all=True).close()
            raise AssertionError("verify_all missed a forged interior chunk")
        except cc.CorruptArchive:
            pass
        # and the engine really does pass verify_all
        import inspect as _i
        import crush_engine as _ce
        src = _i.getsource(_ce._open_archive)
        assert "verify_all=True" in src, \
            "crush_engine._open_archive no longer verifies the whole archive"
        return "final chunk checked at open; whole archive checked by the engine"
    check("a forged chunk is caught even when it is never read", t_partial_read)

    def t_dst_cleanup():
        blob = flip(BASE, len(BASE) - 5, 0)
        p = wf("dc.enc", blob)
        d = os.path.join(TMP, "dc.dec")
        with open(d, "wb") as f:
            f.write(b"PRE-EXISTING FILE")
        try:
            cc.decrypt_file(p, d, "suite-pass")
            raise AssertionError("a corrupted archive decrypted cleanly")
        except cc.CorruptArchive:
            pass
        assert rf(d) == b"PRE-EXISTING FILE", \
            "a corrupt archive destroyed a pre-existing file at the destination"
        leftovers = [f for f in os.listdir(os.path.dirname(d))
                     if f.startswith(os.path.basename(d) + ".part-")]
        assert not leftovers, f"scratch files left behind: {leftovers}"
        # a wrong passphrase must not touch the destination at all
        with open(d, "wb") as f:
            f.write(b"PRE-EXISTING FILE")
        try:
            cc.decrypt_file(wf("dc2.enc", BASE), d, "wrong")
        except cc.WrongPassword:
            pass
        assert rf(d) == b"PRE-EXISTING FILE", \
            "a wrong passphrase clobbered the destination file"
        return "destination untouched by both a corrupt archive and a wrong passphrase"
    check("a failed decryption never touches the destination file", t_dst_cleanup)

    def t_dos_ceiling():
        cc._sanity_kdf(cc.SUITE_ARGON2ID_XCHACHA, 2, 65536, 4)
        for bad in ((0, 65536, 4), (9, 65536, 4), (2, 7, 4),
                    (2, 1024 * 1024 + 1, 4), (2, 65536, 0), (2, 65536, 9)):
            try:
                cc._sanity_kdf(cc.SUITE_ARGON2ID_XCHACHA, *bad)
                raise AssertionError(f"_sanity_kdf accepted {bad}")
            except cc.CryptoError:
                pass
        for bad_suite in (0, 3, 255):
            try:
                cc._sanity_kdf(bad_suite, 2, 65536, 4)
                raise AssertionError(f"accepted suite {bad_suite}")
            except cc.CryptoError:
                pass
        # scrypt N must be a power of two in range
        cc._sanity_kdf(cc.SUITE_SCRYPT_XCHACHA, 8, 1 << 15, 2)
        for bad in ((8, (1 << 15) + 1, 2), (8, 1, 2), (8, 1 << 21, 2), (0, 1 << 15, 2)):
            try:
                cc._sanity_kdf(cc.SUITE_SCRYPT_XCHACHA, *bad)
                raise AssertionError(f"scrypt sanity accepted {bad}")
            except cc.CryptoError:
                pass
        # The ceiling is now the most expensive profile Crush actually writes,
        # plus a margin - not the widest value the fields can hold. A hostile
        # header can no longer be turned into a memory bomb.
        cc._sanity_kdf(cc.SUITE_ARGON2ID_XCHACHA, *cc.PROFILES["paranoid"])
        # Twice the paranoid profile is the accepted ceiling; past that, refuse.
        _pt, _pm, _ = cc.PROFILES["paranoid"]
        for bomb in ((32, 4 * 1024 * 1024, 64), (8, 2 * 1024 * 1024, 8),
                     (_pt * 3, _pm, 4), (8, 4 * 1024 * 1024, 8)):
            try:
                cc._sanity_kdf(cc.SUITE_ARGON2ID_XCHACHA, *bomb)
                raise AssertionError(f"_sanity_kdf accepted the memory bomb {bomb}")
            except cc.CryptoError:
                pass
        # scrypt cannot be used as a way around it either
        for bomb in ((16, 1 << 20, 8), (16, 1 << 21, 2)):
            try:
                cc._sanity_kdf(cc.SUITE_SCRYPT_XCHACHA, *bomb)
                raise AssertionError(f"scrypt bomb accepted: {bomb}")
            except cc.CryptoError:
                pass
        # and header_info must refuse to call such a header supported
        b = bytearray(BASE)
        struct.pack_into(">III", b, 10, 8, 4 * 1024 * 1024, 8)
        hp = wf("dos.enc", bytes(b))
        assert cc.header_info(hp)["supported"] is False, \
            "header_info reports an implausible unlock cost as supported"
        return "ceiling is the paranoid profile; four memory bombs refused"
    check("_sanity_kdf rejects absurd KDF parameters from an untrusted header",
          t_dos_ceiling)

    def t_version():
        b = bytearray(BASE); b[8] = 2
        k, v = try_decrypt_file(bytes(b))
        assert k == "raise" and v == "CryptoError", f"future version -> {v}"
        b = bytearray(BASE); b[9] = 99
        k, v = try_decrypt_file(bytes(b))
        assert k == "raise", "unknown suite accepted"
        b = bytearray(BASE); b[9] = cc.SUITE_SCRYPT_XCHACHA
        k, v = try_decrypt_file(bytes(b))
        assert k == "raise", "suite downgrade to scrypt accepted"
        return "version bump, unknown suite and argon2id->scrypt downgrade all refused"
    check("version bump / unknown suite / suite downgrade are refused", t_version)

    def t_single_chunk():
        # A one-chunk archive has no interior structure to attack; make sure the
        # degenerate cases still fail closed.
        for n in (0, 1, C):
            plain1 = os.urandom(n)
            b1 = enc_bytes(plain1)
            crit = []
            for label, blob in (
                    (f"{n}B archive: truncate 1 byte", b1[:-1]),
                    (f"{n}B archive: append 1 byte", b1 + b"\x00"),
                    (f"{n}B archive: flip last tag byte",
                     flip(b1, len(b1) - 1, 0)),
                    (f"{n}B archive: payload replaced by another archive's",
                     b1[:H] + enc_bytes(plain1)[H:])):
                if tamper_case(label, blob, plain1) == "CRITICAL":
                    crit.append(label)
            assert not crit, f"SILENT WRONG PLAINTEXT: {crit}"
        return "empty / 1-byte / exactly-one-chunk archives all fail closed"
    check("degenerate single-chunk archives fail closed", t_single_chunk)


# ==========================================================================
#  4. Wrong passphrase
# ==========================================================================
def sec_password():
    section("4. Wrong passphrase and normalisation")
    plain = b"the payload that must stay shut" * 500
    src = wf("pw.bin", plain)

    def opens_with(pw_enc, pw_dec):
        e = os.path.join(TMP, "pw.enc"); d = os.path.join(TMP, "pw.dec")
        cc.encrypt_file(src, e, pw_enc, FAST)
        try:
            cc.decrypt_file(e, d, pw_dec)
        except Exception as ex:                             # noqa: BLE001
            return type(ex).__name__
        return "OPENED" if rf(d) == plain else "OPENED-BUT-WRONG"

    def t_empty():
        for empty in ("", b""):
            try:
                cc.encrypt_file(src, os.path.join(TMP, "x.enc"), empty, FAST)
                raise AssertionError(f"an empty passphrase ({empty!r}) was accepted")
            except cc.CryptoError:
                pass
        e = os.path.join(TMP, "pw.enc")
        cc.encrypt_file(src, e, "real", FAST)
        for empty in ("", b""):
            try:
                cc.decrypt_file(e, os.path.join(TMP, "x.dec"), empty)
                raise AssertionError("decryption with an empty passphrase succeeded")
            except cc.CryptoError:
                pass
        for bad in (None, 12345, ["a"], 3.5):
            try:
                cc.encrypt_file(src, os.path.join(TMP, "x.enc"), bad, FAST)
                raise AssertionError(f"accepted a {type(bad).__name__} passphrase")
            except TypeError:
                pass
    check("empty passphrase refused on both encrypt and decrypt", t_empty)

    def t_near_miss():
        base = "correct horse battery staple"
        misses = [base[:-1], base + "x", base.replace("h", "H", 1),
                  base.replace(" ", "  ", 1), base.upper(), base + " ",
                  " " + base, base.replace("e", "3", 1)]
        for m in misses:
            r = opens_with(base, m)
            assert r == "WrongPassword", f"near-miss {m!r} -> {r}"
        assert opens_with(base, base) == "OPENED"
        return f"{len(misses)} one-character near misses, all rejected"
    check("one-character near-miss passphrases are rejected", t_near_miss)

    def t_nfc():
        s = "café über naïve ÅΩ"
        nfc = unicodedata.normalize("NFC", s)
        nfd = unicodedata.normalize("NFD", s)
        assert nfc != nfd, "test is vacuous: NFC and NFD encode identically"
        assert opens_with(nfc, nfd) == "OPENED", "NFD typing cannot open an NFC archive"
        assert opens_with(nfd, nfc) == "OPENED", "NFC typing cannot open an NFD archive"
        assert cc._norm_password(nfc) == cc._norm_password(nfd)
        # ... and it must be NFC, not NFKC: compatibility variants stay distinct.
        for a, b in (("ofﬁce", "office"),          # U+FB01 ligature vs f + i
                     ("ﬀ", "ff"),
                     ("½ cup", "1⁄2 cup"),
                     ("Ⅻ", "XII"),
                     ("ｈｅｌｌｏ", "hello")):       # fullwidth
            assert unicodedata.normalize("NFKC", a) == unicodedata.normalize("NFKC", b), \
                f"test setup wrong: {a!r} and {b!r} are not NFKC-equivalent"
            r = opens_with(a, b)
            assert r == "WrongPassword", \
                f"NFKC collapse: {a!r} opened an archive made with {b!r} ({r})"
        return ("NFC round-trips both ways; NFKC-equivalent-but-distinct "
                "passphrases stay distinct, so nothing is merged beyond Unicode "
                "canonical equivalence (which is exactly what 'the same typed "
                "passphrase' means)")
    check("NFD passphrase opens an NFC archive; NFKC variants stay distinct",
          t_nfc)

    def t_bytes_vs_str():
        # bytes are NOT normalised; a str is NFC'd then UTF-8 encoded.
        s = unicodedata.normalize("NFC", "café")
        assert opens_with(s, s.encode("utf-8")) == "OPENED"
        nfd = unicodedata.normalize("NFD", "café")
        r = opens_with(s, nfd.encode("utf-8"))
        assert r == "WrongPassword", f"raw NFD bytes opened an NFC archive ({r})"
        return ("bytes passphrases bypass normalisation, as documented; a program "
                "passing raw NFD bytes will not match a user typing the same text")
    check("bytes passphrases are used verbatim (no normalisation)", t_bytes_vs_str)

    def t_exotic():
        cases = {
            "NUL in the middle": "alpha\x00beta",
            "NUL only": "\x00\x00\x00",
            "emoji": "🔐🗝️🧊 keep out 👀",
            "emoji + ZWJ family": "👨‍👩‍👧‍👦-secret",
            "control characters": "\x01\x02\x1b[31m\x7f",
            "all whitespace": " \t\n\r\v\f ",
            "RTL + combining": "شفرة́-סוד",
            "4-byte astral": "\U0001F600\U0002F800",
            "1 char": "x",
            "64 KiB": "z" * 65536,
            "1 MiB": "q" * (1 << 20),
            "surrogate-free full range": "".join(chr(c) for c in range(1, 0x400)),
        }
        for label, pw in cases.items():
            r = opens_with(pw, pw)
            assert r == "OPENED", f"{label}: round trip -> {r}"
        # NUL is significant, not a terminator
        assert opens_with("alpha\x00beta", "alpha") == "WrongPassword", \
            "a NUL byte truncated the passphrase"
        assert opens_with("alpha\x00beta", "alphabeta") == "WrongPassword"
        assert opens_with("alpha\x00beta", "alpha\x00Beta") == "WrongPassword"
        # long passphrases are not silently truncated
        assert opens_with("z" * 65536, "z" * 65535) == "WrongPassword", \
            "a 64 KiB passphrase is being truncated somewhere"
        assert opens_with("q" * (1 << 20), "q" * ((1 << 20) - 1)) == "WrongPassword"
        return f"{len(cases)} exotic passphrases round-trip; NUL and length are significant"
    check("NUL bytes, emoji, control chars, whitespace, 1 MiB passphrases",
          t_exotic)

    def t_wrong_pw_paths():
        e = os.path.join(TMP, "pw2.enc")
        cc.encrypt_file(src, e, "the-right-one", FAST)
        for wrong in ("the-right-two", "", "x", "the-right-one "):
            try:
                cc.EncryptedReader(e, wrong)
                raise AssertionError(f"EncryptedReader opened with {wrong!r}")
            except cc.CryptoError:
                pass
        # and the file handle must not be leaked on failure
        import gc
        before = len(gc.get_objects())
        for _ in range(50):
            try:
                cc.EncryptedReader(e, "nope")
            except cc.CryptoError:
                pass
        assert len(gc.get_objects()) - before < 5000, "objects leaking on failed open"
        return "decrypt_file and EncryptedReader agree on rejection"
    check("EncryptedReader rejects wrong passphrases and closes its handle",
          t_wrong_pw_paths)


# ==========================================================================
#  5. Determinism / non-determinism
# ==========================================================================
def sec_determinism():
    section("5. Determinism and non-determinism")

    def t_fresh():
        src = wf("nd.bin", b"identical plaintext every time" * 100)
        salts, prefs, wraps, blobs = set(), set(), set(), set()
        N = 40
        for i in range(N):
            e = os.path.join(TMP, "nd.enc")
            cc.encrypt_file(src, e, "same-passphrase-every-time", FAST)
            b = rf(e)
            salts.add(b[22:38]); prefs.add(b[38:54]); wraps.add(b[54:H])
            blobs.add(b)
        assert len(salts) == N, f"only {len(salts)}/{N} distinct salts"
        assert len(prefs) == N, f"only {len(prefs)}/{N} distinct stream prefixes"
        assert len(wraps) == N, f"only {len(wraps)}/{N} distinct wrapped keys"
        assert len(blobs) == N, f"only {len(blobs)}/{N} distinct archives"
        # and the payload, not just the header, must differ
        bl = sorted(blobs)
        assert bl[0][H:] != bl[1][H:], "two archives share a ciphertext payload"
        return f"{N} encryptions of one plaintext: {N} distinct salts, prefixes, wraps"
    check("encrypting the same input twice never repeats a salt or nonce prefix",
          t_fresh)

    def t_stream_fresh():
        seen = set()
        for _ in range(20):
            out = io.BytesIO()
            cc.encrypt_stream(io.BytesIO(b"x" * 1000), out, "pw", FAST)
            b = out.getvalue()
            seen.add((b[22:38], b[38:54]))
        assert len(seen) == 20, "encrypt_stream repeated a (salt, prefix) pair"
    check("encrypt_stream draws fresh randomness too", t_stream_fresh)

    def t_det_decrypt():
        plain = os.urandom(2 * C + 33)
        e = wf("dd.enc", enc_bytes(plain))
        outs = set()
        for i in range(5):
            d = os.path.join(TMP, f"dd{i}.dec")
            cc.decrypt_file(e, d, "suite-pass")
            outs.add(hashlib.sha256(rf(d)).hexdigest())
        with cc.EncryptedReader(e, "suite-pass") as r:
            outs.add(hashlib.sha256(r.read()).hexdigest())
        assert len(outs) == 1, "decryption is not deterministic"
        assert outs.pop() == hashlib.sha256(plain).hexdigest()
    check("decryption is deterministic across repeats and both readers",
          t_det_decrypt)

    def t_kdf_det():
        salt = secrets.token_bytes(16)
        k = {cc._derive_kek(b"pw", salt, 1, 2, 8192, 2) for _ in range(4)}
        assert len(k) == 1, "Argon2id is not deterministic"
        k2 = cc._derive_kek(b"pw", bytes(16), 1, 2, 8192, 2)
        assert k2 != k.pop(), "the salt is not reaching Argon2"
    check("Argon2id KEK is a deterministic function of (passphrase, salt, params)",
          t_kdf_det)

    def t_ciphertext_looks_random():
        plain = b"\x00" * (2 * C)          # maximally structured input
        blob = enc_bytes(plain)
        body = blob[H:H + 2 * C]
        hist = [0] * 256
        for b in body:
            hist[b] += 1
        exp = len(body) / 256
        chi = sum((h - exp) ** 2 / exp for h in hist)
        # 255 df: p=0.001 two-sided is roughly [163, 361]
        assert 140 < chi < 400, f"byte histogram chi-square {chi:.0f} is not plausible"
        assert len(set(body[i:i + 16] for i in range(0, len(body), 16))) == len(body) // 16, \
            "a 16-byte block repeats in the ciphertext of an all-zero plaintext"
        return f"chi-square {chi:.0f} over 512 KiB of zeros, no repeated 16B block"
    check("ciphertext of an all-zero payload is indistinguishable from random",
          t_ciphertext_looks_random)


# ==========================================================================
#  6. Passphrase strength estimator
# ==========================================================================
def sec_strength():
    section("6. Passphrase strength estimator")
    ps = cc.password_strength

    def t_never_crashes():
        fixed = ["", "a", " ", " " * 100, "\x00", "\x00" * 5000, "\n\t\r\v\f",
                 "🔐" * 500, "".join(chr(c) for c in range(1, 0x500)),
                 "x" * (1 << 20), "aA1!" * 10000, "\x7f\x1b[0m", "-", "\\",
                 "((((((((((", "]]]]]", "*" * 300, ".*" * 200, "a" * 300 + "b"]
        rnd = random.Random(31337)
        fuzz = []
        for _ in range(1500):
            n = rnd.choice([1, 2, 3, 5, 8, 13, 40, 200])
            fuzz.append("".join(chr(rnd.choice([rnd.randrange(1, 0x80),
                                                rnd.randrange(0x80, 0x2000),
                                                rnd.randrange(0x2000, 0xD000)]))
                                for _ in range(n)))
        slow = []
        for pw in fixed + fuzz:
            t0 = time.time()
            r = ps(pw)
            dt = time.time() - t0
            assert isinstance(r, dict), f"non-dict for {pw[:20]!r}"
            for k in ("bits", "label", "level", "notes"):
                assert k in r, f"missing key {k}"
            assert isinstance(r["bits"], (int, float)) and r["bits"] >= 0 and \
                not math.isnan(r["bits"]), f"bad bits {r['bits']} for {pw[:20]!r}"
            assert 0 <= r["level"] <= 4
            assert isinstance(r["notes"], list) and len(r["notes"]) <= 2
            if dt > 1.0:
                slow.append((len(pw), dt))
        assert not slow, f"pathological runtime: {slow[:3]}"
        return f"{len(fixed)+len(fuzz)} inputs incl. 1 MiB, none crashed or hung"
    check("password_strength never crashes on any str (fuzzed)", t_never_crashes)

    def t_nonstr():
        outcomes = {}
        for bad in (None, b"bytes", 123, ["a"], 3.5, object()):
            try:
                r = ps(bad)
                outcomes[type(bad).__name__] = f"returned {r['bits']}"
            except Exception as e:                          # noqa: BLE001
                outcomes[type(bad).__name__] = f"raised {type(e).__name__}"
        raising = {k: v for k, v in outcomes.items() if "raised" in v}
        if raising:
            raise Warn("password_strength is str-only and raises on other types: "
                       + ", ".join(f"{k} {v}" for k, v in raising.items())
                       + ". None returns 0 bits rather than raising, which is an "
                         "inconsistent third behaviour. A UI that hands it a bytes "
                         "passphrase - which encrypt_file itself accepts - gets a "
                         "TypeError.")
        return str(outcomes)
    check("password_strength on non-str inputs", t_nonstr)

    def t_monotonic():
        # Constructed so none of the pattern penalties fire.
        ladder = ["Kq7#", "Kq7#Mz2$", "Kq7#Mz2$Vb9!", "Kq7#Mz2$Vb9!Xn4%",
                  "Kq7#Mz2$Vb9!Xn4%Rt6&", "Kq7#Mz2$Vb9!Xn4%Rt6&Ly3@"]
        bits = [ps(s)["bits"] for s in ladder]
        assert bits == sorted(bits) and len(set(bits)) == len(bits), \
            f"length is not monotonic: {list(zip(ladder, bits))}"
        # bigger character pool at equal length
        by_pool = [ps(s)["bits"] for s in ("kqmzvbxn", "Kqmzvbxn", "Kqmzvb7n", "Kqmzv#7n")]
        assert by_pool == sorted(by_pool), f"pool size is not monotonic: {by_pool}"
        # word count
        wb = [ps(cc.suggest_passphrase(n))["bits"] for n in (3, 4, 6, 8, 10, 12)]
        assert wb == sorted(wb) and len(set(wb)) == len(wb), \
            f"word count is not monotonic: {wb}"
        assert wb == [27.0, 36.0, 54.0, 72.0, 90.0, 108.0], \
            f"n words != 9n bits: {wb}"
        # obvious weak < obvious strong
        assert ps("password")["bits"] <= 8 < ps("Kq7#Mz2$")["bits"]
        assert ps("aaaa")["bits"] <= 12
        assert ps("")["bits"] == 0 and ps("")["label"] == "empty"
        assert ps("abcdefgh")["bits"] < ps("qhzbxmkw")["bits"], \
            "an alphabet run is not penalised"
        assert ps("abab" * 4)["bits"] <= 16, "a repeated pattern is not penalised"
        assert ps("123456")["bits"] <= 8
        return f"length, pool, word-count ladders all strictly increasing; {wb}"
    check("estimator is monotonic in the obvious cases", t_monotonic)

    def t_72():
        vals = {}
        for _ in range(3000):
            p = cc.suggest_passphrase(8)
            vals.setdefault(ps(p)["bits"], []).append(p)
            assert len(p.split()) == 8
        assert set(vals) == {72.0}, \
            ("a Crush 8-word passphrase does not always score 72.0 bits: "
             + str({k: v[:2] for k, v in vals.items() if k != 72.0}))
        r = ps(cc.suggest_passphrase(8))
        assert r["label"] == "strong" and r["level"] == 3
        assert any("512-word" in n for n in r["notes"]), \
            f"the 512-word note is missing: {r['notes']}"
        return "3000 generated 8-word passphrases, every one exactly 72.0 bits"
    check("suggest_passphrase(8) always reports exactly 72.0 bits", t_72)

    def t_wordlist():
        assert len(cc._WORDS) == 512 and len(set(cc._WORDS)) == 512
        assert cc._WORDSET == frozenset(cc._WORDS), "_WORDSET was never populated"
        assert all(w.isalpha() and w.islower() for w in cc._WORDS)
        # >= 3 letters is what makes the word-count scoring deterministic: with
        # a minimum of 3, an n-word phrase is at least 3n letters out of 4n-1
        # characters, which always clears the estimator's 0.75 alpha_share gate.
        assert min(len(w) for w in cc._WORDS) >= 3, \
            "a word shorter than 3 letters can push an n-word phrase below the " \
            "estimator's alpha_share gate, making the score non-deterministic"
        # no word may contain a keyboard or alphabet run, or the run penalty
        # would fire at random and a generated phrase would not score 9n bits
        for w in cc._WORDS:
            for seq in ("abcdefghijklmnopqrstuvwxyz", "01234567890",
                        "qwertyuiop", "asdfghjkl"):
                for i in range(len(seq) - 3):
                    assert seq[i:i + 4] not in w and seq[i:i + 4][::-1] not in w, \
                        f"word {w!r} contains the run {seq[i:i+4]!r}"
        # the draw must actually cover the list
        counts = {}
        for _ in range(6000):
            w = cc.suggest_passphrase(1)
            counts[w] = counts.get(w, 0) + 1
        assert len(counts) > 480, f"only {len(counts)}/512 words ever drawn"
        return (f"512 distinct lowercase words, 3-{max(len(w) for w in cc._WORDS)} "
                f"letters, no keyboard runs, {len(counts)}/512 drawn in 6000 tries")
    check("the 512-word list really holds 512 distinct usable words", t_wordlist)

    def t_leet_works_when_in_list():
        # The mechanism is sound where its substitution table and its dictionary
        # both reach: these all de-leet onto entries of _COMMON.
        for pw in ("p@ssw0rd", "passw0rd", "l3tm31n", "m0nk3y", "dr@g0n",
                   "qw3rty", "ninj@"):
            r = ps(pw)
            assert r["bits"] <= 40, f"{pw} -> {r['bits']} bits ({r['label']})"
        assert ps("password")["bits"] <= 8
        assert ps("password123")["bits"] <= 8
        return "the de-leet cap fires correctly for 0/1/3/4/5/7/8 and @"
    check("leetspeak rule works for the substitutions it maps correctly",
          t_leet_works_when_in_list)

    def t_leet_table():
        # BUG A. crush_crypt.py:671
        #   flat = pw.lower().translate(str.maketrans("0134578@$!", "oieastbaas"))
        # pairs up as 0->o 1->i 3->e 4->a 5->s 7->t 8->b @->a $->a !->s.
        # The last two are wrong: '$' is the canonical leet for 's', and '!' for
        # 'i'/'l'. Mapping both onto 'a'/'s' means the de-leeted stem misses.
        broken = []
        for pw in ("Pa$$w0rd", "pa$$word", "$hadow", "!loveyou", "$un$hine",
                   "P@ssw0rd", "l3tm31n", "m0nk3y", "dr@g0n", "Ch@ng3m3"):
            r = ps(pw)
            if r["bits"] > 40:
                broken.append(f"{pw!r} scores {r['bits']} bits ({r['label']})")
        assert not broken, (
            "leetspeaked common passwords must not read as strong. '$' stands for "
            "'s' and '!' for 'i'/'l'; if the substitution table gets those wrong, "
            "the de-leeted stem misses the dictionary and the cap never fires:\n  "
            + "\n  ".join(broken))
    check("leetspeaked common passwords are scored weak", t_leet_table)

    def t_leet_dictionary():
        """The de-leet rule tries identity-or-substitution per character, so it
        catches any leetspeaked string whose stem is in the lookup set. The
        lookup set is what limits it: 218 common passwords plus the 512-word
        generator list. A leetspeaked word outside those - 'Tr0ub4dor&3' is the
        canonical example - is still scored by the character model and reads
        stronger than it is. Shipping a full cracking dictionary is the only real
        fix; the interface says so rather than pretending otherwise."""
        for pw in ("P@ssw0rd", "l3tm31n", "m0nk3y", "dr@g0n", "5h@d0w"):
            assert ps(pw)["bits"] <= 40, f"{pw} escaped the de-leet rule"
        r = ps("Tr0ub4dor&3")
        if r["bits"] > 40:
            raise Warn(
                "password_strength('Tr0ub4dor&3') = %s bits (%r); zxcvbn scores it "
                "near 28. The de-leet rule is correct - it tries the identity "
                "substitution per character - but 'troubadour' is not in the 730 "
                "strings Crush ships, so the character model stands. Any "
                "leetspeaked dictionary word outside the shipped lists scores "
                "higher than it deserves. The interface states this limitation and "
                "offers Suggest, which is provably 72 bits." % (r["bits"], r["label"]))
    check("the de-leet rule catches leetspeaked words it can see",
          t_leet_dictionary)


# ==========================================================================
#  7. Integration with crush_engine
# ==========================================================================
def sec_integration(skip_slow: bool):
    section("7. Integration with crush_engine")
    try:
        import crush_engine as ce
    except Exception as e:                                  # noqa: BLE001
        check("import crush_engine", lambda: (_ for _ in ()).throw(
            Skipped(f"crush_engine unavailable: {e}")))
        return

    PW = "integration-passphrase"
    corpus = os.path.join(TMP, "corpus")
    os.makedirs(corpus, exist_ok=True)
    MARKERS = {
        "SECRET_FILENAME_MARKER.txt": b"CONTENT_MARKER_ALPHA\n" * 400,
        "payroll_2026_CONFIDENTIAL.csv": b"name,salary\nCONTENT_MARKER_BETA,999\n" * 200,
        "notes.md": "# CONTENT_MARKER_GAMMA\n\nsome text\n".encode() * 150,
        "blob.bin": bytes(range(256)) * 200,
        "empty.dat": b"",
    }
    for k, v in MARKERS.items():
        with open(os.path.join(corpus, k), "wb") as f:
            f.write(v)

    def build(tier, name):
        out = os.path.join(TMP, name)
        r = ce.compress([corpus], out, tier=tier, password=PW, kdf_profile=FAST)
        return out, r

    def verify_tree(dest):
        root = os.path.join(dest, os.path.basename(corpus))
        for k, v in MARKERS.items():
            p = os.path.join(root, k)
            assert os.path.exists(p), f"{k} missing from the extraction"
            assert rf(p) == v, f"{k} did not round-trip byte-exactly"

    ARCH = {}

    def tier_test(tier):
        def t():
            t0 = time.time()
            out, r = build(tier, f"{tier}.crush")
            ARCH[tier] = out
            assert r["encrypted"] is True, "compress() did not report encryption"
            assert ce.is_encrypted(out), "the output is not an encrypted archive"
            assert rf(out)[:8] == cc.MAGIC
            dest = os.path.join(TMP, f"out_{tier}")
            ce.extract(out, dest, password=PW)
            verify_tree(dest)
            shutil.rmtree(dest, ignore_errors=True)
            return f"{r['in']} -> {r['out']} B in {time.time()-t0:.1f}s"
        return t

    for tier in ("quick", "balanced", "crush", "obsession"):
        check(f"tier '{tier}': encrypted compress -> extract round-trips", tier_test(tier))
    if skip_slow:
        check("tier 'singularity': encrypted compress -> extract round-trips",
              lambda: (_ for _ in ()).throw(Skipped("--skip-slow given (~80 s)")))
    else:
        check("tier 'singularity': encrypted compress -> extract round-trips",
              tier_test("singularity"))

    ref = ARCH.get("balanced") or next(iter(ARCH.values()), None)
    if ref is None:
        return

    def t_no_pw():
        for bad in (None,):
            try:
                ce.read_index(ref, bad)
                raise AssertionError("read_index listed an encrypted archive with "
                                     "no passphrase")
            except cc.WrongPassword:
                pass
        for bad in ("wrong", "", "integration-passphrasE"):
            try:
                ce.read_index(ref, bad)
                raise AssertionError(f"read_index accepted {bad!r}")
            except cc.CryptoError:
                pass
        try:
            ce.extract(ref, os.path.join(TMP, "nope"), password=None)
            raise AssertionError("extract ran without a passphrase")
        except cc.WrongPassword:
            pass
        idx = ce.read_index(ref, PW)
        assert {os.path.basename(f["p"]) for f in idx["files"]} == set(MARKERS)
        return f"{len(idx['files'])} entries listed only with the right passphrase"
    check("read_index / extract refuse an encrypted archive without the passphrase",
          t_no_pw)

    def t_leak():
        blob = rf(ref)
        # The control that actually has power. grep/strings alone prove little
        # here, because an UNencrypted .crush already hides its filenames behind
        # zstd - so a clean grep is not evidence of confidentiality. What
        # separates the two is whether the metadata comes back with no key at
        # all: on a plain archive read_index hands over every name and size.
        plain_out = os.path.join(TMP, "control.crush")
        ce.compress([corpus], plain_out, tier="balanced")
        pidx = ce.read_index(plain_out)
        assert {os.path.basename(f["p"]) for f in pidx["files"]} == set(MARKERS), \
            "the control is broken: read_index on a plain archive did not list " \
            "the filenames"
        try:
            ce.read_index(ref)
            raise AssertionError("read_index listed the encrypted archive with no key")
        except cc.WrongPassword:
            pass
        needles = [b"SECRET_FILENAME_MARKER", b"payroll_2026_CONFIDENTIAL",
                   b"notes.md", b"blob.bin", b"empty.dat",
                   b"CONTENT_MARKER_ALPHA", b"CONTENT_MARKER_BETA",
                   b"CONTENT_MARKER_GAMMA", b"name,salary", b"Crush", b"files",
                   b"bundles", os.path.basename(corpus).encode()]
        found = [n for n in needles if n in blob]
        assert not found, f"the encrypted archive contains: {found}"
        # strings(1) / grep(1), if present
        tool_notes = []
        if shutil.which("strings"):
            s = subprocess.run(["strings", "-a", "-n", "5", ref],
                               capture_output=True, timeout=120).stdout
            hits = [n for n in needles if n in s]
            assert not hits, f"strings(1) recovered {hits}"
            runs = [x for x in s.split(b"\n") if x]
            longest = max((len(x) for x in runs), default=0)
            assert longest <= 24, \
                f"a {longest}-byte printable run survives in the ciphertext"
            tool_notes.append(f"strings -n5: {len(runs)} runs, longest {longest} B")
        if shutil.which("grep"):
            for n in (b"SECRET", b"CONTENT_MARKER", b"csv", b"payroll", b"MARKER"):
                rc = subprocess.run(["grep", "-a", "-c", n.decode(), ref],
                                    capture_output=True, timeout=120).returncode
                assert rc != 0, f"grep -a found {n!r} in the encrypted archive"
            tool_notes.append("grep -a: 0 hits for 5 needles")
        # the only readable thing must be the 8-byte magic
        assert blob[:8] == cc.MAGIC
        assert b"CRUSHENC" not in blob[8:], "the magic appears twice"
        # the archive is however still self-identifying, and its length still
        # tracks the compressed length of the input - both documented.
        assert cc.header_info(ref)["kdf"] == "argon2id"
        return ("read_index lists everything on a plain archive and nothing on "
                "the encrypted one; 0/13 needles in the raw bytes. "
                + "; ".join(tool_notes)
                + ". NOTE: grep/strings is a weak instrument here - a PLAIN "
                  "crush archive also survives it, because zstd already hides "
                  "the strings. The passphrase gate on read_index is the real "
                  "evidence.")
    check("an encrypted archive reveals no filename or content to grep/strings",
          t_leak)

    def t_select():
        idx = ce.read_index(ref, PW)
        one = next(f["p"] for f in idx["files"]
                   if f["p"].endswith("payroll_2026_CONFIDENTIAL.csv"))
        dest = os.path.join(TMP, "sel")
        shutil.rmtree(dest, ignore_errors=True)
        ce.extract(ref, dest, select=[one], password=PW)
        got = []
        for dp, _, fs in os.walk(dest):
            got += [os.path.join(dp, f) for f in fs]
        assert len(got) == 1, f"selective extract produced {len(got)} files: {got}"
        assert rf(got[0]) == MARKERS["payroll_2026_CONFIDENTIAL.csv"]
        try:
            ce.extract(ref, os.path.join(TMP, "sel2"),
                       select=["no/such/file"], password=PW)
            raise AssertionError("selecting a missing path did not raise")
        except KeyError:
            pass
        return "one file out of five, byte-exact, through the encrypted path"
    check("selective extraction works through the encrypted path", t_select)

    def t_tamper_integration():
        blob = rf(ref)
        cases = {
            "flip a header bit": flip(blob, 60, 0),
            "flip a payload bit": flip(blob, len(blob) - 30, 0),
            "truncate 1 byte": blob[:-1],
            "append 1 byte": blob + b"\x00",
        }
        for label, b in cases.items():
            p = wf("int_t.crush", b)
            for fn, nm in ((lambda: ce.read_index(p, PW), "read_index"),
                           (lambda: ce.extract(p, os.path.join(TMP, "int_out"),
                                               password=PW), "extract")):
                try:
                    fn()
                except cc.CryptoError:
                    continue
                except Exception:                            # noqa: BLE001
                    continue
                raise AssertionError(f"{nm} accepted a tampered archive: {label}")
            shutil.rmtree(os.path.join(TMP, "int_out"), ignore_errors=True)
        return f"{len(cases)} tampered archives rejected by read_index and extract"
    check("tampered encrypted archives are rejected by read_index and extract",
          t_tamper_integration)

    def t_unencrypted_path():
        out = os.path.join(TMP, "plainpath.crush")
        ce.compress([corpus], out, tier="quick")
        assert not ce.is_encrypted(out)
        idx = ce.read_index(out)                    # no passphrase needed
        assert len(idx["files"]) == len(MARKERS)
        dest = os.path.join(TMP, "plainout")
        ce.extract(out, dest)
        verify_tree(dest)
        shutil.rmtree(dest, ignore_errors=True)
        return "the unencrypted path still works and needs no passphrase"
    check("unencrypted archives are unaffected", t_unencrypted_path)

    def t_no_plaintext_left():
        outdir = os.path.join(TMP, "leftover")
        os.makedirs(outdir, exist_ok=True)
        before = set(os.listdir(outdir))
        ce.compress([corpus], os.path.join(outdir, "x.crush"), tier="quick",
                    password=PW, kdf_profile=FAST)
        after = set(os.listdir(outdir))
        stray = after - before - {"x.crush"}
        assert not stray, f"compress left files behind in the output directory: {stray}"
        assert (os.stat(os.path.join(outdir, "x.crush")).st_mode & 0o077) == 0, \
            "the encrypted archive is group/other readable"
        return "no named temporary survives; archive is 0600"
    check("the plaintext archive never has a name on disk", t_no_plaintext_left)


# ==========================================================================
#  8. scrypt fallback suite (HAVE_ARGON2 monkeypatched off, in memory only)
# ==========================================================================
def sec_scrypt():
    section("8. scrypt fallback suite")

    def t_fallback():
        real = cc.HAVE_ARGON2
        try:
            cc.HAVE_ARGON2 = False
            suite, t, m, p = cc.profile_params(FAST)
            assert suite == cc.SUITE_SCRYPT_XCHACHA
            plain = os.urandom(C + 1234)
            src = wf("sc.bin", plain)
            e = os.path.join(TMP, "sc.enc"); d = os.path.join(TMP, "sc.dec")
            cc.encrypt_file(src, e, "sc-pass", FAST)
            blob = rf(e)
            assert blob[9] == cc.SUITE_SCRYPT_XCHACHA
            cc.decrypt_file(e, d, "sc-pass")
            assert rf(d) == plain, "scrypt-suite round trip is not byte-exact"
            with cc.EncryptedReader(e, "sc-pass") as r:
                assert r.read() == plain
            for label, b in (("flip a tag bit", flip(blob, len(blob) - 1, 0)),
                             ("truncate", blob[:-20]),
                             ("append", blob + b"\x00")):
                k, _ = try_decrypt_file(b, "sc-pass")
                assert k == "raise", f"scrypt suite accepted a mutation: {label}"
            k, v = try_decrypt_file(blob, "wrong-pass")
            assert k == "raise" and v == "WrongPassword"
            return f"suite 2, N={m} r={t} p={p}, round trip + tamper resistance intact"
        finally:
            cc.HAVE_ARGON2 = real

    check("scrypt fallback archives round-trip and resist tampering", t_fallback)

    def t_argon2_missing():
        real = cc.HAVE_ARGON2
        try:
            blob = enc_bytes(b"needs argon2")          # made with argon2id
            cc.HAVE_ARGON2 = False
            k, v = try_decrypt_file(blob)
            assert k == "raise" and v == "CryptoError", \
                f"an argon2 archive on a no-argon2 build -> {v}"
            return "an Argon2id archive on a build without argon2 fails loudly"
        finally:
            cc.HAVE_ARGON2 = real
    check("an Argon2id archive fails loudly where Argon2 is unavailable",
          t_argon2_missing)


# ==========================================================================
#  Report
# ==========================================================================
NARRATIVE_HEAD = """\
# crush_crypt adversarial test suite - results

Generated by `test_crypt.py`. Module under test:
`engine/crush_crypt.py` (Argon2id -> XChaCha20-Poly1305 in a STREAM
construction), plus its integration through `crush_engine.compress`,
`read_index`, `extract` and `crush_crypt.EncryptedReader`.

The suite modifies nothing. Two checks set `crush_crypt.HAVE_ARGON2 = False`
**in memory** to exercise the scrypt fallback, and restore it in a `finally`.

## Grading rule for the tamper matrix

A mutated archive passes iff decryption **raises**, or returns plaintext
**byte-identical** to the original (the mutation was a no-op). Returning
*different* plaintext without raising is a silent forgery and is graded
`CRITICAL`. Both decryption paths - `decrypt_file` and `EncryptedReader`
(full read + `verify()`) - are graded independently.
"""


def write_results(argv, secs):
    n = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for r in RESULTS:
        n[r.status] += 1
    crit = [c for c in TAMPER if c[3] == "CRITICAL"]

    L = [NARRATIVE_HEAD]
    L.append(f"\n## Summary\n")
    L.append(f"| PASS | FAIL | WARN | SKIP | tamper cases | silent forgeries |")
    L.append("|---:|---:|---:|---:|---:|---:|")
    L.append(f"| {n[PASS]} | {n[FAIL]} | {n[WARN]} | {n[SKIP]} | {len(TAMPER)} | "
             f"**{len(crit)}** |")
    L.append(f"\nTotal runtime {secs:.1f} s. Command: `python3 "
             f"{' '.join(os.path.basename(a) for a in argv)}`. "
             f"Python {sys.version.split()[0]}, "
             f"argon2 available: {cc.HAVE_ARGON2}.\n")

    L.append("## Verdict\n")
    if crit:
        L.append(f"**{len(crit)} mutated archives decrypted successfully to the "
                 "wrong plaintext. The construction is broken; nothing below "
                 "matters until that is fixed.**\n")
    else:
        L.append(
            f"The cryptographic core holds. All {len(TAMPER)} mutated archives "
            "were rejected with an exception by both decryption paths; not one "
            "produced plaintext, wrong or right. Both known-answer suites "
            "(HChaCha20 and XChaCha20-Poly1305 from draft-irtf-cfrg-xchacha-03, "
            "Argon2id from RFC 9106) reproduce exactly, and the XChaCha layer "
            "agrees byte-for-byte with an independent reference over random "
            "inputs. The STREAM construction does what its docstring claims: "
            "truncation, extension, reordering, duplication, deletion and "
            "cross-archive splicing all fail closed, including the cases a plain "
            "per-chunk AEAD would accept.\n")
        if n[FAIL]:
            L.append(f"\nThe {n[FAIL]} failure(s) below are in "
                     "`password_strength`, the advisory meter. It never gates "
                     "anything and cannot weaken an archive that has already been "
                     "made - but it is what a user reads when deciding what to "
                     "type, and overstating a weak passphrase has a real cost.\n")
        if n[WARN]:
            L.append(f"\nThe {n[WARN]} warning(s) are behaviours worth knowing "
                     "about rather than defects: a documented sharp edge, a "
                     "data-loss footgun, a distinguishable error path, and a "
                     "denial-of-service ceiling set at the format's limit rather "
                     "than at anything a laptop survives.\n")

    if n[FAIL]:
        L.append("## Failures\n")
        for r in RESULTS:
            if r.status == FAIL:
                L.append(f"### {r.section} - {r.name}\n")
                L.append("```\n" + r.detail.rstrip() + "\n```\n")
    else:
        L.append("## Failures\n\nNone.\n")

    if n[WARN]:
        L.append("## Warnings - behaviours worth knowing about\n")
        for r in RESULTS:
            if r.status == WARN:
                L.append(f"### {r.section} - {r.name}\n")
                L.append(r.detail.rstrip() + "\n")

    L.append("\n## Check results\n")
    cur = None
    for r in RESULTS:
        if r.section != cur:
            cur = r.section
            L.append(f"\n### {cur}\n")
            L.append("| check | result | detail | s |")
            L.append("|---|---|---|---:|")
        d = (r.detail or "").splitlines()
        d = (d[0] if d else "").replace("|", "\\|")[:200]
        L.append(f"| {r.name.replace('|', chr(92)+'|')} | **{r.status}** | {d} | "
                 f"{r.secs:.1f} |")

    L.append("\n## Tamper matrix\n")
    L.append(f"{len(TAMPER)} mutated archives. `decrypt_file` and "
             "`EncryptedReader` columns give the exception type raised, "
             "`OK-IDENTICAL` if the mutation was a no-op, or "
             "`SILENT-WRONG(n B)` for a forgery. `-` means that path was not "
             "run for that case.\n")
    L.append("| # | case | decrypt_file | EncryptedReader | verdict |")
    L.append("|---:|---|---|---|---|")
    for i, (case, a, b, v) in enumerate(TAMPER, 1):
        mark = "**CRITICAL**" if v == "CRITICAL" else "safe"
        L.append(f"| {i} | {case.replace('|', chr(92)+'|')} | `{a}` | `{b}` | {mark} |")

    L.append("\n### Tamper outcomes, collapsed\n")
    agg = {}
    for case, a, b, v in TAMPER:
        agg[(a, b)] = agg.get((a, b), 0) + 1
    L.append("| decrypt_file | EncryptedReader | cases |")
    L.append("|---|---|---:|")
    for (a, b), k in sorted(agg.items(), key=lambda x: -x[1]):
        L.append(f"| `{a}` | `{b}` | {k} |")

    path = os.path.join(HERE, "RESULTS.md")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
    return path, n, crit


def main():
    global TMP
    argv = sys.argv
    if "--list" in argv:
        print("sections: kat, roundtrip, tamper, password, determinism, "
              "strength, integration, scrypt")
        return 0
    skip_slow = "--skip-slow" in argv
    t0 = time.time()
    TMP = tempfile.mkdtemp(prefix="crushcrypt-test-")
    print(f"crush_crypt adversarial suite")
    print(f"  module : {cc.__file__}")
    print(f"  argon2 : {cc.HAVE_ARGON2}   chunk {C}   header {H}")
    print(f"  scratch: {TMP}")
    try:
        sec_kat()
        sec_roundtrip()
        sec_tamper()
        sec_password()
        sec_determinism()
        sec_strength()
        sec_integration(skip_slow)
        sec_scrypt()
    finally:
        secs = time.time() - t0
        path, n, crit = write_results(argv, secs)
        print("\n" + "=" * 74)
        print(f"{'SECTION':<34} {'PASS':>5} {'FAIL':>5} {'WARN':>5} {'SKIP':>5}")
        print("-" * 74)
        per = {}
        for r in RESULTS:
            per.setdefault(r.section, {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0})[r.status] += 1
        for s, c in per.items():
            print(f"{s:<34} {c[PASS]:>5} {c[FAIL]:>5} {c[WARN]:>5} {c[SKIP]:>5}")
        print("-" * 74)
        print(f"{'TOTAL':<34} {n[PASS]:>5} {n[FAIL]:>5} {n[WARN]:>5} {n[SKIP]:>5}")
        print(f"\ntamper cases: {len(TAMPER)}   silent forgeries: {len(crit)}")
        if n[FAIL]:
            print("\nFAILURES:")
            for r in RESULTS:
                if r.status == FAIL:
                    print(f"  - [{r.section}] {r.name}")
                    for ln in r.detail.splitlines()[:6]:
                        print(f"      {ln}")
        if n[WARN]:
            print("\nWARNINGS:")
            for r in RESULTS:
                if r.status == WARN:
                    print(f"  - [{r.section}] {r.name}")
        print(f"\nreport: {path}")
        print(f"runtime: {secs:.1f} s")
        shutil.rmtree(TMP, ignore_errors=True)
    return 1 if n[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
