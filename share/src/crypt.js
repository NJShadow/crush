/* Crush share - opening an encrypted archive, exactly as crush_crypt.py does.
 *
 *   header (134 bytes): "CRUSHENC" | ver | suite | t m p (BE u32) | salt 16 |
 *                       prefix 16 | commit 32 | wrapped file key 48
 *   KEK    = Argon2id(passphrase NFC/UTF-8, salt, t, m KiB, p) -> 32 bytes
 *   commit = HKDF-SHA256(KEK, info "crush/commit/v1")      compared first
 *   FK     = XChaCha20-Poly1305-open(KEK, nonce 0^24, wrapped, AAD = header[0:86])
 *   SK     = HKDF-SHA256(FK, info "crush/stream/v1")
 *   chunk i (256 KiB + 16-byte tag): nonce = prefix | i as 7-byte BE | final flag
 *
 * Order of checks is the audited one: refuse implausible KDF costs BEFORE any
 * work (authentication here IS key derivation), then the key commitment, then
 * the wrap. The final chunk is authenticated first (its flag is the truncation
 * defence) and then every chunk is, before a single plaintext byte is used.
 * Argon2id and XChaCha20-Poly1305 run in Monocypher 4.0.2 compiled to WASM;
 * SHA-256/HKDF use WebCrypto. */
