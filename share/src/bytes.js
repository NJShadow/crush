/* Crush share - byte helpers and an index-safe JSON parser.
 *
 * Everything here reads data that came out of an archive, and an archive may
 * have been made by someone hostile. So: every length is checked against what
 * is actually there, varints are capped, and nothing is allocated from a size
 * the input merely claims without a ceiling. */
(function (CR) {
"use strict";

class CrushError extends Error {
  constructor(msg, kind) { super(msg); this.kind = kind || "corrupt"; }
}
CR.CrushError = CrushError;
function fail(msg, kind) { throw new CrushError(msg, kind); }
CR.fail = fail;

// A ceiling on any single buffer this page will allocate from archive data.
// Browsers cap ArrayBuffers well below this anyway; the point is to fail with
// a sentence instead of a RangeError when a size field is absurd.
CR.MAX_ALLOC = 2 ** 31 - 1;

function checkLen(n, what) {
  if (typeof n === "bigint") n = Number(n);
  if (!Number.isSafeInteger(n) || n < 0 || n > CR.MAX_ALLOC)
    fail("the archive states an impossible size for " + what);
  return n;
}
CR.checkLen = checkLen;

/* int(x) for index values: accepts Number or BigInt, insists on a safe integer. */
function int(x, what) {
  if (typeof x === "bigint") {
    if (x > BigInt(Number.MAX_SAFE_INTEGER) || x < -BigInt(Number.MAX_SAFE_INTEGER))
      fail("a value in the archive index is out of range (" + (what || "?") + ")");
    return Number(x);
  }
  if (typeof x === "boolean") return x ? 1 : 0;   // Python's json accepts bool as int
  if (typeof x !== "number" || !Number.isInteger(x))
    fail("a value in the archive index is not an integer (" + (what || "?") + ")");
  return x;
}
CR.int = int;
CR.big = (x) => (typeof x === "bigint" ? x : BigInt(int(x)));

function concat(parts, total) {
  if (total === undefined) { total = 0; for (const p of parts) total += p.length; }
  const out = new Uint8Array(checkLen(total, "a joined buffer"));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}
CR.concat = concat;

function equal(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
CR.equal = equal;

CR.hex = (u8) => Array.from(u8, (b) => b.toString(16).padStart(2, "0")).join("");
CR.fromHex = function (s) {
  if (typeof s !== "string" || s.length % 2 || /[^0-9a-fA-F]/.test(s))
    fail("a hex field in the archive index is malformed");
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(2 * i, 2), 16);
  return out;
};
CR.latin1 = function (s) {          // Python str.encode("latin-1")
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c > 255) fail("a latin-1 field in the archive index is malformed");
    out[i] = c;
  }
  return out;
};
CR.ascii = (s) => { const o = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) o[i] = s.charCodeAt(i) & 255; return o; };
CR.utf8 = (s) => new TextEncoder().encode(s);
CR.utf8dec = (u8) => new TextDecoder("utf-8").decode(u8);

/* A growable byte buffer - Python's bytearray, for the inverse transforms. */
class Buf {
  constructor(cap) { this.b = new Uint8Array(Math.max(16, cap || 0)); this.n = 0; }
  _grow(need) {
    if (need <= this.b.length) return;
    let c = this.b.length * 2;
    while (c < need) c *= 2;
    const nb = new Uint8Array(checkLen(Math.min(c, Math.max(need, CR.MAX_ALLOC)), "an output buffer"));
    nb.set(this.b.subarray(0, this.n));
    this.b = nb;
  }
  push(x) { this._grow(this.n + 1); this.b[this.n++] = x; }
  add(u8) { this._grow(this.n + u8.length); this.b.set(u8, this.n); this.n += u8.length; }
  bytes() { return this.b.subarray(0, this.n); }
}
CR.Buf = Buf;

/* Little-endian base-128 varint reader shared by Prism, Echo, Vector, Genome.
 * Python's readers raise IndexError when they run off the end; this does the
 * same, and also refuses varints past 64 bits (Python would happily build a
 * huge integer; nothing Crush writes is that wide). Returns Number when it
 * fits, otherwise BigInt via uvBig(). */
