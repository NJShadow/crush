/* Crush share - adapter from the container reader to the WASM decoders
 * (crush_codecs.js, built by the 'wasm' teammate from Crush's own C sources:
 * zstd, LZMA SDK, Brotli, bzip2, crushcm, pinned zlib 1.3.1, preflate,
 * crushjpg). The page embeds only the modules its archive needs. */
(function (CR) {
"use strict";
const CC = () => {
  if (!globalThis.CC) throw new CR.Unavailable("this page carries no decoders");
  return globalThis.CC;
};
let ready = null;
CR.codecsInit = function (only) {
  if (!ready) ready = CC().init({ only });
  return ready;
};
CR.codecs = {
  async zstd(blob, maxOut) { await ready; return CC().zstd(blob, { maxOut }); },
  async zlib(blob, maxOut) { await ready; return CC().inflate(blob, { wbits: 15, maxOut }); },
  async decode(id, blob, prior, rawlen) {
    await ready;
    // +1: the LZMA2 decoder refuses an exact-size limit (see channels/share-to-wasm.md);
    // the reader checks the decoded length itself.
    return CC().decodeCodec(id, blob, prior, { maxOut: rawlen + 1 });
  },
};
let katPassed = null;
async function kat() {
  if (katPassed === null) {
    try { katPassed = await CC().selfTestDeflate(); } catch (e) { katPassed = false; }
  }
  if (!katPassed) throw new CR.Unavailable("this browser's copy of Crush's pinned zlib failed its known-answer test, so compressed streams cannot be rebuilt exactly here");
}
CR.native = {
  async deflate(idx, raw, wbits) { await ready; await kat(); return CC().zpinDeflate(idx, raw, wbits); },
  async pfReencode(pairs) { await ready; return pairs.map(([corr, raw]) => CC().preflateReencode(corr, raw)); },
  async jpegUnpack(blobs) { await ready; return blobs.map((b) => CC().jpegDecode(b)); },
};
})(globalThis.CR = globalThis.CR || {});
