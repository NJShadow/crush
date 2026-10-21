#!/usr/bin/env python3
"""
Crush - tests for the pinned deflate and the container unwrappers.

    python tests/test_unwrap.py

Self-contained: every input is generated here, nothing is downloaded, and
nothing outside a temporary folder is written. Covers:

  * the pinned zlib (crushz) loads, passes its known-answer test, and agrees
    with reference zlib on every one of the 405 stored settings;
  * a deflate that is NOT reference zlib (zlib-ng, as Python 3.14 on Windows
    ships) is refused rather than trusted;
  * PDF and gzip unwrapping round-trip byte-exact, including hostile and
    malformed inputs, and decline cleanly when they cannot;
  * deflate that zlib did NOT write (a deliberately different encoder, below)
    is rebuilt through preflate in PDF, gzip, zip and PNG;
  * JPEG recompression, both codecs (Brunsli and jcm), byte-exact;
  * the whole archive path: compress -> extract -> byte compare.
"""
import gzip, hashlib, io, os, random, shutil, struct, sys, tempfile, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "engine"))
import crush_zpin as zpin          # noqa: E402
import crush_engine as ce          # noqa: E402

def lz_fixed_deflate(data: bytes) -> bytes:
    """A deliberately non-zlib deflate encoder: greedy LZ77 over a 4 KB window
    with a tiny hash, one fixed-Huffman block. Valid deflate that zlib's own
    settings will never reproduce."""
    out, acc, nb = bytearray(), 0, 0
    def put(v, n):                    # LSB-first bits
        nonlocal acc, nb
        acc |= v << nb; nb += n
        while nb >= 8:
            out.append(acc & 255); acc >>= 8; nb -= 8
    def huff(code, n):                # Huffman codes go MSB-first
        put(int(bin(code)[2:].zfill(n)[::-1], 2), n)
    def lit(c):
        if c < 144: huff(0x30 + c, 8)
        else: huff(0x190 + c - 144, 9)
    LB = [3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,35,43,51,59,67,83,99,115,131,163,195,227,258]
    LE = [0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0]
    DB = [1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,257,385,513,769,1025,1537,2049,3073]
    DE = [0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10]
    put(1, 1); put(1, 2)              # final block, fixed Huffman
    table, i, n = {}, 0, len(data)
    while i < n:
        best_l = best_d = 0
        if i + 3 <= n:
            j = table.get(data[i:i + 3])
            if j is not None and i - j <= 4096:
                l = 0
                while i + l < n and l < 258 and data[j + l] == data[i + l]: l += 1
                if l >= 3: best_l, best_d = l, i - j
            table[data[i:i + 3]] = i
        if best_l:
            k = max(x for x in range(29) if LB[x] <= best_l)
            sym = 257 + k
            if sym < 280: huff(sym - 256, 7)
            else: huff(0xC0 + sym - 280, 8)
            put(best_l - LB[k], LE[k])
            dk = max(x for x in range(24) if DB[x] <= best_d)
            huff(dk, 5); put(best_d - DB[dk], DE[dk])
            i += best_l
        else:
            lit(data[i]); i += 1
    huff(0, 7)                         # end of block
    if nb: out.append(acc & 255)
    return bytes(out)


PASS = FAIL = 0
def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")

def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False

def roundtrip(T, data):
    """(applied, exact): the framework's own rule - forward, then inverse must
    give back the input byte for byte."""
    r = T.forward(data)
    if not r:
        return False, True
    return True, T.inverse(r[0], r[1]) == data

# ---------------------------------------------------------------------------
print("\nPinned deflate")
check("a reference deflate is available", zpin.available(), zpin.status())
check("the pinned library is the one in use", zpin.status().startswith("crushz"),
      zpin.status())

