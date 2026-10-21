// Monocypher-in-WASM and the JS SHA/HKDF against vectors from argon2-cffi and
// the cryptography package (the exact libraries crush_crypt.py uses).
const load = require("./load"); const CR = load();
const fs = require("fs");
const V = JSON.parse(fs.readFileSync(process.argv[2]));
const h = (s) => Uint8Array.from(Buffer.from(s, "hex"));
(async () => {
  let ok = 0, bad = 0;
  for (const [pw, salt, t, m, p, want] of V.argon2) {
    const t0 = Date.now();
    const got = CR.hex(await CR.argon2id(h(pw), h(salt), t, m, p));
    if (got === want) ok++; else { bad++; console.log("ARGON2 MISMATCH", t, m, p); }
    if (m >= 65536) console.log(`argon2id t=${t} m=${m} p=${p}: ${Date.now() - t0} ms (node, WASM)`);
  }
  console.log(`argon2id: ${ok}/${ok + bad} match`);
  ok = bad = 0;
  const op = await CR.xchachaOpener(262144 + 16);
  let rej = 0;
  for (const [key, nonce, ad, msg, ct] of V.xchacha) {
    op.setKey(h(key));
    const got = op.open(h(nonce), h(ct), ad ? h(ad) : null);
    if (got && CR.hex(got) === msg) ok++; else { bad++; console.log("XCHACHA MISMATCH"); }
    // every single-bit flip of the tag and of the first ciphertext byte must be refused
    const c = h(ct); c[c.length - 1] ^= 1;
    if (op.open(h(nonce), c, ad ? h(ad) : null) === null) rej++;
    const c2 = h(ct); c2[0] ^= 0x80;
    if (op.open(h(nonce), c2, ad ? h(ad) : null) === null) rej++;
  }
  console.log(`xchacha20-poly1305 open: ${ok}/${ok + bad} match; ${rej}/${2 * V.xchacha.length} tampered refused`);
  ok = bad = 0;
  for (const force of [false, true]) {
    CR.forceJsHash = force;
    for (const [ikm, commit, stream] of V.hkdf) {
      const c = CR.hex(await CR.hkdf(h(ikm), "crush/commit/v1", 32));
      const s = CR.hex(await CR.hkdf(h(ikm), "crush/stream/v1", 32));
      if (c === commit && s === stream) ok++; else { bad++; console.log("HKDF MISMATCH", force); }
    }
  }
  CR.forceJsHash = false;
  console.log(`hkdf-sha256 (WebCrypto + JS fallback): ${ok}/${ok + bad} match`);
  // SHA-256 JS fallback vs WebCrypto across lengths around block edges
  let sOk = 0, sN = 0;
  for (let n = 0; n < 300; n++) {
    const d = new Uint8Array(n * 7 + (n % 3)); for (let i = 0; i < d.length; i++) d[i] = (i * 31 + n) & 255;
    sN++; if (CR.hex(CR.sha256js(d)) === CR.hex(new Uint8Array(await crypto.subtle.digest("SHA-256", d)))) sOk++;
  }
  console.log(`sha256 JS fallback vs WebCrypto: ${sOk}/${sN}`);
  process.exit(bad ? 1 : 0);
})();
