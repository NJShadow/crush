/* Crush share - "save all" as one .zip.
 *
 * Stored (uncompressed) entries: the files are already verified bytes, every
 * OS can open a stored zip, and storing costs no time. Built as a Blob from
 * parts, so the browser can keep large files out of the JS heap. Names are
 * UTF-8 (general-purpose flag bit 11). Entries or totals past 4 GiB would
 * need ZIP64 and are refused with a sentence instead. */
(function (CR) {
"use strict";
let T = null;
function crcTable() {
  if (T) return T;
  T = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; T[n] = c >>> 0; }
  return T;
}
CR.crc32 = function (u8) {
  const t = crcTable(); let c = 0xFFFFFFFF;
  for (let i = 0; i < u8.length; i++) c = t[(c ^ u8[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
};
function dosTime(ms) {
  const d = new Date(ms);
  if (!(d.getFullYear() >= 1980)) return [0, 33];
  return [((d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1)) & 0xFFFF,
          (((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate()) & 0xFFFF];
}
/* entries: [{name, blob: Blob, size, crc, mtime: seconds}] -> Blob */
CR.makeZip = function (entries) {
  const parts = [], central = [];
  let off = 0;
  for (const e of entries) {
    const name = CR.utf8(e.name), size = e.size;
    if (size >= 0xFFFFFFFF || off >= 0xFFFFFFFF) throw new CR.CrushError("too large for one zip (over 4 GB) - save the files one at a time", "limit");
    const crc = e.crc >>> 0;
    const [tm, dt] = dosTime((e.mtime || 0) * 1000);
    const lh = new DataView(new ArrayBuffer(30));
    lh.setUint32(0, 0x04034b50, true); lh.setUint16(4, 20, true); lh.setUint16(6, 0x0800, true);
    lh.setUint16(8, 0, true); lh.setUint16(10, tm, true); lh.setUint16(12, dt, true);
    lh.setUint32(14, crc, true); lh.setUint32(18, size, true); lh.setUint32(22, size, true);
    lh.setUint16(26, name.length, true); lh.setUint16(28, 0, true);
    parts.push(lh.buffer, name, e.blob);
    const ch = new DataView(new ArrayBuffer(46));
    ch.setUint32(0, 0x02014b50, true); ch.setUint16(4, 20, true); ch.setUint16(6, 20, true);
    ch.setUint16(8, 0x0800, true); ch.setUint16(10, 0, true); ch.setUint16(12, tm, true); ch.setUint16(14, dt, true);
    ch.setUint32(16, crc, true); ch.setUint32(20, size, true); ch.setUint32(24, size, true);
    ch.setUint16(28, name.length, true); ch.setUint32(38, 0, true); ch.setUint32(42, off, true);
    central.push(ch.buffer, name);
    off += 30 + name.length + size;
  }
  let csize = 0; for (const c of central) csize += c.byteLength || c.length;
  if (entries.length >= 0xFFFF || off >= 0xFFFFFFFF) throw new CR.CrushError("too many or too large files for one zip - save them one at a time", "limit");
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, entries.length, true); end.setUint16(10, entries.length, true);
  end.setUint32(12, csize, true); end.setUint32(16, off, true);
  return new Blob(parts.concat(central, [end.buffer]), { type: "application/zip" });
};
})(globalThis.CR = globalThis.CR || {});