(function (CR) {
"use strict";

const MAGIC = "CRUSHENC";
const VERSION = 1, SUITE_ARGON2ID = 1, SUITE_SCRYPT = 2;
const HEADER_PREFIX_LEN = 86, HEADER_LEN = 134;
const CHUNK = 256 * 1024, TAG = 16;
const PARANOID_T = 4, PARANOID_M = 1024 * 1024;   // crush_crypt.PROFILES["paranoid"]

class WrongPassword extends CR.CrushError { constructor(m) { super(m, "password"); } }
CR.WrongPassword = WrongPassword;

CR.isEncrypted = (u8) => u8.length >= 8 && String.fromCharCode(...u8.subarray(0, 8)) === MAGIC;

/* What can be read without the passphrase: enough to say how long and how
 * much memory unlocking will take, nothing about the contents. */
CR.headerInfo = function (u8) {
  if (u8.length < HEADER_LEN || !CR.isEncrypted(u8)) CR.fail("not an encrypted Crush archive");
  const dv = new DataView(u8.buffer, u8.byteOffset, HEADER_LEN);
  const info = { version: u8[8], suite: u8[9], t: dv.getUint32(10), m: dv.getUint32(14), p: dv.getUint32(18) };
  try { sanity(info.suite, info.t, info.m, info.p); info.sane = true; }
  catch (e) { info.sane = false; info.why = e.message; }
  info.supported = info.sane && info.version === VERSION && info.suite === SUITE_ARGON2ID;
  return info;
};

/* crush_crypt._sanity_kdf, line for line. */
function sanity(suite, t, m, p) {
  if (suite === SUITE_ARGON2ID) {
    if (!(1 <= t && t <= 8)) CR.fail("refusing an archive with Argon2 time cost " + t, "header");
    if (!(8 <= m && m <= 1024 * 1024)) CR.fail("refusing an archive that asks for " + Math.floor(m / 1024) + " MB of memory", "header");
    if (!(1 <= p && p <= 8)) CR.fail("refusing an archive with parallelism " + p, "header");
    if (m < 8 * p) CR.fail("refusing an archive with invalid Argon2 parameters", "header");
    if (t * m > 2 * PARANOID_T * PARANOID_M) CR.fail("refusing an archive whose unlock cost is implausible", "header");
    return;
  }
  if (suite === SUITE_SCRYPT)
    CR.fail("this archive was locked with the scrypt fallback (made where Argon2 was not installed); this page only carries Argon2id", "header");
  CR.fail("unknown cipher suite " + suite, "header");
}
CR.kdfSanity = sanity;

function b64bytes(b64) {
  const s = atob(b64), u = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
  return u;
}
let wasmModule = null;
async function module() {
  if (!wasmModule) wasmModule = await WebAssembly.compile(CR.cryptoWasm || b64bytes(CR.CRYPTO_WASM_B64));
  return wasmModule;
}
/* A fresh instance per job: WASM memory can grow but never shrink, so the
 * 256 MB Argon2 arena is dropped with its instance instead of being held for
 * the rest of the session. */
async function instance(extraBytes) {
  const inst = await WebAssembly.instantiate(await module(), {});
  const x = inst.exports, mem = x.memory;
  const base = (x.heap_base() + 15) & ~15;
  const need = base + extraBytes;
  const have = mem.buffer.byteLength;
  if (need > have) {
    try { mem.grow(Math.ceil((need - have) / 65536)); }
    catch (e) { return { error: e }; }
  }
  return { x, mem, base };
}

function ctEqual(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
  return d === 0;
}

CR.argon2id = async function (pw, salt, t, m, p) {
  // layout: [out 32][pass][salt][pad to 1 KiB multiple][work m KiB]
  const fixed = 64 + pw.length + salt.length;
  const fixedR = Math.ceil(fixed / 1024) * 1024;
  const r = await instance(fixedR + m * 1024);
  if (r.error) CR.fail("this device could not provide the " + Math.round(m / 1024) +
    " MB of memory this archive needs to unlock (" + r.error.message + ")", "memory");
  const { x, mem, base } = r;
  let u = new Uint8Array(mem.buffer);
  const pOut = base, pPw = base + 64, pSalt = pPw + pw.length, pWork = base + fixedR;
  u.set(pw, pPw); u.set(salt, pSalt);
  x.argon2id(pOut, 32, pPw, pw.length, pSalt, salt.length, t, m, p, pWork);
  u = new Uint8Array(mem.buffer);
  const kek = u.slice(pOut, pOut + 32);
  u.fill(0, base, pSalt + salt.length);          // the passphrase does not linger in the arena
  return kek;
};

/* One-shot AEAD open on a small instance. */
async function opener(maxCt) {
  const r = await instance(64 + 32 + 24 + 256 + 2 * (maxCt + 64));
  if (r.error) CR.fail("could not allocate memory for decryption", "memory");
  const { x, mem, base } = r;
  const pKey = base, pNonce = base + 32, pAd = base + 64, pCt = base + 64 + 256, pOut = pCt + maxCt + 64;
  return {
    setKey(k) { new Uint8Array(mem.buffer).set(k, pKey); },
    open(nonce, ct, ad) {
      const u = new Uint8Array(mem.buffer);
      if (ct.length > maxCt || (ad && ad.length > 256)) CR.fail("internal: buffer too small");
      u.set(nonce, pNonce);
      if (ad) u.set(ad, pAd);
      u.set(ct, pCt);
      const rc = x.xchacha_open(pOut, pKey, pNonce, pAd, ad ? ad.length : 0, pCt, ct.length);
      if (rc !== 0) return null;
      return u.slice(pOut, pOut + ct.length - TAG);
    },
    wipe() { new Uint8Array(mem.buffer).fill(0, base, pOut + maxCt + 64); },
  };
}
CR.xchachaOpener = opener;

function nonce(prefix, counter, final) {
  const n = new Uint8Array(24);
  n.set(prefix, 0);
  let c = counter;
  for (let k = 22; k >= 16; k--) { n[k] = c % 256; c = Math.floor(c / 256); }
  n[23] = final ? 1 : 0;
  return n;
}

/* Decrypt a whole encrypted archive. Returns the plaintext .crush bytes, or
 * throws WrongPassword / CrushError. progress(stage, done, total). */
CR.decryptArchive = async function (u8, passphrase, progress) {
  progress = progress || (() => {});
  if (u8.length < HEADER_LEN || !CR.isEncrypted(u8)) CR.fail("not an encrypted Crush archive");
  const header = u8.subarray(0, HEADER_LEN);
  const ver = header[8], suite = header[9];
  if (ver !== VERSION) CR.fail("this archive is format version " + ver + "; this page understands " + VERSION, "header");
  const dv = new DataView(u8.buffer, u8.byteOffset, HEADER_LEN);
  const t = dv.getUint32(10), m = dv.getUint32(14), p = dv.getUint32(18);
  sanity(suite, t, m, p);
  const salt = header.subarray(22, 38), prefix = header.subarray(38, 54);
  const commit = header.subarray(54, 86), wrapped = header.subarray(86, 134);

  if (typeof passphrase !== "string") CR.fail("internal: passphrase must be text");
  const pw = CR.utf8(passphrase.normalize("NFC"));
  if (!pw.length) CR.fail("the passphrase is empty", "password");

  progress("kdf", 0, 1);
  const kek = await CR.argon2id(pw, salt, t, m, p);
  pw.fill(0);
  progress("kdf", 1, 1);
  if (!ctEqual(commit, await CR.hkdf(kek, "crush/commit/v1", 32)))
    throw new WrongPassword("wrong passphrase, or this archive is damaged");

  const op = await opener(CHUNK + TAG);
  op.setKey(kek);
  const fk = op.open(new Uint8Array(24), wrapped, header.subarray(0, HEADER_PREFIX_LEN));
  kek.fill(0);
  if (!fk) { op.wipe(); throw new WrongPassword("wrong passphrase, or this archive is damaged"); }
  const sk = await CR.hkdf(fk, "crush/stream/v1", 32);
  fk.fill(0);
  op.setKey(sk);
  sk.fill(0);

  // chunk geometry, as EncryptedReader computes it
  const csize = u8.length - HEADER_LEN;
  if (csize < TAG) CR.fail("the archive has no payload");
  const enc = CHUNK + TAG;
  const full = Math.floor(csize / enc), rem = csize % enc;
  let nChunks, lastPlain;
  if (rem === 0) { nChunks = full; lastPlain = CHUNK; }
  else {
    if (rem < TAG) CR.fail("the archive ends mid-chunk");
    nChunks = full + 1; lastPlain = rem - TAG;
  }
  const size = (nChunks - 1) * CHUNK + lastPlain;
  const out = new Uint8Array(CR.checkLen(size, "the decrypted archive"));
  const body = u8.subarray(HEADER_LEN);
  const one = (i) => {
    const last = i === nChunks - 1;
    const blob = body.subarray(i * enc, last ? csize : (i + 1) * enc);
    const plain = op.open(nonce(prefix, i, last), blob, null);
    if (!plain) {
      op.wipe(); out.fill(0);
      CR.fail("the archive failed its integrity check at chunk " + i + " - it has been altered or truncated", "tamper");
    }
    out.set(plain, i * CHUNK);
  };
  // final chunk first: its 0x01 flag is the whole truncation defence
  one(nChunks - 1);
  for (let i = 0; i < nChunks - 1; i++) {
    one(i);
    if (i % 16 === 0) { progress("decrypt", i + 1, nChunks); await new Promise((r) => setTimeout(r, 0)); }
  }
  progress("decrypt", nChunks, nChunks);
  op.wipe();
  return out;
};

})(globalThis.CR = globalThis.CR || {});