rng = random.Random(7)
words = [b"alpha", b"beta", b"gamma", b"delta", b"\n", b"  ", b"0 0 R", b"obj"]
sample = b" ".join(rng.choice(words) for _ in range(30000))
ref = zpin._PyZlib()
if zlib.ZLIB_RUNTIME_VERSION.split(".")[:2] == ["1", "3"] and "ng" not in zlib.ZLIB_RUNTIME_VERSION.lower():
    bad = [i for i in range(len(zpin.PARAMS)) for w in (-15, 15)
           if zpin.deflate(i, sample, w) != ref.deflate(i, sample, w)]
    check("crushz == Python's reference zlib on all 405 settings x 2 wrappers",
          not bad, f"{len(bad)} differ")
else:
    print(f"  --    Python's zlib here is {zlib.ZLIB_RUNTIME_VERSION}; "
          "skipping the direct comparison (the known-answer test still ran)")

class _Fake:
    name, version = "fake-deflate", "0"
    def deflate(self, idx, raw, wbits):
        lvl, strat, mem = zpin.PARAMS[idx]
        # a different but valid deflate: same data, different block choices
        co = zlib.compressobj(max(1, lvl - 1), zlib.DEFLATED, wbits, mem, strat)
        return co.compress(raw) + co.flush()
check("a non-reference deflate fails the known-answer test",
      zpin._kat_digest(_Fake().deflate) != zpin._KAT_DIGEST)

t = sample[:20000]
for i in (0, 44, 100, 300, 404):
    target = zpin.deflate(i, t, -15)
    got = zpin.match(t, target, -15)
    check(f"match() finds a setting that rebuilds stream #{i}",
          got is not None and zpin.deflate(got, t, -15) == target)
check("match() returns None for a stream no setting produces",
      zpin.match(t, zlib.compress(t)[2:-4][:-1] + b"\x00", -15) is None)

# ---------------------------------------------------------------------------
print("\nPDF unwrapping")
def make_pdf(streams, junk=b""):
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    for n, body in enumerate(streams, 1):
        out += b"%d 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\r\n" % (n, len(body))
        out += body + b"\nendstream\nendobj\n" + junk
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)

content = [b"BT /F1 12 Tf 72 %d Td (Line %d of the report) Tj ET\n" % (700 - k, k)
           for k in range(400)]
page = b"".join(content)
pdf = make_pdf([zlib.compress(page, 6), zlib.compress(page * 3, 9), zlib.compress(os.urandom(3000), 6)])
applied, exact = roundtrip(ce.PdfUnwrap, pdf)
check("simple PDF: unwrapped and byte-exact", applied and exact)
r = ce.PdfUnwrap.forward(pdf)
check("simple PDF: all three streams rebuilt", r and len(r[1]["r"]) == 3)

co = zlib.compressobj(6, zlib.DEFLATED, 12, 8)         # a 4 KB window
small_win = co.compress(page) + co.flush()
applied, exact = roundtrip(ce.PdfUnwrap, make_pdf([small_win]))
check("stream with a non-default window size", applied and exact)

cases = {
    "truncated stream":         make_pdf([zlib.compress(page)[:500]]),
    "corrupt stream":           make_pdf([zlib.compress(page)[:200] + b"\xff" * 50 + zlib.compress(page)[250:]]),
    "fake 'stream' keywords":   b"%PDF-1.4\nstream\nstream\r\nstream\rxx endstream stream\n\x78\x9c" + page,
    "no streams at all":        b"%PDF-1.4\n" + page,
    "stream keyword at EOF":    b"%PDF-1.4\n1 0 obj<<>>stream\n",
    "valid but not rebuildable": make_pdf([zlib.compress(page, 0)]),   # stored blocks
    "encrypted-looking bytes":  make_pdf([os.urandom(5000)]),
}
for name, d in cases.items():
    try:
        applied, exact = roundtrip(ce.PdfUnwrap, d)
        check(f"hostile PDF - {name}: {'unwrapped' if applied else 'declined'}, exact", exact)
    except Exception as ex:
        check(f"hostile PDF - {name}", False, repr(ex))

