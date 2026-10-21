"""Crush encryption - Argon2id + XChaCha20-Poly1305 in STREAM mode.

DESIGN NOTES, so the choices can be checked rather than taken on trust.

  Threat model
  ------------
  The adversary has the .crush file and unlimited offline time. They may also
  modify it and hand it back. They must learn nothing about the contents beyond
  the file's length, and must not be able to make Crush emit anything other than
  the original plaintext or a hard error. They do NOT have the passphrase, and
  they do not have code execution on the machine while Crush is running - no
  file format can defend against that.

  Key hierarchy
  -------------
    passphrase + 16-byte random salt
       -> Argon2id(t, m, p)                  -> 32-byte KEK
    random 32-byte file key (FK) from the OS CSPRNG
       -> wrapped with XChaCha20-Poly1305 under KEK, AAD = the whole header
       -> HKDF-SHA256(FK, info="crush/stream/v1")   -> 32-byte stream key

  Every archive gets a fresh salt and a fresh FK, so no key and no nonce is ever
  reused across archives even if the same passphrase is used every time. The KEK
  encrypts exactly one message in its life, which is why a fixed all-zero nonce
  is correct for the wrap rather than merely convenient.

  Because the header is the wrap's associated data, changing a single bit of it -
  the KDF cost, the salt, the version, anything - makes the unwrap fail. The
  header does not need its own MAC.

  Cipher and nonces
  -----------------
  XChaCha20-Poly1305: a 24-byte nonce, of which the first 16 bytes select an
  HChaCha20 subkey and the last 8 are used as the ChaCha20-Poly1305 nonce with a
  4-byte zero prefix. The wide nonce is what makes random nonces safe; here it
  also gives room for an explicit counter.

  Payload is encrypted in 256 KiB chunks under the STREAM construction of
  Hoang-Reyhanitabar-Rogaway-Vizar - the same one `age` uses:

      nonce = 16-byte random stream prefix || 7-byte big-endian counter
              || 1-byte final flag

  The counter makes reordering, duplicating or dropping an interior chunk fail.
  The final flag - 0x01 on the last chunk and 0x00 on every other - makes
  truncation fail, which a plain per-chunk AEAD does not: without it, an attacker
  can cut the file short and every remaining chunk still authenticates. The
  decoder additionally refuses a stream whose last chunk is not flagged.

  Order of operations
  -------------------
  Compress, then encrypt. The reverse cannot work: ciphertext is
  indistinguishable from noise and does not compress. This has a known and
  unavoidable consequence, stated here rather than buried - THE LENGTH OF THE
  OUTPUT LEAKS INFORMATION ABOUT THE INPUT, because it is the compressed length.
  An attacker who can make you archive a file of their choosing alongside your
  secret can, in principle, learn something from how well the two compress
  together. This is the same effect behind CRIME and BREACH. It is inherent to
  compressing before encrypting and no archive format avoids it.

  What is hidden and what is not
  ------------------------------
  Hidden: file names, file sizes, directory structure, file count, types, hashes,
  and all content. The entire archive including its index is encrypted; an
  encrypted archive cannot even be listed without the passphrase.

  Not hidden: that the file is a Crush archive, the format version, the KDF cost
  parameters, and the total ciphertext length (from which the compressed size is
  known to within a few dozen bytes).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import struct
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidTag

try:
    from argon2.low_level import Type as _ArgonType, hash_secret_raw as _argon2_raw
    HAVE_ARGON2 = True
except Exception:                                  # pragma: no cover
    HAVE_ARGON2 = False


# ==========================================================================
#  Format constants
# ==========================================================================
MAGIC = b"CRUSHENC"
VERSION = 1

SUITE_ARGON2ID_XCHACHA = 1      # Argon2id -> XChaCha20-Poly1305-STREAM
SUITE_SCRYPT_XCHACHA = 2        # scrypt   -> XChaCha20-Poly1305-STREAM (fallback)

SALT_LEN = 16
KEY_LEN = 32
STREAM_PREFIX_LEN = 16          # the HChaCha20 half of the XChaCha nonce
WRAP_LEN = KEY_LEN + 16         # ciphertext + Poly1305 tag
CHUNK = 256 * 1024
TAG_LEN = 16

MAX_COUNTER = (1 << 56) - 1     # 7-byte counter; 256 KiB chunks -> 16 EiB

# Argon2id profiles: (time_cost, memory_kib, parallelism)
PROFILES = {
    "fast":     (2, 64 * 1024, 4),
    "balanced": (3, 256 * 1024, 4),
    "paranoid": (4, 1024 * 1024, 4),
}
DEFAULT_PROFILE = "balanced"

# scrypt fallback, only if argon2-cffi is genuinely unavailable.
SCRYPT_PARAMS = {
    "fast":     (1 << 15, 8, 2),
    "balanced": (1 << 17, 8, 2),
    "paranoid": (1 << 19, 8, 2),
}


class CryptoError(Exception):
    """Base for every failure in this module."""


class WrongPassword(CryptoError):
    """The passphrase did not open the archive.

    A tampered salt, stream prefix, commitment or wrapped key raises this too,
    and deliberately does not say which: those bytes are secret-dependent, and
    distinguishing them would tell an attacker something about their guess.

    Damage to the fields that are NOT secret-dependent - the magic, the version,
    the suite, the KDF costs - raises a specific CryptoError instead. Those bytes
    are cleartext in every archive, so an attacker already knows them and naming
    the problem costs nothing while making a real corruption diagnosable."""


class CorruptArchive(CryptoError):
    """Authentication failed on the payload, or the stream ended early."""


class NotEncrypted(CryptoError):
    """The file is not an encrypted Crush archive."""


# ==========================================================================
#  XChaCha20-Poly1305
# ==========================================================================
def _hchacha20(key: bytes, nonce16: bytes) -> bytes:
    """HChaCha20 subkey derivation (RFC draft-irtf-cfrg-xchacha, section 2.2).

    ChaCha20's block function with the standard constants, the key, and the
    16-byte nonce as the counter+nonce words, returning words 0-3 and 12-15
    WITHOUT the feed-forward addition. `cryptography` exposes ChaCha20 only as a
    stream cipher, so the block is obtained by encrypting 64 zero bytes with
    counter/nonce set to nonce16 - which yields exactly the (unfed-forward)
    keystream block - and the two 16-byte halves are read back out.
    """
    if len(key) != 32:
        raise ValueError("HChaCha20 key must be 32 bytes")
    if len(nonce16) != 16:
        raise ValueError("HChaCha20 nonce must be 16 bytes")
    enc = Cipher(algorithms.ChaCha20(key, nonce16), mode=None).encryptor()
    block = enc.update(b"\x00" * 64) + enc.finalize()
    # The keystream block is the ChaCha20 state after 20 rounds PLUS the initial
    # state. HChaCha20 wants it without that addition, so subtract the initial
    # state back off for the eight words we keep.
    consts = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
    out = bytearray(32)
    ks = struct.unpack("<16I", block)
    init = list(consts) + list(struct.unpack("<8I", key)) + list(struct.unpack("<4I", nonce16))
    keep = [0, 1, 2, 3, 12, 13, 14, 15]
    for i, w in enumerate(keep):
        struct.pack_into("<I", out, i * 4, (ks[w] - init[w]) & 0xFFFFFFFF)
    return bytes(out)


class XChaCha20Poly1305:
    """XChaCha20-Poly1305 AEAD: a 24-byte nonce over ChaCha20-Poly1305."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes):
        if len(key) != KEY_LEN:
            raise ValueError("key must be 32 bytes")
        self._key = key

    def _sub(self, nonce24: bytes):
        if len(nonce24) != 24:
            raise ValueError("nonce must be 24 bytes")
        subkey = _hchacha20(self._key, nonce24[:16])
        return ChaCha20Poly1305(subkey), b"\x00\x00\x00\x00" + nonce24[16:]

    def encrypt(self, nonce24: bytes, data: bytes, aad: Optional[bytes]) -> bytes:
        aead, n12 = self._sub(nonce24)
        return aead.encrypt(n12, data, aad)

    def decrypt(self, nonce24: bytes, data: bytes, aad: Optional[bytes]) -> bytes:
        aead, n12 = self._sub(nonce24)
        return aead.decrypt(n12, data, aad)


