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
