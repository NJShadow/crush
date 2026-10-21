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