# ==========================================================================
#  Header
# ==========================================================================
#  magic        8   b"CRUSHENC"
#  version      1
#  suite        1
#  kdf t        4   big-endian
#  kdf m        4   big-endian (KiB for argon2, N for scrypt)
#  kdf p        4   big-endian
#  salt        16
#  prefix      16   random stream-nonce prefix
#  commit      32   HKDF(KEK, "crush/commit/v1") - key commitment
#  wrapped     48   file key sealed under the KEK, AAD = bytes 0..85 above
#  ----------------
#  total      134
COMMIT_LEN = 32
HEADER_PREFIX_LEN = 8 + 1 + 1 + 4 + 4 + 4 + SALT_LEN + STREAM_PREFIX_LEN + COMMIT_LEN   # 86
HEADER_LEN = HEADER_PREFIX_LEN + WRAP_LEN                                                # 134


def _pack_header_prefix(suite: int, t: int, m: int, p: int,
                        salt: bytes, prefix: bytes, commit: bytes) -> bytes:
    return (MAGIC + bytes([VERSION, suite]) + struct.pack(">III", t, m, p)
            + salt + prefix + commit)


def _commitment(kek: bytes) -> bytes:
    """A key commitment.

    AEADs in the ChaCha20-Poly1305 family are not key-committing: it is possible
    to construct a ciphertext that decrypts successfully under two different
    keys. Nothing in this design gives an attacker a decryption oracle, so it is
    not exploitable here - but the module promises that a wrong key yields "a
    hard error", and that promise should be true for ANY key rather than true
    because of a 16-byte tag. This binds the header to exactly one KEK."""
    return HKDF(algorithm=hashes.SHA256(), length=COMMIT_LEN, salt=None,
                info=b"crush/commit/v1").derive(kek)


