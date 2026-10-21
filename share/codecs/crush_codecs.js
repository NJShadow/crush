// crush_codecs.js - every decoder Crush needs, as WebAssembly, for node and
// browsers. One ES module, no dependencies, no network access when the
// bundled variant (crush_codecs.bundle.js) is used.
//
//   import * as cc from "./crush_codecs.js";
//   await cc.init();                         // compiles the .wasm modules
//   const raw = cc.decodeCodec("lzma", blob); // any Crush bundle codec id
//
// Every decode function takes a Uint8Array and returns a new Uint8Array, and
// throws CodecError on any failure (corrupt input, output larger than
// opts.maxOut, a WebAssembly trap). Calls are synchronous once init() has
// resolved; the slow ones (crushcm, jcm, PPMd) belong in a Web Worker so the
// page stays responsive.
//
// Each codec runs in its own WebAssembly instance. An instance is discarded
// and rebuilt after any failure (a trap can leave its heap inconsistent) and
// after any call that grew its memory past KEEP_BYTES, so a 200 MB model is
// not held after it is needed. crushcm keeps all of its model in C globals
// and therefore always gets a fresh instance.
//
// Sources, licences and the byte-exactness proof: REPORT.md.

const EMBEDDED = null; // replaced by base64 strings in crush_codecs.bundle.js

export const VERSION = "crush_codecs 1";
export const DEFAULT_MAX_OUT = 1 << 30;     // 1 GiB per decoded buffer
const KEEP_BYTES = 64 << 20;

export class CodecError extends Error {
  constructor(codec, code, detail) {
    super(`${codec}: decode failed (${code})${detail ? ": " + detail : ""}`);
    this.name = "CodecError";
    this.codec = codec;
    this.code = code;
  }
}

class ExitTrap extends Error {}

// ---- module loading --------------------------------------------------------
const FILES = {
  crushz: "crushz.wasm", zstd: "zstd.wasm", lzma: "lzma.wasm",
  brotli: "brotli.wasm", bzip2: "bzip2.wasm", crushcm: "crushcm.wasm",
  crushpf: "crushpf.wasm", crushjpg: "crushjpg.wasm",
};
const modules = {};          // name -> WebAssembly.Module
const live = {};             // name -> { inst, mem, ex, stderr }

function b64bytes(s) {
  const bin = atob(s);
  const u = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return u;
}

async function loadBytes(name) {
  if (EMBEDDED) {
    if (!EMBEDDED[name]) throw new Error(`${name}.wasm is not in this bundle`);
    return b64bytes(EMBEDDED[name]);
  }
  const url = new URL(FILES[name], import.meta.url);
  if (typeof process !== "undefined" && process.versions && process.versions.node
      && url.protocol === "file:") {
    const fs = await import("node:fs/promises");
    return new Uint8Array(await fs.readFile(url));
  }
  const r = await fetch(url);
  if (!r.ok) throw new Error(`cannot load ${url}: ${r.status}`);
  return new Uint8Array(await r.arrayBuffer());
}

/** Compile the WebAssembly modules. opts.only = ["lzma", ...] to load a
 *  subset (names: crushz zstd lzma brotli bzip2 crushcm crushpf crushjpg). */
export async function init(opts = {}) {
  const names = opts.only || Object.keys(FILES);
  await Promise.all(names.map(async (n) => {
    if (!FILES[n]) throw new Error(`unknown module ${n}`);
    if (!modules[n]) modules[n] = await WebAssembly.compile(await loadBytes(n));
  }));
  return names;
}

export function loaded() { return Object.keys(modules); }

// Minimal WASI: the C code only ever writes diagnostics to stderr or exits.
function wasiImports(state) {
  const ENOSYS = 52;
  const imp = {
    fd_write(fd, iovs, iovcnt, nwritten) {
      const dv = new DataView(state.mem.buffer);
      let n = 0;
      for (let i = 0; i < iovcnt; i++) {
        const p = dv.getUint32(iovs + 8 * i, true), l = dv.getUint32(iovs + 8 * i + 4, true);
        if (fd === 2 && state.stderr.length < 4096)
          state.stderr += new TextDecoder().decode(new Uint8Array(state.mem.buffer, p, l));
        n += l;
      }
      dv.setUint32(nwritten, n, true);
      return 0;
    },
    proc_exit(code) { throw new ExitTrap(`exit(${code})`); },
  };
  return new Proxy(imp, { get: (t, k) => (k in t ? t[k] : () => ENOSYS) });
}

