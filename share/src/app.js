/* Crush share - the page itself.
 *
 * Everything archive-derived (file names above all) is put on the page with
 * textContent, never as HTML: an archive is a file someone else made. */
(function (CR) {
"use strict";
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

/* ---------------- embedded binary ---------------- */
// windows-1252 bytes 0x80-0x9F decode to these code points (WHATWG index);
// the five undefined bytes decode to U+0081, U+008D, U+008F, U+0090, U+009D.
const W1252 = [0x20AC, 0x81, 0x201A, 0x192, 0x201E, 0x2026, 0x2020, 0x2021, 0x2C6, 0x2030, 0x160, 0x2039, 0x152, 0x8D, 0x17D, 0x8F,
  0x90, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014, 0x2DC, 0x2122, 0x161, 0x203A, 0x153, 0x9D, 0x17E, 0x178];
const BACK = new Int16Array(0x2200).fill(-1);
for (let b = 0; b < 256; b++) BACK[b] = b;
for (let i = 0; i < 32; i++) BACK[W1252[i]] = 0x80 + i;
const UNESC = { 0x30: 0x00, 0x72: 0x0D, 0x6C: 0x3C, 0x7E: 0x7E };

function readBinary(node) {
  const enc = node.getAttribute("data-enc"), len = +node.getAttribute("data-len");
  const out = new Uint8Array(len);
  let o = 0;
  if (enc === "raw") {
    let esc = false;
    for (const t of node.childNodes) {
      const s = t.data;
      for (let i = 0; i < s.length; i++) {
        const c = s.charCodeAt(i);
        const b = c < 0x2200 ? BACK[c] : -1;
        if (b < 0) throw new CR.CrushError("this page's embedded data was changed by the way it was saved or delivered (it was re-read as a different text encoding)", "transport");
        if (esc) { const u = UNESC[b]; if (u === undefined) throw new CR.CrushError("this page's embedded data is damaged", "transport"); out[o++] = u; esc = false; }
        else if (b === 0x7E) esc = true;
        else { if (o >= len) throw new CR.CrushError("this page's embedded data is longer than it should be", "transport"); out[o++] = b; }
      }
    }
  } else {
    let carry = "";
    for (const t of node.childNodes) {
      const s = carry + t.data.replace(/[^A-Za-z0-9+/=]/g, "");
      const cut = s.length - (s.length % 4);
      const bin = atob(s.slice(0, cut));
      for (let i = 0; i < bin.length; i++) out[o++] = bin.charCodeAt(i);
      carry = s.slice(cut);
    }
    if (carry) throw new CR.CrushError("this page's embedded data is damaged", "transport");
  }
  if (o !== len) throw new CR.CrushError("this page's embedded data is shorter than it should be - it was cut off somewhere along the way", "transport");
  return out;
}

/* Decoders and priors are stored raw-deflated; the browser's own
 * DecompressionStream inflates them (Chrome 80+, Firefox 113+, Safari 16.4+),
 * and each is checked against the SHA-256 recorded when the page was made. */
async function readSupport(node) {
  let u8 = readBinary(node);
  if (node.getAttribute("data-z") === "deflate-raw") {
    if (typeof DecompressionStream !== "function")
      throw new CR.CrushError("this browser is too old to open this page (it lacks DecompressionStream); any current Chrome, Edge, Firefox or Safari can", "browser");
    const ds = new Blob([u8]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    u8 = new Uint8Array(await new Response(ds).arrayBuffer());
    if (u8.length !== +node.getAttribute("data-rawlen") || CR.hex(await CR.sha256(u8)) !== node.getAttribute("data-sha256"))
      throw new CR.CrushError("one of this page's built-in decoders is damaged - the file was altered in transit", "transport");
  }
  return u8;
}

/* ---------------- formatting ---------------- */
function size(n) {
  if (n < 1024) return n + " bytes";
  const u = ["KB", "MB", "GB"]; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < 2);
  return (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)) + " " + u[i];
}
function plural(n, one, many) { return n + " " + (n === 1 ? one : (many || one + "s")); }
function status(text, kind) { const s = $("status"); if (kind === "bad" && text) text = text[0].toUpperCase() + text.slice(1).replace(/\.?$/, "."); s.textContent = text; s.className = "status" + (kind ? " " + kind : ""); }
function progress(frac) {
  const p = $("progress");
  if (frac === null) { p.hidden = true; return; }
  p.hidden = false; p.value = Math.max(0, Math.min(1, frac));
}

/* ---------------- state ---------------- */
const S = { meta: null, archive: null, plain: null, idx: null, rows: [], results: [], checking: false };

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name; a.rel = "noopener";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function renderList() {
  const idx = S.idx, list = $("files");
  list.textContent = "";
  const order = idx.files.map((f, i) => i).sort((a, b) => idx.files[a].p.localeCompare(idx.files[b].p));
  let total = 0;
  for (const i of order) {
    const f = idx.files[i];
    total += f.s;
    const row = el("li", "file");
    const name = el("div", "fname");
    const parts = f.p.split("/");
    if (parts.length > 1) name.appendChild(el("span", "dir", parts.slice(0, -1).join("/") + "/"));
    name.appendChild(el("span", "base", parts[parts.length - 1]));
    const meta = el("div", "fmeta");
    meta.appendChild(el("span", "fsize", size(f.s)));
    const st = el("span", "chip wait", "not checked yet");
    meta.appendChild(st);
    const btn = el("button", "save", "Save");
    btn.disabled = true;
    btn.title = "Save " + CR.safeName(f.p) + " (" + size(f.s) + ")";
    btn.addEventListener("click", () => {
      const r = S.results[i];
      if (r && r.ok) saveBlob(r.blob, CR.safeName(f.p));
    });
    const why = el("div", "why"); why.hidden = true;
    row.append(name, meta, btn, why);
    list.appendChild(row);
    S.rows[i] = { row, st, btn, why };
  }
  const n = idx.files.length;
  $("summary").textContent = plural(n, "file") + ", " + size(total) + " in all" +
    " \u2014 packed into " + size(S.archive.length) + (S.meta.encrypted ? " (locked)" : "") + ".";
  $("listcard").hidden = false;
  $("saveall").textContent = n === 1 ? "Save " + CR.safeName(idx.files[0].p) + " (" + size(total) + ")" : "Save all " + n + " files as one .zip (" + size(total) + ")";
  $("saveall-note").textContent = n === 1
    ? "Your browser saves it to your Downloads folder, or asks you where, depending on its settings."
    : "The .zip holds every file that passed its check, in its folders, uncompressed (so any computer can open it). Your browser saves it to Downloads or asks where. You can also save files one at a time with the buttons above.";
}

async function check() {
  if (S.checking) return;
  S.checking = true;
  $("checkbtn").hidden = true;
  const n = S.idx.files.length;
  let done = 0, good = 0, bad = 0;
  status("Rebuilding and checking " + plural(n, "file") + "\u2026");
  progress(0);
  const t0 = performance.now();
  await CR.extract(S.plain, S.idx, null, (i, r) => {
    const R = S.rows[i];
    done++;
    if (r.ok) {
      const blob = new Blob([r.data], { type: "application/octet-stream" });
      S.results[i] = { ok: true, blob, size: r.data.length, crc: CR.crc32(r.data) };
      R.st.className = "chip ok"; R.st.textContent = r.dupOf ? "checked \u2713 (duplicate)" : "checked \u2713";
      R.btn.disabled = false; good++;
    } else {
      S.results[i] = { ok: false, why: r.why };
      R.st.className = "chip bad"; R.st.textContent = "could not be rebuilt";
      R.why.hidden = false; R.why.textContent = "Not saved: " + r.why + "."; bad++;
    }
    progress(done / n);
  }, (p) => { if (p.stage === "bundle") status("Rebuilding and checking " + plural(n, "file") + "\u2026 (part " + (p.i + 1) + " of " + p.n + ")"); });
  progress(null);
  const secs = ((performance.now() - t0) / 1000).toFixed(1);
  S.plain = S.meta.encrypted ? null : S.plain;     // decrypted bytes are not kept once files are out
  if (!bad) status("All " + plural(n, "file") + " rebuilt and checked against their fingerprints in " + secs + " s. They are exactly the files that were packed.", "good");
  else status(good + " of " + n + " files are exactly as packed. " + plural(bad, "file") + " could not be rebuilt here \u2014 the reason is shown next to each.", "warn");
  $("saveall").disabled = good === 0;
  $("saveall").hidden = false;
  if (bad && n > 1) $("saveall-note").textContent = "The .zip will hold the " + plural(good, "file") + " that passed their check; the " + plural(bad, "file") + " marked in red will not be in it.";
  document.body.dataset.state = "checked";
  document.body.dataset.good = String(good);
  document.body.dataset.bad = String(bad);
}

function saveAll() {
  const idx = S.idx;
  const ok = idx.files.map((f, i) => [f, S.results[i]]).filter(([, r]) => r && r.ok);
  if (!ok.length) return;
  if (idx.files.length === 1) { saveBlob(ok[0][1].blob, CR.safeName(ok[0][0].p)); return; }
  try {
    const zip = CR.makeZip(ok.map(([f, r]) => ({ name: CR.safePath(f.p), blob: r.blob, size: r.size, crc: r.crc, mtime: f.m })));
    saveBlob(zip, (S.meta.zipname || "crush-files") + ".zip");
  } catch (e) { status(e.message, "bad"); }
}

async function afterOpen() {
  S.idx = await CR.readIndex(S.plain);
  const req = CR.requirements(S.idx);
  const unknown = req.transforms.filter((t) => !CR.TRANSFORMS[t]);
  renderList();
  details(req, unknown);
  let total = 0; for (const f of S.idx.files) total += f.s;
  if (total <= (S.meta.autocheck || 200 * 1048576)) await check();
  else {
    status("Ready. Checking rebuilds every file in this browser and compares it with the fingerprint recorded when it was packed. For " + size(total) + " that can take a while and needs about that much memory.");
    $("checkbtn").hidden = false;
    document.body.dataset.state = "ready";
  }
}

function details(req, unknown) {
  const d = $("tech");
  const add = (k, v) => { const r = el("div", "kv"); r.append(el("span", "k", k), el("span", "v", v)); d.appendChild(r); };
  add("Archive", size(S.archive.length) + ", SHA-256 " + S.meta.sha256);
  if (S.idx) {
    add("Made with", "Crush, " + (S.idx.tier || "?") + " tier" + (S.idx.created ? ", " + new Date(S.idx.created * 1000).toLocaleString() : ""));
    add("Codecs", req.codecs.join(", "));
    add("Transforms", req.transforms.length ? req.transforms.join(", ") : "none");
    if (unknown.length) add("Not openable here", unknown.join(", "));
  }
  add("Decoders in this page", (S.meta.modules || []).join(", ") || "none");
  add("Encryption", S.meta.encrypted ? "Argon2id + XChaCha20-Poly1305 (Monocypher 4.0.2, in WebAssembly)" : "none");
  add("Network", "none \u2014 this page makes no requests; it works offline");
}

async function unlock(ev) {
  ev.preventDefault();
  const pw = $("pw").value;
  if (!pw) { status("Type the passphrase first.", "warn"); return; }
  $("unlockbtn").disabled = true; $("pw").disabled = true;
  try {
    S.plain = await CR.decryptArchive(S.archive, pw, (stage, i, n) => {
      if (stage === "kdf" && i === 0) status("Checking the passphrase\u2026 (this is deliberately slow)");
      if (stage === "decrypt") { status("Passphrase accepted. Decrypting and checking every part of the archive\u2026"); progress(i / n); }
    });
    $("pw").value = "";
    progress(null);
    $("lockcard").hidden = true;
    await afterOpen();
  } catch (e) {
    progress(null);
    $("unlockbtn").disabled = false; $("pw").disabled = false;
    if (e instanceof CR.WrongPassword) { status("That passphrase is not the right one (or the file is damaged). Nothing was decrypted. Try again.", "bad"); $("pw").select(); }
    else status(CR.explain(e), "bad");
    document.body.dataset.state = "unlock-failed";
  }
}

async function main() {
  try {
    S.meta = JSON.parse($("crush-meta").textContent);
    if ($("crush-canary")) {
      const can = readBinary($("crush-canary"));
      for (let b = 0; b < 256; b++) if (can[b] !== b) throw new CR.CrushError("this page's embedded data was changed by the way it was saved or delivered (it was re-read as a different text encoding)", "transport");
    }
    const aparts = Array.from(document.querySelectorAll("script[data-archive-part]"));
    if (aparts.length !== S.meta.parts) throw new CR.CrushError("this page is incomplete - part of its archive is missing", "transport");
    if (aparts.length === 1) S.archive = readBinary(aparts[0]);
    else {
      S.archive = new Uint8Array(S.meta.size);
      let o = 0;
      for (let k = 0; k < aparts.length; k++) {
        if (+aparts[k].getAttribute("data-archive-part") !== k) throw new CR.CrushError("this page's archive parts are out of order", "transport");
        const b = readBinary(aparts[k]);
        if (o + b.length > S.archive.length) throw new CR.CrushError("this page's embedded data is longer than it should be", "transport");
        S.archive.set(b, o); o += b.length;
        aparts[k].textContent = "";            // the text copy is no longer needed
      }
      if (o !== S.archive.length) throw new CR.CrushError("this page's embedded data is shorter than it should be - it was cut off somewhere along the way", "transport");
    }
    const got = CR.hex(await CR.sha256(S.archive));
    if (got !== S.meta.sha256) throw new CR.CrushError("this page's embedded archive does not match its fingerprint - the file was damaged in transit. Ask for it again.", "transport");
    const W = globalThis.__CRUSH_WASM__ || (globalThis.__CRUSH_WASM__ = {});
    for (const n of document.querySelectorAll("script[data-wasm]")) W[n.getAttribute("data-wasm")] = await readSupport(n);
    CR.priors = {};
    for (const n of document.querySelectorAll("script[data-prior]")) CR.priors[n.getAttribute("data-prior")] = await readSupport(n);
    const cw = document.querySelector("script[data-crypto]");
    if (cw) CR.cryptoWasm = await readSupport(cw);
    if (S.meta.modules && S.meta.modules.length) CR.codecsInit(S.meta.modules);
    $("archsize").textContent = size(S.archive.length);
    if (CR.isEncrypted(S.archive)) {
      S.meta.encrypted = true;
      const h = CR.headerInfo(S.archive);
      if (!h.supported) throw new CR.CrushError(h.why || "this archive's lock is not one this page can open", "header");
      $("lockcost").textContent = "Unlocking needs about " + Math.round(h.m / 1024) + " MB of memory and takes a few seconds on a computer, longer on a phone. That cost is deliberate: it is what makes guessing passphrases slow for anyone else.";
      $("lockcard").hidden = false;
      status("Locked: file names and contents stay hidden until the right passphrase is entered.");
      document.body.dataset.state = "locked";
      $("pw").focus();
    } else {
      S.plain = S.archive;
      await afterOpen();
    }
  } catch (e) {
    status(CR.explain(e), "bad");
    document.body.dataset.state = "error";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("unlockform").addEventListener("submit", unlock);
  $("showpw").addEventListener("change", (e) => { $("pw").type = e.target.checked ? "text" : "password"; });
  $("saveall").addEventListener("click", saveAll);
  $("checkbtn").addEventListener("click", check);
  main();
});
CR.app = S;
})(globalThis.CR = globalThis.CR || {});
