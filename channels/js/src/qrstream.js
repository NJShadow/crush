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