function instance(name) {
  let s = live[name];
  if (s) return s;
  const m = modules[name];
  if (!m) throw new Error(`${name}.wasm is not loaded - call init() first`);
  s = { stderr: "" };
  const inst = new WebAssembly.Instance(m, { wasi_snapshot_preview1: wasiImports(s) });
  s.inst = inst; s.ex = inst.exports; s.mem = inst.exports.memory;
  if (s.ex._initialize) s.ex._initialize();
  live[name] = s;
  return s;
}

/** Drop every live instance (frees their memory once collected). */
export function release() { for (const k of Object.keys(live)) delete live[k]; }

// Run fn(ex, put, outSlot) in codec `name`'s instance. put(u8) copies a buffer
// into the instance and returns its pointer. fn returns the C status code;
// on 0 the (ptr,len) in outSlot is copied out and freed.
function run(name, label, fn, fresh = false) {
  if (fresh) delete live[name];
  const s = instance(name);
  const ex = s.ex;
  const ptrs = [];
  s.stderr = "";
  try {
    const put = (u8) => {
      const n = u8 ? u8.length : 0;
      const p = ex.malloc(n || 1);
      if (!p) throw new CodecError(label, "oom", "cannot allocate input");
      if (n) new Uint8Array(s.mem.buffer, p, n).set(u8);
      ptrs.push(p);
      return p;
    };
    const slot = ex.malloc(16);
    if (!slot) throw new CodecError(label, "oom");
    ptrs.push(slot);
    new Uint8Array(s.mem.buffer, slot, 16).fill(0);
    const rc = fn(ex, put, slot);
    if (rc !== 0) throw new CodecError(label, rc, s.stderr.trim());
    const dv = new DataView(s.mem.buffer);
    const op = dv.getUint32(slot, true), ol = dv.getUint32(slot + 4, true);
    const out = new Uint8Array(ol);
    out.set(new Uint8Array(s.mem.buffer, op, ol));
    ex.free(op);
    for (const p of ptrs) ex.free(p);
    if (fresh || s.mem.buffer.byteLength > KEEP_BYTES) delete live[name];
    return out;
  } catch (e) {
    delete live[name];               // a trap may leave the heap inconsistent
    if (e instanceof CodecError) throw e;
    const why = e instanceof ExitTrap ? `${e.message} ${s.stderr.trim()}` :
      (e instanceof WebAssembly.RuntimeError ? `trap: ${e.message}` : String(e));
    throw new CodecError(label, "trap", why);
  }
}

const u32 = (n) => n >>> 0;
function maxOutOf(opts) {
  const m = opts && opts.maxOut != null ? opts.maxOut : DEFAULT_MAX_OUT;
  return Math.min(m, 0xFFFFFFFF) >>> 0;
}

// ---- codecs ----------------------------------------------------------------

/** crushcm (Crush's context-mixing codec). prior: the bytes of a
 *  priors/<name>.prime file for the crushcm_p codec, else omitted. */
export function crushcm(blob, prior = null, opts = {}) {
  return run("crushcm", prior ? "crushcm_p" : "crushcm", (ex, put, o) => {
    const pi = put(blob), pp = prior ? put(prior) : 0;
    return ex.cm_decode(pi, u32(blob.length), pp, prior ? u32(prior.length) : 0,
      maxOutOf(opts), o, o + 4);
  }, true);
}

/** Zstandard frame; opts.dict = bytes of a priors/<name>.zdict file. */
export function zstd(blob, opts = {}) {
  const dict = opts.dict || null;
  return run("zstd", "zstd", (ex, put, o) => {
    const pi = put(blob), pd = dict ? put(dict) : 0;
    return ex.zstd_decode(pi, u32(blob.length), pd, dict ? u32(dict.length) : 0,
      maxOutOf(opts), o, o + 4);
  });
}

