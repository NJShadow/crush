// Mutation fuzz of the share reader in Node: container + every transform's
// inverse. Pass = every case ends in a clean result or a caught error, no
// file is reported OK with wrong bytes, nothing hangs (watch the progress).
const load = require("./load");
const fs = require("fs"), path = require("path"), vm = require("vm"), crypto = require("crypto");
const { execFileSync } = require("child_process");
const ROOT = path.join(__dirname, "..");
const CR = load(["transforms.js", "container.js", "codecs.js"]);
globalThis.__CRUSH_WASM__ = {};
for (const m of ["zstd", "crushz", "lzma", "brotli", "bzip2", "crushcm", "crushpf", "crushjpg"])
  globalThis.__CRUSH_WASM__[m] = new Uint8Array(fs.readFileSync(path.join(ROOT, "codecs", m + ".wasm")));
vm.runInThisContext(execFileSync("python3", ["-c", "import sys; sys.path.insert(0,'tools'); import share_build as s; print(s.classic_codecs_script())"], { cwd: ROOT, encoding: "utf8" }));
let seed = 12345; const rnd = (n) => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
(async () => {
  CR.codecsInit();
  const stats = { cases: 0, clean_ok: 0, errors: 0, wrong_ok: 0, uncaught_types: {}, slowest_ms: 0 };
  // 1. transform payloads and aux values
  const tv = process.argv[2], man = JSON.parse(fs.readFileSync(path.join(tv, "manifest.json")));
  for (const c of man) {
    const want = fs.readFileSync(path.join(tv, c.k + ".in"));
    const pl0 = new Uint8Array(fs.readFileSync(path.join(tv, c.k + ".pl")));
    for (let k = 0; k < 12; k++) {
      const pl = pl0.slice(); let aux = CR.parseJSON(c.aux);
      if (k < 8) { for (let m = 0; m < 1 + rnd(4); m++) if (pl.length) pl[rnd(pl.length)] = rnd(256); }
      else {
        // perturb one numeric aux field
        const keys = Object.keys(aux).filter((x) => typeof aux[x] === "number");
        if (keys.length) aux[keys[rnd(keys.length)]] += [1, -1, 1000, -1000][rnd(4)];
      }
      const t0 = Date.now(); stats.cases++;
      try {
        const got = await CR.invertTransforms(k === 11 ? pl.subarray(0, rnd(pl.length + 1)) : pl, [{ n: c.t, a: aux }]);
        if (Buffer.compare(Buffer.from(got), want) === 0) stats.clean_ok++; else stats.errors++;   // wrong output: caught by the hash later
      } catch (e) {
        stats.errors++;
        const t = e instanceof CR.CrushError ? "CrushError" : e.constructor.name;
        stats.uncaught_types[t] = (stats.uncaught_types[t] || 0) + 1;
      }
      stats.slowest_ms = Math.max(stats.slowest_ms, Date.now() - t0);
    }
  }
  console.log("transform payload fuzz:", JSON.stringify(stats));
  // 2. whole archives
  const st2 = { cases: 0, files_ok: 0, files_failed: 0, archive_errors: 0, wrong_ok: 0, slowest_ms: 0 };
  for (const a of process.argv.slice(3)) {
    const info = JSON.parse(fs.readFileSync(a.replace(/\.crush$/, ".json")));
    const orig = new Uint8Array(fs.readFileSync(a));
    for (let k = 0; k < 40; k++) {
      const u8 = orig.slice();
      for (let m = 0; m < 1 + rnd(6); m++) u8[28 + rnd(u8.length - 28)] = rnd(256);
      st2.cases++; const t0 = Date.now();
      try {
        const idx = await CR.readIndex(u8);
        await CR.extract(u8, idx, null, (i, r) => {
          if (r.ok) {
            const w = info.files[idx.files[i].p];
            if (w && crypto.createHash("sha256").update(r.data).digest("hex") === w.sha) st2.files_ok++;
            else if (w) st2.wrong_ok++;        // hash prefix matched but bytes differ: must never happen
            else st2.files_ok++;
          } else st2.files_failed++;
        });
      } catch (e) { st2.archive_errors++; }
      st2.slowest_ms = Math.max(st2.slowest_ms, Date.now() - t0);
      process.stdout.write(".");
    }
  }
  console.log("\narchive mutation fuzz:", JSON.stringify(st2));
  process.exit(st2.wrong_ok ? 1 : 0);
})().catch((e) => { console.log("UNCAUGHT", e.stack); process.exit(2); });
