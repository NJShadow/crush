// Loads the share-file sources into a Node global, the same way the HTML does.
const fs = require("fs"), path = require("path"), vm = require("vm");
const SRC = path.join(__dirname, "..", "src");
module.exports = function load(extra) {
  globalThis.CR = {};
  CR.CRYPTO_WASM_B64 = fs.readFileSync(path.join(__dirname, "..", "crypto", "crush_crypto.wasm")).toString("base64");
  for (const f of ["bytes.js", "sha256.js", "crypt.js"].concat(extra || [])) {
    vm.runInThisContext(fs.readFileSync(path.join(SRC, f), "utf8"), { filename: f });
  }
  return globalThis.CR;
};
