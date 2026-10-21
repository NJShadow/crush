// Every (original, payload, aux) triple Python produced must invert exactly in JS.
const load = require("./load"); const CR = load(["transforms.js"]);
const fs = require("fs"), path = require("path");
const dir = process.argv[2];
const man = JSON.parse(fs.readFileSync(path.join(dir, "manifest.json")));
(async () => {
  let ok = 0, bad = 0; const per = {};
  for (const c of man) {
    const want = fs.readFileSync(path.join(dir, c.k + ".in"));
    const pl = new Uint8Array(fs.readFileSync(path.join(dir, c.k + ".pl")));
    const aux = CR.parseJSON(c.aux);
    const t0 = Date.now();
    let got, err = null;
    try { got = await CR.invertTransforms(pl, [{ n: c.t, a: aux }]); } catch (e) { err = e; }
    const ms = Date.now() - t0;
    const pass = !err && Buffer.compare(Buffer.from(got.buffer, got.byteOffset, got.length), want) === 0;
    per[c.t] = per[c.t] || [0, 0]; per[c.t][pass ? 0 : 1]++;
    if (pass) ok++; else {
      bad++;
      let where = "";
      if (!err) { let i = 0; while (i < want.length && i < got.length && want[i] === got[i]) i++; where = ` first diff at ${i} (len ${got.length} vs ${want.length})`; }
      console.log("FAIL", c.k, c.name, err ? err.stack.split("\n").slice(0, 3).join(" | ") : where);
    }
    if (ms > 2000) console.log(`  slow: ${c.name} ${ms} ms for ${want.length} bytes`);
  }
  console.log(JSON.stringify(per));
  console.log(`transforms: ${ok}/${ok + bad} byte-exact`);
  process.exit(bad ? 1 : 0);
})();
