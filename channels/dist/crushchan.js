/* crushchan core.js - packet format, GF(2) fountain, Reed-Solomon GF(256),
 * SHA-256, CRC-32, Base45, base64url.  Integer-only; mirrors py/crushchan/core.py
 * exactly (cross-checked by tests/test_core_cross.py).
 * Exposes globalThis.CrushChannels.core (and module.exports under node).
 * No network, no dependencies. */
(function (root) {
  'use strict';
  var MAGIC = 0xC5, KIND_FOUNTAIN = 0x11, KIND_RS = 0x12, HDR = 18, TRL = 4;
  var KMAX_FOUNTAIN = 1024, MAX_L = 1 << 28, MIN_T = 8, MAX_T = 8192, MAX_SYMBOLS = 1 << 20;

  /* ------------------------------------------------------------ SHA-256 */
  var K256 = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);
  function sha256(msg) {
    var len = msg.length, nb = ((len + 9 + 63) >> 6) << 6;
    var m = new Uint8Array(nb); m.set(msg); m[len] = 0x80;
    var bits = len * 8; // < 2^53
    var hi = Math.floor(bits / 4294967296), lo = bits >>> 0;
    m[nb - 8] = hi >>> 24; m[nb - 7] = hi >>> 16; m[nb - 6] = hi >>> 8; m[nb - 5] = hi;
    m[nb - 4] = lo >>> 24; m[nb - 3] = lo >>> 16; m[nb - 2] = lo >>> 8; m[nb - 1] = lo;
    var h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    var w = new Uint32Array(64);
    for (var off = 0; off < nb; off += 64) {
      for (var i = 0; i < 16; i++) { var o = off + i * 4; w[i] = (m[o] << 24) | (m[o + 1] << 16) | (m[o + 2] << 8) | m[o + 3]; }
      for (i = 16; i < 64; i++) {
        var a0 = w[i - 15], a1 = w[i - 2];
        var s0 = ((a0 >>> 7) | (a0 << 25)) ^ ((a0 >>> 18) | (a0 << 14)) ^ (a0 >>> 3);
        var s1 = ((a1 >>> 17) | (a1 << 15)) ^ ((a1 >>> 19) | (a1 << 13)) ^ (a1 >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      var a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
      for (i = 0; i < 64; i++) {
        var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        var ch = (e & f) ^ (~e & g);
        var t1 = (hh + S1 + ch + K256[i] + w[i]) | 0;
        var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        var mj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + mj) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }
    var out = new Uint8Array(32);
    for (i = 0; i < 8; i++) { out[i * 4] = h[i] >>> 24; out[i * 4 + 1] = h[i] >>> 16; out[i * 4 + 2] = h[i] >>> 8; out[i * 4 + 3] = h[i]; }
    return out;
  }
  function hex(b) { var s = ''; for (var i = 0; i < b.length; i++) s += (b[i] < 16 ? '0' : '') + b[i].toString(16); return s; }

  /* ------------------------------------------------------------ CRC-32 */
  var CRC_T = new Uint32Array(256);
  for (var n = 0; n < 256; n++) { var c = n; for (var k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; CRC_T[n] = c >>> 0; }
  function crc32(b, start, end) {
    var c = 0xFFFFFFFF; start = start || 0; end = end === undefined ? b.length : end;
    for (var i = start; i < end; i++) c = CRC_T[(c ^ b[i]) & 255] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  /* ------------------------------------------------------------ PRNG */
  function mix32(x) {
    x = x >>> 0;
    x ^= x >>> 16; x = Math.imul(x, 0x7FEB352D) >>> 0;
    x ^= x >>> 15; x = Math.imul(x, 0x846CA68B) >>> 0;
    x ^= x >>> 16; return x >>> 0;
  }
  function rowSeed(session, block, esi) {
    var s = mix32((session ^ mix32((Math.imul(block, 0x85EBCA6B) + 0x632BE5AB) >>> 0)) >>> 0);
    s = mix32((s + (Math.imul(esi >>> 0, 0x9E3779B9) >>> 0)) >>> 0);
    return s || 0x6D2B79F5;
  }
  /* coefficient bitset for one ESI, as Uint32Array of ceil(kb/32) words (bit j = symbol j) */
  function fountainRow(session, block, esi, kb) {
    var nw = (kb + 31) >>> 5, v = new Uint32Array(nw);
    if (esi < kb) { v[esi >>> 5] = (1 << (esi & 31)) >>> 0; return v; }
    var seed = rowSeed(session, block, esi), any = 0;
    for (var i = 0; i < nw; i++) { v[i] = mix32((seed + Math.imul(i, 0x9E3779B9)) >>> 0); }
    if (kb & 31) v[nw - 1] &= ((1 << (kb & 31)) - 1) >>> 0;
    for (i = 0; i < nw; i++) any |= v[i];
    if (!any) { var j = esi % kb; v[j >>> 5] = (1 << (j & 31)) >>> 0; }
    return v;
  }

  /* ------------------------------------------------------------ layout & packets */
  function layout(L, T, kind, Krs) {
    var ktot = Math.floor((L + 32 + T - 1) / T), B, kb;
    if (kind === KIND_RS) { kb = Krs; B = Math.floor((ktot + kb - 1) / kb); }
    else { B = Math.floor((ktot + KMAX_FOUNTAIN - 1) / KMAX_FOUNTAIN); kb = Math.floor((ktot + B - 1) / B); }
    return { ktot: ktot, B: B, kb: kb };
  }
  function rd32(b, o) { return ((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0; }
  function rd16(b, o) { return (b[o] << 8) | b[o + 1]; }
  function wr32(b, o, v) { b[o] = v >>> 24; b[o + 1] = v >>> 16; b[o + 2] = v >>> 8; b[o + 3] = v; }
  function wr16(b, o, v) { b[o] = v >>> 8; b[o + 1] = v; }

  function parse(p) {
    if (!p || p.length < HDR + TRL + MIN_T || p[0] !== MAGIC) return null;
    var kind = p[1];
    if (kind !== KIND_FOUNTAIN && kind !== KIND_RS) return null;
    var session = rd32(p, 2), L = rd32(p, 6), T = rd16(p, 10);
    if (T < MIN_T || T > MAX_T || L > MAX_L || p.length !== HDR + TRL + T || Math.floor((L + 32 + T - 1) / T) > MAX_SYMBOLS) return null;
    if (rd32(p, p.length - 4) !== crc32(p, 0, p.length - 4)) return null;
    var d = { kind: kind, session: session, L: L, T: T, payload: p.subarray(HDR, HDR + T) }, lay;
    if (kind === KIND_FOUNTAIN) {
      d.block = rd16(p, 12); d.esi = rd32(p, 14);
      lay = layout(L, T, kind);
      if (d.block >= lay.B) return null;
    } else {
      d.K = p[12]; d.N = p[13]; d.block = rd16(p, 14); d.esi = rd16(p, 16);
      if (d.K < 1 || d.N < d.K || d.esi >= d.N) return null;
      lay = layout(L, T, kind, d.K);
      if (d.block >= lay.B) return null;
    }
    d.B = lay.B; d.kb = lay.kb; d.ktot = lay.ktot;
    return d;
  }
  function pack(kind, session, L, T, f12, f14, payload) {
    var p = new Uint8Array(HDR + T + TRL);
    p[0] = MAGIC; p[1] = kind; wr32(p, 2, session); wr32(p, 6, L); wr16(p, 10, T);
    p.set(f12, 12); p.set(f14, 14); p.set(payload, HDR);
    wr32(p, HDR + T, crc32(p, 0, HDR + T));
    return p;
  }

  /* ------------------------------------------------------------ fountain encoder */
  function FountainEncoder(data, T) {
    if (!(T >= MIN_T && T <= MAX_T)) throw new Error('bad T');
    var hash = sha256(data), L = data.length, lay = layout(L, T, KIND_FOUNTAIN);
    var src = new Uint8Array(lay.B * lay.kb * T); src.set(data); src.set(hash, L);
    this.data = data; this.T = T; this.L = L; this.B = lay.B; this.kb = lay.kb; this.ktot = lay.ktot;
    this.session = rd32(hash, 0); this.src = src; this.hash = hash;
  }
  FountainEncoder.prototype.packet = function (block, esi) {
    var T = this.T, kb = this.kb, row = fountainRow(this.session, block, esi, kb);
    var out = new Uint8Array(T), base = block * kb * T, src = this.src;
    for (var w = 0; w < row.length; w++) {
      var x = row[w];
      while (x) {
        var t = x & -x, j = (w << 5) + (31 - Math.clz32(t));
        var o = base + j * T;
        for (var i = 0; i < T; i++) out[i] ^= src[o + i];
        x = (x ^ t) >>> 0;
      }
    }
    var f12 = new Uint8Array(2), f14 = new Uint8Array(4);
    wr16(f12, 0, block); wr32(f14, 0, esi);
    return pack(KIND_FOUNTAIN, this.session, this.L, T, f12, f14, out);
  };
  /* n-th packet of the endless schedule: source symbols first, then repair, round-robin over blocks */
  FountainEncoder.prototype.nth = function (n) {
    return this.packet(n % this.B, Math.floor(n / this.B));
  };

  /* ------------------------------------------------------------ fountain decoder (online GF(2) elimination) */
  function FountainBlockDecoder(session, block, kb, real, T) {
    this.session = session; this.block = block; this.kb = kb; this.T = T;
    this.nw = (kb + 31) >>> 5; this.pw = (T + 3) >>> 2;
    this.pivC = new Array(kb); this.pivV = new Array(kb); this.rank = 0;
    for (var j = real; j < kb; j++) { var c = new Uint32Array(this.nw); c[j >>> 5] = (1 << (j & 31)) >>> 0; this._add(c, new Uint32Array(this.pw)); }
  }
  FountainBlockDecoder.prototype._add = function (c, v) {
    var nw = this.nw, pw = this.pw, w = 0;
    for (;;) {
      while (w < nw && c[w] === 0) w++;
      if (w === nw) return false;
      var b = (w << 5) + (31 - Math.clz32(c[w] & -c[w]));
      var pc = this.pivC[b];
      if (!pc) { this.pivC[b] = c; this.pivV[b] = v; this.rank++; return true; }
      var pv = this.pivV[b], i;
      for (i = w; i < nw; i++) c[i] ^= pc[i];
      for (i = 0; i < pw; i++) v[i] ^= pv[i];
    }
  };
  FountainBlockDecoder.prototype.add = function (esi, payload) {
    if (this.rank === this.kb) return false;
    var v = new Uint32Array(this.pw), v8 = new Uint8Array(v.buffer); v8.set(payload);
    return this._add(fountainRow(this.session, this.block, esi, this.kb), v);
  };
  FountainBlockDecoder.prototype.done = function () { return this.rank === this.kb; };
  FountainBlockDecoder.prototype.solve = function () {
    var kb = this.kb, nw = this.nw, pw = this.pw, sol = new Array(kb);
    for (var b = kb - 1; b >= 0; b--) {
      var c = this.pivC[b], v = this.pivV[b];
      c[b >>> 5] ^= (1 << (b & 31)) >>> 0;
      for (var w = b >>> 5; w < nw; w++) {
        var x = c[w];
        while (x) {
          var t = x & -x, j = (w << 5) + (31 - Math.clz32(t)), s = sol[j];
          for (var i = 0; i < pw; i++) v[i] ^= s[i];
          x = (x ^ t) >>> 0;
        }
      }
      sol[b] = v;
    }
    var out = new Uint8Array(kb * this.T);
    for (b = 0; b < kb; b++) out.set(new Uint8Array(sol[b].buffer, 0, this.T), b * this.T);
    return out;
  };

  /* ------------------------------------------------------------ Reed-Solomon GF(256), systematic Cauchy */
  var GF_EXP = new Uint8Array(512), GF_LOG = new Uint8Array(256);
  (function () { var x = 1; for (var i = 0; i < 255; i++) { GF_EXP[i] = x; GF_LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11D; } for (i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255]; })();
  function gmul(a, b) { return (a === 0 || b === 0) ? 0 : GF_EXP[GF_LOG[a] + GF_LOG[b]]; }
  function ginv(a) { return GF_EXP[255 - GF_LOG[a]]; }
  function rsCoef(K, i, j) { return i < K ? (i === j ? 1 : 0) : ginv(i ^ j); }
  function mulTable(c) { var t = new Uint8Array(256); for (var v = 0; v < 256; v++) t[v] = gmul(c, v); return t; }

  function rsEncode(data, T, K, N) {
    if (!(K >= 1 && K <= N && N <= 255)) throw new Error('bad K/N');
    var hash = sha256(data), L = data.length, lay = layout(L, T, KIND_RS, K), session = rd32(hash, 0);
    var src = new Uint8Array(lay.B * K * T); src.set(data); src.set(hash, L);
    var out = [];
    for (var b = 0; b < lay.B; b++) {
      for (var i = 0; i < N; i++) {
        var sh;
        if (i < K) sh = src.subarray((b * K + i) * T, (b * K + i + 1) * T);
        else {
          sh = new Uint8Array(T);
          for (var j = 0; j < K; j++) {
            var t = mulTable(rsCoef(K, i, j)), o = (b * K + j) * T;
            for (var q = 0; q < T; q++) sh[q] ^= t[src[o + q]];
          }
        }
        var f14 = new Uint8Array(4); wr16(f14, 0, b); wr16(f14, 2, i);
        out.push(pack(KIND_RS, session, L, T, new Uint8Array([K, N]), f14, sh));
      }
    }
    return out;
  }
  function RSBlockDecoder(K, N, real) {
    this.K = K; this.N = N; this.kb = K; this.real = real; this.zeros = K - real; this.shards = {}; this.count = 0;
  }
  Object.defineProperty(RSBlockDecoder.prototype, 'rank', { get: function () { return Math.min(this.K, this.count + this.zeros); } });
  RSBlockDecoder.prototype.done = function () { return this.count + this.zeros >= this.K; };
  RSBlockDecoder.prototype.add = function (idx, payload) {
    if (this.shards[idx] || this.done() || (idx >= this.real && idx < this.K)) return false;
    this.shards[idx] = new Uint8Array(payload); this.count++; return true;
  };
  RSBlockDecoder.prototype.solve = function (T) {
    var K = this.K, rows = [], vals = [], j, r, c;
    for (j = this.real; j < K; j++) { r = new Uint8Array(K); r[j] = 1; rows.push(r); vals.push(new Uint8Array(T)); }
    var keys = Object.keys(this.shards).map(Number).sort(function (a, b) { return a - b; });
    for (var q = 0; q < keys.length && rows.length < K; q++) {
      r = new Uint8Array(K); for (c = 0; c < K; c++) r[c] = rsCoef(K, keys[q], c);
      rows.push(r); vals.push(new Uint8Array(this.shards[keys[q]]));
    }
    for (var col = 0; col < K; col++) {
      var pr = col; while (pr < K && rows[pr][col] === 0) pr++;
      if (pr === K) throw new Error('RS singular (cannot happen for MDS)');
      var tr = rows[col]; rows[col] = rows[pr]; rows[pr] = tr; var tv = vals[col]; vals[col] = vals[pr]; vals[pr] = tv;
      var inv = ginv(rows[col][col]);
      if (inv !== 1) { var ti = mulTable(inv); for (c = 0; c < K; c++) rows[col][c] = ti[rows[col][c]]; for (c = 0; c < T; c++) vals[col][c] = ti[vals[col][c]]; }
      for (r = 0; r < K; r++) {
        var f = rows[r][col];
        if (r === col || !f) continue;
        var tf = mulTable(f), rc = rows[col], rr = rows[r], vc = vals[col], vr = vals[r];
        for (c = 0; c < K; c++) rr[c] ^= tf[rc[c]];
        for (c = 0; c < T; c++) vr[c] ^= tf[vc[c]];
      }
    }
    var out = new Uint8Array(K * T);
    for (r = 0; r < K; r++) out.set(vals[r], r * T);
    return out;
  };

  /* ------------------------------------------------------------ channel-agnostic receiver */
  function Receiver() {
    this.p = null; this.blocks = null; this.result = null; this.error = null;
    this.stats = { packets: 0, bad: 0, foreign: 0, dup: 0, useful: 0 };
  }
  /* returns true when complete. Throws never; sets .error on hash mismatch. */
  Receiver.prototype.feed = function (pkt) {
    if (this.result) return true;
    this.stats.packets++;
    var d = parse(pkt);
    if (!d) { this.stats.bad++; return false; }
    if (!this.p) {
      this.p = { kind: d.kind, session: d.session, L: d.L, T: d.T, B: d.B, kb: d.kb, N: d.N, ktot: d.ktot };
      this.p.K = d.K;
      this.blocks = new Array(d.B);   // created lazily: a hostile header cannot force big allocations
      this.nblocks = 0;
    } else if (d.kind !== this.p.kind || d.session !== this.p.session || d.L !== this.p.L || d.T !== this.p.T || (d.kind === KIND_RS && (d.N !== this.p.N || d.kb !== this.p.kb))) {
      this.stats.foreign++; return false;
    }
    var blk = this.blocks[d.block];
    if (!blk) {
      var real = Math.max(0, Math.min(d.kb, this.p.ktot - d.block * d.kb));
      blk = this.blocks[d.block] = d.kind === KIND_FOUNTAIN ? new FountainBlockDecoder(d.session, d.block, d.kb, real, d.T) : new RSBlockDecoder(d.K, d.N, real);
      this.nblocks++;
    }
    if (blk.add(d.esi, d.payload)) this.stats.useful++; else this.stats.dup++;
    if (this.nblocks < this.p.B) return false;
    for (var i = 0; i < this.blocks.length; i++) if (!this.blocks[i].done()) return false;
    var T = this.p.T, L = this.p.L, all = new Uint8Array(this.p.B * this.p.kb * T);
    for (i = 0; i < this.blocks.length; i++) all.set(this.blocks[i].solve(T), i * this.p.kb * T);
    var data = all.slice(0, L), h = all.subarray(L, L + 32), h2 = sha256(data);
    for (i = 0; i < 32; i++) if (h[i] !== h2[i]) { this.error = 'SHA-256 mismatch: output refused'; this.reset(); return false; }
    if (rd32(h, 0) !== this.p.session) { this.error = 'session mismatch'; this.reset(); return false; }
    this.result = data; this.sha256 = hex(h2); return true;
  };
  Receiver.prototype.reset = function () { this.p = null; this.blocks = null; };
  Receiver.prototype.progress = function () {
    if (!this.blocks) return 0; var a = 0, t = 0;
    for (var i = 0; i < this.blocks.length; i++) if (this.blocks[i]) a += Math.min(this.blocks[i].rank, this.blocks[i].kb);
    return a / (this.p.B * this.p.kb);
  };

  /* ------------------------------------------------------------ text encodings */
  var B45 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:', B45I = {};
  for (var q = 0; q < 45; q++) B45I[B45[q]] = q;
  function b45encode(b) {
    var s = '', i;
    for (i = 0; i + 1 < b.length; i += 2) { var n = b[i] * 256 + b[i + 1]; s += B45[n % 45] + B45[Math.floor(n / 45) % 45] + B45[Math.floor(n / 2025)]; }
    if (b.length & 1) { n = b[b.length - 1]; s += B45[n % 45] + B45[Math.floor(n / 45)]; }
    return s;
  }
  function b45decode(s) {
    var n = s.length; if (n % 3 === 1) return null;
    var out = new Uint8Array(Math.floor(n / 3) * 2 + (n % 3 === 2 ? 1 : 0)), o = 0, i, v, a, b2, c2;
    for (i = 0; i + 3 <= n; i += 3) {
      a = B45I[s[i]]; b2 = B45I[s[i + 1]]; c2 = B45I[s[i + 2]];
      if (a === undefined || b2 === undefined || c2 === undefined) return null;
      v = a + b2 * 45 + c2 * 2025; if (v > 0xFFFF) return null;
      out[o++] = v >> 8; out[o++] = v & 255;
    }
    if (n % 3 === 2) {
      a = B45I[s[n - 2]]; b2 = B45I[s[n - 1]];
      if (a === undefined || b2 === undefined) return null;
      v = a + b2 * 45; if (v > 255) return null; out[o++] = v;
    }
    return out;
  }
  var B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_', B64I = new Int16Array(128).fill(-1);
  for (q = 0; q < 64; q++) B64I[B64.charCodeAt(q)] = q;
  function b64uEncode(b) {
    var s = [], i, n;
    for (i = 0; i + 2 < b.length; i += 3) { n = (b[i] << 16) | (b[i + 1] << 8) | b[i + 2]; s.push(B64[n >> 18] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63] + B64[n & 63]); }
    if (b.length - i === 1) { n = b[i] << 16; s.push(B64[n >> 18] + B64[(n >> 12) & 63]); }
    else if (b.length - i === 2) { n = (b[i] << 16) | (b[i + 1] << 8); s.push(B64[n >> 18] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63]); }
    return s.join('');
  }
  function b64uDecode(s) {
    var n = s.length; if (n % 4 === 1) return null;
    var out = new Uint8Array(Math.floor(n * 3 / 4)), o = 0, acc = 0, bits = 0;
    for (var i = 0; i < n; i++) {
      var c = s.charCodeAt(i), v = c < 128 ? B64I[c] : -1; if (v < 0) return null;
      acc = ((acc << 6) | v) & 0xFFFFFF; bits += 6;
      if (bits >= 8) { bits -= 8; out[o++] = (acc >> bits) & 255; }
    }
    return out.subarray(0, o);
  }

  var core = {
    MAGIC: MAGIC, KIND_FOUNTAIN: KIND_FOUNTAIN, KIND_RS: KIND_RS, HDR: HDR, TRL: TRL, OVERHEAD: HDR + TRL,
    sha256: sha256, hex: hex, crc32: crc32, mix32: mix32, fountainRow: fountainRow, layout: layout,
    parse: parse, pack: pack, FountainEncoder: FountainEncoder, rsEncode: rsEncode, Receiver: Receiver,
    b45encode: b45encode, b45decode: b45decode, b64uEncode: b64uEncode, b64uDecode: b64uDecode
  };
  root.CrushChannels = root.CrushChannels || {};
  root.CrushChannels.core = core;
  if (typeof module !== 'undefined' && module.exports) module.exports = core;
})(typeof globalThis !== 'undefined' ? globalThis : this);

/* crushchan qrstream.js - QR helpers, fountain stream sender, camera/image QR receiver.
 * Needs: core.js, vendor qrcodegen.js (sender), vendor jsQR.js (receiver fallback).
 * API (globalThis.CrushChannels):
 *   qr.symbolSize(version, ecl)                    -> fountain symbol size T that fills one code
 *   qr.encode(packetBytes, version, ecl)           -> qrcodegen.QrCode
 *   qr.draw(canvas, qrCode, quietModules)
 *   stream.Sender(canvas, bytes, {version, ecl, rate}) .start() .stop() .n
 *   scan.decodeImageData(imageData, {multi})       -> Promise<[string]>   (BarcodeDetector or jsQR)
 *   scan.Camera(videoEl, onStrings, opts)          .start() .stop()
 */
(function (root) {
  'use strict';
  var CC = root.CrushChannels = root.CrushChannels || {};
  var core = CC.core;
  function qrgen() { return root.qrcodegen || (typeof qrcodegen !== 'undefined' ? qrcodegen : null); }

  /* ---------------------------------------------------------------- QR helpers */
  function eclObj(e) { var Q = qrgen().QrCode.Ecc; return { l: Q.LOW, m: Q.MEDIUM, q: Q.QUARTILE, h: Q.HIGH }[String(e).toLowerCase()]; }
  function alnumCapacity(version, ecl) {
    var bits = qrgen().QrCode.getNumDataCodewords(version, eclObj(ecl)) * 8 - 4 - (version < 10 ? 9 : version < 27 ? 11 : 13);
    return Math.floor(bits / 11) * 2 + (bits % 11 >= 6 ? 1 : 0);
  }
  function maxPacketBytes(version, ecl) { var c = alnumCapacity(version, ecl); return Math.floor(c / 3) * 2 + (c % 3 === 2 ? 1 : 0); }
  function symbolSize(version, ecl) { return maxPacketBytes(version, ecl) - core.OVERHEAD; }
  function encode(packet, version, ecl) {
    var g = qrgen();
    return g.QrCode.encodeSegments([g.QrSegment.makeAlphanumeric(core.b45encode(packet))], eclObj(ecl), version, version, -1, false);
  }
  function draw(canvas, q, quiet) {
    quiet = quiet === undefined ? 4 : quiet;
    var n = q.size + 2 * quiet, ctx = canvas.getContext('2d');
    if (canvas.width !== n || canvas.height !== n) { canvas.width = n; canvas.height = n; }
    var img = ctx.createImageData(n, n), d = img.data;
    for (var i = 0; i < d.length; i += 4) { d[i] = d[i + 1] = d[i + 2] = 255; d[i + 3] = 255; }
    for (var y = 0; y < q.size; y++) for (var x = 0; x < q.size; x++) if (q.getModule(x, y)) {
      var o = ((y + quiet) * n + x + quiet) * 4; d[o] = d[o + 1] = d[o + 2] = 0;
    }
    ctx.putImageData(img, 0, 0);
  }
  CC.qr = { alnumCapacity: alnumCapacity, maxPacketBytes: maxPacketBytes, symbolSize: symbolSize, encode: encode, draw: draw };

  /* ---------------------------------------------------------------- stream sender */
  function Sender(canvas, bytes, opts) {
    opts = opts || {};
    this.canvas = canvas; this.version = opts.version || 20; this.ecl = opts.ecl || 'l'; this.rate = opts.rate || 10;
    this.enc = new core.FountainEncoder(bytes, symbolSize(this.version, this.ecl));
    this.n = opts.start || 0; this.timer = null; this.onframe = opts.onframe || null;
  }
  Sender.prototype.frame = function () {
    var pk = this.enc.nth(this.n);
    draw(this.canvas, encode(pk, this.version, this.ecl), 4);
    if (this.onframe) this.onframe(this.n, this);
    this.n++;
  };
  Sender.prototype.start = function () {
    var self = this, t0 = performance.now(), shown = 0;
    this.stop();
    function tick() {
      // paced by wall clock so a slow machine drops rate instead of drifting
      var due = Math.floor((performance.now() - t0) * self.rate / 1000);
      if (due >= shown) { self.frame(); shown = due + 1; }
      self.timer = requestAnimationFrame(tick);
    }
    tick();
  };
  Sender.prototype.stop = function () { if (this.timer) cancelAnimationFrame(this.timer); this.timer = null; };
  CC.stream = { Sender: Sender };

  /* ---------------------------------------------------------------- scanning */
  var detector = null, detectorChecked = false;
  function getDetector() {
    if (detectorChecked) return Promise.resolve(detector);
    detectorChecked = true;
    if (!('BarcodeDetector' in root)) return Promise.resolve(null);
    return Promise.resolve(root.BarcodeDetector.getSupportedFormats ? root.BarcodeDetector.getSupportedFormats() : ['qr_code'])
      .then(function (f) { if (f.indexOf('qr_code') >= 0) detector = new root.BarcodeDetector({ formats: ['qr_code'] }); return detector; })
      .catch(function () { return null; });
  }
  function jsqrFn() { var j = root.jsQR; return j && j.default ? j.default : j; }

  function blankQuad(d, W, H, L) {
    var pts = [L.topLeftCorner, L.topRightCorner, L.bottomRightCorner, L.bottomLeftCorner];
    var cx = 0, cy = 0, i; for (i = 0; i < 4; i++) { cx += pts[i].x / 4; cy += pts[i].y / 4; }
    var q = pts.map(function (p) { return { x: cx + (p.x - cx) * 1.25, y: cy + (p.y - cy) * 1.25 }; });
    var x0 = Math.max(0, Math.floor(Math.min.apply(null, q.map(function (p) { return p.x; })))), x1 = Math.min(W - 1, Math.ceil(Math.max.apply(null, q.map(function (p) { return p.x; }))));
    var y0 = Math.max(0, Math.floor(Math.min.apply(null, q.map(function (p) { return p.y; })))), y1 = Math.min(H - 1, Math.ceil(Math.max.apply(null, q.map(function (p) { return p.y; }))));
    for (var y = y0; y <= y1; y++) for (var x = x0; x <= x1; x++) {
      var ins = true;
      for (i = 0; i < 4; i++) { var a = q[i], b = q[(i + 1) % 4]; if ((b.x - a.x) * (y - a.y) - (b.y - a.y) * (x - a.x) < 0) { ins = false; break; } }
      if (ins) { var o = (y * W + x) * 4; d[o] = d[o + 1] = d[o + 2] = 255; }
    }
  }
  function cropImageData(src, x0, y0, w, h) {
    var out = new Uint8ClampedArray(w * h * 4), W = src.width;
    for (var y = 0; y < h; y++) out.set(src.data.subarray(((y0 + y) * W + x0) * 4, ((y0 + y) * W + x0 + w) * 4), y * w * 4);
    return { data: out, width: w, height: h };
  }
  /* jsQR finds one code per call: for multi, blank each found code and retry, and when
   * the whole image fails, also try overlapping tiles (a failing code can mask others). */
  /* Preprocessing. jsQR's binarizer turns sensor noise in flat white areas into
   * speckle once a 8x8 block's range exceeds 24 levels; a 2x2 box filter fixes most of
   * it (measured in tests/sim_stream.py: see REPORT.md). variant: 'raw' | 'b2' | 'b3'. */
  function prep(img, variant) {
    var W = img.width, H = img.height, s = img.data, n = W * H, g = new Uint8Array(n), i, x, y;
    for (i = 0; i < n; i++) g[i] = (s[i * 4] * 54 + s[i * 4 + 1] * 183 + s[i * 4 + 2] * 19) >> 8;
    if (variant === 'bin') return binarize(g, W, H);
    var k = variant === 'b3' ? 3 : variant === 'b2' ? 2 : 1, out = new Uint8ClampedArray(n * 4);
    if (k === 1) { for (i = 0; i < n; i++) { out[i * 4] = out[i * 4 + 1] = out[i * 4 + 2] = g[i]; out[i * 4 + 3] = 255; } return out; }
    for (y = 0; y < H; y++) for (x = 0; x < W; x++) {
      var sum = 0, cnt = 0;
      for (var dy = 0; dy < k; dy++) { var yy = y + dy - (k === 3 ? 1 : 0); if (yy < 0 || yy >= H) continue;
        for (var dx = 0; dx < k; dx++) { var xx = x + dx - (k === 3 ? 1 : 0); if (xx < 0 || xx >= W) continue; sum += g[yy * W + xx]; cnt++; } }
      var v = (sum / cnt) | 0, o = (y * W + x) * 4; out[o] = out[o + 1] = out[o + 2] = v; out[o + 3] = 255;
    }
    return out;
  }
  /* 'bin': our own adaptive threshold handed to jsQR as a clean black/white image.
   * 2x2 box filter, then local mean over a (min(W,H)/24)-wide window via an integral image;
   * a pixel is black only if it is darker than the local mean by > 6% of the local range
   * floor, so flat white paper/screen areas become solid white instead of speckle. */
  function binarize(g, W, H) {
    var n = W * H, f = new Uint8Array(n), x, y, i;
    for (y = 0; y < H; y++) for (x = 0; x < W; x++) {
      var x1 = x + 1 < W ? x + 1 : x, y1 = y + 1 < H ? y + 1 : y;
      f[y * W + x] = (g[y * W + x] + g[y * W + x1] + g[y1 * W + x] + g[y1 * W + x1]) >> 2;
    }
    var I = new Float64Array((W + 1) * (H + 1));
    for (y = 0; y < H; y++) { var rs = 0; for (x = 0; x < W; x++) { rs += f[y * W + x]; I[(y + 1) * (W + 1) + x + 1] = I[y * (W + 1) + x + 1] + rs; } }
    var r = Math.max(4, Math.round(Math.min(W, H) / 48)), out = new Uint8ClampedArray(n * 4);
    for (y = 0; y < H; y++) {
      var ya = Math.max(0, y - r), yb = Math.min(H, y + r + 1);
      for (x = 0; x < W; x++) {
        var xa = Math.max(0, x - r), xb = Math.min(W, x + r + 1);
        var sum = I[yb * (W + 1) + xb] - I[ya * (W + 1) + xb] - I[yb * (W + 1) + xa] + I[ya * (W + 1) + xa];
        var mean = sum / ((yb - ya) * (xb - xa)), v = f[y * W + x] < mean * 0.94 ? 0 : 255, o = (y * W + x) * 4;
        out[o] = out[o + 1] = out[o + 2] = v; out[o + 3] = 255;
      }
    }
    return out;
  }
  function jsqrMulti(img, multi, variants) {
    var jsQR = jsqrFn(), found = [], seen = {};
    if (!jsQR) return found;
    if (!multi) {
      variants = variants || ['b2'];
      for (var vi = 0; vi < variants.length && !found.length; vi++) jsqrTry(jsQR, prep(img, variants[vi]), img.width, img.height, found, seen, 1);
      return found;
    }
    /* Several codes per image (paper pages). jsQR returns only the "best" code and gives up
     * when that one fails, and finder patterns of neighbouring codes confuse it. So: find the
     * code-like regions ourselves (blocks with strong local contrast, closed, connected
     * components that are roughly square), crop each with a margin, and decode each crop on
     * the raw image, then on the 2x2-filtered one. Uniform areas (a hand, a stain) are skipped. */
    var W = img.width, H = img.height, raw = prep(img, 'raw'), b2 = null;
    var regs = findCodeRegions(raw, W, H);
    for (var ri = 0; ri < regs.length; ri++) {
      var R = regs[ri];
      var c = cropImageData({ data: raw, width: W }, R.x, R.y, R.w, R.h);
      if (jsqrTry(jsQR, c.data, R.w, R.h, found, seen, 2)) continue;
      if (!b2) b2 = prep(img, 'b2');
      c = cropImageData({ data: b2, width: W }, R.x, R.y, R.w, R.h);
      jsqrTry(jsQR, c.data, R.w, R.h, found, seen, 2);
    }
    if (!found.length) jsqrTry(jsQR, raw, W, H, found, seen, 4);
    return found;
  }
  function findCodeRegions(rgba, W, H) {
    var bs = Math.max(4, Math.round(Math.min(W, H) / 160)), bw = Math.ceil(W / bs), bh = Math.ceil(H / bs);
    var act = new Uint8Array(bw * bh), x, y, i, j;
    for (j = 0; j < bh; j++) for (i = 0; i < bw; i++) {
      var mn = 255, mx = 0;
      for (y = j * bs; y < Math.min(H, (j + 1) * bs); y++) for (x = i * bs; x < Math.min(W, (i + 1) * bs); x++) {
        var v = rgba[(y * W + x) * 4]; if (v < mn) mn = v; if (v > mx) mx = v;
      }
      act[j * bw + i] = mx - mn > 50 ? 1 : 0;
    }
    function morph(src, dil) {
      var out = new Uint8Array(bw * bh);
      for (var jj = 0; jj < bh; jj++) for (var ii = 0; ii < bw; ii++) {
        var acc = dil ? 0 : 1;
        for (var dy = -1; dy <= 1; dy++) for (var dx = -1; dx <= 1; dx++) {
          var yy = jj + dy, xx = ii + dx, v2 = (yy < 0 || yy >= bh || xx < 0 || xx >= bw) ? 0 : src[yy * bw + xx];
          if (dil) acc |= v2; else acc &= v2;
        }
        out[jj * bw + ii] = acc;
      }
      return out;
    }
    act = morph(morph(act, false), true);       // open: drop thin text lines (labels, header)
    act = morph(morph(act, true), false);       // close small holes inside codes
    var lab = new Int32Array(bw * bh), regs = [], stack = [];
    for (var k = 0; k < bw * bh; k++) {
      if (!act[k] || lab[k]) continue;
      var x0 = bw, x1 = -1, y0 = bh, y1 = -1, cnt = 0; lab[k] = regs.length + 1; stack.push(k);
      while (stack.length) {
        var q = stack.pop(), qx = q % bw, qy = (q - qx) / bw; cnt++;
        if (qx < x0) x0 = qx; if (qx > x1) x1 = qx; if (qy < y0) y0 = qy; if (qy > y1) y1 = qy;
        var nb = [q - 1, q + 1, q - bw, q + bw];
        for (var t = 0; t < 4; t++) {
          var n2 = nb[t]; if (n2 < 0 || n2 >= bw * bh) continue;
          if ((t === 0 && qx === 0) || (t === 1 && qx === bw - 1)) continue;
          if (act[n2] && !lab[n2]) { lab[n2] = lab[k]; stack.push(n2); }
        }
      }
      var w = x1 - x0 + 1, h = y1 - y0 + 1;
      regs.push({ x0: x0, y0: y0, w: w, h: h, fill: cnt / (w * h) });
    }
    var out = [];
    function emit(x0, y0, w, h) {
      var m = Math.round(Math.max(w, h) * 0.12) + 1;
      var X = Math.max(0, (x0 - m) * bs), Y = Math.max(0, (y0 - m) * bs);
      var X1 = Math.min(W, (x0 + w + m) * bs), Y1 = Math.min(H, (y0 + h + m) * bs);
      out.push({ x: X, y: Y, w: X1 - X, h: Y1 - Y });
    }
    for (k = 0; k < regs.length; k++) {
      var r = regs[k];
      if (r.w < 6 || r.h < 6 || r.fill < 0.35) continue;
      // codes in a row/column that touched each other (through labels) form an n:1 strip: split it
      var nx = Math.max(1, Math.round(r.w / r.h)), ny = Math.max(1, Math.round(r.h / r.w));
      if (nx > 6 || ny > 6) continue;
      for (var sy = 0; sy < ny; sy++) for (var sx = 0; sx < nx; sx++)
        emit(r.x0 + Math.floor(sx * r.w / nx), r.y0 + Math.floor(sy * r.h / ny), Math.ceil(r.w / nx), Math.ceil(r.h / ny));
    }
    return out;
  }
  /* up to `rounds` codes from one buffer (found codes are blanked between rounds). Returns count decoded. */
  function jsqrTry(jsQR, d, W, H, found, seen, rounds) {
    var n = 0;
    for (var k = 0; k < rounds; k++) {
      var r = jsQR(d, W, H, { inversionAttempts: 'dontInvert' });
      if (!r) break;
      n++;
      if (!seen[r.data]) { seen[r.data] = 1; found.push(r.data); }
      if (k + 1 < rounds) blankQuad(d, W, H, r.location);
    }
    return n;
  }
  /* zxing-cpp (WASM, via zxing-wasm) when the page inlined it: much stronger than jsQR on
   * noisy / multi-code images (measured in REPORT.md). Loaded from an inline base64 string,
   * never from the network. */
  var zxPromise = null;
  function zxReady() {
    if (zxPromise) return zxPromise;
    var Z = root.ZXingWASM, b64 = root.CC_ZXING_WASM_B64;
    if (!Z || !b64) return (zxPromise = Promise.resolve(false));
    var bin = typeof atob === 'function' ? atob(b64) : Buffer.from(b64, 'base64').toString('binary');
    var u = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    zxPromise = Promise.resolve(Z.prepareZXingModule({ overrides: { wasmBinary: u.buffer }, fireImmediately: true }))
      .then(function () { return true; }, function () { return false; });
    return zxPromise;
  }
  function zxDecode(img, multi) {
    return root.ZXingWASM.readBarcodesFromImageData({ data: img.data, width: img.width, height: img.height, colorSpace: 'srgb' },
      { formats: ['QRCode'], maxNumberOfSymbols: multi ? 64 : 1, tryHarder: true })
      .then(function (rs) { return rs.filter(function (r) { return r.isValid; }).map(function (r) { return r.text; }); });
  }
  /* imageData: {data: RGBA, width, height}. opts.source: canvas for BarcodeDetector.
   * Engine order: BarcodeDetector (if the browser has one) -> zxing-cpp WASM (if inlined) -> jsQR. */
  function decodeImageData(imageData, opts) {
    opts = opts || {};
    function soft() {
      if (opts.engine === 'jsqr') return Promise.resolve(jsqrMulti(imageData, !!opts.multi, opts.variants));
      return zxReady().then(function (ok) {
        return ok ? zxDecode(imageData, !!opts.multi) : jsqrMulti(imageData, !!opts.multi, opts.variants);
      });
    }
    if (opts.engine) return soft();
    return getDetector().then(function (det) {
      if (!det) return soft();
      return det.detect(opts.source || imageData).then(function (rs) {
        var out = rs.map(function (r) { return r.rawValue; });
        return (out.length || !opts.fallback) ? out : soft();
      }, soft);
    });
  }
  function engineName() {
    return getDetector().then(function (det) { return det ? 'BarcodeDetector' : zxReady().then(function (ok) { return ok ? 'zxing-cpp' : 'jsQR'; }); });
  }

  /* Camera loop: grabs the newest video frame whenever the previous decode finished. */
  function Camera(video, onStrings, opts) {
    this.video = video; this.onStrings = onStrings; this.opts = opts || {};
    this.canvas = document.createElement('canvas'); this.running = false; this.stream = null;
    this.stats = { frames: 0, ms: 0, codes: 0, engine: '' };
  }
  Camera.prototype.start = function () {
    var self = this;
    var constraints = { audio: false, video: { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } } };
    if (this.opts.deviceId) constraints.video.deviceId = { exact: this.opts.deviceId };
    return navigator.mediaDevices.getUserMedia(constraints).then(function (s) {
      self.stream = s; self.video.srcObject = s; self.video.setAttribute('playsinline', ''); self.video.muted = true;
      return self.video.play();
    }).then(function () { return engineName(); }).then(function (name) {
      self.stats.engine = name;
      self.running = true; self.loop();
    });
  };
  Camera.prototype.loop = function () {
    var self = this;
    if (!this.running) return;
    var v = this.video, W = v.videoWidth, H = v.videoHeight;
    if (!W || !H) { setTimeout(function () { self.loop(); }, 50); return; }
    var maxW = this.opts.maxWidth || 1280, s = Math.min(1, maxW / W);
    var w = Math.round(W * s), h = Math.round(H * s), c = this.canvas;
    if (c.width !== w) c.width = w; if (c.height !== h) c.height = h;
    var ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(v, 0, 0, w, h);
    var t0 = performance.now();
    var img = ctx.getImageData(0, 0, w, h);
    decodeImageData(img, { source: c, multi: !!this.opts.multi }).then(function (strs) {
      self.stats.frames++; self.stats.ms += performance.now() - t0; self.stats.codes += strs.length;
      if (strs.length) self.onStrings(strs);
    }, function () { }).then(function () {
      if (self.running) (root.requestAnimationFrame ? requestAnimationFrame : setTimeout)(function () { self.loop(); });
    });
  };
  Camera.prototype.stop = function () {
    this.running = false;
    if (this.stream) this.stream.getTracks().forEach(function (t) { t.stop(); });
    this.stream = null;
  };
  CC.scan = { decodeImageData: decodeImageData, zxReady: zxReady, engineName: engineName, jsqrMulti: jsqrMulti, prep: prep, findCodeRegions: findCodeRegions, Camera: Camera, getDetector: getDetector };

  /* ---------------------------------------------------------------- string -> packet helper */
  CC.feedStrings = function (receiver, strs) {
    var any = false;
    for (var i = 0; i < strs.length; i++) {
      var b = core.b45decode(String(strs[i]).trim());
      if (b && receiver.feed(b)) any = true;
    }
    return any || !!receiver.result;
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);

/* crushchan audio.js - acoustic OFDM modem (mirrors py/crushchan/audio.py).
 * Needs core.js.  API (globalThis.CrushChannels.audio):
 *   MODES, FS
 *   modulate(bytes, {mode, repair}) -> Float32Array at 48 kHz  (whole archive, fountain-coded)
 *   wav(float32) -> Uint8Array (16-bit PCM WAV)
 *   new Rx(receiver)  .push(Float32Array at 48 kHz)  .stats   (streaming demodulator; auto-detects mode)
 *   Mic(rx) .start() .stop()   microphone capture (echo cancellation / AGC / noise suppression OFF)
 *   decodeFile(arrayBuffer, rx) -> Promise  (any audio file the browser can decode)
 * Floating point is used only up to the CRC-32 / SHA-256 checks; it cannot produce wrong bytes. */
(function (root) {
  'use strict';
  var CC = root.CrushChannels = root.CrushChannels || {};
  var core = CC.core;
  var FS = 48000, CHIRP_N = 4096, GAP0 = 256, GAP1 = 1024, AUDIO_T = 200, PKT_BITS = (AUDIO_T + core.OVERHEAD) * 8;
  var MODES = {
    fast: { bps: 2, nfft: 1024, cp: 384, k0: 32, nc: 96 },
    robust: { bps: 1, nfft: 2048, cp: 1024, k0: 64, nc: 192 },
    echo: { bps: 1, nfft: 4096, cp: 2048, k0: 128, nc: 384 }
  };
  function gcd(a, b) { while (b) { var t = a % b; a = b; b = t; } return a; }
  function geom(mode) {
    var m = MODES[mode], coded = 2 * (PKT_BITS + 6), nsym = Math.ceil(coded / (m.nc * m.bps)), L = nsym * m.nc * m.bps;
    return { nsym: nsym, L: L, flen: CHIRP_N + GAP0 + (nsym + 1) * (m.nfft + m.cp) + GAP1 };
  }
  function perm(L) { var P = 1735; while (gcd(P, L) !== 1) P += 2; var p = new Int32Array(L); for (var i = 0; i < L; i++) p[i] = (i * P) % L; return p; }
  var CHIRP = (function () {
    var c = new Float64Array(CHIRP_N), T = CHIRP_N / FS, f0 = 800, f1 = 7000, r = 256;
    for (var i = 0; i < CHIRP_N; i++) {
      var t = i / FS, w = 1;
      if (i < r) w = 0.5 - 0.5 * Math.cos(Math.PI * i / r); else if (i >= CHIRP_N - r) w = 0.5 - 0.5 * Math.cos(Math.PI * (CHIRP_N - 1 - i) / r);
      c[i] = Math.sin(2 * Math.PI * (f0 * t + (f1 - f0) / (2 * T) * t * t)) * w;
    }
    return c;
  })();
  var CHIRP_E = 0; for (var q = 0; q < CHIRP_N; q++) CHIRP_E += CHIRP[q] * CHIRP[q];

  /* ---------------------------------------------------------- FFT (radix-2, in place) */
  function fft(re, im, inv) {
    var n = re.length, i, j = 0, k, len;
    for (i = 1; i < n; i++) { var bit = n >> 1; for (; j & bit; bit >>= 1) j ^= bit; j ^= bit; if (i < j) { var t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; } }
    for (len = 2; len <= n; len <<= 1) {
      var ang = 2 * Math.PI / len * (inv ? 1 : -1), wr = Math.cos(ang), wi = Math.sin(ang), h = len >> 1;
      for (i = 0; i < n; i += len) {
        var cr = 1, ci = 0;
        for (k = 0; k < h; k++) {
          var a = i + k, b = a + h, xr = re[b] * cr - im[b] * ci, xi = re[b] * ci + im[b] * cr;
          re[b] = re[a] - xr; im[b] = im[a] - xi; re[a] += xr; im[a] += xi;
          var ncr = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = ncr;
        }
      }
    }
  }

  /* ---------------------------------------------------------- convolutional code K=7 (171,133) */
  var G0 = 0x79, G1 = 0x5B;
  function par(x) { x ^= x >> 4; x ^= x >> 2; x ^= x >> 1; return x & 1; }
  function convEncode(bits) {
    var out = new Uint8Array(2 * (bits.length + 6)), s = 0, o = 0;
    for (var i = 0; i < bits.length + 6; i++) { s = ((s << 1) | (i < bits.length ? bits[i] : 0)) & 0x7F; out[o++] = par(s & G0); out[o++] = par(s & G1); }
    return out;
  }
  var PREV = new Int32Array(128), OUTB = new Float64Array(256);
  (function () {
    for (var ns = 0; ns < 64; ns++) for (var j = 0; j < 2; j++) {
      var ps = (ns >> 1) | (j << 5), full = ((ps << 1) | (ns & 1)) & 0x7F;
      PREV[ns * 2 + j] = ps; OUTB[(ns * 2 + j) * 2] = 1 - 2 * par(full & G0); OUTB[(ns * 2 + j) * 2 + 1] = 1 - 2 * par(full & G1);
    }
  })();
  function viterbi(soft, nbits) {
    var n = nbits + 6, metric = new Float64Array(64).fill(-1e18), nm = new Float64Array(64), dec = new Uint8Array(n * 64);
    metric[0] = 0;
    for (var t = 0; t < n; t++) {
      var s0 = soft[2 * t], s1 = soft[2 * t + 1];
      for (var ns = 0; ns < 64; ns++) {
        var a = metric[PREV[ns * 2]] + OUTB[ns * 4] * s0 + OUTB[ns * 4 + 1] * s1;
        var b = metric[PREV[ns * 2 + 1]] + OUTB[ns * 4 + 2] * s0 + OUTB[ns * 4 + 3] * s1;
        if (b > a) { nm[ns] = b; dec[t * 64 + ns] = 1; } else { nm[ns] = a; dec[t * 64 + ns] = 0; }
      }
      var tmp = metric; metric = nm; nm = tmp;
    }
    var s = 0, bits = new Uint8Array(n);
    for (t = n - 1; t >= 0; t--) { bits[t] = s & 1; s = PREV[s * 2 + dec[t * 64 + s]]; }
    var out = new Uint8Array(nbits >> 3);
    for (var i = 0; i < nbits; i++) out[i >> 3] |= bits[i] << (7 - (i & 7));
    return out;
  }

  /* ---------------------------------------------------------- modulator */
  function ofdmSymbol(ph, m, out, off) {
    var N = m.nfft, re = new Float64Array(N), im = new Float64Array(N);
    for (var k = 0; k < m.nc; k++) {
      var b = m.k0 + k, c = Math.cos(ph[k]), s = Math.sin(ph[k]);
      re[b] = c; im[b] = s; re[N - b] = c; im[N - b] = -s;   // Hermitian -> real signal
    }
    fft(re, im, true);
    for (var i = 0; i < m.cp; i++) out[off + i] = re[N - m.cp + i] / N * 2;
    for (i = 0; i < N; i++) out[off + m.cp + i] = re[i] / N * 2;
  }
  function modulatePacket(pkt, mode) {
    var m = MODES[mode], g = geom(mode), bits = new Uint8Array(pkt.length * 8);
    for (var i = 0; i < bits.length; i++) bits[i] = (pkt[i >> 3] >> (7 - (i & 7))) & 1;
    var coded = convEncode(bits), P = perm(g.L), inter = new Uint8Array(g.L);
    for (i = 0; i < coded.length; i++) inter[P[i]] = coded[i];
    var ph = new Float64Array(m.nc);
    for (var k = 0; k < m.nc; k++) ph[k] = (core.mix32((0xA0D10 + k) >>> 0) & 3) * Math.PI / 2;
    var symLen = m.nfft + m.cp, body = new Float64Array((g.nsym + 1) * symLen);
    ofdmSymbol(ph, m, body, 0);
    for (var s = 0; s < g.nsym; s++) {
      for (k = 0; k < m.nc; k++) {
        var inc;
        if (m.bps === 2) { var b0 = inter[(s * m.nc + k) * 2], b1 = inter[(s * m.nc + k) * 2 + 1]; inc = (b0 === 0 && b1 === 0) ? 0 : (b0 === 1 && b1 === 0) ? 1 : (b0 === 1 && b1 === 1) ? 2 : 3; inc *= Math.PI / 2; }
        else inc = inter[s * m.nc + k] * Math.PI;
        ph[k] += inc;
      }
      ofdmSymbol(ph, m, body, (s + 1) * symLen);
    }
    var e = 0; for (i = 0; i < body.length; i++) e += body[i] * body[i];
    var sc = 0.18 / (Math.sqrt(e / body.length) + 1e-12);
    var out = new Float32Array(g.flen);
    for (i = 0; i < CHIRP_N; i++) out[i] = CHIRP[i] * 0.5;
    for (i = 0; i < body.length; i++) out[CHIRP_N + GAP0 + i] = Math.max(-0.85, Math.min(0.85, body[i] * sc));
    return out;
  }
  function modulate(bytes, opts) {
    opts = opts || {};
    var mode = opts.mode || 'echo', repair = opts.repair === undefined ? 0.5 : opts.repair;
    var enc = new core.FountainEncoder(bytes, AUDIO_T), nframes = Math.ceil(enc.ktot * (1 + repair)) + 2;
    var g = geom(mode), lead = Math.round(0.4 * FS), out = new Float32Array(2 * lead + nframes * g.flen);
    for (var n = 0; n < nframes; n++) out.set(modulatePacket(enc.nth(n), mode), lead + n * g.flen);
    return out;
  }
  function wav(x, fs) {
    fs = fs || FS;
    var n = x.length, b = new Uint8Array(44 + 2 * n), dv = new DataView(b.buffer);
    function str(o, s) { for (var i = 0; i < s.length; i++) b[o + i] = s.charCodeAt(i); }
    str(0, 'RIFF'); dv.setUint32(4, 36 + 2 * n, true); str(8, 'WAVEfmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, fs, true); dv.setUint32(28, fs * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); str(36, 'data'); dv.setUint32(40, 2 * n, true);
    for (var i = 0; i < n; i++) dv.setInt16(44 + 2 * i, Math.round(Math.max(-1, Math.min(1, x[i])) * 32767), true);
    return b;
  }

  /* ---------------------------------------------------------- demodulator */
  function demodFrame(x, start, mode) {
    var m = MODES[mode], g = geom(mode), N = m.nfft, base = start + CHIRP_N + GAP0, margin = 24;
    if (base + (g.nsym + 1) * (N + m.cp) > x.length || start < 0) return null;
    var Yr = [], Yi = [], s, k;
    for (s = 0; s <= g.nsym; s++) {
      var o = base + s * (N + m.cp) + m.cp - margin, re = new Float64Array(N), im = new Float64Array(N);
      for (var i = 0; i < N; i++) re[i] = x[o + i];
      fft(re, im, false);
      Yr.push(re.subarray(m.k0, m.k0 + m.nc)); Yi.push(im.subarray(m.k0, m.k0 + m.nc));
    }
    var soft = new Float64Array(g.L), mag = 0, idx = 0;
    var zr = new Float64Array(g.nsym * m.nc), zi = new Float64Array(g.nsym * m.nc);
    for (s = 0; s < g.nsym; s++) for (k = 0; k < m.nc; k++) {
      var ar = Yr[s + 1][k], ai = Yi[s + 1][k], br = Yr[s][k], bi = -Yi[s][k];
      var r = ar * br - ai * bi, im2 = ar * bi + ai * br;
      zr[idx] = r; zi[idx] = im2; mag += Math.sqrt(r * r + im2 * im2); idx++;
    }
    mag = mag / idx + 1e-12;
    var c = Math.SQRT1_2;
    for (i = 0; i < idx; i++) {
      if (m.bps === 2) { soft[2 * i] = (zr[i] * c - zi[i] * c) / mag; soft[2 * i + 1] = (zr[i] * c + zi[i] * c) / mag; }
      else soft[i] = zr[i] / mag;
    }
    var P = perm(g.L), de = new Float64Array(g.L);
    for (i = 0; i < g.L; i++) de[i] = soft[P[i]];
    return viterbi(de, PKT_BITS);
  }

  /* Streaming receiver: keeps a sliding buffer, finds chirps by normalised matched filter,
   * demodulates each frame with every mode and keeps the one whose CRC-32 checks. */
  function Rx(receiver) {
    this.rx = receiver || new core.Receiver();
    this.buf = new Float32Array(1 << 20); this.len = 0; this.scan = 0; this.offset = 0;
    this.stats = { frames: 0, ok: 0, bad: 0, mode: null, level: 0 };
    var maxF = 0; for (var md in MODES) maxF = Math.max(maxF, geom(md).flen);
    this.maxF = maxF;
    var B = 16384; this.B = B;
    var cr = new Float64Array(B), ci = new Float64Array(B);
    for (var i = 0; i < CHIRP_N; i++) cr[i] = CHIRP[CHIRP_N - 1 - i];
    fft(cr, ci, false); this.Hr = cr; this.Hi = ci;
  }
  Rx.prototype.push = function (x) {
    if (this.len + x.length > this.buf.length) {
      // drop everything before the scan point (keep one frame of history)
      var keep = Math.max(0, this.scan - 64), nb = new Float32Array(Math.max(this.buf.length, (this.len - keep + x.length) * 2));
      nb.set(this.buf.subarray(keep, this.len)); this.buf = nb; this.len -= keep; this.scan -= keep; this.offset += keep;
    }
    this.buf.set(x, this.len); this.len += x.length;
    var lv = 0; for (var i = 0; i < x.length; i++) lv = Math.max(lv, Math.abs(x[i])); this.stats.level = lv;
    this.process();
  };
  Rx.prototype.process = function () {
    var B = this.B, step = B - CHIRP_N + 1, thr = 0.35;
    while (!this.rx.result && this.scan + B + this.maxF <= this.len + this.maxF && this.scan + B <= this.len) {
      var re = new Float64Array(B), im = new Float64Array(B), x = this.buf, s0 = this.scan, i;
      for (i = 0; i < B; i++) re[i] = x[s0 + i];
      fft(re, im, false);
      for (i = 0; i < B; i++) { var a = re[i], b = im[i]; re[i] = a * this.Hr[i] - b * this.Hi[i]; im[i] = a * this.Hi[i] + b * this.Hr[i]; }
      fft(re, im, true);
      // valid outputs: index j (chirp starting at s0+j) is re[j + CHIRP_N - 1]/B for j in [0, step)
      var e = 0; for (i = 0; i < CHIRP_N; i++) e += x[s0 + i] * x[s0 + i];
      var found = -1, best = 0;
      for (var j = 0; j < step; j++) {
        if (j > 0) { var o = x[s0 + j - 1], nw = x[s0 + j + CHIRP_N - 1]; e += nw * nw - o * o; }
        var v = re[j + CHIRP_N - 1] / B / (Math.sqrt(Math.max(e, 0) * CHIRP_E) + 1e-9);
        if (found < 0 && v > thr) { found = j; best = v; }
        else if (found >= 0) { if (j > found + CHIRP_N) break; if (v > best) { best = v; found = j; } }
      }
      if (found < 0) { this.scan += step; continue; }
      if (found > step - CHIRP_N - 1 && found > 64) { this.scan = s0 + found - 64; continue; }  // peak may lie in the next block
      var start = s0 + found;
      if (start + this.maxF > this.len) {
        // need the whole frame; try the modes whose frames are complete, else wait
        if (!this.tryModes(start, true)) return;
      } else this.tryModes(start, false);
    }
  };
  Rx.prototype.tryModes = function (start, partial) {
    var x = this.buf.subarray(0, this.len), order = this.stats.mode ? [this.stats.mode] : [];
    for (var md in MODES) if (order.indexOf(md) < 0) order.push(md);
    var incomplete = false;
    for (var q = 0; q < order.length; q++) {
      var g = geom(order[q]);
      if (start + g.flen > this.len) { incomplete = true; continue; }
      var pkt = demodFrame(x, start, order[q]);
      if (pkt && core.parse(pkt)) {
        this.stats.frames++; this.stats.ok++; this.stats.mode = order[q];
        this.rx.feed(pkt); this.scan = start + g.flen - CHIRP_N / 2; return true;
      }
    }
    if (incomplete && partial) return false;   // wait for more audio
    this.stats.frames++; this.stats.bad++; this.scan = start + CHIRP_N; return true;
  };

  /* ---------------------------------------------------------- microphone */
  function Mic(rx) { this.rx = rx; this.ctx = null; this.stream = null; }
  Mic.prototype.start = function () {
    var self = this;
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 } })
      .then(function (s) {
        self.stream = s;
        var AC = root.AudioContext || root.webkitAudioContext;
        try { self.ctx = new AC({ sampleRate: FS }); } catch (e) { self.ctx = new AC(); }
        var src = self.ctx.createMediaStreamSource(s), rate = self.ctx.sampleRate;
        var proc = self.ctx.createScriptProcessor(4096, 1, 1), carry = null;
        proc.onaudioprocess = function (ev) {
          var d = ev.inputBuffer.getChannelData(0);
          self.rx.push(rate === FS ? new Float32Array(d) : resampleLinear(d, rate));
        };
        src.connect(proc); proc.connect(self.ctx.destination);
        self.proc = proc; self.rate = rate;
        return rate;
      });
  };
  Mic.prototype.stop = function () {
    if (this.proc) this.proc.disconnect(); if (this.ctx) this.ctx.close();
    if (this.stream) this.stream.getTracks().forEach(function (t) { t.stop(); });
  };
  /* only used if the browser refuses a 48 kHz AudioContext (band is <= 7 kHz, so linear is adequate) */
  var rsState = { pos: 0, last: 0 };
  function resampleLinear(d, rate) {
    var ratio = rate / FS, out = [], p = rsState.pos;
    while (p < d.length - 1) { var i = Math.floor(p), f = p - i; out.push((i < 0 ? rsState.last : d[i]) * (1 - f) + d[i + 1] * f); p += ratio; }
    rsState.pos = p - d.length; rsState.last = d[d.length - 1];
    return new Float32Array(out);
  }
  function decodeFile(buf, rx) {
    var OAC = root.OfflineAudioContext || root.webkitOfflineAudioContext, ctx = new OAC(1, FS, FS);
    return new Promise(function (res, rej) { ctx.decodeAudioData(buf, res, rej); }).then(function (ab) {
      var x = ab.getChannelData(0);
      if (ab.numberOfChannels > 1) { var y = new Float32Array(x.length); for (var c = 0; c < ab.numberOfChannels; c++) { var d = ab.getChannelData(c); for (var i = 0; i < y.length; i++) y[i] += d[i] / ab.numberOfChannels; } x = y; }
      for (var o = 0; o < x.length && !rx.rx.result; o += 48000) rx.push(x.subarray(o, Math.min(x.length, o + 48000)));
      rx.push(new Float32Array(rx.maxF + rx.B));   // flush
      return rx;
    });
  }

  CC.audio = { FS: FS, MODES: MODES, AUDIO_T: AUDIO_T, geom: geom, modulate: modulate, modulatePacket: modulatePacket, wav: wav,
    demodFrame: demodFrame, Rx: Rx, Mic: Mic, decodeFile: decodeFile, fft: fft };
})(typeof globalThis !== 'undefined' ? globalThis : this);

/* crushchan ui.js - shared result panel. On success it dispatches
 *   window 'crush-archive' CustomEvent {detail: {bytes: Uint8Array, sha256: hex, channel}}
 * so a host page (e.g. Crush's self-extracting share .html) can take the bytes directly. */
(function (root) {
  'use strict';
  var CC = root.CrushChannels = root.CrushChannels || {};
  function fmt(n) { return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KiB' : (n / 1048576).toFixed(2) + ' MiB'; }
  function save(bytes, name) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
    a.download = name || 'received.crush';
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function showResult(el, bytes, sha, channel, extra) {
    el.innerHTML = '';
    var h = document.createElement('div'); h.className = 'cc-ok';
    h.textContent = 'Received ' + fmt(bytes.length) + ' - SHA-256 verified';
    var s = document.createElement('div'); s.className = 'cc-hash'; s.textContent = 'SHA-256 ' + sha;
    var b = document.createElement('button'); b.textContent = 'Save archive'; b.className = 'cc-btn';
    b.onclick = function () { save(bytes, (CC.saveName || 'received') + '.crush'); };
    el.appendChild(h); el.appendChild(s);
    if (extra) { var e = document.createElement('div'); e.className = 'cc-extra'; e.textContent = extra; el.appendChild(e); }
    el.appendChild(b);
    CC.lastResult = { bytes: bytes, sha256: sha, channel: channel };
    try { root.dispatchEvent(new CustomEvent('crush-archive', { detail: CC.lastResult })); } catch (err) { }
  }
  function readFile(file) {
    return new Promise(function (res, rej) { var r = new FileReader(); r.onload = function () { res(new Uint8Array(r.result)); }; r.onerror = rej; r.readAsArrayBuffer(file); });
  }
  CC.ui = { fmt: fmt, save: save, showResult: showResult, readFile: readFile };
})(typeof globalThis !== 'undefined' ? globalThis : this);