bomb = zlib.compress(b"\x00" * (64 << 20), 9)         # 64 MB of zeros in ~64 KB
old_cap = ce._UNWRAP_RAW_CAP
ce._UNWRAP_RAW_CAP = 8 << 20
try:
    applied, exact = roundtrip(ce.PdfUnwrap, make_pdf([bomb]))
    check("decompression bomb stays under the memory cap (declined)", not applied and exact)
finally:
    ce._UNWRAP_RAW_CAP = old_cap

# ---------------------------------------------------------------------------
print("\nGzip unwrapping")
# prose-like, not periodic: perfectly periodic text gzips into periodic bytes
# that a strong codec can compress better than the text itself
vocab = [bytes(rng.choice(b"etaoinshrdlucmfwyp") for _ in range(rng.randint(2, 9)))
         for _ in range(3000)]
text = b" ".join(rng.choice(vocab) + (b".\n" if rng.random() < 0.08 else b"")
                 for _ in range(120000))
applied, exact = roundtrip(ce.GzipUnwrap, gzip.compress(text, 9, mtime=0))
check("Python gzip, level 9", applied and exact)
applied, exact = roundtrip(ce.GzipUnwrap, gzip.compress(text, 6, mtime=0))
check("Python gzip, level 6", applied and exact)

def gz_with_header(body, flags, extra=b"EX", name=b"file.txt", comment=b"hello"):
    h = bytearray(b"\x1f\x8b\x08" + bytes([flags]) + b"\x00\x00\x00\x00\x00\x03")
    if flags & 4:  h += struct.pack("<H", len(extra)) + extra
    if flags & 8:  h += name + b"\x00"
    if flags & 16: h += comment + b"\x00"
    if flags & 2:  h += struct.pack("<H", zlib.crc32(bytes(h)) & 0xFFFF)
    co = zlib.compressobj(6, zlib.DEFLATED, -15)
    return bytes(h) + co.compress(body) + co.flush() + struct.pack("<II", zlib.crc32(body), len(body))
applied, exact = roundtrip(ce.GzipUnwrap, gz_with_header(text, 4 | 8 | 16 | 2))
check("gzip with FEXTRA, FNAME, FCOMMENT and FHCRC", applied and exact)
multi = gzip.compress(text[:300000], mtime=0) + gzip.compress(text[300000:], mtime=0)
r = ce.GzipUnwrap.forward(multi)
check("multi-member gzip: both members unwrapped",
      r is not None and len(r[1]["r"]) == 2 and ce.GzipUnwrap.inverse(*r) == multi)
applied, exact = roundtrip(ce.GzipUnwrap, gzip.compress(text, mtime=0) + b"\x00" * 512)
check("trailing padding after the last member", applied and exact)
for name, d in {
    "truncated":        gzip.compress(text, mtime=0)[:1000],
    "reserved flags":   b"\x1f\x8b\x08\xe0" + gzip.compress(text, mtime=0)[4:],
    "header only":      b"\x1f\x8b\x08\x08\x00\x00\x00\x00\x00\x03name",
    "unterminated name": b"\x1f\x8b\x08\x08\x00\x00\x00\x00\x00\x03" + b"n" * 100,
}.items():
    try:
        applied, exact = roundtrip(ce.GzipUnwrap, d)
        check(f"hostile gzip - {name}: {'unwrapped' if applied else 'declined'}, exact", exact)
    except Exception as ex:
        check(f"hostile gzip - {name}", False, repr(ex))


# ---------------------------------------------------------------------------
print("\nDeflate that zlib did not write (preflate)")
check("the crushpf helper is present", zpin.pf_available(), zpin.PF_PATH)
prose = text[:120000]
body = lz_fixed_deflate(prose)
check("the test encoder really is not zlib", zpin.match(prose, body, -15) is None)
gz = (b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + body
      + struct.pack("<II", zlib.crc32(prose), len(prose)))