const LZMA_FILTER = { plain: [0, 0], text: [0, 0], x86: [1, 0], delta4: [2, 4] };
/** Raw LZMA2 (Python lzma FORMAT_RAW). opts.filter: "plain" | "x86" |
 *  "delta4" | "text"; opts.dictSize defaults to Crush's 64 MiB. */
export function lzma2(blob, opts = {}) {
  const f = LZMA_FILTER[opts.filter || "plain"];
  if (!f) throw new CodecError("lzma2", "filter", `unknown filter ${opts.filter}`);
  const dict = opts.dictSize != null ? opts.dictSize : 64 << 20;
  return run("lzma", "lzma2", (ex, put, o) =>
    ex.lzma2_decode(put(blob), u32(blob.length), u32(dict), f[0], f[1],
      maxOutOf(opts), o, o + 4));
}

/** Raw Ppmd7 (PPMdH, 7z range coder) stream of known decoded length n. */
export function ppmd7Raw(stream, n, opts = {}) {
  const order = opts.order || 16, mem = opts.mem || (192 << 20);
  return run("lzma", "ppmd", (ex, put, o) =>
    ex.ppmd7_decode(put(stream), u32(stream.length), u32(n), order, u32(mem),
      maxOutOf(opts), o, o + 4));
}

/** Crush's "ppmd" codec: u64 LE decoded length + Ppmd7 order 16, 192 MiB. */
export function ppmd(blob, opts = {}) {
  if (blob.length < 8) throw new CodecError("ppmd", "short");
  const dv = new DataView(blob.buffer, blob.byteOffset, 8);
  const lo = dv.getUint32(0, true), hi = dv.getUint32(4, true);
  if (hi !== 0 || lo > maxOutOf(opts)) throw new CodecError("ppmd", "size", "declared length too large");
  return ppmd7Raw(blob.subarray(8), lo, opts);
}

export function brotli(blob, opts = {}) {
  return run("brotli", "brotli", (ex, put, o) =>
    ex.brotli_decode(put(blob), u32(blob.length), maxOutOf(opts), o, o + 4));
}

export function bzip2(blob, opts = {}) {
  return run("bzip2", "bzip2", (ex, put, o) =>
    ex.bzip2_decode(put(blob), u32(blob.length), maxOutOf(opts), o, o + 4));
}

/** zlib inflate. opts.wbits as Python's zlib: 15 zlib (default), -15 raw
 *  deflate, 31 gzip, 47 auto. Bytes after the end of the stream are ignored. */
export function inflate(blob, opts = {}) {
  const wbits = opts.wbits != null ? opts.wbits : 15;
  return run("crushz", "inflate", (ex, put, o) =>
    ex.crushz_inflate(put(blob), u32(blob.length), wbits, maxOutOf(opts), o, o + 4, o + 8));
}

// The pinned-deflate settings table, identical to crush_zpin.PARAMS: archives
// store an index into it. (level, strategy, memLevel)
export const ZPIN_PARAMS = [];
for (const l of [6, 9, 1, 8, 7, 5, 4, 3, 2])
  for (const s of [0, 1, 2, 3, 4])
    for (const m of [8, 9, 7, 6, 5, 4, 3, 2, 1]) ZPIN_PARAMS.push([l, s, m]);

/** Pinned zlib 1.3.1 deflate with explicit settings (crushz_deflate). */
export function deflate(raw, level, wbits, memLevel, strategy) {
  let cap = raw.length + (raw.length >>> 3) + 4096;
  for (;;) {
    let again = false;
    const out = run("crushz", "deflate", (ex, put, o) => {
      const pi = put(raw);
      const po = ex.malloc(cap);
      if (!po) return -999;
      const rc = ex.crushz_deflate(pi, u32(raw.length), level, wbits, memLevel, strategy,
        po, u32(cap), o + 4);
      new DataView(ex.memory.buffer).setUint32(o, po, true);
      if (rc === 1) { again = true; return 0; }
      return rc;
    });
    if (!again) return out;
    cap *= 2;
  }
}

/** crush_zpin.deflate(idx, raw, wbits): rebuild a stream from its index. */
export function zpinDeflate(idx, raw, wbits) {
  const p = ZPIN_PARAMS[idx];
  if (!p) throw new CodecError("deflate", "param", `no setting ${idx}`);
  return deflate(raw, p[0], wbits, p[2], p[1]);
}

