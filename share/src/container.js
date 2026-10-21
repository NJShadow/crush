/* Crush share - reading a .crush container: index, bundles, Echo, dedup,
 * selective extraction. A port of read_index / _bundle_raw / extract from
 * crush_engine.py. Codec decoding is delegated to CR.codecs (the WASM
 * decoders); everything else is here.
 *
 *   "CRUSH\0" ver flags | index offset u64 | index length u64 | index codec u32
 *   ... bundles ...      | index (zstd or zlib JSON)
 */
(function (CR) {
"use strict";
// Codecs this page can decode (crush_codecs.js). Anything else - crushnn_m,
// the learned code model, needs the native app - is reported per file.
const PAGE_CODECS = ["store", "zstd12", "zstd19", "zstd22", "lzma", "lzma_x86", "lzma_d4",
  "lzma_text", "brotli", "bzip2", "ppmd", "crushcm", "zstd19d", "zstd22d", "crushcm_p"];
const fail = CR.fail, int = CR.int;
const HASH_HEX = 32;
const DEFAULTS = { cls: "generic", g: "", ex: true, dup: -1, b: -1, o: 0, l: 0, t: [], x: 0o644 };

CR.isCrush = (u8) => u8.length >= 28 && u8[0] === 67 && u8[1] === 82 && u8[2] === 85 && u8[3] === 83 && u8[4] === 72 && u8[5] === 0;

function u64(dv, o) {
  const lo = dv.getUint32(o, true), hi = dv.getUint32(o + 4, true);
  if (hi >= 2097152) fail("an offset in the archive header is out of range");
  return hi * 4294967296 + lo;
}

/* Index entries come from the archive: check their types once, here, so the
 * rest of the code can trust shapes (but never sizes) from now on. */
function checkEntry(e, nFiles, nBundles) {
  const out = Object.assign({}, DEFAULTS, e);
  if (typeof out.p !== "string" || !out.p.length) fail("a file in the index has no name");
  out.s = int(out.s, "size"); out.dup = int(out.dup, "dup"); out.b = int(out.b, "bundle");
  out.o = int(out.o, "offset"); out.l = int(out.l, "length");
  if (typeof out.h !== "string" || !/^[0-9a-f]+$/.test(out.h)) fail("a file in the index has a malformed hash");
  if (!Array.isArray(out.t)) fail("a file in the index has a malformed transform chain");
  for (const st of out.t) if (!st || typeof st.n !== "string" || typeof st.a !== "object" || st.a === null) fail("a transform step is malformed");
  if (out.dup >= nFiles || out.dup < -1) fail("a duplicate pointer is out of range");
  if (out.s < 0 || out.o < 0 || out.l < 0) fail("a negative size in the index");
  out.m = typeof out.m === "number" ? out.m : Number(out.m) || 0;
  return out;
}

CR.readIndex = async function (u8) {
  if (!CR.isCrush(u8)) fail("this is not a Crush archive (the first bytes are wrong)");
  const dv = new DataView(u8.buffer, u8.byteOffset, 28);
  const ver = u8[6];
  if (ver !== 1) fail("this archive is container version " + ver + "; this page understands 1");
  const ioff = u64(dv, 8), ilen = u64(dv, 16), icodec = dv.getUint32(24, true);
  if (ioff + ilen > u8.length || ioff < 28) fail("the archive's index lies outside the file - it is truncated or damaged");
  const ic = u8.subarray(ioff, ioff + ilen);
  let ib;
  if (icodec === 1) ib = await CR.codecs.zstd(ic, 1 << 30);
  else if (icodec === 2) ib = await CR.codecs.zlib(ic, 1 << 30);
  else fail("unknown index codec " + icodec);
  const idx = CR.parseJSON(CR.utf8dec(ib));
  if (!idx || !Array.isArray(idx.files) || !Array.isArray(idx.bundles)) fail("the archive index is malformed");
  idx.files = idx.files.map((f) => checkEntry(f, idx.files.length, idx.bundles.length));
  const ids = new Set();
  for (const b of idx.bundles) {
    for (const k of ["id", "off", "clen", "rawlen"]) b[k] = int(b[k], "bundle " + k);
    if (typeof b.codec !== "string") fail("a bundle has no codec");
    if (b.off < 28 || b.off + b.clen > u8.length) fail("a bundle lies outside the file");
    if (ids.has(b.id)) fail("two bundles share an id");
    ids.add(b.id);
    b.prior = b.prior || null; b.echo = b.echo || null;
    if (b.echo) for (const k of ["nu", "ni", "raw", "h"]) b.echo[k] = int(b.echo[k], "echo " + k);
  }
  return idx;
};

/* What this archive needs, so the share file can say up front whether it can
 * open every file (and make_share can refuse to build one that cannot). */
CR.requirements = function (idx) {
  const codecs = new Set(), transforms = new Set(), priors = new Set();
  for (const b of idx.bundles) { codecs.add(b.codec); if (b.prior) priors.add(b.prior + (b.codec === "crushcm_p" ? ".prime" : ".zdict")); }
  for (const f of idx.files) for (const st of f.t) transforms.add(st.n);
  return { codecs: [...codecs], transforms: [...transforms], priors: [...priors] };
};

/* Echo: content-defined chunk dedup across a whole bundle. */
function echoUnpack(hdr) {
  const r = new CR.Reader(hdr);
  const nu = r.uv(), lens = [];
  for (let i = 0; i < nu; i++) lens.push(r.uv());
  const ni = r.uv();
  const ids = new Array(CR.checkLen(ni, "a chunk list"));
  let prev = 0;
  for (let k = 0; k < ni; k++) {
    const z = r.uv();
    const d = (z % 2) ? -(z + 1) / 2 : z / 2;
    prev += d;
    ids[k] = prev;
  }
  return [lens, ids];
}

CR.bundleRaw = async function (u8, b) {
  const ei = b.echo;
  let p = b.off;
  const eh = ei ? u8.subarray(p, p + ei.h) : null;
  if (ei) p += ei.h;
  const comp = u8.subarray(p, p + b.clen);
  if (comp.length !== b.clen) fail("a bundle is truncated");
  if (PAGE_CODECS.indexOf(b.codec) < 0)
    throw new CR.Unavailable("this file was compressed with '" + b.codec + "', which only the Crush app can decode");
  let prior = null;
  if (b.prior) {
    prior = CR.priors && CR.priors[b.prior + (b.codec === "crushcm_p" ? ".prime" : ".zdict")];
    if (!prior) throw new CR.Unavailable("this archive needs Crush's shared prior '" + b.prior + "', which this page does not carry");
  }
  let raw = await CR.codecs.decode(b.codec, comp, prior, b.rawlen);
  if (raw.length !== b.rawlen) fail("a bundle decoded to the wrong length");
  if (ei) {
    const hdr = ei.z ? await CR.codecs.zstd(eh, 1 << 30) : await CR.codecs.zlib(eh, 1 << 30);
    const [lens, ids] = echoUnpack(hdr);
    const starts = []; let pos = 0;
    for (const L of lens) { starts.push(pos); pos += L; }
    let total = 0;
    for (const j of ids) { if (!(j >= 0 && j < lens.length)) fail("an Echo chunk reference is out of range"); total += lens[j]; }
    const out = new Uint8Array(CR.checkLen(total, "an Echo bundle"));
    let o = 0;
    for (const j of ids) { out.set(raw.subarray(starts[j], starts[j] + lens[j]), o); o += lens[j]; }
    raw = out;
  }
  return raw;
};

function dupSource(files, i) {
  let seen = 0;
  while (files[i].dup >= 0 && seen < 64) { i = files[i].dup; seen++; }
  return i;
}

/* Decode, invert and verify. onFile(i, {ok, data|null, why}) is called for
 * every SELECTED file (duplicates included) as soon as it is known. Files the
 * selection needs only as a dedup source are decoded but not reported. */
CR.extract = async function (u8, idx, select, onFile, progress) {
  const files = idx.files;
  const want = select ? new Set(select) : new Set(files.map((_, i) => i));
  const need = new Set([...want].map((i) => dupSource(files, i)));
  const byBundle = new Map();
  for (const i of [...need].sort((a, b) => a - b)) {
    const b = files[i].b;
    if (files[i].dup >= 0) { continue; }      // a 64-deep dup chain: reported as failed below
    if (!byBundle.has(b)) byBundle.set(b, []);
    byBundle.get(b).push(i);
  }
  const results = new Map();               // source index -> {ok, data, why}
  const bundlesById = new Map(idx.bundles.map((b) => [b.id, b]));
  let done = 0;
  const order = [...byBundle.keys()].sort((a, b) => a - b);
  for (const bi of order) {
    const members = byBundle.get(bi);
    const b = bundlesById.get(bi);
    if (progress) progress({ stage: "bundle", i: done, n: order.length, cls: b ? b.cls : "?" });
    let raw = null, why = null;
    try {
      if (!b) fail("a file points at a bundle that does not exist");
      raw = await CR.bundleRaw(u8, b);
    } catch (e) { why = e; }
    for (const i of members) {
      const e = files[i];
      let r;
      if (!raw) r = { ok: false, data: null, why: explain(why) };
      else {
        try {
          const payload = raw.subarray(e.o, e.o + e.l);
          const data = await CR.invertTransforms(payload, e.t);
          if (!e.ex) throw new CR.Unavailable("this file was stored content-exact ('harder' mode), which only the Crush app can check");
          const h = e.h;
          if (h.length < HASH_HEX) throw new CR.CrushError("the stored fingerprint is too short to trust", "verify");
          const got = CR.hex(await CR.sha256(data)).slice(0, h.length);
          if (got !== h) r = { ok: false, data: null, why: "it did not match its fingerprint after decoding, so it was not kept" };
          else if (data.length !== e.s) r = { ok: false, data: null, why: "it decoded to the wrong size" };
          else r = { ok: true, data, why: null };
        } catch (ex) { r = { ok: false, data: null, why: explain(ex) }; }
      }
      results.set(i, r);
      if (want.has(i)) onFile(i, r);
      await new Promise((res) => setTimeout(res, 0));
    }
    raw = null;
    done++;
  }
  for (const i of [...want].sort((a, b) => a - b)) {
    if (files[i].dup < 0) continue;
    const si = dupSource(files, i);
    const src = results.get(si);
    // Python looks the twin up by hash; requiring the pointed-to file to carry
    // the same verified hash is the same guarantee.
    if (src && src.ok && files[si].h === files[i].h)
      onFile(i, { ok: true, data: src.data, why: null, dupOf: files[si].p });
    else onFile(i, { ok: false, data: null, why: src ? (src.why || "its identical twin could not be rebuilt") : "its identical twin is missing" });
  }
  if (progress) progress({ stage: "done", i: order.length, n: order.length });
};

function explain(e) {
  if (!e) return "unknown error";
  if (e instanceof CR.Unavailable) return e.message;
  if (e instanceof CR.CrushError) {
    if (["transport", "header", "browser", "password", "memory", "limit", "tamper", "verify"].includes(e.kind)) return e.message;
    return "the archive is damaged here: " + e.message;
  }
  if (e && e.name === "CodecError") return "the archive is damaged here (" + e.message + ")";
  if (e instanceof RangeError) return "this device ran out of memory for it";
  return String(e && e.message || e);
}
CR.explain = explain;

/* Stored paths come out of an archive someone else made. They are only ever
 * used as download names here (the browser picks the folder), but they are
 * still cleaned: no directory parts, no control characters. */
CR.safeName = function (p) {
  const parts = p.replace(/\\/g, "/").split("/").filter((s) => s && s !== "." && s !== "..");
  const base = parts.length ? parts[parts.length - 1] : "file";
  return base.replace(/[\u0000-\u001f\u007f<>:"|?*]/g, "_").slice(0, 200) || "file";
};
CR.safePath = function (p) {
  const parts = p.replace(/\\/g, "/").split("/").filter((s) => s && s !== "." && s !== "..");
  return parts.map((s) => s.replace(/[\u0000-\u001f\u007f<>:"|?*]/g, "_").slice(0, 200)).join("/") || "file";
};
})(globalThis.CR = globalThis.CR || {});