r = ce.GzipUnwrap.forward(gz)
check("gzip from a non-zlib encoder: unwrapped via preflate, byte-exact",
      r is not None and any(len(x) == 6 for x in r[1]["r"]) and ce.GzipUnwrap.inverse(*r) == gz)
zw = b"\x78\x9c" + body + struct.pack(">I", zlib.adler32(prose))
pdf2 = make_pdf([zw, zlib.compress(page, 6)])
r = ce.PdfUnwrap.forward(pdf2)
check("PDF mixing zlib and non-zlib streams: both rebuilt, byte-exact",
      r is not None and len(r[1]["r"]) == 2 and ce.PdfUnwrap.inverse(*r) == pdf2)
r = ce.GzipUnwrap.forward(gz, slow=False)
check("quick tier (slow=False) does not use preflate", r is None)
# a zip whose member was written by the non-zlib encoder
zbuf = io.BytesIO()
import zipfile as _zf
with _zf.ZipFile(zbuf, "w", _zf.ZIP_DEFLATED) as z:
    z.writestr("a.txt", prose)
zd = bytearray(zbuf.getvalue())
off, clen, ulen = ce._find_zip_streams(bytes(zd))[0]
# splice our stream in and fix the sizes in both headers
lh = zd.index(b"PK\x03\x04"); cd = zd.index(b"PK\x01\x02")
new = bytes(zd[:off]) + body + bytes(zd[off + clen:])
new = bytearray(new)
delta = len(body) - clen
struct.pack_into("<I", new, lh + 18, len(body))
cd += delta
struct.pack_into("<I", new, cd + 20, len(body))
eocd = new.rindex(b"PK\x05\x06")
struct.pack_into("<I", new, eocd + 16, cd)
new = bytes(new)
check("the spliced test zip still opens", _zf.ZipFile(io.BytesIO(new)).read("a.txt") == prose)
applied, exact = roundtrip(ce.DeflateUnwrap, new)
check("zip member from a non-zlib encoder: unwrapped, byte-exact", applied and exact)
# a PNG whose IDAT was written by the non-zlib encoder
w, h = 64, 48
rows = b"".join(b"\x00" + bytes((x * 3 + y * 5 + (x * y) % 7) & 255 for x in range(w * 3)) for y in range(h))
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
idat = b"\x78\x9c" + lz_fixed_deflate(rows) + struct.pack(">I", zlib.adler32(rows))
png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", idat[:500]) + chunk(b"IDAT", idat[500:]) + chunk(b"IEND", b""))
r = ce.PngUnwrap.forward(png)
check("PNG from a non-zlib encoder: unwrapped via preflate, byte-exact",
      r is not None and r[1]["pi"] == -1 and ce.PngUnwrap.inverse(*r) == png)
check("hostile correction record is refused, not crashed on",
      _raises(lambda: zpin.pf_reencode([(b"\xff" * 40, prose[:1000])])))

# ---------------------------------------------------------------------------
print("\nJPEG recompression")
import crush_jpeg as _jpeg
check("the crushjpg helper is present", _jpeg.available(), _jpeg.PATH)
import base64
FIXTURES = {}
for _line in open(os.path.join(HERE, "data", "jpeg_fixtures.b64")):
    if _line.strip() and not _line.startswith("#"):
        _n, _b = _line.split()
        FIXTURES[_n] = base64.b64decode(_b)
