/* Crush share bootstrap: the reader below is stored raw-deflated to keep the
 * page small. This inflates it with the browser's own DecompressionStream,
 * checks its SHA-256 against the value recorded when the page was made, and
 * runs it. Readable source: share/src in the Crush folder. */
(async function () {
  window.__crushBoot = 1;
  var el = document.getElementById("crush-code");
  function fail(msg) {
    var s = document.getElementById("status");
    if (s) { s.textContent = msg; s.className = "status bad"; }
    document.body.setAttribute("data-state", "error");
  }
  try {
    if (typeof DecompressionStream !== "function") return fail("This browser is too old to open this page. Any current Chrome, Edge, Firefox or Safari can.");
    var t = "", n = el.childNodes;
    for (var i = 0; i < n.length; i++) t += n[i].data;
    // the code blob is ASCII-escaped raw bytes: undo the four escapes
    var b = new Uint8Array(t.length), o = 0;
    for (var j = 0; j < t.length; j++) {
      var c = t.charCodeAt(j);
      if (c > 255) { var m = [0x20AC,0x81,0x201A,0x192,0x201E,0x2026,0x2020,0x2021,0x2C6,0x2030,0x160,0x2039,0x152,0x8D,0x17D,0x8F,0x90,0x2018,0x2019,0x201C,0x201D,0x2022,0x2013,0x2014,0x2DC,0x2122,0x161,0x203A,0x153,0x9D,0x17E,0x178].indexOf(c); if (m < 0) return fail("This page was damaged when it was saved or sent (its text encoding was changed). Ask for it again."); c = 0x80 + m; }
      if (el.getAttribute("data-enc") === "raw" && c === 0x7E) { var e = t.charCodeAt(++j); c = e === 0x30 ? 0 : e === 0x72 ? 13 : e === 0x6C ? 60 : 126; }
      b[o++] = c;
    }
    b = b.subarray(0, o);
    if (el.getAttribute("data-enc") !== "raw") b = Uint8Array.from(atob(t.replace(/\s+/g, "")), function (x) { return x.charCodeAt(0); });
    var code = await new Response(new Blob([b]).stream().pipeThrough(new DecompressionStream("deflate-raw"))).arrayBuffer();
    if (window.crypto && crypto.subtle) {
      var h = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", code)), function (x) { return (x < 16 ? "0" : "") + x.toString(16); }).join("");
      if (h !== el.getAttribute("data-sha256")) return fail("This page's program was damaged in transit. Ask for it again.");
    }
    var s = document.createElement("script");
    s.textContent = new TextDecoder("ascii").decode(code);
    document.body.appendChild(s);
    if (document.readyState !== "loading") document.dispatchEvent(new Event("DOMContentLoaded"));
  } catch (err) { fail("This page could not start: " + (err && err.message || err)); }
})();
