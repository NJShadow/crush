// Node: read engine-built archives with the share-file reader and compare
// every file against the original source bytes (SHA-256 of the whole file).
const load = require("./load");
const fs = require("fs"), path = require("path"), vm = require("vm"), crypto = require("crypto");
const { execFileSync } = require("child_process");
const ROOT = path.join(__dirname, "..");
const CR = load(["transforms.js", "container.js", "codecs.js"]);
globalThis.__CRUSH_WASM__ = {};
for (const m of ["zstd", "crushz", "lzma", "brotli", "bzip2", "crushcm", "crushpf", "crushjpg"])
  globalThis.__CRUSH_WASM__[m] = new Uint8Array(fs.readFileSync(path.join(ROOT, "codecs", m + ".wasm")));
const cc = execFileSync("python3", ["-c", "import sys; sys.path.insert(0,'tools'); import share_build as s; print(s.classic_codecs_script())"], { cwd: ROOT, encoding: "utf8", maxBuffer: 1 << 26 });
vm.runInThisContext(cc, { filename: "crush_codecs(classic).js" });
CR.priors = {};
for (const f of fs.readdirSync(path.join(ROOT, "..", "priors"))) CR.priors[f] = new Uint8Array(fs.readFileSync(path.join(ROOT, "..", "priors", f)));

(async () => {
  CR.codecsInit();
  let allBad = 0;
  for (const a of process.argv.slice(2)) {
    const info = JSON.parse(fs.readFileSync(a.replace(/\.crush$/, ".json")));
    let u8 = new Uint8Array(fs.readFileSync(a));
    const t0 = Date.now();
    if (CR.isEncrypted(u8)) {
      // wrong passphrase first: must be refused, and nothing decoded
      try { await CR.decryptArchive(u8, "not the passphrase"); console.log("  WRONG PASSPHRASE ACCEPTED"); allBad++; }
      catch (e) { if (!(e instanceof CR.WrongPassword)) { console.log("  wrong passphrase gave", e.message); allBad++; } }
      u8 = await CR.decryptArchive(u8, info.password);
    }
    const tDec = Date.now() - t0;
    const idx = await CR.readIndex(u8);
    let ok = 0, bad = 0;
    await CR.extract(u8, idx, null, (i, r) => {
      const e = idx.files[i], want = info.files[e.p];
      if (r.ok && want && crypto.createHash("sha256").update(r.data).digest("hex") === want.sha) ok++;
      else { bad++; if (bad < 6) console.log("  FAIL", e.p, r.why); }
    });
    allBad += bad;
    console.log(`${path.basename(a)}: ${ok}/${idx.files.length} files byte-exact (${info.files ? Object.keys(info.files).length : "?"} expected), ` +
      `${Date.now() - t0} ms (decrypt ${tDec} ms); codecs ${JSON.stringify(CR.requirements(idx).codecs)} transforms ${JSON.stringify(CR.requirements(idx).transforms)}`);
    if (ok !== Object.keys(info.files).length) allBad++;
  }
  process.exit(allBad ? 1 : 0);
})().catch((e) => { console.log(e.stack); process.exit(2); });
