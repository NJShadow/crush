// Every engine-encoded codec blob must decode byte-exactly through the WASM decoders.
const fs = require("fs"), path = require("path"), vm = require("vm");
const { execFileSync } = require("child_process");
const ROOT = path.join(__dirname, "..");
globalThis.__CRUSH_WASM__ = {};
for (const m of ["zstd", "crushz", "lzma", "brotli", "bzip2", "crushcm", "crushpf", "crushjpg"])
  globalThis.__CRUSH_WASM__[m] = new Uint8Array(fs.readFileSync(path.join(ROOT, "codecs", m + ".wasm")));
vm.runInThisContext(execFileSync("python3", ["-c", "import sys; sys.path.insert(0,'tools'); import share_build as s; print(s.classic_codecs_script())"], { cwd: ROOT, encoding: "utf8" }));
(async () => {
  await CC.init();
  const dir = process.argv[2], man = JSON.parse(fs.readFileSync(path.join(dir, "manifest.json")));
  const per = {}; let bad = 0;
  for (const c of man) {
    const want = fs.readFileSync(path.join(dir, c.k + ".in")), blob = new Uint8Array(fs.readFileSync(path.join(dir, c.k + ".blob")));
    const prior = c.prior ? new Uint8Array(fs.readFileSync(path.join(ROOT, "..", "priors", c.prior))) : null;
    let ok = false, t0 = Date.now();
    try { ok = Buffer.compare(Buffer.from(CC.decodeCodec(c.codec, blob, prior, { maxOut: want.length + 1 })), want) === 0; }
    catch (e) { console.log("ERR", c.codec, c.input, e.message); }
    const ms = Date.now() - t0;
    per[c.codec] = per[c.codec] || { ok: 0, n: 0, ms: 0 }; per[c.codec].n++; per[c.codec].ms += ms; if (ok) per[c.codec].ok++; else bad++;
  }
  for (const [k, v] of Object.entries(per)) console.log(`${k.padEnd(10)} ${v.ok}/${v.n}  ${v.ms} ms`);
  console.log(bad ? `FAILURES: ${bad}` : "all codec cases byte-exact");
})();