def _derive_kek(password: bytes, salt: bytes, suite: int,
                t: int, m: int, p: int) -> bytes:
    if suite == SUITE_ARGON2ID_XCHACHA:
        if not HAVE_ARGON2:
            raise CryptoError(
                "this archive needs Argon2id, which is not installed here.\n"
                "Run:  pip install argon2-cffi")
        try:
            return _argon2_raw(secret=password, salt=salt, time_cost=t,
                               memory_cost=m, parallelism=p, hash_len=KEY_LEN,
                               type=_ArgonType.ID)
        except CryptoError:
            raise
        except Exception as e:
            # argon2-cffi and scrypt raise their own types, including plain
            # ValueError when the platform refuses the allocation. Callers catch
            # CryptoError; anything else escapes as an unhandled traceback.
            raise CryptoError(f"the key derivation could not run: {e}") from None
    if suite == SUITE_SCRYPT_XCHACHA:
        try:
            return hashlib.scrypt(password, salt=salt, n=m, r=t, p=p,
                                  dklen=KEY_LEN, maxmem=2 * m * t * 128 + (1 << 26))
        except Exception as e:
            raise CryptoError(f"the key derivation could not run: {e}") from None
    raise CryptoError(f"unknown cipher suite {suite}")


def _stream_key(file_key: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=None,
                info=b"crush/stream/v1").derive(file_key)


def _nonce(prefix: bytes, counter: int, final: bool) -> bytes:
    if counter > MAX_COUNTER:
        raise CryptoError("archive too large for one key")
    return prefix + counter.to_bytes(7, "big") + (b"\x01" if final else b"\x00")


def _read_exact(f, n: int) -> bytes:
    """Read exactly n bytes unless the source is genuinely exhausted.

    `read()` is allowed to return fewer bytes than asked for - pipes, sockets and
    TLS streams routinely do - and this format's chunk boundaries are implied by
    position, not marked. A short read at encryption time therefore produces an
    archive that is silently, permanently undecryptable. So: loop."""
    out = bytearray()
    while len(out) < n:
        part = f.read(n - len(out))
        if not part:
            break
        out += part
    return bytes(out)


def _norm_password(password) -> bytes:
    if isinstance(password, bytes):
        pw = password
    elif isinstance(password, str):
        # NFC so the same typed passphrase derives the same key regardless of
        # how the platform's keyboard composed its accents.
        import unicodedata
        pw = unicodedata.normalize("NFC", password).encode("utf-8")
    else:
        raise TypeError("password must be str or bytes")
    if not pw:
        raise CryptoError("the passphrase is empty")
    return pw


# ==========================================================================
#  Public API
# ==========================================================================
def profile_params(profile: str):
    profile = profile if profile in PROFILES else DEFAULT_PROFILE
    if HAVE_ARGON2:
        t, m, p = PROFILES[profile]
        return SUITE_ARGON2ID_XCHACHA, t, m, p
    n, r, p = SCRYPT_PARAMS.get(profile, SCRYPT_PARAMS[DEFAULT_PROFILE])
    return SUITE_SCRYPT_XCHACHA, r, n, p


