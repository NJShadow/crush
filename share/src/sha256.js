/* Crush share - SHA-256, HMAC-SHA256 and HKDF-SHA256.
 *
 * WebCrypto is used whenever the browser offers it. Some ways of opening a
 * local file (certain in-app previewers, pages served over plain http) hide
 * crypto.subtle, and a share file that silently stops verifying in those
 * places would be worse than useless - so a small pure-JS SHA-256 is carried
 * as a fallback. Both paths are checked against each other by the test suite. */
(function (CR) {
"use strict";

const K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);

function sha256js(msg) {
  const h = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
  const w = new Uint32Array(64);
  const n = msg.length;
  const total = Math.ceil((n + 9) / 64) * 64;
  const tail = new Uint8Array(total - Math.floor(n / 64) * 64);
  const full = Math.floor(n / 64) * 64;
  tail.set(msg.subarray(full));
  tail[n - full] = 0x80;
  const bits = n * 8;
  const tv = new DataView(tail.buffer);
  tv.setUint32(tail.length - 4, bits >>> 0);
  tv.setUint32(tail.length - 8, Math.floor(bits / 2 ** 32));
  function block(b, o) {
    for (let i = 0; i < 16; i++) w[i] = (b[o + 4*i] << 24) | (b[o + 4*i + 1] << 16) | (b[o + 4*i + 2] << 8) | b[o + 4*i + 3];
    for (let i = 16; i < 64; i++) {
      const x = w[i-15], y = w[i-2];
      const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
      const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
      w[i] = (w[i-16] + s0 + w[i-7] + s1) | 0;
    }
    let a = h[0], bb = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (let i = 0; i < 64; i++) {
      const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const t1 = (hh + S1 + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
      const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const t2 = (S0 + ((a & bb) ^ (a & c) ^ (bb & c))) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = bb; bb = a; a = (t1 + t2) | 0;
    }
    h[0] += a; h[1] += bb; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
  }
  for (let o = 0; o < full; o += 64) block(msg, o);
  for (let o = 0; o < tail.length; o += 64) block(tail, o);
  const out = new Uint8Array(32);
  const ov = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) ov.setUint32(4 * i, h[i]);
  return out;
}
CR.sha256js = sha256js;

const subtle = (globalThis.crypto && globalThis.crypto.subtle) || null;
CR.hasSubtle = !!subtle;
CR.forceJsHash = false;            // the tests flip this to exercise the fallback

CR.sha256 = async function (u8) {
  if (subtle && !CR.forceJsHash) {
    try { return new Uint8Array(await subtle.digest("SHA-256", u8)); } catch (e) { /* fall through */ }
  }
  return sha256js(u8);
};

function hmacjs(key, msg) {
  if (key.length > 64) key = sha256js(key);
  const k = new Uint8Array(64); k.set(key);
  const ip = new Uint8Array(64 + msg.length), op = new Uint8Array(64 + 32);
  for (let i = 0; i < 64; i++) { ip[i] = k[i] ^ 0x36; op[i] = k[i] ^ 0x5c; }
  ip.set(msg, 64);
  op.set(sha256js(ip), 64);
  return sha256js(op);
}

/* HKDF-SHA256 with salt=None (RFC 5869: a string of HashLen zeros), exactly
 * as crush_crypt calls cryptography's HKDF. */
CR.hkdf = async function (ikm, info, length) {
  info = typeof info === "string" ? CR.ascii(info) : info;
  if (subtle && !CR.forceJsHash) {
    try {
      const k = await subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
      return new Uint8Array(await subtle.deriveBits(
        { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(32), info }, k, length * 8));
    } catch (e) { /* fall through */ }
  }
  const prk = hmacjs(new Uint8Array(32), ikm);
  const out = new Uint8Array(length);
  let t = new Uint8Array(0), o = 0;
  for (let c = 1; o < length; c++) {
    const m = new Uint8Array(t.length + info.length + 1);
    m.set(t); m.set(info, t.length); m[m.length - 1] = c;
    t = hmacjs(prk, m);
    out.set(t.subarray(0, Math.min(32, length - o)), o);
    o += 32;
  }
  return out;
};

})(globalThis.CR = globalThis.CR || {});