/** crushpf 'r': preflate correction record + unpacked data -> original
 *  deflate stream. */
export function preflateReencode(correction, unpacked) {
  return run("crushpf", "preflate", (ex, put, o) =>
    ex.pf_reencode(put(correction), u32(correction.length), put(unpacked),
      u32(unpacked.length), o, o + 4));
}

/** crushjpg 'd': packed record ('B' Brunsli or 'J' jcm) -> original JPEG. */
export function jpegDecode(packed) {
  return run("crushjpg", "jpeg", (ex, put, o) =>
    ex.jpg_decode(put(packed), u32(packed.length), o, o + 4));
}

// ---- Crush's codec table ----------------------------------------------------
/** Decode one bundle payload by its Crush codec id (index field "codec").
 *  prior: bytes of the priors file the bundle names (.prime for crushcm_p,
 *  .zdict for zstd19d/zstd22d). */
export function decodeCodec(id, blob, prior = null, opts = {}) {
  switch (id) {
    case "store": return blob.slice();
    case "zstd12": case "zstd19": case "zstd22": return zstd(blob, opts);
    case "zstd19d": case "zstd22d":
      if (!prior) throw new CodecError(id, "prior", "needs its .zdict prior");
      return zstd(blob, { ...opts, dict: prior });
    case "lzma": return lzma2(blob, { ...opts, filter: "plain" });
    case "lzma_x86": return lzma2(blob, { ...opts, filter: "x86" });
    case "lzma_d4": return lzma2(blob, { ...opts, filter: "delta4" });
    case "lzma_text": return lzma2(blob, { ...opts, filter: "text" });
    case "brotli": return brotli(blob, opts);
    case "bzip2": return bzip2(blob, opts);
    case "ppmd": return ppmd(blob, opts);
    case "crushcm": return crushcm(blob, null, opts);
    case "crushcm_p":
      if (!prior) throw new CodecError(id, "prior", "needs its .prime prior");
      return crushcm(blob, prior, opts);
    default: throw new CodecError(id, "unknown", "not a codec this build knows");
  }
}

export const CODEC_IDS = ["store", "zstd12", "zstd19", "zstd22", "lzma", "lzma_x86",
  "lzma_d4", "lzma_text", "brotli", "bzip2", "ppmd", "crushcm", "zstd19d", "zstd22d",
  "crushcm_p"];

// ---- self-test ---------------------------------------------------------------
// crush_zpin's known-answer test: a digest of reference zlib 1.3.1 output for
// a spread of settings. Passing it means this deflate rebuilds streams exactly
// as the native crushz.dll / libcrushz.so do.
const KAT_DIGEST = "97bb08e19d712f0607e69c514e391400a504855168a40ccf3b576fda0ef5d9a8";
function katInput() {
  const words = ["the", "crush", "stream", "object", "endobj", "0 0 R", "/Type",
    "\n", "  ", "<<", ">>", "BT", "ET", "Tf", "1.000", "q Q"].map((w) => new TextEncoder().encode(w));
  let x = 0x2545F491;
  const out = [];
  while (out.length < 60000) {
    x = (Math.imul(x, 1103515245) + 12345) & 0x7FFFFFFF;
    for (const b of words[(x >> 16) % words.length]) out.push(b);
    out.push(32);
    if ((x >> 8) % 7 === 0) out.push((x >> 3) & 0xFF, (x >> 11) & 0xFF);
  }
  return new Uint8Array(out);
}
async function sha256hex(u8) {
  const h = await crypto.subtle.digest("SHA-256", u8);
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
/** Runs the pinned-deflate known-answer test. Resolves to true / false. */
export async function selfTestDeflate() {
  const data = katInput();
  const parts = [];
  ZPIN_PARAMS.forEach(([l, s, m], i) => {
    if ((([1, 6, 9].includes(l)) && m === 8) || ([3, 7].includes(l) && s === 0 && [1, 9].includes(m)))
      for (const wb of [-15, 15]) parts.push(zpinDeflate(i, data, wb));
  });
  const all = new Uint8Array(parts.reduce((a, p) => a + p.length, 0));
  let o = 0;
  for (const p of parts) { all.set(p, o); o += p.length; }
  return (await sha256hex(all)) === KAT_DIGEST;
}