class Reader {
  constructor(b, i) { this.b = b; this.i = i || 0; }
  uvBig() {
    let r = 0n, s = 0n, k = 0;
    for (;;) {
      if (this.i >= this.b.length) fail("a length field runs past the end of its data");
      const c = this.b[this.i++];
      r |= BigInt(c & 0x7f) << s;
      if (!(c & 0x80)) return r;
      s += 7n;
      if (++k > 10) fail("a length field in the archive is malformed");
    }
  }
  uv() {
    let r = 0, mul = 1, k = 0;
    for (;;) {
      if (this.i >= this.b.length) fail("a length field runs past the end of its data");
      const c = this.b[this.i++];
      r += (c & 0x7f) * mul;
      if (!(c & 0x80)) break;
      mul *= 128;
      if (++k > 7) fail("a length field in the archive is too large");
    }
    if (!Number.isSafeInteger(r)) fail("a length field in the archive is too large");
    return r;
  }
  /* Python slicing semantics: take(n) past the end returns what is there. */
  take(n) {
    const v = this.b.subarray(this.i, this.i + n);
    this.i += n;
    return v;
  }
  byte() {
    if (this.i >= this.b.length) fail("a field runs past the end of its data");
    return this.b[this.i++];
  }
}
CR.Reader = Reader;

/* ---------------------------------------------------------------------
 * JSON, as Python's json.dumps writes it, without losing integers.
 * JSON.parse turns 2**64-1 into 18446744073709552000; the Oracle and
 * Prism transforms store exactly such numbers. Integers that do not fit a
 * double exactly come back as BigInt; NaN / Infinity are accepted because
 * Python emits them. Depth is capped so a hostile index cannot blow the stack.
 * ------------------------------------------------------------------- */
function parseJSON(text) {
  let i = 0, depth = 0;
  const n = text.length;
  const ws = () => { while (i < n) { const c = text.charCodeAt(i); if (c === 32 || c === 9 || c === 10 || c === 13) i++; else break; } };
  const err = () => fail("the archive index is not valid JSON (at " + i + ")");
  function str() {
    i++;  // opening quote
    let out = "", start = i;
    for (;;) {
      if (i >= n) err();
      const c = text.charCodeAt(i);
      if (c === 34) { out += text.slice(start, i); i++; return out; }
      if (c === 92) {
        out += text.slice(start, i);
        const e = text[i + 1];
        if (e === "u") {
          const h = text.substr(i + 2, 4);
          if (!/^[0-9a-fA-F]{4}$/.test(h)) err();
          out += String.fromCharCode(parseInt(h, 16)); i += 6;
        } else {
          const m = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" }[e];
          if (m === undefined) err();
          out += m; i += 2;
        }
        start = i;
      } else if (c < 32) err();
      else i++;
    }
  }
  function num() {
    const m = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/.exec(text.slice(i, i + 400));
    if (!m) err();
    i += m[0].length;
    if (m[2] === undefined && m[3] === undefined) {
      const v = Number(m[0]);
      if (Number.isSafeInteger(v)) return v;
      return BigInt(m[0]);
    }
    return Number(m[0]);
  }
  function val() {
    ws();
    if (i >= n) err();
    const c = text[i];
    if (c === "{") {
      if (++depth > 64) fail("the archive index is nested too deeply");
      i++; const o = {}; ws();
      if (text[i] === "}") { i++; depth--; return o; }
      for (;;) {
        ws(); if (text[i] !== '"') err();
        const k = str(); ws();
        if (text[i] !== ":") err(); i++;
        const v = val();
        if (k === "__proto__") Object.defineProperty(o, k, { value: v, enumerable: true, writable: true, configurable: true });
        else o[k] = v;
        ws();
        if (text[i] === ",") { i++; continue; }
        if (text[i] === "}") { i++; depth--; return o; }
        err();
      }
    }
    if (c === "[") {
      if (++depth > 64) fail("the archive index is nested too deeply");
      i++; const a = []; ws();
      if (text[i] === "]") { i++; depth--; return a; }
      for (;;) {
        a.push(val()); ws();
        if (text[i] === ",") { i++; continue; }
        if (text[i] === "]") { i++; depth--; return a; }
        err();
      }
    }
    if (c === '"') return str();
    if (text.startsWith("true", i)) { i += 4; return true; }
    if (text.startsWith("false", i)) { i += 5; return false; }
    if (text.startsWith("null", i)) { i += 4; return null; }
    if (text.startsWith("NaN", i)) { i += 3; return NaN; }
    if (text.startsWith("Infinity", i)) { i += 8; return Infinity; }
    if (text.startsWith("-Infinity", i)) { i += 9; return -Infinity; }
    return num();
  }
  const v = val(); ws();
  if (i !== n) err();
  return v;
}
CR.parseJSON = parseJSON;

/* Python's str(int) and int(bytes) for the transforms that print numbers. */
CR.decBytes = (v) => CR.ascii(v.toString());

})(globalThis.CR = globalThis.CR || {});
