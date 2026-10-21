/* Crush share - the inverse transforms, ported from crush_engine.py and the
 * domain modules, for archives opened in a browser.
 *
 * Each inverse is a line-for-line port of the Python one, including its
 * integer semantics: NumPy's wrapping int64/uint64 arithmetic is reproduced
 * exactly (BigInt.asIntN / asUintN, or paired 32-bit halves on the hot
 * paths), and Python's unbounded integers become BigInt. Nothing here trusts
 * the archive: a malformed payload throws, and every file is SHA-256 checked
 * after its chain is inverted, so a wrong result can never reach the user as
 * if it were right.
 *
 * Portability verdicts (see REPORT.md):
 *   oracle prism transpose audio_predict floatarray genome loom   - ported here
 *   deflate_unwrap pdf_unwrap gzip_unwrap png_unwrap               - ported here; the
 *       deflate re-encoder (pinned zlib 1.3.1) and preflate come from WASM helpers
 *   jpeg_pack jpeg_embedded                                        - ported; crushjpg WASM
 *   jpegxl flac zip_recompose perc_image perc_audio                - NOT portable
 */
(function (CR) {
"use strict";
const fail = CR.fail, int = CR.int, big = CR.big;
const MASK64 = (1n << 64n) - 1n;
const asI64 = (x) => BigInt.asIntN(64, x);
const asU64 = (x) => BigInt.asUintN(64, x);

class Unavailable extends CR.CrushError { constructor(m) { super(m, "unavailable"); } }
CR.Unavailable = Unavailable;

/* Python floor division and modulo for BigInt (Python ints and NumPy int64 /, %
 * both floor; JS BigInt truncates). */
function fdiv(a, b) { if (b === 0n) fail("division by zero in a transform"); const q = a / b; return (a % b !== 0n && ((a < 0n) !== (b < 0n))) ? q - 1n : q; }
function fmod(a, b) { if (b === 0n) return 0n; const r = a % b; return (r !== 0n && ((r < 0n) !== (b < 0n))) ? r + b : r; }
CR.fdiv = fdiv; CR.fmod = fmod;

function u8le(n) { return n; }
function readU32LE(b) {               // np.frombuffer(b, "<u4")
  if (b.length % 4) fail("a 32-bit array has a ragged length");
  const out = new Float64Array(b.length / 4);
  for (let i = 0, j = 0; i < out.length; i++, j += 4) out[i] = (b[j] | (b[j+1] << 8) | (b[j+2] << 16)) + b[j+3] * 16777216;
  return out;
}
function readU64LEasNum(b) {          // np.frombuffer(b, "<u8").astype(int64), values used as indices / lengths
  if (b.length % 8) fail("a 64-bit array has a ragged length");
  const out = new Float64Array(b.length / 8);
  for (let i = 0, j = 0; i < out.length; i++, j += 8) {
    const lo = (b[j] | (b[j+1] << 8) | (b[j+2] << 16)) + b[j+3] * 16777216;
    const hi = (b[j+4] | (b[j+5] << 8) | (b[j+6] << 16)) + b[j+7] * 16777216;
    if (hi >= 2097152) fail("a length in the archive is out of range");
    out[i] = hi * 4294967296 + lo;
  }
  return out;
}

/* =====================================================================
 *  W-bit unsigned vectors (W = 1, 2, 4 or 8 bytes) as paired 32-bit halves.
 *  NumPy's uint arithmetic wraps mod 2^(8W); so does every op here.
 * =================================================================== */
class UVec {
  constructor(n, W) {
    this.n = n; this.W = W;
    this.lo = new Uint32Array(CR.checkLen(n, "an array"));
    this.hi = W === 8 ? new Uint32Array(n) : null;
    this.m = W === 8 ? 0xFFFFFFFF : W === 4 ? 0xFFFFFFFF : W === 2 ? 0xFFFF : 0xFF;
  }
  static fromLE(b, W, n) {            // np.frombuffer(b, "<uW", count=n)
    if (n === undefined) { if (b.length % W) fail("an array has a ragged length"); n = b.length / W; }
    if (b.length < n * W) fail("an array is shorter than its stated count");
    const v = new UVec(n, W);
    for (let i = 0, j = 0; i < n; i++, j += W) {
      let lo = b[j];
      if (W >= 2) lo |= b[j+1] << 8;
      if (W >= 4) lo = (lo | (b[j+2] << 16) | (b[j+3] << 24)) >>> 0;
      v.lo[i] = lo;
      if (W === 8) v.hi[i] = (b[j+4] | (b[j+5] << 8) | (b[j+6] << 16) | (b[j+7] << 24)) >>> 0;
    }
    return v;
  }
  toLE(be) {                          // .tobytes() (or .astype(">uW").tobytes())
    const W = this.W, out = new Uint8Array(this.n * W);
    for (let i = 0, j = 0; i < this.n; i++, j += W) {
      const lo = this.lo[i], hi = this.hi ? this.hi[i] : 0;
      for (let k = 0; k < W; k++) {
        const byte = k < 4 ? (lo >>> (8 * k)) & 255 : (hi >>> (8 * (k - 4))) & 255;
        out[be ? j + W - 1 - k : j + k] = byte;
      }
    }
    return out;
  }
  get(i) { return this.hi ? (BigInt(this.hi[i]) << 32n) | BigInt(this.lo[i]) : BigInt(this.lo[i]); }
  copy() { const v = new UVec(this.n, this.W); v.lo.set(this.lo); if (this.hi) v.hi.set(this.hi); return v; }
}
CR.UVec = UVec;

function xorAccumulate(v, start, end, step) {   // np.bitwise_xor.accumulate over v[start:end:step], in place
  let pl = 0, ph = 0, first = true;
  for (let i = start; i < end; i += step) {
    if (first) { pl = v.lo[i]; ph = v.hi ? v.hi[i] : 0; first = false; continue; }
    pl = (pl ^ v.lo[i]) >>> 0; v.lo[i] = pl;
    if (v.hi) { ph = (ph ^ v.hi[i]) >>> 0; v.hi[i] = ph; }
  }
}
function addAccumulate(v, start, end, step) {   // np.add.accumulate(dtype=uW), in place, wrapping
  start = start || 0; end = end === undefined ? v.n : end; step = step || 1;
  let pl = 0, ph = 0, first = true;
  const m = v.m;
  for (let i = start; i < end; i += step) {
    if (first) { pl = v.lo[i]; ph = v.hi ? v.hi[i] : 0; first = false; continue; }
    const s = pl + v.lo[i];
    if (v.hi) {
      const c = s > 0xFFFFFFFF ? 1 : 0;
      pl = s >>> 0; ph = (ph + v.hi[i] + c) >>> 0; v.hi[i] = ph;
    } else pl = (s & m) >>> 0;
    v.lo[i] = pl;
  }
}
function unzigzagVec(v) {             // ((z >> 1) ^ (0 - (z & 1))) mod 2^(8W)
  const m = v.m;
  for (let i = 0; i < v.n; i++) {
    const neg = v.lo[i] & 1;
    if (v.hi) {
      const lo = ((v.lo[i] >>> 1) | ((v.hi[i] & 1) << 31)) >>> 0, hi = v.hi[i] >>> 1;
      v.lo[i] = neg ? ~lo >>> 0 : lo; v.hi[i] = neg ? ~hi >>> 0 : hi;
    } else {
      const lo = v.lo[i] >>> 1;
      v.lo[i] = (neg ? ~lo : lo) & m;
      if (m === 0xFFFFFFFF) v.lo[i] >>>= 0;
    }
  }
}
function unmonoVec(v) {               // m ^ (top ? signbit : all-ones)
  const W = v.W;
  for (let i = 0; i < v.n; i++) {
    if (W === 8) {
      const top = v.hi[i] >>> 31;
      if (top) v.hi[i] = (v.hi[i] ^ 0x80000000) >>> 0;
      else { v.hi[i] = ~v.hi[i] >>> 0; v.lo[i] = ~v.lo[i] >>> 0; }
    } else {
      const bits = 8 * W, top = (v.lo[i] >>> (bits - 1)) & 1;
      const sb = bits === 32 ? 0x80000000 : 1 << (bits - 1);
      v.lo[i] = (top ? (v.lo[i] ^ sb) : (~v.lo[i] & v.m)) >>> 0;
    }
  }
}
function shlVec(v, t) {               // (v << t) mod 2^(8W)
  t = t & 255;
  for (let i = 0; i < v.n; i++) {
    if (v.hi) {
      const x = asU64(((BigInt(v.hi[i]) << 32n) | BigInt(v.lo[i])) << BigInt(t));
      v.lo[i] = Number(x & 0xFFFFFFFFn); v.hi[i] = Number(x >> 32n);
    } else {
      v.lo[i] = t >= 32 ? 0 : ((v.lo[i] * 2 ** t) % 4294967296) & v.m;
      if (v.m === 0xFFFFFFFF) v.lo[i] >>>= 0;
    }
  }
}

/* ---------------------------------------------------------------------
 *  crush_vector (FloatArray) - decoders
 * ------------------------------------------------------------------- */
const V = {};
V.getVarint = function (buf, off) { const r = new CR.Reader(buf, off); const v = r.uv(); return [v, r.i]; };
V.unpackStreams = function (buf, off) {
  const r = new CR.Reader(buf, off);
  const n = r.uv(), lens = [];
  for (let i = 0; i < n; i++) lens.push(r.uv());
  const out = [];
  for (const l of lens) out.push(r.take(l));
  return [out, r.i];
};
V.planesInv = function (streams, cnst, n, W) {
  const b = new Uint8Array(CR.checkLen(n * W, "a plane matrix"));
  for (let p = 0; p < Math.min(streams.length, W); p++) {
    const c = p < cnst.length ? cnst[p] : -1;
    if (c >= 0) { for (let i = 0; i < n; i++) b[i * W + p] = c; }
    else if (streams[p].length) {
      const s = streams[p];
      if (s.length < n) fail("a byte plane is shorter than its array");
      for (let i = 0; i < n; i++) b[i * W + p] = s[i];
    }
  }
  return UVec.fromLE(b, W, n);
};
const M_RAW = 0, M_SPLIT = 1, M_XOR1 = 2, M_XORK = 3, M_ODELTA1 = 4, M_ODELTA2 = 5,
      M_LORENZO = 6, M_IDELTA1 = 7, M_IDELTA2 = 8, M_SEM = 9, M_TRUNC = 20, M_DECIMAL = 21, M_NARROW = 22;
const BLK = 512;

V.decBody = function (streams, cnst, mode, W, n, extra) {
  if (mode === M_RAW) return UVec.fromLE(streams[0], W, n);
  if (mode === M_SPLIT) return V.planesInv(streams, cnst, n, W);
  if (mode === M_XOR1) { const r = V.planesInv(streams, cnst, n, W); xorAccumulate(r, 0, n, 1); return r; }
  if (mode === M_XORK) {
    const sel = streams[0];
    const r = V.planesInv(streams.slice(1), cnst, n, W);
    const out = new UVec(n, W);
    for (let bi = 0; bi < sel.length; bi++) {
      const s0 = bi * BLK, s1 = Math.min(s0 + BLK, n);
      if (s1 <= s0) break;
      const j = sel[bi] + 1;
      for (let c = 0; c < j; c++) {
        const lo = s0 + c;
        if (lo >= s1) break;
        let al = 0, ah = 0, first = true;
        for (let i = lo; i < s1; i += j) {
          if (first) { al = r.lo[i]; ah = r.hi ? r.hi[i] : 0; first = false; }
          else { al = (al ^ r.lo[i]) >>> 0; if (r.hi) ah = (ah ^ r.hi[i]) >>> 0; }
          let ol = al, oh = ah;
          if (lo - j >= 0) { ol = (ol ^ out.lo[lo - j]) >>> 0; if (r.hi) oh = (oh ^ out.hi[lo - j]) >>> 0; }
          out.lo[i] = ol; if (out.hi) out.hi[i] = oh;
        }
      }
    }
    return out;
  }
  if (mode === M_ODELTA1 || mode === M_ODELTA2 || mode === M_IDELTA1 || mode === M_IDELTA2) {
    const d = V.planesInv(streams, cnst, n, W);
    unzigzagVec(d);
    if ((mode === M_ODELTA2 || mode === M_IDELTA2) && n > 1) addAccumulate(d, 1, n, 1);
    addAccumulate(d, 0, n, 1);
    if (mode === M_ODELTA1 || mode === M_ODELTA2) unmonoVec(d);
    return d;
  }
  if (mode === M_LORENZO) {
    if (extra.length !== 4) fail("a Lorenzo channel has a bad header");
    const w = (extra[0] | (extra[1] << 8) | (extra[2] << 16)) + extra[3] * 16777216;
    if (w === 0) fail("a Lorenzo channel has zero width");
    const rows = Math.floor(n / w);
    const r = V.planesInv(streams, cnst, n, W);
    unzigzagVec(r);
    for (let row = 0; row < rows; row++) addAccumulate(r, row * w, row * w + w, 1);     // axis=1
    for (let col = 0; col < w && rows > 0; col++) addAccumulate(r, col, rows * w, w);    // axis=0
    if (n > rows * w) addAccumulate(r, rows * w, n, 1);
    unmonoVec(r);
    return r;
  }
  if (mode === M_SEM) {
    const bits = W * 8;
    const [ebits, mbits] = W === 8 ? [11, 52] : [8, 23];
    const sb = streams[0];
    if (sb.length * 8 < n) fail("a sign stream is too short");
    let expv, rest, crest;
    if (ebits > 8) { expv = V.planesInv(streams.slice(1, 3), cnst.slice(0, 2), n, 2); rest = streams.slice(3); crest = cnst.slice(2); }
    else {
      if (streams[1].length < n) fail("an exponent stream is too short");
      expv = new UVec(n, 1); for (let i = 0; i < n; i++) expv.lo[i] = streams[1][i];
      rest = streams.slice(2); crest = cnst.slice(1);
    }
    const man = V.planesInv(rest, crest, n, W);
    for (let i = 0; i < n; i++) {
      const s = (sb[i >> 3] >> (i & 7)) & 1;
      if (W === 8) {
        // sign << 63 | exp << 52 | man, mod 2^64 (exp may be up to 16 bits if hostile)
        const e = expv.lo[i];
        man.hi[i] = ((man.hi[i] | ((e << 20) >>> 0) | (s ? 0x80000000 : 0)) >>> 0);
      } else {
        man.lo[i] = (man.lo[i] | ((expv.lo[i] << 23) >>> 0) | (s ? 0x80000000 : 0)) >>> 0;
      }
    }
    return man;
  }
  fail("unknown numeric channel mode " + mode);
};
V.unpackChannel = function (buf, off) {
  const r = new CR.Reader(buf, off);
  const mode = r.byte();
  const nc = r.uv(), cnst = [];
  for (let i = 0; i < nc; i++) { const f = r.byte(); cnst.push(f ? r.byte() : -1); }
  const xl = r.uv();
  const extra = r.take(xl);
  const [streams, o2] = V.unpackStreams(buf, r.i);
  return [mode, cnst, extra, streams, o2];
};

/* Exact powers of ten: 10.0**d in Python is exact for d <= 22. */
const POW10 = []; for (let d = 0; d <= 22; d++) POW10.push(Number("1e" + d));

/* float32 bit pattern -> float64 bit pattern, in integers, as x86 cvtss2sd
 * does it (exact; NaN keeps its payload and is quieted). JS engines may
 * canonicalise NaN, so NaNs are never allowed through a Float32Array read. */
function widenF32(bits) {
  const s = bits >>> 31, e = (bits >>> 23) & 255, m = bits & 0x7FFFFF;
  let hi, lo;
  if (e === 255) {
    const mm = m ? (m | 0x400000) : 0;
    hi = (s << 31) | 0x7FF00000 | (mm >>> 3); lo = (mm & 7) << 29;
  } else if (e === 0) {
    if (m === 0) { hi = s << 31; lo = 0; }
    else {
      let p = 22; while (!((m >>> p) & 1)) p--;
      const e64 = p - 149 + 1023;
      const frac = (m - (1 << p)) * 2 ** (52 - p);          // < 2^52, exact
      hi = (s << 31) | (e64 << 20) | Math.floor(frac / 4294967296); lo = frac % 4294967296;
    }
  } else {
    hi = (s << 31) | ((e - 127 + 1023) << 20) | (m >>> 3); lo = (m & 7) << 29;
  }
  return [hi >>> 0, lo >>> 0];
}
CR.widenF32 = widenF32;

const _f64 = new Float64Array(1), _f64u = new Uint32Array(_f64.buffer);
const _f32 = new Float32Array(1), _f32u = new Uint32Array(_f32.buffer);

V.decodeChannel = function (buf, off, W, n) {
  const [mode, cnst, extra, streams, off2] = V.unpackChannel(buf, off);
  if (mode === M_TRUNC) {
    const body = streams[0];
    if (!body.length) fail("an empty numeric channel");
    const t = body[0];
    const [m2, c2, x2, s2] = V.unpackChannel(body, 1);
    const v = V.decBody(s2, c2, m2, W, n, x2);
    shlVec(v, t);
    return [v, off2];
  }
  if (mode === M_DECIMAL || mode === M_NARROW) {
    const body = streams[0];
    let d = 0, nbad, o;
    if (mode === M_DECIMAL) {
      if (body.length < 5) fail("a decimal channel has a bad header");
      d = body[0]; nbad = (body[1] | (body[2] << 8) | (body[3] << 16)) + body[4] * 16777216; o = 5;
    } else {
      if (body.length < 4) fail("a narrowed channel has a bad header");
      nbad = (body[0] | (body[1] << 8) | (body[2] << 16)) + body[3] * 16777216; o = 4;
    }
    const [exc, o2] = V.unpackStreams(body, o);
    const [m2, c2, x2, s2] = V.unpackChannel(body, o2);
    let out;
    if (mode === M_DECIMAL) {
      const mu = V.decBody(s2, c2, m2, 8, n, x2);
      const sc = d <= 22 ? POW10[d] : Math.pow(10, d);
      out = new UVec(n, W);
      for (let i = 0; i < n; i++) {
        // int64 -> float64 is one correctly-rounded addition of two exact
        // doubles (hi*2^32 is exact), then one correctly-rounded division:
        // the same two IEEE-754 operations NumPy performs.
        const hiS = mu.hi[i] | 0;
        const x = (hiS * 4294967296 + mu.lo[i]) / sc;
        if (W === 8) { _f64[0] = x; out.lo[i] = _f64u[0]; out.hi[i] = _f64u[1]; }
        else { _f32[0] = x; out.lo[i] = _f32u[0]; }            // round-to-nearest-even, as astype("<f4")
      }
    } else {
      if (W !== 8) fail("a narrowed channel must be float64");
      const mu = V.decBody(s2, c2, m2, 4, n, x2);
      out = new UVec(n, 8);
      for (let i = 0; i < n; i++) { const [h, l] = widenF32(mu.lo[i]); out.hi[i] = h; out.lo[i] = l; }
    }
    if (nbad) {
      const idx = readU64LEasNum(exc[0]);
      const vals = UVec.fromLE(exc[1], W);
      if (vals.n !== idx.length && vals.n !== 1) fail("an exception list is malformed");
      let acc = 0;
      for (let k = 0; k < idx.length; k++) {
        acc += idx[k];
        if (!(acc < n)) fail("an exception index is out of range");
        const s = vals.n === 1 ? 0 : k;
        out.lo[acc] = vals.lo[s]; if (out.hi) out.hi[acc] = vals.hi[s];
      }
    }
    return [out, off2];
  }
  return [V.decBody(streams, cnst, mode, W, n, extra), off2];
};

function floatarrayInverse(buf) {
  if (!(buf[0] === 0x46 && buf[1] === 0x43 && buf[2] === 0x30 && buf[3] === 0x31)) fail("bad floatarray payload");
  const r = new CR.Reader(buf, 4);
  const W = r.byte(), be = r.byte();
  if (![1, 2, 4, 8].includes(W)) fail("bad floatarray item size");
  const C = r.uv(), rows = r.uv(); r.uv();          // n is informational
  let [extra, off] = V.unpackStreams(buf, r.i);
  CR.checkLen(rows * C * W, "a numeric array");
  const cols = [];
  for (let c = 0; c < C; c++) { const [col, o] = V.decodeChannel(buf, off, W, rows); cols.push(col); off = o; }
  const rest = UVec.fromLE(extra[0], W);
  const u = new UVec(rows * C + rest.n, W);
  for (let c = 0; c < C; c++) for (let i = 0; i < rows; i++) {
    u.lo[i * C + c] = cols[c].lo[i]; if (u.hi) u.hi[i * C + c] = cols[c].hi[i];
  }
  u.lo.set(rest.lo, rows * C); if (u.hi) u.hi.set(rest.hi, rows * C);
  return CR.concat([u.toLE(!!be), extra[1]]);
}

/* ---------------------------------------------------------------------
 *  Oracle - program synthesis. Programs regenerate their bytes exactly.
 * ------------------------------------------------------------------- */
const DT = { "<u1": [1, false, false], "<u2": [2, false, false], "<u4": [4, false, false], "<u8": [8, false, false],
             "<i2": [2, true, false], "<i4": [4, true, false], "<i8": [8, true, false], ">u2": [2, false, true], ">u4": [4, false, true] };
function dtOf(s) { const d = DT[s]; if (!d) fail("unknown Oracle dtype " + s); return { w: d[0], signed: d[1], be: d[2] }; }

/* Writes value v (BigInt, any sign) as the low 8w bits in the dtype's byte order. */
function putVal(out, i, w, be, v) {
  let x = BigInt.asUintN(8 * w, v);
  for (let k = 0; k < w; k++) { out[be ? i * w + w - 1 - k : i * w + k] = Number(x & 255n); x >>= 8n; }
}
function putVal32(out, i, w, be, x) {   // x: number, low 32 bits valid
  for (let k = 0; k < w; k++) { out[be ? i * w + w - 1 - k : i * w + k] = (x >>> (8 * k)) & 255; }
}

function oracleGenerate(prog, blk, tail) {
  const k = prog.k;
  const n = (x) => CR.checkLen(int(x, "n"), "an Oracle program");
  if (k === "const") { const out = new Uint8Array(n(prog.n)); out.fill(int(prog.b) & 255); return out; }
  if (k === "period") {
    const N = n(prog.n);
    if (!blk.length) fail("an empty Oracle period");
    const out = new Uint8Array(N);
    for (let o = 0; o < N; o += blk.length) out.set(blk.subarray(0, Math.min(blk.length, N - o)), o);
    return out;
  }
  if (k === "template") return oracleTemplate(prog);
  const cnt = n(prog.n);
  const { w, be } = dtOf(prog.dt);
  const out = new Uint8Array(CR.checkLen(cnt * w + tail.length, "an Oracle program"));
  if (k === "modlin") {
    // numpy: (c0 + step * arange(n)) % M in wrapping int64, then .astype(dt)
    const c0 = big(prog.c0), step = big(prog.step), M = big(prog.M);
    for (let i = 0; i < cnt; i++) {
      const v = fmod(asI64(c0 + asI64(step * BigInt(i))), M);
      putVal(out, i, w, be, v);
    }
  } else if (k === "poly" || k === "poly_slow") {
    // finite-difference regeneration; every result is reduced to the low 8w
    // bits, so mod 2^32 arithmetic is exact for w <= 4 and BigInt for w = 8
    const col = prog.c.map(big);
    if (!col.length) fail("an empty Oracle polynomial");
    if (w <= 4) {
      const cur = col.map((x) => Number(BigInt.asUintN(32, x)));
      for (let i = 0; i < cnt; i++) {
        putVal32(out, i, w, be, cur[0]);
        for (let d = 0; d < cur.length - 1; d++) cur[d] = (cur[d] + cur[d + 1]) >>> 0;
      }
    } else {
      const cur = col.map((x) => asU64(x));
      for (let i = 0; i < cnt; i++) {
        putVal(out, i, w, be, cur[0]);
        for (let d = 0; d < cur.length - 1; d++) cur[d] = asU64(cur[d] + cur[d + 1]);
      }
    }
  } else if (k === "linrec") {
    const a = prog.a.map(big), init = prog.i.map(big);
    if (!a.length) fail("an empty Oracle recurrence");
    const kk = a.length - 1, b = a[kk];
    const W = BigInt(8 * w);
    const hist = [];
    for (let i = 0; i < Math.min(init.length, cnt); i++) { hist.push(BigInt.asUintN(8 * w, init[i])); putVal(out, i, w, be, init[i]); }
    for (let i = init.length; i < cnt; i++) {
      let v = b;
      for (let j = 0; j < kk; j++) { const h = hist[i - j - 1]; if (h === undefined) fail("an Oracle recurrence is malformed"); v += a[j] * h; }
      v = BigInt.asUintN(8 * w, v);
      hist.push(v); putVal(out, i, w, be, v);
      if (hist.length > kk + 8 && i > 64) hist[i - kk - 4] = undefined;   // only the last k values are needed
    }
    void W;
  } else if (k === "lcg") {
    const a = big(prog.a), c = big(prog.c);
    let x = BigInt.asUintN(8 * w, big(prog.s));
    if (w <= 4) {
      let xn = Number(x); const an = Number(BigInt.asUintN(32, a)), cn = Number(BigInt.asUintN(32, c));
      for (let i = 0; i < cnt; i++) { putVal32(out, i, w, be, xn); xn = (Math.imul(an, xn) + cn) >>> 0; }
    } else {
      for (let i = 0; i < cnt; i++) { putVal(out, i, w, be, x); x = asU64(a * x + c); }
    }
  } else fail("unknown Oracle program kind " + k);
  out.set(tail, cnt * w);
  return out;
}

function oracleTemplate(prog) {
  const skel = prog.skel.map(CR.latin1);
  const N = CR.checkLen(int(prog.n), "an Oracle template");
  const models = prog.f.map((m) => ({ cur: m.c.map(big), w: int(m.w) }));
  const out = new CR.Buf(1024);
  const NL = 10;
  for (let i = 0; i < N; i++) {
    if (i) out.push(NL);
    for (let j = 0; j < skel.length; j++) {
      out.add(skel[j]);
      if (j < models.length) {
        const m = models[j];
        let s = m.cur[0].toString();
        if (m.w) s = s.padStart(m.w, "0");
        out.add(CR.ascii(s));
        for (let d = 0; d < m.cur.length - 1; d++) m.cur[d] += m.cur[d + 1];
      }
    }
  }
  out.add(CR.latin1(prog.t));
  return out.bytes().slice();
}

function oracleInverse(payload, aux) {
  const prog = CR.parseJSON(aux.p);
  let o = 0, blk = new Uint8Array(0);
  if (prog._blk_n !== undefined) { const L = int(prog._blk_n); blk = payload.subarray(o, o + L); o += L; }
  const tail = payload.subarray(o, o + (prog._tail_n !== undefined ? int(prog._tail_n) : 0));
  return oracleGenerate(prog, blk, tail);
}

/* ---------------------------------------------------------------------
 *  Prism - structural decomposition of JSON / delimited text / markup.
 * ------------------------------------------------------------------- */
function prismUnpack(buf) {
  const r = new CR.Reader(buf);
  const n = r.uv(), lens = [];
  for (let i = 0; i < n; i++) lens.push(r.uv());
  return lens.map((L) => r.take(L));
}
function genIntpoly(col, n) {           // exact (Python ints)
  const cur = col.slice(), out = new Array(n);
  for (let i = 0; i < n; i++) { out[i] = cur[0]; for (let d = 0; d < cur.length - 1; d++) cur[d] += cur[d + 1]; }
  return out;
}
const unzig = (z) => ((z & 1n) ? -((z + 1n) >> 1n) : (z >> 1n));
function readColumn(mode, buf, count) {
  count = CR.checkLen(count, "a column");
  if (mode === 1) {
    // numpy: uint64 zigzag -> int64, then a wrapping int64 cumsum
    const r = new CR.Reader(buf), out = new Array(count);
    let acc = 0n;
    for (let i = 0; i < count; i++) {
      const z = r.uvBig();
      if (z > MASK64) fail("an integer column value is out of range");
      const d = asI64(z >> 1n) ^ -asI64(z & 1n);
      acc = asI64(acc + d);
      out[i] = CR.decBytes(acc);
    }
    return out;
  }
  if (mode === 2 || mode === 3) {
    const r = new CR.Reader(buf);
    const k = r.uv(), lead = [];
    for (let i = 0; i < k; i++) lead.push(r.take(r.uv()));
    const m = count - k;
    if (m < 0) fail("a column law has more leading values than rows");
    if (mode === 2) {
      const p = r.uv(), base = [];
      for (let i = 0; i < p; i++) base.push(r.take(r.uv()));
      if (m && !p) fail("a cyclic column has period zero");
      const out = lead.slice();
      for (let i = 0; i < m; i++) out.push(base[i % p]);
      return out;
    }
    const order = r.uv(), col = [];
    for (let i = 0; i < order; i++) col.push(unzig(r.uvBig()));
    if (!order && m) fail("an empty column polynomial");
    const out = genIntpoly(col, m);
    const nz = r.uv();
    let prev = -1;
    for (let q = 0; q < nz; q++) {
      const gap = r.uv(), val = unzig(r.uvBig());
      const i = prev + 1 + gap;
      if (i >= m) fail("a column correction is out of range");
      out[i] += val; prev = i;
    }
    return lead.concat(out.map((v) => CR.decBytes(v)));
  }
  if (mode !== 0) fail("unknown column mode " + mode);
  const r = new CR.Reader(buf), out = [];
  for (let i = 0; i < count; i++) out.push(r.take(r.uv()));
  return out;
}
function prismJson(payload, aux) {
  const parts = prismUnpack(payload);
  if (parts.length < 5) fail("a JSON shred is missing streams");
  const [skel, punct, ws, keyseq, kd] = parts;
  const streams = parts.slice(5);
  const order = aux.order.map((x) => int(x)), modes = aux.modes.map((x) => int(x)), counts = aux.counts.map((x) => int(x));
  const colvals = new Map(), colpos = new Map();
  for (let i = 0; i < order.length; i++) {
    if (i >= streams.length) fail("a JSON shred is missing a column");
    colvals.set(order[i], readColumn(modes[i], streams[i], counts[i])); colpos.set(order[i], 0);
  }
  const rk = new CR.Reader(kd), keydict = [];
  for (let i = 0; i < int(aux.nkeys); i++) keydict.push(rk.take(rk.uv()));
  const rw = new CR.Reader(ws), rq = new CR.Reader(keyseq);
  let pp = 0;
  const out = new CR.Buf(payload.length * 2);
  let cur = -1; const stack = [];
  for (let s = 0; s < skel.length; s++) {
    const c = skel[s];
    if (c === 87) out.add(rw.take(rw.uv()));                    // W
    else if (c === 80) {                                          // P
      if (pp >= punct.length) { pp++; continue; }                 // Python: take(1) past the end is b""
      const ch = punct[pp++]; out.push(ch);
      if (ch === 123) { stack.push(cur); cur = -1; }
      else if (ch === 125) cur = stack.length ? stack.pop() : -1;
      else if (ch === 91) stack.push(cur);
      else if (ch === 93) cur = stack.length ? stack.pop() : -1;
    } else if (c === 75) {                                        // K
      cur = rq.uv();
      if (cur >= keydict.length) fail("a JSON key index is out of range");
      out.add(keydict[cur]);
    } else {
      const col = colvals.get(cur);
      if (!col) fail("a JSON value has no column");
      const p = colpos.get(cur);
      if (p >= col.length) fail("a JSON column ran out of values");
      colpos.set(cur, p + 1);
      out.add(col[p]);
    }
  }
  return out.bytes();
}
function prismDelim(payload, aux) {
  const parts = prismUnpack(payload);
  if (parts.length < 3) fail("a delimited shred is missing streams");
  const [widths, extra, eol] = parts;
  const streams = parts.slice(3);
  const delim = CR.latin1(aux.d);
  const N = int(aux.n), lines = int(aux.lines);
  const cols = [];
  for (let i = 0; i < N; i++) {
    if (i >= streams.length) fail("a delimited shred is missing a column");
    cols.push(readColumn(int(aux.modes[i]), streams[i], int(aux.counts[i])));
  }
  let pos = 0;
  const rx = new CR.Reader(extra);
  const out = new CR.Buf(payload.length * 2);
  for (let i = 0; i < lines; i++) {
    if (i) out.push(10);
    if (i >= widths.length || i >= eol.length) fail("a delimited shred has too few lines");
    if (widths[i] === 0) {
      for (let c = 0; c < N; c++) {
        if (c) out.add(delim);
        if (pos >= cols[c].length) fail("a delimited column ran out of values");
        out.add(cols[c][pos]);
      }
      pos++;
    } else out.add(rx.take(rx.uv()));
    if (eol[i]) out.push(13);
  }
  if (aux.tnl) out.push(10);
  return out.bytes();
}
function prismXml(payload) {
  const parts = prismUnpack(payload);
  if (parts.length !== 3) fail("a markup shred has the wrong number of streams");
  const [skel, tags, text] = parts;
  const rt = new CR.Reader(tags), rx = new CR.Reader(text);
  const out = new CR.Buf(payload.length);
  for (const c of skel) out.add(c === 0 ? rt.take(rt.uv()) : rx.take(rx.uv()));
  return out.bytes();
}
function prismInverse(payload, aux) {
  if (aux.k === "json") return prismJson(payload, aux);
  if (aux.k === "delim") return prismDelim(payload, aux);
  if (aux.k === "xml") return prismXml(payload, aux);
  fail("unknown Prism kind " + aux.k);
}

/* ---------------------------------------------------------------------
 *  ByteTranspose
 * ------------------------------------------------------------------- */
function transposeInverse(payload, aux) {
  const w = int(aux.w), delta = !!aux.d, origLen = int(aux.n), zig = !!aux.z;
  if (![1, 2, 4, 8].includes(w)) fail("bad transpose width");
  const n = Math.floor(origLen / w) * w, rows = n / w;
  if (payload.length < n) fail("a transposed payload is short");
  const head = payload.subarray(0, n), tail = payload.subarray(n);
  const t = new Uint8Array(n);
  for (let p = 0; p < w; p++) { const src = p * rows; for (let r = 0; r < rows; r++) t[r * w + p] = head[src + r]; }
  if (!delta) return CR.concat([t, tail]);
  const v = UVec.fromLE(t, w, rows);
  if (zig) unzigzagVec(v);
  addAccumulate(v, 0, rows, 1);
  return CR.concat([v.toLE(false), tail]);
}

/* ---------------------------------------------------------------------
 *  AudioPredict - fixed polynomial prediction on 16-bit PCM
 * ------------------------------------------------------------------- */
function audioInverse(payload, aux) {
  const doff = int(aux.d), ch = int(aux.ch), ns = int(aux.ns);
  const orders = CR.fromHex(aux.o);
  if (ch !== 1 && ch !== 2) fail("bad channel count");
  const total = CR.checkLen(ns * ch, "an audio stream");
  const planes = payload.subarray(doff, doff + total * 4);
  if (planes.length !== total * 4) fail("an audio payload is short");
  const B = 4096;
  const r = new Float64Array(total);
  for (let i = 0; i < total; i++) {
    const z = planes[i] + planes[total + i] * 256 + planes[2 * total + i] * 65536 + planes[3 * total + i] * 16777216;
    r[i] = (z % 2 === 0) ? z / 2 : -(z + 1) / 2;                  // (z >> 1) ^ -(z & 1)
  }
  const chans = [];
  let oi = 0, off = 0;
  for (let c = 0; c < ch; c++) {
    const cr = r.subarray(off, off + ns); off += ns;
    const out = new Float64Array(ns);
    for (let s = 0; s < ns; s += B) {
      const e = Math.min(s + B, ns);
      if (oi >= orders.length) fail("an audio block has no predictor order");
      const order = orders[oi++];
      for (let i = s; i < e; i++) out[i] = cr[i];
      for (let k = 0; k < order; k++) for (let i = s + 1; i < e; i++) out[i] += out[i - 1];
    }
    chans.push(out);
  }
  const raw = new Uint8Array(total * 2);
  const put = (i, v) => { const x = ((v % 65536) + 65536) % 65536; raw[2 * i] = x & 255; raw[2 * i + 1] = x >> 8; };
  if (ch === 2) for (let i = 0; i < ns; i++) { put(2 * i, chans[0][i]); put(2 * i + 1, chans[1][i] + chans[0][i]); }
  else for (let i = 0; i < ns; i++) put(i, chans[0][i]);
  return CR.concat([payload.subarray(0, doff), raw, payload.subarray(doff + total * 4)]);
}

/* ---------------------------------------------------------------------
 *  Loom - inferred record layouts
 * ------------------------------------------------------------------- */
function loomFieldInverse(flat, r, w, mode, endian) {
  const out = new Uint8Array(r * w);           // (r, w) row-major
  if (r === 0) return out;
  if (flat.length !== r * w) fail("a Loom field has the wrong size");
  if (mode === 0) { for (let i = 0; i < w; i++) for (let k = 0; k < r; k++) out[k * w + i] = flat[i * r + k]; return out; }
  if (mode === 2) {
    for (let i = 0; i < w; i++) { let acc = 0; for (let k = 0; k < r; k++) { acc = k ? acc ^ flat[i * r + k] : flat[i * r + k]; out[k * w + i] = acc; } }
    return out;
  }
  if (mode !== 1) fail("unknown Loom field mode");
  if (w > 8) fail("a Loom field is too wide");
  const bits = 8 * w;
  const mask = bits < 64 ? (1n << BigInt(bits)) - 1n : MASK64;
  if (w <= 4) {
    const m = 2 ** bits - 1;                     // plain Numbers: no int32 sign traps
    let acc = 0;
    for (let k = 0; k < r; k++) {
      let z = 0;
      for (let i = 0; i < w; i++) z += flat[i * r + k] * 2 ** (8 * i);
      // _unzigzag at 64 bits then & mask: (z >> 1) ^ (0 - (z & 1)) mod 2^bits
      const half = Math.floor(z / 2);
      const d = (z % 2) ? m - half : half;                          // ~half within the mask
      acc = (acc + d) % (m + 1);
      for (let i = 0; i < w; i++) {
        const sh = 8 * (endian === 0 ? i : (w - 1 - i));
        out[k * w + i] = Math.floor(acc / 2 ** sh) & 255;
      }
    }
    return out;
  }
  let acc = 0n;
  for (let k = 0; k < r; k++) {
    let z = 0n;
    for (let i = 0; i < w; i++) z |= BigInt(flat[i * r + k]) << BigInt(8 * i);
    const d = ((z >> 1n) ^ asU64(0n - (z & 1n))) & mask;
    acc = asU64(acc + d) & mask;
    for (let i = 0; i < w; i++) {
      const sh = BigInt(8 * (endian === 0 ? i : (w - 1 - i)));
      out[k * w + i] = Number((acc >> sh) & 255n);
    }
  }
  return out;
}
function loomInverse(payload, aux) {
  if (aux.kind === "plain") return payload;
  if (aux.kind === "stride") {
    const s = int(aux.S), r = int(aux.R), sizes = aux.sizes.map((x) => int(x));
    const total = sizes.reduce((a, b) => a + b, 0);
    const hlen = int(aux.hlen), tlen = int(aux.tlen);
    const flat = payload.subarray(0, total);
    const head = payload.subarray(total, total + hlen), tail = payload.subarray(total + hlen, total + hlen + tlen);
    const grid = new Uint8Array(CR.checkLen(r * s, "a Loom grid"));
    let off = 0, c = 0;
    const fields = aux.fields;
    for (let f = 0; f < Math.min(fields.length, sizes.length); f++) {
      const [w, mode, endian] = fields[f].map((x) => int(x));
      if (c + w > s) fail("a Loom field runs past its record");
      const blk = loomFieldInverse(flat.subarray(off, off + sizes[f]), r, w, mode, endian);
      for (let k = 0; k < r; k++) grid.set(blk.subarray(k * w, k * w + w), k * s + c);
      off += sizes[f]; c += w;
    }
    return CR.concat([head, grid, tail]);
  }
  if (aux.kind === "vlen") {
    const nrec = int(aux.nrec), lb = int(aux.lenblob), align = int(aux.align || 0), cb = int(aux.colblob);
    const lp = payload.subarray(0, lb);
    if (lp.length !== 2 * nrec || nrec === 0) fail("a Loom length table is malformed");
    const lens = new Float64Array(nrec), starts = new Float64Array(nrec);
    let total = 0;
    for (let i = 0; i < nrec; i++) { lens[i] = lp[i] | (lp[nrec + i] << 8); starts[i] = total; total += lens[i]; }
    const out = new Uint8Array(total);
    const delim = int(aux.delim) & 255;
    for (let i = 0; i < nrec; i++) { const e = starts[i] + lens[i] - 1; if (e < 0) fail("a Loom record has length zero"); out[e] = delim; }
    const col = payload.subarray(lb, lb + cb);
    let off = 0;
    const maxlen = int(aux.maxlen);
    for (let j = 0; j < maxlen - 1; j++) {
      for (let i = 0; i < nrec; i++) {
        const body = lens[i] - 1;
        if (body > j) {
          if (off >= col.length) fail("a Loom column table is short");
          const idx = align === 0 ? starts[i] + j : starts[i] + body - 1 - j;
          out[idx] = col[off++];
        }
      }
    }
    const tail = payload.subarray(lb + cb, lb + cb + int(aux.tlen));
    return CR.concat([out, tail]);
  }
  fail("unknown Loom layout " + aux.kind);
}

/* ---------------------------------------------------------------------
 *  Helix (crush_genome) - FASTQ / FASTA / VCF-SAM-CSV
 * ------------------------------------------------------------------- */
const G = {};
G.parse = function (b) {
  if (!(b[0] === 0x47 && b[1] === 0x4E && b[2] === 0x30 && b[3] === 0x31)) fail("not a genome payload");
  const kind = b[4];
  const r = new CR.Reader(b, 5);
  const hl = r.uv(); const hdr = r.take(hl);
  const ng = r.uv(), sizes = [];
  for (let i = 0; i < ng; i++) sizes.push(r.uv());
  const [flat] = V.unpackStreams(b, r.i);
  const groups = []; let i = 0;
  for (const s of sizes) { groups.push(flat.slice(i, i + s)); i += s; }
  return [kind, hdr, groups];
};
/* _ints_decode: an int64 array; values come back as Number when exact, else BigInt */
G.ints = function (blob, n) {
  const r = new CR.Reader(blob, 0);
  const mo = r.byte();
  const nc = r.uv(), cnst = [];
  for (let i = 0; i < nc; i++) { const f = r.byte(); cnst.push(f ? r.byte() : -1); }
  const [streams] = V.unpackStreams(blob, r.i);
  const u = V.decBody(streams, cnst, mo, 8, n, new Uint8Array(0));
  const out = new Array(n);
  for (let i = 0; i < n; i++) {
    const hi = u.hi[i] | 0;
    if (hi >= -2097152 && hi < 2097152) out[i] = hi * 4294967296 + u.lo[i];
    else out[i] = asI64((BigInt(u.hi[i]) << 32n) | BigInt(u.lo[i]));
  }
  return out;
};
const ACGT = [65, 67, 71, 84];
G.seqDecode = function (mode, streams, nkept, total) {
  if (mode === 0) return streams[0];
  const [packed, dbad, badchars] = streams;
  const kept = new Uint8Array(Math.min(nkept, packed.length * 4));
  for (let i = 0; i < kept.length; i++) kept[i] = ACGT[(packed[i >> 2] >> (2 * (i & 3))) & 3];
  if (nkept > packed.length * 4) fail("a packed sequence stream is short");
  const out = new Uint8Array(CR.checkLen(total, "a sequence"));
  const bd = readU32LE(dbad);
  if (bd.length) {
    const isBad = new Uint8Array(total);
    let acc = 0;
    if (badchars.length !== bd.length) fail("a sequence exception list is malformed");
    for (let k = 0; k < bd.length; k++) { acc += bd[k]; if (acc >= total) fail("a sequence exception is out of range"); out[acc] = badchars[k]; isBad[acc] = 1; }
    let q = 0;
    for (let i = 0; i < total; i++) if (!isBad[i]) { if (q >= kept.length) fail("a sequence stream is short"); out[i] = kept[q++]; }
    if (q !== kept.length) fail("a sequence stream has the wrong length");
  } else {
    if (kept.length !== total) fail("a sequence stream has the wrong length");
    out.set(kept);
  }
  return out;
};
G.qualDecode = function (mode, streams, rlen, nreads) {
  if (mode === 0) return streams[0];
  if (mode === 1) {
    const dz = streams[0], out = new Uint8Array(dz.length);
    let acc = 0;
    for (let i = 0; i < dz.length; i++) {
      const d = ((dz[i] >> 1) ^ (-(dz[i] & 1) & 255)) & 255;
      acc = i ? (acc + d) & 255 : d; out[i] = acc;
    }
    return out;
  }
  const t = streams[0];
  if (t.length !== rlen * nreads || (rlen === 0 && t.length)) fail("a quality matrix has the wrong shape");
  const out = new Uint8Array(t.length);
  for (let c = 0; c < rlen; c++) for (let r = 0; r < nreads; r++) out[r * rlen + c] = t[c * nreads + r];
  return out;
};
function scatter(out, dest, lens, src) {
  let tot = 0; for (let i = 0; i < lens.length; i++) tot += lens[i];
  if (tot === 0) return;
  if (src.length !== tot) fail("a scattered stream has the wrong length");
  let o = 0;
  for (let i = 0; i < lens.length; i++) {
    const L = lens[i];
    if (L < 0 || dest[i] + L > out.length || dest[i] < 0) fail("a scattered field is out of range");
    out.set(src.subarray(o, o + L), dest[i]); o += L;
  }
}
const floorMod10 = (v) => (typeof v === "bigint" ? Number(fmod(v, 10n)) : ((v % 10) + 10) % 10);
const floorDiv10 = (v) => (typeof v === "bigint" ? fdiv(v, 10n) : Math.floor(v / 10));
G.idRebuild = function (streams, meta, nlines) {
  const { nf, L, skel, wconst, gaps } = meta;
  let si = 0;
  const fields = [];
  for (let j = 0; j < nf; j++) {
    let w;
    if (wconst[j] < 0) { const s = streams[si++]; if (!s) fail("an identifier stream is missing"); w = Float64Array.from(s); }
    else w = new Float64Array(nlines).fill(wconst[j]);
    if (!streams[si]) fail("an identifier stream is missing");
    const vals = G.ints(streams[si++], nlines);
    if (w.length !== nlines) fail("an identifier width list has the wrong length");
    fields.push([w, vals]);
  }
  const lens = new Float64Array(nlines).fill(L);
  for (const [w] of fields) for (let i = 0; i < nlines; i++) lens[i] += w[i];
  let total = 0; const lstart = new Float64Array(nlines);
  for (let i = 0; i < nlines; i++) { lstart[i] = total; total += lens[i]; }
  const out = new Uint8Array(CR.checkLen(total, "identifiers"));
  const cur = lstart.slice();
  let gpos = 0;
  const putGap = (g) => {
    if (gpos + g > skel.length) fail("an identifier skeleton is short");
    for (let i = 0; i < nlines; i++) { out.set(skel.subarray(gpos, gpos + g), cur[i]); cur[i] += g; }
    gpos += g;
  };
  for (let j = 0; j < nf; j++) {
    if (gaps[j]) putGap(gaps[j]);
    const [w, vals] = fields[j];
    let wmax = 0; for (let i = 0; i < nlines; i++) wmax = Math.max(wmax, w[i]);
    for (let i = 0; i < nlines; i++) {
      let v = vals[i];
      const digits = new Uint8Array(wmax);
      for (let k = wmax - 1; k >= 0; k--) { digits[k] = floorMod10(v) + 48; v = floorDiv10(v); }
      out.set(digits.subarray(wmax - w[i]), cur[i]); cur[i] += w[i];
    }
  }
  if (gaps[nf]) putGap(gaps[nf]);
  return [out, lens];
};
G.fastq = function (payload) {
  const [kind, b, groups] = G.parse(payload);
  if (kind !== 1) fail("not a FASTQ payload");
  const r = new CR.Reader(b);
  r.uv();
  const nrec = r.uv(), trailNl = r.byte(), constLen = r.uv() - 1, plusEmpty = r.byte(),
        seqmode = r.byte(), nkept = r.uv(), qmode = r.byte(), templated = r.byte();
  let tmeta = null;
  if (templated) {
    const ml = r.uv(); const m = r.take(ml); const q = new CR.Reader(m);
    const nf = q.byte(), L = q.uv(), skel = q.take(L), wconst = [], gaps = [];
    for (let i = 0; i < nf; i++) { const f = q.byte(); wconst.push(f ? q.byte() : -1); }
    for (let i = 0; i < nf + 1; i++) gaps.push(q.uv());
    tmeta = { nf, L, skel, wconst, gaps };
  }
  if (groups.length !== 4) fail("a FASTQ payload has the wrong number of stream groups");
  const [aux, idpart, sstreams, qstreams] = groups;
  let ai = 0, seql, plusblob, plusl;
  if (constLen < 0) seql = readU32LE(aux[ai++]); else seql = new Float64Array(nrec).fill(constLen);
  if (plusEmpty) { plusblob = new Uint8Array(0); plusl = new Float64Array(nrec); }
  else { plusblob = aux[ai++]; plusl = readU32LE(aux[ai++]).map((x) => x - 1); }
  let idblob, idls;
  if (templated) [idblob, idls] = G.idRebuild(idpart, tmeta, nrec);
  else { idblob = idpart[0]; idls = readU32LE(idpart[1]); }
  if (seql.length !== nrec || idls.length !== nrec || plusl.length !== nrec) fail("a FASTQ payload has inconsistent record counts");
  let seqtotal = 0; for (const x of seql) seqtotal += x;
  const seqchars = G.seqDecode(seqmode, sstreams, nkept, seqtotal);
  const qualchars = G.qualDecode(qmode, qstreams, constLen > 0 ? constLen : 0, nrec);
  const rstart = new Float64Array(nrec); let tot = 0;
  for (let i = 0; i < nrec; i++) { rstart[i] = tot; tot += 1 + idls[i] + 1 + seql[i] + 1 + 1 + plusl[i] + 1 + seql[i] + 1; }
  const out = new Uint8Array(CR.checkLen(tot, "a FASTQ file")).fill(10);
  const pid = new Float64Array(nrec), pseq = new Float64Array(nrec), pplus = new Float64Array(nrec), pq = new Float64Array(nrec);
  for (let i = 0; i < nrec; i++) {
    out[rstart[i]] = 64; pid[i] = rstart[i] + 1; pseq[i] = rstart[i] + 2 + idls[i];
    const pp = pseq[i] + seql[i] + 1; out[pp] = 43; pplus[i] = pp + 1; pq[i] = pp + 1 + plusl[i] + 1;
  }
  scatter(out, pid, idls, idblob);
  scatter(out, pseq, seql, seqchars);
  if (plusblob.length) scatter(out, pplus, plusl, plusblob);
  scatter(out, pq, seql, qualchars);
  return trailNl ? out : out.subarray(0, out.length - 1);
};
G.fasta = function (payload) {
  const [kind, b, groups] = G.parse(payload);
  if (kind !== 2) fail("not a FASTA payload");
  const r = new CR.Reader(b);
  r.uv();
  const nlines = r.uv(), trailNl = r.byte(), seqmode = r.byte(), nkept = r.uv(), firstLower = r.byte();
  if (groups.length !== 2 || groups[0].length !== 4) fail("a FASTA payload is malformed");
  const [layout, sstreams] = groups;
  const hb = layout[0];
  const ishdr = new Uint8Array(Math.min(nlines, hb.length * 8));
  for (let i = 0; i < ishdr.length; i++) ishdr[i] = (hb[i >> 3] >> (i & 7)) & 1;
  const L = readU32LE(layout[1]), hdrblob = layout[2], runs = readU64LEasNum(layout[3]);
  if (L.length !== ishdr.length) fail("a FASTA layout is inconsistent");
  let seqtotal = 0; for (let i = 0; i < L.length; i++) if (!ishdr[i]) seqtotal += L[i];
  const up = G.seqDecode(seqmode, sstreams, nkept, seqtotal);
  if (up.length !== seqtotal) fail("a FASTA sequence has the wrong length");
  const seqblob = new Uint8Array(up);
  let bnd = 0;
  for (let i = 0; i < runs.length; i++) {
    const e = bnd + runs[i];
    if (i % 2 === (firstLower ? 0 : 1)) for (let k = bnd; k < Math.min(e, seqtotal); k++) seqblob[k] = (seqblob[k] + 32) & 255;
    bnd = e;
  }
  let tot = 0; const lstart = new Float64Array(L.length);
  for (let i = 0; i < L.length; i++) { lstart[i] = tot; tot += L[i] + 1; }
  const out = new Uint8Array(CR.checkLen(tot, "a FASTA file")).fill(10);
  const hd = [], hl = [], sd = [], sl = [];
  for (let i = 0; i < L.length; i++) { if (ishdr[i]) { hd.push(lstart[i]); hl.push(L[i]); } else { sd.push(lstart[i]); sl.push(L[i]); } }
  scatter(out, hd, hl, hdrblob);
  scatter(out, sd, sl, seqblob);
  return trailNl ? out : out.subarray(0, out.length - 1);
};
function renderFixed(vals, k) {                 // f"{ip}.{fp:0{k}d}" per value
  const p = 10n ** BigInt(k);
  return vals.map((x) => {
    const xb = BigInt(x), neg = xb < 0n, ax = neg ? -xb : xb;
    return CR.ascii((neg ? "-" : "") + (ax / p).toString() + "." + (ax % p).toString().padStart(k, "0"));
  });
}
function splitNL(u8) { const out = []; let s = 0; for (let i = 0; i < u8.length; i++) if (u8[i] === 10) { out.push(u8.subarray(s, i)); s = i + 1; } out.push(u8.subarray(s)); return out; }
G.colDecode = function (mode, streams, n) {
  if (mode === 0) return n ? splitNL(streams[0]) : [];
  if (mode === 1) return G.ints(streams[0], n).map((x) => CR.decBytes(x));
  if (streams.length < 2 || !streams[1].length) fail("a fixed-point column is missing its scale");
  return renderFixed(G.ints(streams[0], n), streams[1][0]);
};
G.tabular = function (payload) {
  const [kind, b, groups] = G.parse(payload);
  if (kind !== 3) fail("not a tabular payload");
  const r = new CR.Reader(b);
  r.uv();
  const delim = r.byte(), trailNl = r.byte(), nrow = r.uv(), maxf = r.uv(), constNf = r.byte();
  const modes = Array.from(r.take(maxf));
  if (modes.length !== maxf) fail("a tabular header is short");
  const aux = groups[0];
  const preamble = aux[0];
  const nf = constNf ? new Float64Array(nrow).fill(maxf) : readU32LE(aux[1]);
  const cols = [];
  for (let j = 0; j < maxf; j++) {
    let cnt = 0; for (let i = 0; i < nf.length; i++) if (nf[i] > j) cnt++;
    if (!groups[1 + j]) fail("a tabular column is missing");
    cols.push(G.colDecode(modes[j], groups[1 + j], cnt));
  }
  const out = new CR.Buf(payload.length * 2);
  let first = true;
  const line = () => { if (!first) out.push(10); first = false; };
  if (preamble.length) { line(); out.add(preamble); }
  if (constNf) {
    let rows = Infinity; for (const c of cols) rows = Math.min(rows, c.length);   // zip(*cols)
    if (!cols.length) rows = 0;
    for (let i = 0; i < rows; i++) { line(); for (let j = 0; j < maxf; j++) { if (j) out.push(delim); out.add(cols[j][i]); } }
  } else {
    const ptr = new Array(maxf).fill(0);
    for (let i = 0; i < nrow; i++) {
      if (i >= nf.length) fail("a tabular row count is inconsistent");
      line();
      for (let j = 0; j < nf[i]; j++) {
        if (j >= maxf || ptr[j] >= cols[j].length) fail("a tabular column ran out of values");
        if (j) out.push(delim);
        out.add(cols[j][ptr[j]++]);
      }
    }
  }
  if (trailNl) out.push(10);
  return out.bytes();
};
function genomeInverse(payload, aux) {
  const crlf = payload[0];
  let rest = payload.subarray(1), tail = new Uint8Array(0), pay = rest;
  if (((aux && aux.v !== undefined) ? int(aux.v) : 2) >= 2) {
    const r = new CR.Reader(rest, 0);
    const tlen = r.uv();
    tail = rest.subarray(r.i, r.i + tlen);
    pay = rest.subarray(r.i + tlen);
  }
  const k = pay[4];
  let out = k === 1 ? G.fastq(pay) : k === 2 ? G.fasta(pay) : G.tabular(pay);
  out = CR.concat([out, tail]);
  if (!crlf) return out;
  let nl = 0; for (const c of out) if (c === 10) nl++;
  const res = new Uint8Array(out.length + nl);
  for (let i = 0, j = 0; i < out.length; i++) { if (out[i] === 10) res[j++] = 13; res[j++] = out[i]; }
  return res;
}

/* ---------------------------------------------------------------------
 *  Container unwrappers: zip members, PDF streams, gzip bodies, PNG IDAT.
 *  The splicing is ported here; re-encoding the original deflate bytes is
 *  done by WASM builds of the same native helpers the desktop app uses.
 * ------------------------------------------------------------------- */
function needNative(fn, what) {
  const n = CR.native || {};
  if (typeof n[fn] !== "function")
    throw new Unavailable("rebuilding " + what + " needs a decoder this page does not carry");
  return n[fn];
}
async function spliceInverse(payload, aux) {
  const rows = aux.r.map((row) => row.map((x) => int(x)));
  let dtotal = 0; for (const r of rows) if (r.length === 6) dtotal += r[5];
  const body = dtotal ? payload.subarray(0, payload.length - dtotal) : payload;
  const tail = dtotal ? payload.subarray(payload.length - dtotal) : new Uint8Array(0);
  const spans = [], pfPairs = [];
  let src = 0, prevEnd = 0, dpos = 0;
  for (const r of rows) {
    const [off, clen, rlen] = r;
    src += off - prevEnd;
    if (r.length === 6) { pfPairs.push([tail.subarray(dpos, dpos + r[5]), body.subarray(src, src + rlen)]); dpos += r[5]; }
    spans.push([off, clen, src, rlen]);
    src += rlen; prevEnd = off + clen;
  }
  const rebuilt = pfPairs.length ? await needNative("pfReencode", "deflate streams that zlib did not write (preflate)")(pfPairs) : [];
  const out = new CR.Buf(payload.length + 1024);
  src = 0; let pi = 0;
  for (let k = 0; k < rows.length; k++) {
    const r = rows[k], [off, clen, s, rlen] = spans[k];
    out.add(body.subarray(src, s));
    let comp;
    if (r.length === 6) comp = rebuilt[pi++];
    else {
      const wbits = r.length >= 5 ? r[4] : -15;
      comp = await needNative("deflate", "compressed streams with Crush's pinned zlib")(r[3], body.subarray(s, s + rlen), wbits);
    }
    if (!comp || comp.length !== clen) fail("rebuilt stream has the wrong length");
    out.add(comp);
    src = s + rlen;
  }
  out.add(body.subarray(src));
  return out.bytes();
}
async function pngInverse(payload, aux) {
  const sk = CR.fromHex(aux.sk), fi = int(aux.fi), pi = int(aux.pi);
  let comp;
  if (pi === -1) {
    const dl = int(aux.dl);
    const raw = payload.subarray(0, payload.length - dl), corr = payload.subarray(payload.length - dl);
    const zw = CR.fromHex(aux.zw);
    const inner = (await needNative("pfReencode", "a PNG's deflate stream (preflate)")([[corr, raw]]))[0];
    comp = CR.concat([zw.subarray(0, 2), inner, zw.subarray(2)]);
  } else {
    comp = await needNative("deflate", "a PNG's deflate stream")(pi, payload, aux.wb !== undefined ? int(aux.wb) : 15);
  }
  const out = new CR.Buf(comp.length + sk.length + 64);
  out.add(sk.subarray(0, fi));
  let o = 0;
  const sz = aux.sz.map((x) => int(x));
  for (let i = 0; i < sz.length; i++) {
    const ln = sz[i];
    out.add(new Uint8Array([ln >>> 24, (ln >>> 16) & 255, (ln >>> 8) & 255, ln & 255, 73, 68, 65, 84]));
    out.add(comp.subarray(o, o + ln));
    out.add(CR.fromHex(aux.crc[i]));
    o += ln;
  }
  out.add(sk.subarray(fi));
  return out.bytes();
}
async function jpegInverse(payload) {
  const r = await needNative("jpegUnpack", "Crush-recompressed JPEGs (crushjpg)")([payload]);
  if (!r || !r[0]) throw new Unavailable("a recompressed JPEG could not be rebuilt");
  return r[0];
}
async function jpegEmbeddedInverse(payload, aux) {
  const rows = aux.r.map((row) => row.map((x) => int(x)));
  let ptotal = 0; for (const r of rows) ptotal += r[2];
  const body = payload.subarray(0, payload.length - ptotal), tail = payload.subarray(payload.length - ptotal);
  const blobs = []; let q = 0;
  for (const r of rows) { blobs.push(tail.subarray(q, q + r[2])); q += r[2]; }
  const jpegs = blobs.length ? await needNative("jpegUnpack", "Crush-recompressed JPEGs (crushjpg)")(blobs) : [];
  const out = new CR.Buf(payload.length * 2);
  let src = 0, prev = 0;
  for (let k = 0; k < rows.length; k++) {
    const [off, jlen] = rows[k], jp = jpegs[k];
    if (!jp || jp.length !== jlen) fail("rebuilt embedded JPEG has the wrong length");
    const gap = off - prev;
    out.add(body.subarray(src, src + gap)); src += gap;
    out.add(jp); prev = off + jlen;
  }
  out.add(body.subarray(src));
  return out.bytes();
}

/* ---------------------------------------------------------------------
 *  Registry
 * ------------------------------------------------------------------- */
const NOT_PORTABLE = {
  jpegxl: "JPEG XL recompression (needs djxl, which a web page does not carry)",
  flac: "FLAC audio (needs the flac program)",
  zip_recompose: "re-packed zip containers ('harder' mode, rebuilt with Python's zipfile)",
  perc_image: "the lossy photo tier",
  perc_audio: "the lossy audio tier",
};
CR.NOT_PORTABLE = NOT_PORTABLE;
const INVERSES = {
  oracle: oracleInverse, prism: prismInverse, transpose: transposeInverse,
  audio_predict: audioInverse, floatarray: (p) => floatarrayInverse(p),
  genome: genomeInverse, loom: loomInverse,
  deflate_unwrap: spliceInverse, pdf_unwrap: spliceInverse, gzip_unwrap: spliceInverse,
  png_unwrap: pngInverse, jpeg_pack: jpegInverse, jpeg_embedded: jpegEmbeddedInverse,
};
CR.TRANSFORMS = INVERSES;
/* Which transforms need a native helper, and which one. */
CR.TRANSFORM_NEEDS = {
  deflate_unwrap: ["deflate", "pfReencode"], pdf_unwrap: ["deflate", "pfReencode"],
  gzip_unwrap: ["deflate", "pfReencode"], png_unwrap: ["deflate", "pfReencode"],
  jpeg_pack: ["jpegUnpack"], jpeg_embedded: ["jpegUnpack"],
};

CR.invertTransforms = async function (payload, chain) {
  for (let i = chain.length - 1; i >= 0; i--) {
    const step = chain[i];
    const f = INVERSES[step.n];
    if (!f) {
      if (NOT_PORTABLE[step.n]) throw new Unavailable("this file was stored with " + NOT_PORTABLE[step.n] + ", which only the Crush app can rebuild");
      throw new Unavailable("this file uses a transform (" + step.n + ") this page does not know");
    }
    payload = await f(payload, step.a);
  }
  return payload;
};
CR._internal = { V, G, oracleGenerate, readColumn, floatarrayInverse, loomFieldInverse };
})(globalThis.CR = globalThis.CR || {});