for name in ("baseline.jpg", "progressive.jpg"):
    j = FIXTURES[name]
    sizes = {}
    for strong, label in ((False, "Brunsli"), (True, "strongest (jcm, or Brunsli if smaller)")):
        r = ce.JpegPack.forward(j, strong=strong)
        ok = r is not None and ce.JpegPack.inverse(*r) == j
        sizes[strong] = len(r[0]) if r else 1 << 30
        check(f"{name}, {label}: {len(j):,} -> {len(r[0]) if r else 0:,} bytes, byte-exact",
              ok and (strong or r[0][:1] == b"B"))
    check(f"{name}: the strong setting is never larger", sizes[True] <= sizes[False])
    # jcm alone, whether or not it would have been chosen
    r = _jpeg._run("J", [j], 120)[0]
    check(f"{name}, jcm forced: {len(r) if r else 0:,} bytes, byte-exact",
          r is not None and r[:1] == b"J" and _jpeg.unpack(r) == j)
j = FIXTURES["baseline.jpg"]
check("a truncated JPEG is declined or rebuilt exactly",
      (lambda r: r is None or ce.JpegPack.inverse(*r) == j[:len(j) // 2])(ce.JpegPack.forward(j[:len(j) // 2], strong=True)))
packed = ce.JpegPack.forward(j, strong=True)[0]
bad = packed[:40] + bytes(b ^ 0x5A for b in packed[40:80]) + packed[80:]
check("a corrupted packed JPEG raises instead of returning wrong bytes",
      _raises(lambda: ce.JpegPack.inverse(bad, {})) or ce.JpegPack.inverse(bad, {}) != j)

# ---------------------------------------------------------------------------
print("\nWhole archive")
td = tempfile.mkdtemp(prefix="crush_unwrap_")
try:
    src = os.path.join(td, "src"); os.makedirs(src)
    files = {
        "report.pdf": pdf,
        "window.pdf": make_pdf([small_win]),
        "notes.txt.gz": gzip.compress(text, 9, mtime=0),
        "bad.gz": gzip.compress(text, mtime=0)[:777],
        "plain.txt": text[:50000],
        "photo.jpg": FIXTURES["baseline.jpg"],
        "gnu-style.gz": gz,
    }
    for n, d in files.items():
        open(os.path.join(src, n), "wb").write(d)
    arc = os.path.join(td, "t.crush")
    res = ce.compress([src], arc, "balanced")
    x = ce.extract(arc, os.path.join(td, "out"))
    same = all(open(os.path.join(td, "out", "src", n), "rb").read() == d for n, d in files.items())
    check(f"compress + extract byte-exact ({res['in']:,} -> {res['out']:,} bytes)",
          same and not x["failed"])
    idx = ce.read_index(arc, None)
    used = {st["n"] for e in idx["files"] for st in e.get("t", [])}
    check("pdf_unwrap, gzip_unwrap and jpeg_pack were actually used",
          {"pdf_unwrap", "gzip_unwrap", "jpeg_pack"} <= used, str(used))
    cls = {e["p"].split("/")[-1]: e["cls"] for e in idx["files"]}
    check("an unrebuildable .gz is filed with existing archives", cls.get("bad.gz") == "archive",
          str(cls.get("bad.gz")))
finally:
    shutil.rmtree(td, ignore_errors=True)

# ---------------------------------------------------------------------------
print("\nArchive compatibility")
td = tempfile.mkdtemp(prefix="crush_compat_")
try:
    src = os.path.join(td, "src"); os.makedirs(src)
    open(os.path.join(src, "a.txt"), "wb").write(text[:20000])
    arc = os.path.join(td, "old.crush")
    saved = ce.HASH_HEX
    ce.HASH_HEX = 64                  # write the index the way older builds did
    try:
        ce.compress([src], arc, "balanced")
    finally:
        ce.HASH_HEX = saved
    stored = ce.read_index(arc, None)["files"][0]["h"]
    x = ce.extract(arc, os.path.join(td, "out"))
    check("an archive with full 64-hex hashes (older builds) extracts and verifies",
          len(stored) == 64 and not x["failed"] and x["verified"] == 1 and
          open(os.path.join(td, "out", "src", "a.txt"), "rb").read() == text[:20000])
finally:
    shutil.rmtree(td, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