def is_encrypted(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == MAGIC
    except OSError:
        return False


def header_info(path: str) -> dict:
    """What can be read from an encrypted archive WITHOUT the passphrase.

    Deliberately little: enough to tell the user what they are looking at and
    how long unlocking will take, and nothing about the contents."""
    with open(path, "rb") as f:
        h = f.read(HEADER_LEN)
    if len(h) < HEADER_LEN or h[:8] != MAGIC:
        raise NotEncrypted("not an encrypted Crush archive")
    ver, suite = h[8], h[9]
    t, m, p = struct.unpack(">III", h[10:22])
    sane = True
    try:
        _sanity_kdf(suite, t, m, p)
    except CryptoError:
        sane = False            # a header asking for an implausible unlock cost
    return {"version": ver, "suite": suite,
            "kdf": "argon2id" if suite == SUITE_ARGON2ID_XCHACHA else "scrypt",
            "t": t, "m": m, "p": p, "size": os.path.getsize(path),
            "sane": sane,
            "supported": sane and ver == VERSION and suite in (SUITE_ARGON2ID_XCHACHA,
                                                               SUITE_SCRYPT_XCHACHA)}


def _parse_header(header: bytes, password) -> tuple:
    """Validate a header and open it. Returns (aead, prefix).

    Order matters: bounds first (so an untrusted header cannot make the KDF do
    unbounded work), then the key commitment (a constant-time comparison that
    rejects a wrong key outright), then the AEAD unwrap."""
    if len(header) < HEADER_LEN or header[:8] != MAGIC:
        raise NotEncrypted("not an encrypted Crush archive")
    ver, suite = header[8], header[9]
    if ver != VERSION:
        raise CryptoError(f"this archive is format version {ver}; "
                          f"this build understands {VERSION}")
    t, m, p = struct.unpack(">III", header[10:22])
    _sanity_kdf(suite, t, m, p)
    salt = header[22:22 + SALT_LEN]
    off = 22 + SALT_LEN
    prefix = header[off:off + STREAM_PREFIX_LEN]
    off += STREAM_PREFIX_LEN
    commit = header[off:off + COMMIT_LEN]
    wrapped = header[HEADER_PREFIX_LEN:HEADER_LEN]

    kek = _derive_kek(_norm_password(password), salt, suite, t, m, p)
    if not hmac.compare_digest(commit, _commitment(kek)):
        raise WrongPassword("wrong passphrase, or this archive is damaged")
    try:
        file_key = XChaCha20Poly1305(kek).decrypt(
            b"\x00" * 24, wrapped, header[:HEADER_PREFIX_LEN])
    except InvalidTag:
        raise WrongPassword("wrong passphrase, or this archive is damaged")
    return XChaCha20Poly1305(_stream_key(file_key)), prefix


def encrypt_stream(fin, fout, password, profile: str = DEFAULT_PROFILE,
                   progress=None, total: int = 0) -> dict:
    """Encrypt the readable fin into the writable fout. Memory use is one chunk.

    Taking file objects rather than paths is what lets the caller keep the
    plaintext in an unlinked temporary file that never has a name on disk."""
    pw = _norm_password(password)
    suite, t, m, p = profile_params(profile)
    salt = secrets.token_bytes(SALT_LEN)
    prefix = secrets.token_bytes(STREAM_PREFIX_LEN)

    kek = _derive_kek(pw, salt, suite, t, m, p)
    file_key = secrets.token_bytes(KEY_LEN)
    hdr_prefix = _pack_header_prefix(suite, t, m, p, salt, prefix, _commitment(kek))
    wrapped = XChaCha20Poly1305(kek).encrypt(b"\x00" * 24, file_key, hdr_prefix)
    header = hdr_prefix + wrapped
    if len(header) != HEADER_LEN:                       # not an assert: -O strips those
        raise CryptoError("internal error: bad header length")

    aead = XChaCha20Poly1305(_stream_key(file_key))
    fout.write(header)
    counter, done = 0, 0
    block = _read_exact(fin, CHUNK)
    while True:
        nxt = _read_exact(fin, CHUNK)
        last = not nxt
        fout.write(aead.encrypt(_nonce(prefix, counter, last), block, None))
        done += len(block)
        if progress and (counter % 16 == 0 or last):
            progress({"stage": "encrypt", "i": done, "n": max(1, total or done)})
        if last:
            break
        block = nxt
        counter += 1
    return {"header": HEADER_LEN, "chunks": counter + 1, "suite": suite,
            "profile": profile, "in": done}


def encrypt_file(src: str, dst: str, password, profile: str = DEFAULT_PROFILE,
                 progress=None) -> dict:
    """Encrypt src into dst. Streams; memory use is one chunk."""
    pw = _norm_password(password)
    suite, t, m, p = profile_params(profile)
    salt = secrets.token_bytes(SALT_LEN)
    prefix = secrets.token_bytes(STREAM_PREFIX_LEN)

    kek = _derive_kek(pw, salt, suite, t, m, p)
    file_key = secrets.token_bytes(KEY_LEN)
    hdr_prefix = _pack_header_prefix(suite, t, m, p, salt, prefix, _commitment(kek))
    # The KEK enciphers exactly one message ever, so a fixed nonce is correct.
    wrapped = XChaCha20Poly1305(kek).encrypt(b"\x00" * 24, file_key, hdr_prefix)
    header = hdr_prefix + wrapped
    if len(header) != HEADER_LEN:
        raise CryptoError("internal error: bad header length")

    aead = XChaCha20Poly1305(_stream_key(file_key))
    total = os.path.getsize(src)
    done = 0
    # Written 0600 from the start: an archive must never be briefly world
    # readable between creation and the first byte of ciphertext.
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
            fout.write(header)
            counter = 0
            block = _read_exact(fin, CHUNK)
            while True:
                nxt = _read_exact(fin, CHUNK)
                last = not nxt
                fout.write(aead.encrypt(_nonce(prefix, counter, last), block, None))
                done += len(block)
                if progress and (counter % 16 == 0 or last):
                    progress({"stage": "encrypt", "i": done, "n": max(1, total)})
                if last:
                    break
                block = nxt
                counter += 1
    except BaseException:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise
    return {"header": HEADER_LEN, "chunks": counter + 1, "suite": suite,
            "profile": profile, "in": total, "out": os.path.getsize(dst)}


def decrypt_file(src: str, dst: str, password, progress=None) -> dict:
    """Decrypt src into dst.

    The output is written to a scratch name beside dst and moved into place only
    after the final chunk has authenticated, so a corrupt or tampered archive can
    never destroy a file that is already sitting at dst. The scratch file is
    created O_EXCL|O_NOFOLLOW at 0600."""
    with open(src, "rb") as fin:
        aead, prefix = _parse_header(_read_exact(fin, HEADER_LEN), password)
        total = os.path.getsize(src)
        enc_chunk = CHUNK + TAG_LEN
        done = 0
        part = f"{dst}.part-{secrets.token_hex(6)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        fd = os.open(part, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as fout:
                counter = 0
                block = _read_exact(fin, enc_chunk)
                if not block:
                    raise CorruptArchive("the archive has no payload")
                while True:
                    nxt = _read_exact(fin, enc_chunk)
                    last = not nxt
                    try:
                        plain = aead.decrypt(_nonce(prefix, counter, last), block, None)
                    except InvalidTag:
                        # A failure on what looks like the final chunk is exactly
                        # what a truncated file produces: the real chunk there was
                        # flagged 0x00 and this one is being tried as 0x01.
                        raise CorruptArchive(
                            "the archive failed its integrity check at chunk "
                            f"{counter} - it has been altered or truncated")
                    fout.write(plain)
                    done += len(block)
                    if progress and (counter % 16 == 0 or last):
                        progress({"stage": "decrypt", "i": done, "n": max(1, total)})
                    if last:
                        break
                    block = nxt
                    counter += 1
                fout.flush()
                os.fsync(fout.fileno())
            os.replace(part, dst)
        except BaseException:
            try:
                os.unlink(part)
            except OSError:
                pass
            raise
    return {"chunks": counter + 1, "out": os.path.getsize(dst)}


class EncryptedReader:
    """A read-only, seekable view of an encrypted archive's plaintext.

    The archive format needs random access - it seeks to the index, then to each
    bundle - and the obvious way to give it that is to decrypt the whole file to
    a scratch file first. This class exists so that never has to happen: the
    plaintext is produced 256 KiB at a time, in memory, only for the regions
    actually read, and no decrypted byte is ever written to disk.

    Every chunk it hands back has been authenticated. A chunk that is never read
    is never checked, so `verify()` streams the whole payload when a caller wants
    the guarantee for the entire file rather than for what it happened to use.
    """

    def __init__(self, path: str, password, verify_all: bool = False):
        self._f = open(path, "rb")
        try:
            self._aead, self._prefix = _parse_header(
                _read_exact(self._f, HEADER_LEN), password)

            csize = os.path.getsize(path) - HEADER_LEN
            if csize < TAG_LEN:
                raise CorruptArchive("the archive has no payload")
            enc = CHUNK + TAG_LEN
            full, rem = divmod(csize, enc)
            if rem == 0:
                self._n = full
                last_plain = CHUNK
            else:
                if rem < TAG_LEN:
                    raise CorruptArchive("the archive ends mid-chunk")
                self._n = full + 1
                last_plain = rem - TAG_LEN
            self._size = (self._n - 1) * CHUNK + last_plain
            self._pos = 0
            self._ci = -1              # index of the cached chunk
            self._cache = b""

            # The final chunk is authenticated NOW, before anything is read.
            # Its 0x01 flag is what makes truncation detectable, and a caller
            # that never happens to read the end of the file would otherwise
            # never check it - which is precisely how a truncated archive could
            # be listed without complaint.
            self._chunk(self._n - 1)
            if verify_all:
                self.verify()
        except BaseException:
            self._f.close()
            raise

    # -- file-like ---------------------------------------------------------
    def readable(self):  return True
    def writable(self):  return False
    def seekable(self):  return True
    def tell(self):      return self._pos

    def seek(self, off, whence=0):
        if whence == 0:   new = off
        elif whence == 1: new = self._pos + off
        elif whence == 2: new = self._size + off
        else:             raise ValueError("bad whence")
        if new < 0:
            raise OSError("negative seek")
        self._pos = new
        return self._pos

    def _chunk(self, i: int) -> bytes:
        if i == self._ci:
            return self._cache
        if not (0 <= i < self._n):
            raise CorruptArchive("read past the end of the archive")
        enc = CHUNK + TAG_LEN
        self._f.seek(HEADER_LEN + i * enc)
        blob = _read_exact(self._f, enc)
        last = (i == self._n - 1)
        if not blob or (not last and len(blob) != enc):
            raise CorruptArchive(f"the archive ends early at chunk {i}")
        try:
            plain = self._aead.decrypt(_nonce(self._prefix, i, last), blob, None)
        except InvalidTag:
            raise CorruptArchive(
                f"the archive failed its integrity check at chunk {i} - "
                "it has been altered or truncated")
        self._ci, self._cache = i, plain
        return plain

    def read(self, n=-1):
        if n is None or n < 0:
            n = max(0, self._size - self._pos)
        out = bytearray()
        while n > 0 and self._pos < self._size:
            i, off = divmod(self._pos, CHUNK)
            part = self._chunk(i)[off:off + n]
            if not part:
                break
            out += part
            self._pos += len(part)
            n -= len(part)
        return bytes(out)

    def verify(self, progress=None) -> bool:
        """Authenticate every chunk, including any this session never read."""
        for i in range(self._n):
            self._chunk(i)
            if progress and (i % 16 == 0 or i == self._n - 1):
                progress({"stage": "verify", "i": i + 1, "n": self._n})
        return True

    @property
    def plaintext_size(self): return self._size

    def close(self):
        self._cache = b""
        try:
            self._f.close()
        except OSError:
            pass

    def __enter__(self):  return self
    def __exit__(self, *a): self.close(); return False


def _sanity_kdf(suite: int, t: int, m: int, p: int) -> None:
    """Refuse implausible KDF parameters from an untrusted header.

    This is the one place where an attacker's bytes drive work before anything
    has been authenticated, because here authentication IS key derivation: to
    find out whether a header is genuine, Crush must first run the KDF the
    header asks for. A hostile 134-byte file can therefore ask for as much
    memory and as many passes as the ceiling allows.

    The ceiling is set to the most expensive profile Crush itself writes, plus
    a small margin - not to the widest value the fields can hold. The cost of
    the WORST accepted header is bounded at roughly the paranoid profile, so an
    intercepted archive cannot be turned into a memory bomb by flipping a few
    header bytes. Argon2's own m >= 8p rule is enforced here too, so an invalid
    combination is refused rather than raising out of the library."""
    if suite == SUITE_ARGON2ID_XCHACHA:
        if not (1 <= t <= 8):
            raise CryptoError(f"refusing an archive with Argon2 time cost {t}")
        if not (8 <= m <= 1024 * 1024):                 # at most 1 GiB
            raise CryptoError(
                f"refusing an archive that asks for {m // 1024} MB of memory")
        if not (1 <= p <= 8):
            raise CryptoError(f"refusing an archive with parallelism {p}")
        if m < 8 * p:
            raise CryptoError("refusing an archive with invalid Argon2 parameters")
        # Budget the PRODUCT, not each field: t and m are individually plausible
        # at values whose product is not. The ceiling is twice the most expensive
        # profile Crush writes, derived from PROFILES so the two cannot drift.
        _pt, _pm, _ = PROFILES["paranoid"]
        if t * m > 2 * _pt * _pm:
            raise CryptoError("refusing an archive whose unlock cost is implausible")
    elif suite == SUITE_SCRYPT_XCHACHA:
        # t carries r, m carries N.
        if not (1 <= t <= 16) or not (1 <= p <= 8):
            raise CryptoError("refusing an archive with implausible scrypt parameters")
        if m < 2 or m > (1 << 20) or (m & (m - 1)):
            raise CryptoError("refusing an archive with an implausible scrypt N")
        if 128 * m * t > 1024 * 1024 * 1024:            # at most 1 GiB
            raise CryptoError("refusing an archive whose unlock cost is implausible")
    else:
        raise CryptoError(f"unknown cipher suite {suite}")


# ==========================================================================
#  Passphrase strength - shown to the user, never used as a gate
# ==========================================================================
_COMMON = {
    "password", "123456", "12345678", "qwerty", "abc123", "letmein",
    "monkey", "dragon", "111111", "iloveyou", "trustno1", "sunshine",
    "master", "welcome", "shadow", "ashley", "football", "jesus", "michael",
    "ninja", "mustang", "password1", "123456789", "1234567890", "qwerty123",
    "admin", "login", "princess", "solo", "starwars", "passw0rd", "zaq12wsx",
    "baseball", "superman", "batman", "trustme", "whatever", "qazwsx",
    "zxcvbnm", "asdfgh", "liverpool", "jordan", "harley", "ranger", "buster",
    "thomas", "robert", "soccer", "hockey", "killer", "george", "charlie",
    "andrew", "michelle", "love", "jessica", "pepper", "daniel", "access",
    "joshua", "maggie", "silver", "william", "dallas", "yankees", "123123",
    "987654321", "samsung", "matthew", "hunter", "tigger", "purple",
    "ginger", "computer", "amanda", "summer", "nicole", "chelsea", "biteme",
    "matrix", "secret", "diamond", "freedom", "hello", "internet", "service",
    "canada", "nascar", "cowboy", "camaro", "sparky", "maverick", "blowme",
    "heather", "hammer", "taylor", "chester", "peanut", "cameron",
    "corvette", "bulldog", "banana", "chicken", "please", "money", "justin",
    "thunder", "cookie", "jackson", "scooter", "rangers", "gandalf",
    "spider", "bandit", "pookie", "porsche", "guitar", "chicago", "slayer",
    "hardcore", "bigdog", "phoenix", "mickey", "bailey", "knight", "iceman",
    "tigers", "austin", "brandy", "booboo", "coffee", "xxxxxx", "nicholas",
    "legend", "movie", "jasper", "crystal", "testing", "trouble", "gateway",
    "asshole", "wizard", "scorpio", "golden", "lakers", "cricket", "sierra",
    "eagles", "winner", "samantha", "house", "miller", "flower", "jack",
    "rachel", "yellow", "dolphin", "gemini", "apples", "fish", "june",
    "family", "morgan", "tennis", "lover", "fender", "genius", "apollo",
    "panther", "leather", "squirt", "stinger", "boston", "braves", "yamaha",
    "golfer", "rosebud", "toyota", "travis", "hotdog", "paris", "rock",
    "xxxxxxxx", "extreme", "redsox", "enter", "marlboro", "srinivas",
    "driver", "hello123", "admin123", "root", "toor", "guest", "default",
    "changeme", "abcd1234", "a1b2c3", "qwe123", "1qaz2wsx", "1q2w3e4r",
    "asdf1234", "iloveu", "babygirl", "lovely", "654321", "michael1",
    "jennifer", "hannah", "pokemon", "naruto", "snoopy", "skippy", "marina",
    "blazer", "runner", "shooter", "beaver"
}

_WORDSET = frozenset()          # filled in below, once _WORDS exists

# Leet substitutions, as a cracking rule set would apply them: each character
# may stand for itself OR for what it imitates. A single global swap misses
# "Tr0ub4dor&3", where the trailing 3 must become nothing while the 0 and 4 must
# become letters, so the candidates are enumerated instead.
_LEET = {"0": "o", "1": "il", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
         "@": "a", "$": "s", "!": "i", "|": "l", "+": "t", "9": "g", "6": "g",
         "2": "z"}
_LEET_CAP = 4096


def _deleet_candidates(pw: str):
    """Every plausible de-leeted reading of pw, capped so it cannot blow up."""
    letters = [c for c in pw.lower() if c.isalpha() or c in _LEET]
    if not letters or len(letters) > 24:
        return
    outs = [""]
    for c in letters:
        alts = (c,) if c.isalpha() else tuple(_LEET.get(c, ""))
        if not alts:
            continue
        nxt = [o + a for o in outs for a in alts]
        if len(nxt) > _LEET_CAP:
            nxt = nxt[:_LEET_CAP]
        outs = nxt
    seen = set()
    for o in outs:
        # also try dropping a trailing digit-turned-letter, the "word + number"
        # pattern that dominates real password lists
        for cand in (o, o[:-1]):
            if cand and cand not in seen:
                seen.add(cand)
                yield cand


def password_strength(pw) -> dict:
    """A deliberately conservative estimate of guessing difficulty.

    Reports the base-2 log of the search space implied by what was typed, then
    penalises the patterns real cracking tools exploit. It is advice, not a
    gate - a passphrase is never rejected."""
    import math, re
    if isinstance(pw, (bytes, bytearray)):
        pw = pw.decode("utf-8", "replace")
    elif not isinstance(pw, str):
        pw = "" if pw is None else str(pw)
    if len(pw) > 4096:
        pw = pw[:4096]                 # the meter is advice; do not chew a novel
    if not pw:
        return {"bits": 0, "label": "empty", "level": 0, "notes": ["Type something."]}

    notes = []
    lower = pw.lower()
    pool = 0
    if re.search(r"[a-z]", pw): pool += 26
    if re.search(r"[A-Z]", pw): pool += 26
    if re.search(r"[0-9]", pw): pool += 10
    if re.search(r"[^A-Za-z0-9]", pw): pool += 33
    bits = len(pw) * math.log2(pool) if pool else 0

    # A word-based passphrase must be scored by the CHEAPEST attack on it, not
    # the most expensive. "correct horse battery staple" is 28 characters, which
    # the character model calls 130 bits; a dictionary attack guessing whole
    # words needs about 4 x log2(vocabulary), which is nearer 50. Taking the
    # larger of the two is the classic way to tell someone a guessable phrase is
    # unbreakable, so this takes the SMALLER.
    words = [w for w in re.split(r"[^A-Za-z]+", pw) if len(w) >= 2]
    alpha_share = sum(len(w) for w in words) / len(pw)
    if len(words) >= 3 and alpha_share > 0.75:
        lows = [w.lower() for w in words]
        if all(w in _WORDSET for w in lows):
            # Every word is one Crush itself would suggest, so an attacker who
            # knows that - and they would - needs only this 512-word list.
            wb = len(words) * math.log2(len(_WORDS))
            note = (f"{len(words)} words from Crush's own 512-word list: "
                    f"exactly {len(words)} x 9 bits, and nothing is being estimated.")
        else:
            wb = len(words) * math.log2(40000)     # a generous cracking word list
            note = ("Scored as separate words, since guessing whole words is the "
                    "cheaper attack on a phrase like this.")
        if wb < bits:
            bits = wb
            notes.append(note)

    # Leetspeak fools a character-count estimate completely: "Tr0ub4dor&3" looks
    # like four character classes and is one dictionary word with substitutions.
    for stem in _deleet_candidates(pw):
        if len(stem) >= 4 and (stem in _COMMON or stem in _WORDSET):
            # A dictionary word plus a substitution rule: the cost is the word
            # list, plus a little for which rule and what was appended.
            capped = math.log2(40000) + 2 * len(pw)
            if capped < bits:
                bits = capped
                notes.append("This is a dictionary word with characters swapped for "
                             "lookalikes, which is the first rule any cracker applies.")
            break

    if lower in _COMMON or lower.rstrip("0123456789!") in _COMMON:
        bits = min(bits, 8)
        notes.append("This is on every password-cracking list ever published.")
    if len(set(pw)) <= 2 and len(pw) > 3:
        bits = min(bits, 12)
        notes.append("Only a couple of distinct characters.")
    if re.fullmatch(r"(.{1,4}?)\1+", pw):
        bits = min(bits, 16)
        notes.append("A short pattern repeated.")
    for seq in ("abcdefghijklmnopqrstuvwxyz", "01234567890", "qwertyuiop", "asdfghjkl"):
        for i in range(len(seq) - 3):
            if seq[i:i + 4] in lower or seq[i:i + 4][::-1] in lower:
                bits = min(bits, max(16, bits - 12))
                notes.append("Contains a keyboard or alphabet run.")
                break
        else:
            continue
        break

    bits = round(bits, 1)
    if bits < 28:   level, label = 0, "very weak"
    elif bits < 44: level, label = 1, "weak"
    elif bits < 60: level, label = 2, "reasonable"
    elif bits < 80: level, label = 3, "strong"
    else:           level, label = 4, "very strong"
    return {"bits": bits, "label": label, "level": level, "notes": notes[:2]}


def suggest_passphrase(words: int = 8) -> str:
    """A random passphrase drawn from the OS CSPRNG.

    The list below holds exactly 512 words, so every word carries exactly 9 bits
    and eight of them carry exactly 72 - a number that can be checked rather than
    trusted. 72 bits behind Argon2id at 256 MB per guess is out of reach of any
    offline attack that exists. Words are drawn WITH replacement, so a repeat is
    expected occasionally and does not weaken anything."""
    return " ".join(secrets.choice(_WORDS) for _ in range(words))





_WORDS = (
    "acorn acre admiral adobe agate airship alcove alder algae almond alpine "
    "amber amethyst anchor anvil apex apple apricot aqua arbor arcade arch "
    "archer ardent argon armor aroma arrow ascent ash aspen aster atlas atrium "
    "aurora autumn avenue awning azure badge badger bagel balcony bale ballad "
    "bamboo banjo banner barley barn barrel basalt basil basin basket bay bayou "
    "beach beacon beam bean bedrock beech beetle bellows belt bench bergamot "
    "berry beryl birch biscuit bison bit blade blanket blaze bloom blossom "
    "blueprint bluff bobcat bolt bonfire booth border bottle boulder bouquet "
    "bow bowl brace braid brake bramble branch brass bread breeze brick bridge "
    "brim brine bristle bronze brook broom broth brush bubble bucket bud buffer "
    "bugle bulb bundle bunker buoy burlap burrow bushel butane butter button "
    "buzzard cabin cable cache cactus cadence cairn calico camp candle cane "
    "canoe canvas canyon cap cape caramel caravan cargo carol carpet cart carve "
    "cascade case cask casserole castle catalog cattail cauldron cavern cedar "
    "cello cement census chain chair chalk chamber channel chapel charcoal "
    "chart chase chasm cheddar chess chestnut chime chisel chord chorus chowder "
    "chrome chute cider cinder cinnamon circuit citadel citrus clam clamp "
    "clarinet clasp clay cleat cliff climate cloak clock cloud clover coast "
    "cobalt cobble cocoa cog coil coin collar colony column comb comet compass "
    "cone confetti conifer console copper coral cordial cork corner cornice "
    "corridor cottage cotton couch counter courier cove cowl crab cradle craft "
    "crane crank crate crater crayon cream creek crescent crest crew cricket "
    "crimson crocus crop crossing crow crumb crust crystal cube cuff culvert "
    "cupboard curb curl current curtain cushion custard cutter cymbal cypress "
    "dagger dahlia dairy daisy dam damson dandelion dapple dart dash dawn deck "
    "decoy deed delta denim depot derby desert desk dial diamond diesel dill "
    "dimple dinghy ditch dock dodger dogwood dollar dolphin dome domino donut "
    "door dormer dossier dot dove dowel down draft dragon drain drape drawer "
    "dresser drift drill drum dryer duck duct dugout dune dusk dust duvet eagle "
    "earth easel eaves echo eclipse eddy edge eel egret elbow elder elevator "
    "elm ember emerald engine ensign envelope equinox escarp estuary etch ewe "
    "exhaust facade factory fairway falcon fallow fan fanfare farm fathom "
    "faucet fawn feather fedora felt fence fennel fern ferry fescue fever fiber "
    "fiddle field fig filament filter finch finial fir fireside firth fixture "
    "flag flake flame flank flannel flask fleet flicker flint flock floe floor "
    "flour flue flute foam fog foil folio font footpath forest forge fork form "
    "fort fossil fountain fowl fox foyer frame freight fresco fringe frost "
    "fruit fuel fulcrum funnel furnace furrow gable gadget gale gallery gallon "
    "gambit gantry gap garage garden gargoyle garland garnet gate gauge gauze "
    "gazebo gear gecko gem geyser gill gimbal ginger girder glacier glade glass "
    "glaze glen glide globe glove glow goblet gold gondola gopher gorge gourd "
    "grain gramme grange granite grape grass grate gravel gravity graze grebe "
    "green greenhouse grid griffin grill grip grotto grove guild gulch gull "
    "gully gusset gutter gypsum hackle hail hairpin hall halyard hamlet hammer "
    "hammock hamper handle hangar harbor harbour hardware hare harmony harness "
    "harp harvest hatch haven hawk hay haze"
).split()


def constant_time_equal(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


# 512 distinct words, so every word drawn carries exactly 9 bits. Checked here
# rather than asserted in prose - if the list is ever edited, this fails loudly.
assert len(_WORDS) == 512 and len(set(_WORDS)) == 512, \
    "the passphrase word list must hold exactly 512 distinct words"
_WORDSET = frozenset(_WORDS)
