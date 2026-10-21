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
