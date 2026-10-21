#!/usr/bin/env python3
# ============================================================================
#  Crush - content-aware compression engine
#  ---------------------------------------------------------------------------
#  Crush does not have "a" compression algorithm. It has a router.
#
#  Every file is identified by structure, not by extension alone. A reversible
#  TRANSFORM is then applied that exposes the file's real content to a codec -
#  a .docx is unwrapped into the XML it actually contains, a JPEG is re-coded
#  into JPEG XL's stronger entropy model, a WAV is turned into FLAC residuals,
#  a float array is transposed so its exponent bytes line up. Files of the same
#  class are then compressed together in one solid bundle, and a tournament of
#  codecs runs on each bundle, keeping whichever output is smallest.
#
#  THE REVERSIBILITY GUARANTEE
#  Every transform is inverted and byte-compared against the original at
#  compression time, before anything is written. A transform that does not
#  reproduce the input exactly is discarded and the file falls back to plain
#  compression. Crush therefore cannot ship a transform it has not already
#  proven correct on that exact file. Each file's SHA-256 is stored and
#  re-verified on extraction.
# ============================================================================
from __future__ import annotations

import os, io, sys, json, time, zlib, lzma, bz2, struct, hashlib, shutil, subprocess, tempfile
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import zstandard as zstd
except ImportError:
    zstd = None
try:
    import brotli
except ImportError:
    brotli = None

from crush_oracle import Oracle
from crush_prism import Prism
from crush_perceptual import PerceptualImage, PerceptualAudio
import crush_echo as echo

# ---- v4 domain modules. Optional: a missing one costs those transforms only.
try:
    import crush_vector                      # float / numeric array codec
except Exception:
    crush_vector = None
try:
    import crush_genome as _genome           # FASTQ / FASTA / VCF / SAM
except Exception:
    _genome = None
try:
    import crush_loom                        # structure discovery in unknown binaries
except Exception:
    crush_loom = None
try:
    import crush_crypt                          # optional encryption
except Exception:
    crush_crypt = None
_GENOMIC = ("fastq", "fasta", "vcf", "sam")


def is_encrypted(path):
    return crush_crypt is not None and crush_crypt.is_encrypted(path)


class _Locked:
    """Raised through to the caller when an archive needs a passphrase."""



MAGIC = b"CRUSH\x00"
FORMAT_VERSION = 1
CRUSH_VERSION = "1.0.0"          # the release; FORMAT_VERSION is the archive format
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_CM_NAME = "crushcm.exe" if sys.platform.startswith("win") else "crushcm"
CRUSHCM = os.path.join(ENGINE_DIR, _CM_NAME)
if not os.path.exists(CRUSHCM):
    _alt = os.path.join(ENGINE_DIR, "..", "engine", _CM_NAME)
    if os.path.exists(_alt):
        CRUSHCM = os.path.abspath(_alt)

# --------------------------------------------------------------------------
#  Tiers
# --------------------------------------------------------------------------
TIERS = {
    "quick":     dict(label="Quick",     desc="Fast, everyday compression"),
    "balanced":  dict(label="Balanced",  desc="The default. Strong and still quick"),
    "crush":     dict(label="Crush",     desc="Maximum lossless. Runs the full codec tournament"),
    "obsession": dict(label="Obsession", desc="Adds the context-mixing engine. Slow, smallest"),
    "singularity": dict(label="Singularity",
                        desc="Everything: program synthesis, structural shredding, shared priors"),
}

_TOOLS_DIR = os.environ.get("CRUSH_TOOLS") or os.path.join(ENGINE_DIR, "..", "tools")

def _tool(name: str) -> Optional[str]:
    """Look in the app's own tools/ folder first, then on PATH. This lets a
    user enable JPEG XL or FLAC by dropping the executables in, with no
    system-wide install."""
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    local = os.path.abspath(os.path.join(_TOOLS_DIR, exe))
    if os.path.exists(local):
        return local
    return shutil.which(name)

_NN_NAME = "crushnn.exe" if sys.platform.startswith("win") else "crushnn"
CRUSHNN = os.path.join(ENGINE_DIR, _NN_NAME)

HAVE = {
    "cjxl":  _tool("cjxl"),  "djxl": _tool("djxl"),
    "flac":  _tool("flac"),
    "ffmpeg": _tool("ffmpeg"),
    "7z":    _tool("7z"),
    "crushcm": CRUSHCM if os.path.exists(CRUSHCM) else None,
    "crushnn": CRUSHNN if os.path.exists(CRUSHNN) else None,
}
PRIORS_DIR = os.environ.get("CRUSH_PRIORS") or os.path.join(ENGINE_DIR, "..", "priors")
PRIORS_DIR = os.path.abspath(PRIORS_DIR)

def _prior(name, ext):
    p = os.path.join(PRIORS_DIR, name + ext)
    return p if os.path.exists(p) else None

# which shared prior suits which kind of file
PRIOR_FOR_CLASS = {
    "text": "text", "code": "code", "pdf": "web", "zipfamily": "web",
    "db": "data", "numeric": None, "generic": None, "image": None,
    "jpeg": None, "png": None, "wav": None, "media": None, "archive": None, "gzip": None,
    "exe_pe": None, "exe_elf": None, "font": None,
}


# ==========================================================================
#  1. TYPE DETECTION
# ==========================================================================
ZIP_EXTS = {".zip",".docx",".xlsx",".pptx",".docm",".xlsm",".pptm",".odt",".ods",".odp",
            ".jar",".war",".apk",".ipa",".epub",".whl",".egg",".nupkg",".vsix",".crx",".xpi"}
PRECOMPRESSED_EXTS = {".7z",".rar",".gz",".tgz",".bz2",".xz",".zst",".lz4",".br",".cab",".arj",".lzh"}
MEDIA_LOSSY_EXTS = {".mp3",".aac",".m4a",".ogg",".opus",".wma",".mp4",".mkv",".avi",".mov",
                    ".webm",".m4v",".wmv",".flv",".heic",".webp",".avif"}
TEXTY_EXTS = {".txt",".md",".rst",".log",".csv",".tsv",".json",".xml",".html",".htm",".css",
              ".yaml",".yml",".ini",".cfg",".conf",".sql",".tex",".srt",".vtt",".po",".svg"}
CODE_EXTS = {".py",".c",".h",".cpp",".hpp",".cc",".cs",".java",".js",".ts",".jsx",".tsx",".go",
             ".rs",".rb",".php",".swift",".kt",".m",".mm",".pl",".sh",".bat",".ps1",".lua",
             ".r",".scala",".vb",".asm",".f90",".jl",".dart",".vue",".scss",".less"}

CLASS_LABELS = {
    "text":      "Text & documents",
    "code":      "Source code",
    "jpeg":      "JPEG photos",
    "png":       "PNG images",
    "image":     "Other images",
    "wav":       "Uncompressed audio",
    "media":     "Compressed media",
    "exe_pe":    "Windows programs",
    "exe_elf":   "Linux/Unix programs",
    "zipfamily": "Zip-based files",
    "archive":   "Existing archives",
    "gzip":      "Gzip files",
    "numeric":   "Numeric & scientific data",
    "genomic":   "Sequencing & variant data",
    "db":        "Databases",
    "pdf":       "PDF documents",
    "font":      "Fonts",
    "generic":   "Other files",
}

def sniff(path: str, head: bytes) -> str:
    ext = os.path.splitext(path)[1].lower()
    h = head
    if h[:2] == b"PK" and (ext in ZIP_EXTS or h[2:4] in (b"\x03\x04", b"\x05\x06", b"\x07\x08")):
        return "zipfamily"
    if h[:3] == b"\xff\xd8\xff":                      return "jpeg"
    if h[:8] == b"\x89PNG\r\n\x1a\n":                 return "png"
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":       return "wav"
    if h[:4] == b"fLaC":                              return "media"
    if h[:2] == b"MZ":                                return "exe_pe"
    if h[:4] == b"\x7fELF":                           return "exe_elf"
    if h[:4] == b"%PDF":                              return "pdf"
    if h[:16] == b"SQLite format 3\x00":              return "db"
    if h[:4] in (b"\x00\x01\x00\x00", b"OTTO") or h[:4] == b"true": return "font"
    if h[:3] == b"\x1f\x8b\x08":                     return "gzip"
    if h[:6] == b"7z\xbc\xaf\x27\x1c" or h[:4] == b"Rar!" or h[:2] == b"\x1f\x8b" \
       or h[:6] == b"\xfd7zXZ\x00" or h[:4] == b"\x28\xb5\x2f\xfd" or ext in PRECOMPRESSED_EXTS:
        return "archive"
    if h[:6] in (b"GIF87a", b"GIF89a") or h[:2] == b"BM" or h[:4] in (b"II*\x00", b"MM\x00*"):
        return "image"
    if ext in MEDIA_LOSSY_EXTS:                       return "media"
    # Sequencing and variant-call data: a 4-letter alphabet, a quality string
    # with completely different statistics, and templated identifiers. Coded as
    # one anonymous text stream this loses most of what is there.
    if _genome is not None and _genome.sniff(path, h[:1 << 16]) in _GENOMIC:
        return "genomic"
    if ext in CODE_EXTS:                              return "code"
    if ext in TEXTY_EXTS:                             return "text"
    if h:
        printable = sum(1 for b in h if 32 <= b < 127 or b in (9, 10, 13))
        if printable / len(h) > 0.92:
            return "code" if ext in CODE_EXTS else "text"
    return "generic"

# ==========================================================================
#  2. REVERSIBLE TRANSFORMS
#     forward() -> (payload_bytes, aux_dict) or None if not applicable
#     inverse() -> original bytes
#     Every one is verified before use.
# ==========================================================================

# ---- 2a. DEFLATE-stream unwrapping (zip / docx / xlsx / apk / jar / png) ---
# Rebuilding the ORIGINAL compressed bytes on extraction needs a deflate that
# will emit the same bytes on every machine, forever. Python's zlib is not
# that (3.14 on Windows is zlib-ng, which differs), so these transforms go
# through crush_zpin: a pinned zlib 1.3.1 that must pass a known-answer test
# before it is used. See crush_zpin.py for the full story.
import crush_zpin as zpin
_DEFLATE_PARAMS = zpin.PARAMS            # archives store indices into this

def _redeflate(raw: bytes, target: bytes, wbits: int, prefer=(),
               only_prefer: bool = False, exhaustive: bool = True) -> Optional[int]:
    """Find deflate settings that reproduce `target` exactly from `raw`, or
    None. Candidates are compressed incrementally and dropped at the first
    byte that differs, so a miss costs little."""
    return zpin.match(raw, target, wbits, prefer, only_prefer, exhaustive)

def _deflate_with(idx: int, raw: bytes, wbits: int) -> bytes:
    return zpin.deflate(idx, raw, wbits)

def _find_zip_streams(data: bytes):
    """Locate every stored DEFLATE payload via the CENTRAL DIRECTORY, which
    carries true sizes even when local headers defer them to a data descriptor
    (as LibreOffice, Java and many streaming writers do)."""
    import zipfile as _zf
    out = []
    try:
        z = _zf.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except Exception:
        return []
    n = len(data)
    for i in infos:
        if i.compress_type != 8 or i.compress_size <= 0:
            continue
        p = i.header_offset
        if p + 30 > n or data[p:p+4] != b"PK\x03\x04":
            continue
        nlen, elen = struct.unpack_from("<HH", data, p + 26)
        start = p + 30 + nlen + elen
        if start + i.compress_size <= n:
            out.append((start, i.compress_size, i.file_size))
    out.sort()
    # drop any overlaps (defensive)
    clean, last_end = [], 0
    for (o, c, u) in out:
        if o >= last_end:
            clean.append((o, c, u)); last_end = o + c
    return clean

# ---- 2b. Embedded zlib/deflate streams: PDF and gzip ------------------------
# Both formats are "plain bytes with deflate streams spliced in". The shared
# machinery below finds each stream, decompresses it, proves which pinned
# deflate setting rebuilds it byte for byte, and swaps the compressed bytes for
# the raw ones. Everything between streams - headers, dictionaries, trailers,
# padding, even junk - is carried verbatim, so nothing about the surrounding
# format has to be understood correctly for the round-trip to be exact.
import re as _re

_UNWRAP_RAW_CAP = 768 << 20       # never expand one file past this in memory
_INFLATE_STEP = 1 << 18

def _inflate_at(mv, start: int, wbits: int, budget: int):
    """Decompress one stream beginning at `start`. Returns (consumed, raw) or
    None if there is no complete, valid stream there. The output budget stops
    a decompression bomb before it can allocate its way through the machine."""
    d = zlib.decompressobj(wbits)
    n = len(mv)
    p, parts, got = start, [], 0
    try:
        while not d.eof:
            if d.unconsumed_tail:
                buf = d.unconsumed_tail
            else:
                if p >= n:
                    return None
                buf = mv[p:p + _INFLATE_STEP]
                p += len(buf)
            out = d.decompress(buf, budget - got + 1)
            parts.append(out)
            got += len(out)
            if got > budget:
                return None
    except zlib.error:
        return None
    consumed = p - start - len(d.unused_data) - len(d.unconsumed_tail)
    return consumed, b"".join(parts)


class _SpliceUnwrap:
    """Base for transforms whose payload is: the original file with each
    deflate stream replaced by its decompressed contents.

    Two ways to rebuild a stream, tried in this order:
      1. the pinned zlib, when one of its 405 settings reproduces the stream
         exactly - costs a few bytes of recipe;
      2. preflate (crushpf), for deflate that zlib did not write - costs a
         correction record, typically 2-3% of the compressed stream, stored at
         the end of the payload so it does not break up the unwrapped data.
    Recipe rows:
      [off, clen, rlen, setting, wbits]          rebuilt by zlib
      [off, clen, rlen, -1, -15, corr_len]       rebuilt by preflate (raw deflate)
      [off, clen, rlen, setting]                 zip rows written before v5"""
    MIN_STREAM = 16
    EXHAUSTIVE = True                     # try all 405 settings on a zlib miss

    @classmethod
    def _spans(cls, data: bytes, mv, budget_fn):
        raise NotImplementedError

    @classmethod
    def forward(cls, data: bytes, slow: bool = True):
        if not zpin.available():
            return None                       # never unwrap what we could not rebuild
        use_pf = slow and zpin.pf_available()
        mv = memoryview(data)
        rows = []                             # (off, clen, raw, row[, correction])
        pf_q = []                             # indices into rows awaiting preflate
        zhits = misses = total_raw = 0
        recent = []                           # zlib settings that hit, newest first
        t_start = time.time()
        last_end = 0
        def budget():
            return _UNWRAP_RAW_CAP - total_raw
        for (off, clen, raw, wbits) in cls._spans(data, mv, budget):
            if off < last_end or clen < cls.MIN_STREAM:
                continue
            # once zlib has missed 8 times without a single hit, this producer
            # is not zlib: stop paying for the search
            zdead = misses >= 8 and zhits == 0
            if zdead and not use_pf:
                return None
            cheap = misses >= 32 or (time.time() - t_start) > 20
            pi = None
            if not zdead and not (cheap and not recent):
                pi = _redeflate(raw, bytes(mv[off:off + clen]), wbits,
                                prefer=recent, only_prefer=cheap,
                                exhaustive=cls.EXHAUSTIVE)
            if pi is not None:
                zhits += 1
                if pi in recent:
                    recent.remove(pi)
                recent.insert(0, pi)
                rows.append((off, clen, raw, [off, clen, len(raw), pi, wbits]))
            else:
                misses += 1
                if not use_pf:
                    if cheap and not recent:
                        break
                    continue
                # preflate works on raw deflate: a zlib wrapper's 2-byte header
                # and 4-byte checksum stay behind as ordinary bytes
                ioff, iclen = (off + 2, clen - 6) if wbits > 0 else (off, clen)
                if iclen < max(1, cls.MIN_STREAM):
                    continue
                pf_q.append(len(rows))
                rows.append((ioff, iclen, raw, None))
            last_end = off + clen
            total_raw += len(raw)
        if pf_q:
            res = zpin.pf_decode([bytes(mv[rows[k][0]:rows[k][0] + rows[k][1]]) for k in pf_q])
            for k, r in zip(pf_q, res):
                ioff, iclen, raw, _ = rows[k]
                # a correction record bigger than half the stream means the
                # model did not understand this encoder - not worth storing
                if r is not None and r[0] == raw and len(r[1]) <= iclen // 2:
                    rows[k] = (ioff, iclen, raw, [ioff, iclen, len(raw), -1, -15, len(r[1])], r[1])
            rows = [r for r in rows if r[3] is not None]
        if not rows or sum(len(r[2]) for r in rows) < 1024:
            return None
        out, recipe, tail, cursor = bytearray(), [], bytearray(), 0
        for r in rows:
            off, clen, raw, row = r[0], r[1], r[2], r[3]
            out += mv[cursor:off]
            out += raw
            recipe.append(row)
            if len(r) > 4:
                tail += r[4]
            cursor = off + clen
        out += mv[cursor:]
        out += tail
        return bytes(out), {"r": recipe}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        rows = aux["r"]
        dtotal = sum(r[5] for r in rows if len(r) == 6)
        body = payload[:len(payload) - dtotal] if dtotal else payload
        tail = payload[len(payload) - dtotal:] if dtotal else b""
        # pass 1: locate every stream's raw bytes; batch the preflate ones
        spans, src, prev_end, dpos, pf_pairs = [], 0, 0, 0, []
        for r in rows:
            off, clen, rlen = r[0], r[1], r[2]
            src += off - prev_end
            if len(r) == 6:
                pf_pairs.append((tail[dpos:dpos + r[5]], body[src:src + rlen]))
                dpos += r[5]
            spans.append((off, clen, src, rlen))
            src += rlen
            prev_end = off + clen
        rebuilt = iter(zpin.pf_reencode(pf_pairs))
        # pass 2: assemble
        out, src = bytearray(), 0
        for r, (off, clen, s, rlen) in zip(rows, spans):
            out += body[src:s]
            if len(r) == 6:
                comp = next(rebuilt)
            else:
                wbits = r[4] if len(r) >= 5 else -15
                comp = _deflate_with(r[3], body[s:s + rlen], wbits)
            if len(comp) != clen:
                raise ValueError("rebuilt stream has the wrong length")
            out += comp
            src = s + rlen
        out += body[src:]
        return bytes(out)


class DeflateUnwrap(_SpliceUnwrap):
    """Replaces every DEFLATE payload inside a zip container (docx, xlsx, jar,
    apk, epub...) with its decompressed bytes, leaving all headers and
    directory structures untouched. This is what lets a strong codec finally
    see the XML inside a .docx. Members zlib did not write (Microsoft Office,
    7-Zip and others) go through preflate."""
    name = "deflate_unwrap"
    MIN_STREAM = 0
    BUDGET_STREAMS = 65536                # the C matcher makes per-stream cost small

    @classmethod
    def _spans(cls, data, mv, budget):
        streams = _find_zip_streams(data)
        if len(streams) > cls.BUDGET_STREAMS:
            return
        for (off, clen, ulen) in streams:
            if ulen > budget():
                continue
            try:
                raw = zlib.decompress(mv[off:off + clen], -15)
            except Exception:
                continue
            if len(raw) != ulen and ulen != 0xFFFFFFFF:
                continue
            yield off, clen, raw, -15


class PngUnwrap:
    """Splits a PNG into its chunk skeleton plus the raw filtered scanline
    stream. The scanlines are hugely more compressible than the DEFLATE bytes
    that normally hide them."""
    name = "png_unwrap"

    @staticmethod
    def _chunks(d: bytes):
        p = 8
        while p + 8 <= len(d):
            ln = struct.unpack_from(">I", d, p)[0]
            typ = d[p+4:p+8]
            if p + 12 + ln > len(d):
                break
            yield p, ln, typ, d[p+8:p+8+ln], d[p+8+ln:p+12+ln]
            p += 12 + ln

    @staticmethod
    def forward(data: bytes, slow: bool = True):
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        idat_parts, skeleton, idat_span = [], bytearray(), None
        cursor = 0
        first_idat = None
        for (p, ln, typ, body, crc) in PngUnwrap._chunks(data):
            if typ == b"IDAT":
                if first_idat is None:
                    first_idat = p
                    skeleton += data[cursor:p]
                idat_parts.append((ln, body, crc))
                cursor = p + 12 + ln
        if not idat_parts:
            return None
        comp = b"".join(b for (_, b, _) in idat_parts)
        try:
            raw = zlib.decompress(comp)
        except Exception:
            return None
        aux = {"fi": first_idat, "sz": [ln for (ln, _, _) in idat_parts],
               "crc": [c.hex() for (_, _, c) in idat_parts]}
        skeleton += data[cursor:]
        aux["sk"] = skeleton.hex()
        # the zlib header states the encoder's window size; honour it
        wb = (comp[0] >> 4) + 8 if len(comp) > 2 and 9 <= (comp[0] >> 4) + 8 <= 15 else 15
        pi = _redeflate(raw, comp, wb)
        if pi is not None:
            aux["pi"] = pi
            if wb != 15:
                aux["wb"] = wb
            return bytes(raw), aux
        # Not written by zlib (Photoshop, optimisers such as zopflipng, ...):
        # rebuild the inner deflate with preflate. The stream must end exactly
        # at the end of the IDAT data for the header/checksum split to hold.
        if not (slow and zpin.pf_available()) or len(comp) < 16:
            return None
        d = zlib.decompressobj()
        try:
            d.decompress(comp)
        except zlib.error:
            return None
        if not d.eof or d.unused_data:
            return None
        r = zpin.pf_decode([comp[2:-4]])[0]
        if r is None or r[0] != raw or len(r[1]) > len(comp) // 2:
            return None
        aux["pi"] = -1
        aux["zw"] = (comp[:2] + comp[-4:]).hex()
        aux["dl"] = len(r[1])
        return bytes(raw) + r[1], aux

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        sk = bytes.fromhex(aux["sk"])
        if aux["pi"] == -1:
            dl = aux["dl"]
            raw, corr = payload[:len(payload) - dl], payload[len(payload) - dl:]
            zw = bytes.fromhex(aux["zw"])
            comp = zw[:2] + zpin.pf_reencode([(corr, raw)])[0] + zw[2:]
        else:
            comp = _deflate_with(aux["pi"], payload, aux.get("wb", 15))
        out = bytearray(sk[:aux["fi"]])
        o = 0
        for i, ln in enumerate(aux["sz"]):
            out += struct.pack(">I", ln) + b"IDAT" + comp[o:o+ln] + bytes.fromhex(aux["crc"][i])
            o += ln
        out += sk[aux["fi"]:]
        return bytes(out)

class PdfUnwrap(_SpliceUnwrap):
    """Every FlateDecode stream in a PDF - page content, fonts, images, object
    and cross-reference streams - replaced by its decompressed bytes. The PDF's
    own structure is not parsed: a stream is any `stream` keyword followed by
    a valid zlib stream, and anything that does not rebuild exactly is left as
    it was. Encrypted PDFs simply find nothing to unwrap."""
    name = "pdf_unwrap"
    _KW = _re.compile(rb"stream(?:\r\n|\n|\r)")

    @classmethod
    def _spans(cls, data, mv, budget):
        if data[:5] != b"%PDF-" and b"%PDF-" not in data[:1024]:
            return
        skip_to = 0
        for m in cls._KW.finditer(data):
            s = m.end()
            if s < skip_to or data[max(0, m.start() - 3):m.start()] == b"end":
                continue
            if s + 2 > len(data):
                return
            cmf, flg = data[s], data[s + 1]
            if (cmf & 0x0F) != 8 or (cmf >> 4) > 7 or (cmf * 256 + flg) % 31 or flg & 0x20:
                continue
            r = _inflate_at(mv, s, 15, budget())
            if r is None:
                continue
            clen, raw = r
            skip_to = s + clen
            yield s, clen, raw, (cmf >> 4) + 8


class GzipUnwrap(_SpliceUnwrap):
    """A .gz / .tgz / .svgz file's deflate body replaced by its contents, so the
    codec tournament sees the tar or text inside instead of entropy. Headers
    and CRC trailers are carried verbatim; multi-member files work.

    Files from zlib (Python, git archive, PyPI sdists) rebuild from a zlib
    setting. Files from GNU gzip (and so `tar czf`) never match zlib - its
    deflate is an older sibling that makes different block decisions - and go
    through preflate instead. Because every GNU gzip file misses the zlib
    search, gzip only tries the settings real zlib producers use (zpin.COMMON)
    before handing over."""
    name = "gzip_unwrap"
    EXHAUSTIVE = False

    @classmethod
    def _spans(cls, data, mv, budget):
        n, p = len(data), 0
        while p + 18 <= n and data[p:p + 3] == b"\x1f\x8b\x08":
            flg = data[p + 3]
            if flg & 0xE0:
                return
            q = p + 10
            try:
                if flg & 4:
                    q += 2 + struct.unpack_from("<H", data, q)[0]
                if flg & 8:
                    q = data.index(b"\x00", q) + 1
                if flg & 16:
                    q = data.index(b"\x00", q) + 1
            except (ValueError, struct.error):
                return
            if flg & 2:
                q += 2
            if q >= n:
                return
            r = _inflate_at(mv, q, -15, budget())
            if r is None:
                return
            clen, raw = r
            yield q, clen, raw, -15
            p = q + clen + 8                    # CRC32 + ISIZE


import crush_jpeg as _jpeg

class JpegPack:
    """Lossless JPEG recompression with the built-in crushjpg helper. The same
    DCT coefficients, coded with a far stronger model than JPEG's Huffman
    tables; the original file is rebuilt byte for byte. Needs nothing
    installed. Balanced tier uses Brunsli (fast); Crush tier and above use
    jcm, Crush's own context-mixing model (about 5x slower, markedly
    smaller). Competes with jpegxl when cjxl happens to be present."""
    name = "jpeg_pack"

    @staticmethod
    def forward(data: bytes, strong: bool = False):
        if data[:3] != b"\xff\xd8\xff":
            return None
        out = _jpeg.pack(data, strong)
        if out is None or len(out) >= len(data):
            return None
        return out, {}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        return _jpeg.unpack(payload)

class EmbeddedJpeg:
    """JPEGs inside other files - photos in a PDF (DCTDecode streams), in a
    .docx/.pptx/.epub once the zip layer is unwrapped - each recompressed by
    crushjpg exactly as a standalone .jpg would be. Runs as a second stage
    after the container unwrap. Packed images go to the end of the payload
    so they do not interrupt the text around them."""
    name = "jpeg_embedded"

    @staticmethod
    def forward(data: bytes, strong: bool = False):
        spans = _jpeg.find_jpegs(data)
        if not spans:
            return None
        packed = _jpeg.pack_many([data[a:b] for a, b in spans], strong)
        rows, body, tail, cur = [], bytearray(), bytearray(), 0
        for (a, b), pk in zip(spans, packed):
            if pk is None or len(pk) >= b - a:
                continue
            body += data[cur:a]
            rows.append([a, b - a, len(pk)])
            tail += pk
            cur = b
        if not rows:
            return None
        body += data[cur:]
        return bytes(body + tail), {"r": rows}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        rows = aux["r"]
        ptotal = sum(r[2] for r in rows)
        body, tail = payload[:len(payload) - ptotal], payload[len(payload) - ptotal:]
        blobs, q = [], 0
        for r in rows:
            blobs.append(tail[q:q + r[2]]); q += r[2]
        jpegs = _jpeg.unpack_many(blobs)
        out, src, prev = bytearray(), 0, 0
        for (off, jlen, _), jp in zip(rows, jpegs):
            if len(jp) != jlen:
                raise ValueError("rebuilt embedded JPEG has the wrong length")
            gap = off - prev
            out += body[src:src + gap]; src += gap
            out += jp
            prev = off + jlen
        out += body[src:]
        return bytes(out)

class JpegXL:
    """Lossless JPEG -> JPEG XL transcode. Re-codes the exact same DCT
    coefficients with a far stronger entropy coder. Bit-exact: djxl rebuilds
    the original JPEG byte for byte."""
    name = "jpegxl"

    @staticmethod
    def forward(data: bytes):
        if not (HAVE["cjxl"] and HAVE["djxl"]) or data[:3] != b"\xff\xd8\xff":
            return None
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a.jpg"); dst = os.path.join(td, "a.jxl")
            open(src, "wb").write(data)
            r = subprocess.run([HAVE["cjxl"], src, dst, "--lossless_jpeg=1", "-e", "8"],
                               capture_output=True, timeout=600)
            if r.returncode != 0 or not os.path.exists(dst):
                return None
            out = open(dst, "rb").read()
        if len(out) >= len(data):
            return None
        return out, {}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a.jxl"); dst = os.path.join(td, "a.jpg")
            open(src, "wb").write(payload)
            subprocess.run([HAVE["djxl"], src, dst], capture_output=True, timeout=600)
            return open(dst, "rb").read()

class FlacWav:
    """WAV -> FLAC. Linear prediction plus Rice coding on the residual, which
    a byte-oriented compressor cannot approximate."""
    name = "flac"

    @staticmethod
    def forward(data: bytes):
        if not HAVE["flac"] or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a.wav"); dst = os.path.join(td, "a.flac")
            open(src, "wb").write(data)
            r = subprocess.run([HAVE["flac"], "-8", "-f", "-s", "--totally-silent",
                                "--keep-foreign-metadata-if-present", "-o", dst, src],
                               capture_output=True, timeout=1800)
            if r.returncode != 0 or not os.path.exists(dst):
                return None
            out = open(dst, "rb").read()
        if len(out) >= len(data):
            return None
        return out, {}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a.flac"); dst = os.path.join(td, "a.wav")
            open(src, "wb").write(payload)
            subprocess.run([HAVE["flac"], "-d", "-f", "-s", "--totally-silent",
                            "--keep-foreign-metadata-if-present", "-o", dst, src],
                           capture_output=True, timeout=1800)
            return open(dst, "rb").read()


class AudioPredict:
    """Lossless linear-prediction transform for PCM WAV, in pure NumPy.

    Audio samples are strongly correlated with their neighbours, which a
    byte-oriented compressor cannot exploit at all. This does what FLAC's front
    end does - inter-channel decorrelation, then a fixed polynomial predictor
    chosen per block - but stops short of entropy coding, leaving the residuals
    for whichever codec wins the tournament. That keeps the win available with
    no external encoder installed, and often beats FLAC because LZMA/CM code
    the residuals better than Rice coding does.
    """
    name = "audio_predict"
    BLOCK = 4096

    @staticmethod
    def _parse(d: bytes):
        if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
            return None
        p, fmt, data = 12, None, None
        while p + 8 <= len(d):
            cid = d[p:p+4]
            sz = struct.unpack_from("<I", d, p+4)[0]
            body = p + 8
            if cid == b"fmt ":
                fmt = struct.unpack_from("<HHIIHH", d, body)
            elif cid == b"data":
                data = (body, min(sz, len(d) - body))
                break
            p = body + sz + (sz & 1)
        if not fmt or not data:
            return None
        return fmt, data

    @staticmethod
    def _predict(x, order):
        import numpy as np
        if order == 0: return x.copy()
        r = x.copy()
        for _ in range(order):
            r = np.diff(r, prepend=r[0] * 0)
        return r

    @staticmethod
    def _unpredict(r, order):
        import numpy as np
        x = r.copy()
        for _ in range(order):
            x = np.cumsum(x)
        return x

    @staticmethod
    def forward(data: bytes):
        import numpy as np
        pr = AudioPredict._parse(data)
        if not pr:
            return None
        (afmt, ch, rate, brate, align, bits), (doff, dlen) = pr
        if afmt != 1 or bits != 16 or ch not in (1, 2):
            return None
        n = (dlen // (2 * ch)) * (2 * ch)
        if n < 8192:
            return None
        a = np.frombuffer(data[doff:doff+n], dtype="<i2").astype(np.int64).reshape(-1, ch)
        chans = [a[:, 0]] + ([a[:, 1] - a[:, 0]] if ch == 2 else [])
        orders, res = [], []
        B = AudioPredict.BLOCK
        for c in chans:
            out = np.empty_like(c)
            for s in range(0, len(c), B):
                blk = c[s:s+B]
                best, bo = None, 0
                for o in range(0, 5):
                    r = AudioPredict._predict(blk, o)
                    cost = int(np.abs(r).sum())
                    if best is None or cost < best:
                        best, bo, br = cost, o, r
                orders.append(bo)
                out[s:s+B] = br
            res.append(out)
        r = np.concatenate(res)
        z = (r << 1) ^ (r >> 63)                       # zigzag
        if z.max() > 0xFFFFFFFF:
            return None
        z32 = z.astype("<u4")
        planes = z32.view(np.uint8).reshape(-1, 4).T.tobytes()
        aux = {"o": bytes(orders).hex(), "n": n, "d": doff, "ch": ch, "ns": len(chans[0])}
        payload = data[:doff] + planes + data[doff+n:]
        return payload, aux

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        import numpy as np
        doff, n, ch, ns = aux["d"], aux["n"], aux["ch"], aux["ns"]
        orders = list(bytes.fromhex(aux["o"]))
        total = ns * ch
        planes = payload[doff:doff + total * 4]
        z32 = np.frombuffer(planes, dtype=np.uint8).reshape(4, total).T.tobytes()
        z = np.frombuffer(z32, dtype="<u4").astype(np.int64)
        r = (z >> 1) ^ (-(z & 1))                       # un-zigzag
        B = AudioPredict.BLOCK
        chans, oi, off = [], 0, 0
        for _ in range(ch):
            cr = r[off:off+ns]; off += ns
            out = np.empty_like(cr)
            for s in range(0, ns, B):
                out[s:s+B] = AudioPredict._unpredict(cr[s:s+B], orders[oi]); oi += 1
            chans.append(out)
        if ch == 2:
            l = chans[0]; rr = chans[1] + l
            inter = np.empty(ns * 2, dtype=np.int64)
            inter[0::2] = l; inter[1::2] = rr
        else:
            inter = chans[0]
        raw = inter.astype("<i2").tobytes()
        return payload[:doff] + raw + payload[doff + total * 4:]

class ByteTranspose:
    """Byte-plane transposition (optionally after delta coding). Numeric arrays
    interleave their bytes: [m0 m1 m2 e0][m0 m1 m2 e0]... Transposing gathers
    all the slow-moving exponent bytes into one highly repetitive plane."""
    name = "transpose"

    @staticmethod
    def _apply(data: bytes, w: int, delta: bool) -> bytes:
        """Byte-plane transpose, optionally after a modular ZIGZAG delta.

        The delta must be done in unsigned modular arithmetic and then zigzagged.
        The obvious implementation - subtract as signed, store as signed - is a
        trap: a delta of -1 becomes 0xFFFFFFFF, so the sign extension fills the
        three high byte-planes with 0xFF whenever the sequence ticks downward,
        and those planes are exactly the ones transposition was supposed to make
        constant. Zigzag maps -1 to 1 and keeps the high planes at zero, which is
        worth 28% on a random walk. It also removes the int64 overflow that made
        8-byte delta mode throw instead of compressing."""
        import numpy as np
        n = (len(data) // w) * w
        head, tail = data[:n], data[n:]
        dt = np.dtype(f"<u{w}")
        if not delta:
            a = np.frombuffer(head, dtype=np.uint8).reshape(-1, w)
            return a.T.tobytes() + tail
        v = np.frombuffer(head, dtype=dt)
        d = np.empty_like(v)
        if len(v):
            d[0] = v[0]
            d[1:] = v[1:] - v[:-1]                 # wraps, mod 2^(8w)
        bits = np.uint64(8 * w - 1)
        top = (d >> dt.type(bits)).astype(dt)      # 0 or 1
        z = (np.left_shift(d, dt.type(1)) ^ (dt.type(0) - top)).astype(dt)
        return z.view(np.uint8).reshape(-1, w).T.tobytes() + tail

    @staticmethod
    def _unapply(data: bytes, w: int, delta: bool, orig_len: int,
                 zig: bool = True) -> bytes:
        import numpy as np
        n = (orig_len // w) * w
        rows = n // w
        head, tail = data[:n], data[n:]
        dt = np.dtype(f"<u{w}")
        a = np.frombuffer(head, dtype=np.uint8).reshape(w, rows).T
        if not delta:
            return a.tobytes() + tail
        z = np.frombuffer(a.tobytes(), dtype=dt)
        if zig:
            d = (np.right_shift(z, dt.type(1)) ^ (dt.type(0) - (z & dt.type(1)))).astype(dt)
        else:
            d = z
        v = np.add.accumulate(d, dtype=dt)         # wraps, mod 2^(8w)
        return v.tobytes() + tail

    @staticmethod
    def forward(data: bytes):
        if zstd is None or len(data) < 65536:
            return None
        # decide on a sample, but with a strong enough codec that the verdict
        # matches what the real tournament will find
        sample = data[:4 << 20]
        cc = zstd.ZstdCompressor(level=12)
        base = len(cc.compress(sample))
        best, bw, bd = base, None, False
        for w in (2, 4, 8):
            for delta in (False, True):
                try:
                    t = ByteTranspose._apply(sample, w, delta)
                except Exception:
                    continue
                c = len(cc.compress(t))
                if c < best * 0.995:
                    best, bw, bd = c, w, delta
        if bw is None:
            return None
        return ByteTranspose._apply(data, bw, bd), {"w": bw, "d": bd, "n": len(data), "z": 1}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        # archives written before the zigzag fix carry no "z" flag
        return ByteTranspose._unapply(payload, aux["w"], aux["d"], aux["n"],
                                      zig=bool(aux.get("z")))

class ZipRecompose:
    """Fully general Zip-family handling: pull every member out, keep the member
    CONTENTS bit-identical, and rebuild the container on extraction.

    Unlike deflate_unwrap this works no matter which deflate implementation
    produced the original, so it always fires - but the rebuilt container's own
    bytes may differ from the original's (same files, re-packed). Crush treats
    that as a CONTENT-exact rather than BYTE-exact result, marks the file as
    such, and only ever uses it when the user has opted in."""
    name = "zip_recompose"
    exact = False

    @staticmethod
    def forward(data: bytes):
        import zipfile as _zf
        try:
            z = _zf.ZipFile(io.BytesIO(data))
            infos = z.infolist()
        except Exception:
            return None
        if not infos:
            return None
        parts, meta = [], []
        for i in infos:
            try:
                raw = z.read(i)
            except Exception:
                return None
            parts.append(raw)
            meta.append({"n": i.filename, "d": list(i.date_time), "c": i.compress_type,
                         "e": i.external_attr, "in": i.internal_attr,
                         "cs": i.create_system, "cv": i.create_version,
                         "ev": i.extract_version, "f": i.flag_bits,
                         "cm": i.comment.decode("latin-1"), "x": i.extra.hex(),
                         "l": len(raw),
                         "h": hashlib.sha256(raw).hexdigest()})
        return b"".join(parts), {"m": meta, "cm": z.comment.decode("latin-1")}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        import zipfile as _zf
        buf = io.BytesIO()
        off = 0
        with _zf.ZipFile(buf, "w") as z:
            for m in aux["m"]:
                zi = _zf.ZipInfo(m["n"], tuple(m["d"]))
                zi.compress_type = m["c"]; zi.external_attr = m["e"]
                zi.internal_attr = m["in"]; zi.create_system = m["cs"]
                zi.create_version = m["cv"]; zi.extract_version = m["ev"]
                zi.flag_bits = m["f"]; zi.comment = m["cm"].encode("latin-1")
                zi.extra = bytes.fromhex(m["x"])
                z.writestr(zi, payload[off:off + m["l"]])
                off += m["l"]
            z.comment = aux["cm"].encode("latin-1")
        return buf.getvalue()

    @staticmethod
    def verify_content(rebuilt: bytes, aux) -> bool:
        """Content check used in place of a byte check: every member must come
        back with an identical SHA-256."""
        import zipfile as _zf
        try:
            z = _zf.ZipFile(io.BytesIO(rebuilt))
        except Exception:
            return False
        got = {i.filename: i for i in z.infolist()}
        if len(got) != len(aux["m"]):
            return False
        for m in aux["m"]:
            i = got.get(m["n"])
            if i is None:
                return False
            if hashlib.sha256(z.read(i)).hexdigest() != m["h"]:
                return False
        return True

# ---- 2h. v4 domain transforms ---------------------------------------------
class FloatArray:
    """Float and integer arrays, coded as what they are.

    An IEEE-754 array is three streams wearing one coat: sign, exponent and
    mantissa have wildly different statistics, and consecutive values are
    usually close. This splits the array into its real channels, then picks per
    channel between XOR-against-previous with zero-run coding, delta, byte
    planes, and - the one that pays most on exported data - recovering the exact
    decimal a float was printed from and coding the integer instead."""
    name = "floatarray"

    @staticmethod
    def forward(data: bytes, **kw):
        if crush_vector is None:
            return None
        return crush_vector.FloatArray.forward(data)

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        return crush_vector.FloatArray.inverse(payload, aux)


class Genome:
    """Sequencing data, split into the streams it is actually made of."""
    name = "genome"

    @staticmethod
    def forward(data: bytes, **kw):
        if _genome is None:
            return None
        return _genome.Genome.forward(data)

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        return _genome.Genome.inverse(payload, aux)


class Loom:
    """Structure discovery in binary formats nobody wrote a parser for.

    Most binary files that are not already compressed are arrays of records
    with typed fields, and their redundancy runs down the columns rather than
    along the stream. Loom infers the record stride, types each column, regroups
    columns that are planes of one wider value, and codes each field on its own
    terms - with no knowledge of the format."""
    name = "loom"

    MIN_SIZE = 256 << 10          # below this the analysis costs more than it saves

    @staticmethod
    def forward(data: bytes, **kw):
        if crush_loom is None or len(data) < Loom.MIN_SIZE:
            return None
        return crush_loom.forward(data)

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        return crush_loom.inverse(payload, aux)


TRANSFORMS = {t.name: t for t in (DeflateUnwrap, PngUnwrap, PdfUnwrap, GzipUnwrap,
                                  JpegPack, EmbeddedJpeg, JpegXL, FlacWav,
                                  AudioPredict, ByteTranspose, ZipRecompose,
                                  Oracle, Prism, PerceptualImage, PerceptualAudio,
                                  FloatArray, Genome, Loom)}

# Each entry is a list of STAGES. A stage is a list of ALTERNATIVES that
# compete with each other - Crush runs every one that applies, measures how
# compressible each result actually is, and keeps the winner.
TRANSFORM_PLAN = {
    "zipfamily": [["deflate_unwrap", "zip_recompose"], ["jpeg_embedded"]],
    "png":       [["png_unwrap"]],
    "jpeg":      [["jpeg_pack", "jpegxl"]],
    "wav":       [["audio_predict", "flac"], ["transpose"]],
    "numeric":   [["transpose", "floatarray"]],
    "generic":   [["transpose", "floatarray"]],
    "genomic":   [["genome"]],
    "db":        [],
    "pdf":       [["pdf_unwrap"], ["jpeg_embedded"]],
    "gzip":      [["gzip_unwrap"]],
}

_PROBE_CAP = 6 << 20

def _probe(d: bytes) -> int:
    """Estimate how well a byte string will ultimately compress. This has to be
    a STRONG codec: a weak probe wrongly rejects transforms whose benefit only
    materialises under LZMA-class modelling (an unwrapped photographic PNG is
    the classic case - zstd-6 says it got worse, LZMA -9e says it got 5%
    better). Zstandard-19 tracks the final outcome closely enough to decide."""
    d = d[:_PROBE_CAP]
    if zstd is None:
        return len(lzma.compress(d, preset=1))
    return len(zstd.ZstdCompressor(level=19).compress(d))

def plan_for(cls: str, perceptual: dict, deep: bool):
    """Build the transform pipeline for one file.

    Order is deliberate. Oracle runs first because if a generator exists the
    file is finished - nothing downstream has anything left to do. Perceptual
    runs before the lossless media transforms because it supersedes them.
    Prism runs last, on whatever is still a byte stream, because shredding is
    only useful once the file is in its most structural form."""
    stages = [["oracle"]]
    if perceptual:
        if cls == "jpeg" and perceptual.get("photo"):
            stages.append(["perc_image"])
        elif cls == "png" and perceptual.get("photo"):
            stages.append(["perc_image"])
        elif cls == "wav" and perceptual.get("audio"):
            stages.append(["perc_audio"])
    stages += TRANSFORM_PLAN.get(cls, [])
    # Loom's analysis pass is expensive and only pays on structured binaries,
    # so it is a deep-tier stage rather than part of the default route.
    if deep and cls in ("generic", "numeric", "db", "font"):
        stages.append(["loom"])
    if deep and cls in ("text", "code", "generic", "db", "pdf", "numeric"):
        stages.append(["prism"])
    return stages


_UNWRAPPERS = ("deflate_unwrap", "pdf_unwrap", "gzip_unwrap", "png_unwrap")

# ---- portable archives ------------------------------------------------------
# A portable archive can be opened by the self-extracting share page in any
# browser: every transform and codec in it has a byte-exact JavaScript or
# WebAssembly decoder there (share/REPORT.md lists the evidence). Everything
# else is excluded at compression time, so the tournament simply never picks
# it. The format does not change; an index flag records the promise.
PORTABLE_TRANSFORMS = frozenset({
    "oracle", "prism", "transpose", "audio_predict", "floatarray", "genome", "loom",
    "deflate_unwrap", "pdf_unwrap", "gzip_unwrap", "png_unwrap",
    "jpeg_pack", "jpeg_embedded"})
# Codecs needing a shared prior are left out: the page would have to carry the
# prior (110 KB per zdict, 2.6-6 MB per .prime), which costs more than it saves
# on anything small enough to share as one page.
PORTABLE_CODECS = frozenset({
    "store", "zstd12", "zstd19", "zstd22", "lzma", "lzma_x86", "lzma_d4",
    "lzma_text", "brotli", "bzip2", "ppmd", "crushcm"})

def _perc_params(name, perceptual, allow_slow=True, deep=False):
    if name in _UNWRAPPERS:
        return {"slow": allow_slow}
    if name in ("jpeg_pack", "jpeg_embedded"):
        return {"strong": deep}
    if name == "perc_image":
        return {"distance": float(perceptual.get("photo", 1.0))}
    if name == "perc_audio":
        return {"bitrate": str(perceptual.get("audio", "128k"))}
    return {}


def apply_transforms(data: bytes, cls: str, allow_slow: bool = True,
                     allow_recompose: bool = False, perceptual: dict = None,
                     deep: bool = False, portable: bool = False):
    """Run the transform plan for this class, verifying each step.
    Returns (payload, chain, exact) where exact is False only if a
    content-exact (rather than byte-exact) transform was accepted."""
    chain = []
    cur = data
    exact = True
    perceptual = perceptual or {}
    for stage in plan_for(cls, perceptual, deep):
        if not cur:
            break                       # a generator was found; nothing remains
        base_probe = None
        winner = None                      # (probe, name, payload, aux, is_exact)
        for tname in stage:
            T = TRANSFORMS[tname]
            if portable and tname not in PORTABLE_TRANSFORMS:
                continue
            if tname in ("jpegxl", "jpeg_pack", "jpeg_embedded", "flac") and not allow_slow:
                continue
            if tname == "zip_recompose" and not allow_recompose:
                continue
            if tname.startswith("perc_") and not perceptual:
                continue
            if tname in ("jpegxl", "jpeg_pack") and any(c["n"] == "perc_image" for c in chain):
                continue
            if tname in ("flac", "audio_predict") and any(c["n"] == "perc_audio" for c in chain):
                continue
            try:
                res = T.forward(cur, **_perc_params(tname, perceptual, allow_slow, deep))
            except Exception:
                res = None
            if not res:
                continue
            payload, aux = res
            # ---- the guarantee: prove it inverts before accepting it ----
            try:
                back = T.inverse(payload, aux)
            except Exception:
                continue
            t_exact = getattr(T, "exact", True)
            if t_exact:
                if back != cur:
                    continue
            else:
                if not T.verify_content(back, aux):
                    continue
            if payload == b"":
                winner = (0, tname, payload, aux, t_exact)
                break
            if base_probe is None:
                base_probe = _probe(cur)
            score = len(payload) if len(payload) < len(cur) else _probe(payload)
            ref = base_probe if len(payload) >= len(cur) else len(cur)
            if score >= ref * 0.995:
                continue                   # did not actually help - discard
            # prefer a byte-exact transform when a content-exact one is not
            # meaningfully better
            if winner is None or score < winner[0] * (0.97 if t_exact else 1.0):
                if winner is not None and winner[4] and not t_exact and score > winner[0] * 0.92:
                    continue
                winner = (score, tname, payload, aux, t_exact)
        if winner:
            chain.append({"n": winner[1], "a": winner[3]})
            cur = winner[2]
            if not winner[4]:
                exact = False
    return cur, chain, exact

def invert_transforms(payload: bytes, chain) -> bytes:
    for step in reversed(chain):
        payload = TRANSFORMS[step["n"]].inverse(payload, step["a"])
    return payload

# ==========================================================================
#  3. CODEC TOURNAMENT
# ==========================================================================
def _lzma_filters(kind: str, dict_mb: int = 64):
    d = dict_mb << 20
    base = dict(id=lzma.FILTER_LZMA2, preset=9 | lzma.PRESET_EXTREME, dict_size=d)
    if kind == "x86":
        return [dict(id=lzma.FILTER_X86), base]
    if kind == "delta4":
        return [dict(id=lzma.FILTER_DELTA, dist=4), base]
    if kind == "text":
        b = dict(base); b.update(lc=4, lp=0, pb=0)
        return [b]
    return [base]

def c_store(d, **k):   return b"\x00" + d
def c_zstd(d, level=19, window_log=27):
    p = zstd.ZstdCompressionParameters.from_level(
        level, window_log=window_log, enable_ldm=1)
    return zstd.ZstdCompressor(compression_params=p).compress(d)
def c_lzma(d, kind="plain", dict_mb=64):
    return lzma.compress(d, format=lzma.FORMAT_RAW, filters=_lzma_filters(kind, dict_mb))
def c_brotli(d, q=11):
    return brotli.compress(d, quality=q, lgwin=24)
def c_bz2(d):  return bz2.compress(d, 9)

try:
    import pyppmd
except ImportError:
    pyppmd = None

PPMD_MEM = 192 << 20
PPMD_ORDER = 16

def c_ppmd_native(d):
    """PPMd variant H (Ppmd7), length-delimited.

    pyppmd's one-shot compress()/decompress() pair uses the PPMd8 end-marker
    scheme, which fails on some inputs ("Corrupted input data") - it bit us on
    a 10 MB source-code bundle. Ppmd7 with an explicit length is what 7-Zip
    uses and has no such ambiguity, so Crush stores the length itself."""
    if pyppmd is None:
        return None
    enc = pyppmd.Ppmd7Encoder(PPMD_ORDER, PPMD_MEM)
    out = enc.encode(d) + enc.flush()
    return struct.pack("<Q", len(d)) + out

def d_ppmd_native(b):
    n = struct.unpack("<Q", b[:8])[0]
    dec = pyppmd.Ppmd7Decoder(PPMD_ORDER, PPMD_MEM)
    return dec.decode(b[8:], n)

def c_ppmd(d, order=16, mem_mb=256):
    if not HAVE["7z"]:
        return None
    with tempfile.TemporaryDirectory() as td:
        i = os.path.join(td, "b"); o = os.path.join(td, "b.7z")
        open(i, "wb").write(d)
        r = subprocess.run([HAVE["7z"], "a", "-t7z", f"-m0=PPMd:mem={mem_mb}m:o={order}",
                            "-mx=9", "-bso0", "-bsp0", o, i], capture_output=True, timeout=3600)
        if r.returncode != 0 or not os.path.exists(o):
            return None
        return open(o, "rb").read()

def d_ppmd(blob):
    with tempfile.TemporaryDirectory() as td:
        o = os.path.join(td, "b.7z")
        open(o, "wb").write(blob)
        subprocess.run([HAVE["7z"], "e", "-bso0", "-bsp0", f"-o{td}", o],
                       capture_output=True, timeout=3600)
        return open(os.path.join(td, "b"), "rb").read()

def c_zstd_dict(d, prior, level=19):
    """Zstandard with a dictionary trained on this KIND of file. The dictionary
    ships with Crush, so what it contains is not information the archive has to
    carry - the same trade a neural compressor makes with its weights, at a
    thousandth of the cost."""
    if zstd is None or not prior:
        return None
    dd = zstd.ZstdCompressionDict(open(prior, "rb").read())
    return zstd.ZstdCompressor(level=level, dict_data=dd).compress(d)


def d_zstd_dict(b, prior):
    dd = zstd.ZstdCompressionDict(open(prior, "rb").read())
    return zstd.ZstdDecompressor(dict_data=dd).decompress(b, max_output_size=1 << 31)


def c_crushcm_primed(d, prior):
    """Context mixing whose models have already read a corpus of this kind of
    file before they see yours. The decoder reads the same corpus, so nothing
    about it travels in the archive."""
    if not HAVE["crushcm"] or not prior:
        return None
    with tempfile.TemporaryDirectory() as td:
        i = os.path.join(td, "b"); o = os.path.join(td, "b.cm")
        open(i, "wb").write(d)
        r = subprocess.run([HAVE["crushcm"], "c", i, o, "-p", prior],
                           capture_output=True, timeout=14400)
        if r.returncode != 0 or not os.path.exists(o):
            return None
        return open(o, "rb").read()


def d_crushcm_primed(blob, prior):
    with tempfile.TemporaryDirectory() as td:
        i = os.path.join(td, "b.cm"); o = os.path.join(td, "b")
        open(i, "wb").write(blob)
        r = subprocess.run([HAVE["crushcm"], "d", i, o, "-p", prior],
                           capture_output=True, timeout=14400)
        if r.returncode != 0:
            raise RuntimeError("crushcm failed to decode")
        return open(o, "rb").read()


# --------------------------------------------------------------------------
#  crushnn: crushcm's mixer plus a small integer-only transformer trained on
#  source code (round 11). The model (priors/code.cnn, 0.9 MB) is part of the
#  archive format: an archive that used it can only be read with exactly this
#  file, so its SHA-256 is pinned here and checked before every use.
# --------------------------------------------------------------------------
NN_MODELS = {"code": "15e49e91be8ceb7f72e94a9f7dcd685577c38c551b12683c253602f00f64ff81"}
NN_MAX_BUNDLE = 256 * 1024          # ~6 KB/s each way: only small bundles pay
_nn_checked = {}

def _nn_model_ok(path):
    if path not in _nn_checked:
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            h = hashlib.sha256(open(path, "rb").read()).hexdigest()
        except OSError:
            h = ""
        _nn_checked[path] = (h == NN_MODELS.get(name))
    return _nn_checked[path]


def c_crushnn(d, model):
    if not HAVE["crushnn"] or not model or len(d) > NN_MAX_BUNDLE or not _nn_model_ok(model):
        return None
    with tempfile.TemporaryDirectory() as td:
        i = os.path.join(td, "b"); o = os.path.join(td, "b.nn")
        open(i, "wb").write(d)
        r = subprocess.run([HAVE["crushnn"], "c", i, o, "-m", model],
                           capture_output=True, timeout=7200)
        if r.returncode != 0 or not os.path.exists(o):
            return None
        return open(o, "rb").read()


def d_crushnn(blob, model):
    if not HAVE["crushnn"]:
        raise RuntimeError("this archive needs the crushnn helper, which is missing")
    if not model or not _nn_model_ok(model):
        raise RuntimeError("the code model priors/code.cnn is missing or is not the "
                           "exact file this archive was made with")
    # the stored length is untrusted: bound it before the helper runs
    if len(blob) < 12 or blob[:4] != b"CNM1" or \
       int.from_bytes(blob[4:12], "little") > NN_MAX_BUNDLE:
        raise ValueError("crushnn stream header out of range")
    with tempfile.TemporaryDirectory() as td:
        i = os.path.join(td, "b.nn"); o = os.path.join(td, "b")
        open(i, "wb").write(blob)
        r = subprocess.run([HAVE["crushnn"], "d", i, o, "-m", model],
                           capture_output=True, timeout=7200)
        if r.returncode != 0:
            raise RuntimeError("crushnn failed to decode")
        return open(o, "rb").read()


def _prior_ext(cid):
    return {"crushcm_p": ".prime", "crushnn_m": ".cnn"}.get(cid, ".zdict")


def c_crushcm(d):
    if not HAVE["crushcm"]:
        return None
    with tempfile.TemporaryDirectory() as td:
        i = os.path.join(td, "b"); o = os.path.join(td, "b.cm")
        open(i, "wb").write(d)
        r = subprocess.run([HAVE["crushcm"], "c", i, o], capture_output=True, timeout=7200)
        if r.returncode != 0 or not os.path.exists(o):
            return None
        return open(o, "rb").read()

def d_crushcm(blob):
    with tempfile.TemporaryDirectory() as td:
        i = os.path.join(td, "b.cm"); o = os.path.join(td, "b")
        open(i, "wb").write(blob)
        r = subprocess.run([HAVE["crushcm"], "d", i, o], capture_output=True, timeout=7200)
        if r.returncode != 0:
            raise RuntimeError("crushcm failed to decode")
        return open(o, "rb").read()

def _zd(b):
    return zstd.ZstdDecompressor().decompress(b, max_output_size=1 << 31)

# id -> (encoder(data, prior), decoder(blob, prior), human label, needs_prior)
CODECS = {
    "store":     (lambda d, p=None: d,                   lambda b, p=None: b,           "Stored", False),
    "zstd12":    (lambda d, p=None: c_zstd(d, 12),       lambda b, p=None: _zd(b),      "Zstandard-12", False),
    "zstd19":    (lambda d, p=None: c_zstd(d, 19),       lambda b, p=None: _zd(b),      "Zstandard-19", False),
    "zstd22":    (lambda d, p=None: c_zstd(d, 22),       lambda b, p=None: _zd(b),      "Zstandard-22", False),
    "lzma":      (lambda d, p=None: c_lzma(d, "plain"),  lambda b, p=None: lzma.decompress(b, format=lzma.FORMAT_RAW, filters=_lzma_filters("plain")), "LZMA2", False),
    "lzma_x86":  (lambda d, p=None: c_lzma(d, "x86"),    lambda b, p=None: lzma.decompress(b, format=lzma.FORMAT_RAW, filters=_lzma_filters("x86")),   "LZMA2 + x86 filter", False),
    "lzma_d4":   (lambda d, p=None: c_lzma(d, "delta4"), lambda b, p=None: lzma.decompress(b, format=lzma.FORMAT_RAW, filters=_lzma_filters("delta4")),"LZMA2 + delta", False),
    "lzma_text": (lambda d, p=None: c_lzma(d, "text"),   lambda b, p=None: lzma.decompress(b, format=lzma.FORMAT_RAW, filters=_lzma_filters("text")),  "LZMA2 tuned for text", False),
    "brotli":    (lambda d, p=None: c_brotli(d, 11),     lambda b, p=None: brotli.decompress(b), "Brotli-11", False),
    "bzip2":     (lambda d, p=None: c_bz2(d),            lambda b, p=None: bz2.decompress(b),    "bzip2-9", False),
    "ppmd":      (lambda d, p=None: c_ppmd_native(d),    lambda b, p=None: d_ppmd_native(b),     "PPMd order-16", False),
    "crushcm":   (lambda d, p=None: c_crushcm(d),        lambda b, p=None: d_crushcm(b),         "Crush CM (context mixing)", False),
    "zstd19d":   (lambda d, p: c_zstd_dict(d, p, 19),    d_zstd_dict,        "Zstandard-19 + shared dictionary", True),
    "zstd22d":   (lambda d, p: c_zstd_dict(d, p, 22),    d_zstd_dict,        "Zstandard-22 + shared dictionary", True),
    "crushcm_p": (c_crushcm_primed,                      d_crushcm_primed,   "Crush CM + shared prior", True),
    "crushnn_m": (c_crushnn,                             d_crushnn,          "Crush CM + learned code model", True),
}

# crushcm is only ever offered for bundles up to this size (every version); the
# reader refuses a crushcm stream longer than this, and nothing shorter
CRUSHCM_MAX = 200 * 1024 * 1024


def candidates_for(cls: str, tier: str, size: int):
    if cls in ("media", "archive") and tier in ("quick", "balanced"):
        return ["store", "zstd12"]
    if tier == "quick":
        return ["store", "zstd12"]
    if tier == "balanced":
        base = ["store", "zstd19", "lzma"]
        if cls in ("exe_pe", "exe_elf"): base.append("lzma_x86")
        if cls in ("numeric",):          base.append("lzma_d4")
        return base
    big = size > CRUSHCM_MAX
    base = ["store", "zstd22", "lzma", "brotli"]
    if cls in ("text", "code", "pdf", "zipfamily", "db", "gzip"): base += ["lzma_text", "ppmd"]
    if cls in ("exe_pe", "exe_elf"):                      base += ["lzma_x86"]
    if cls in ("numeric", "wav", "image", "generic", "db", "gzip"): base += ["lzma_d4"]
    if cls in ("media", "archive"):                       base = ["store", "zstd22", "lzma", "brotli"]
    if tier in ("obsession", "singularity") and not big and HAVE["crushcm"] \
       and cls not in ("media", "archive"):
        base.append("crushcm")
    if tier in ("crush", "obsession", "singularity"):
        pk = PRIOR_FOR_CLASS.get(cls)
        if pk and _prior(pk, ".zdict"):
            base.append("zstd19d")
            if tier != "crush":
                base.append("zstd22d")
    if tier == "singularity" and HAVE["crushcm"]:
        pk = PRIOR_FOR_CLASS.get(cls)
        if pk and _prior(pk, ".prime") and len(data_len_guard(size)) == 0:
            base.append("crushcm_p")
    if tier == "singularity" and HAVE["crushnn"] and size <= NN_MAX_BUNDLE:
        pk = PRIOR_FOR_CLASS.get(cls)
        if pk in NN_MODELS and _prior(pk, ".cnn"):
            base.append("crushnn_m")
    return base


def data_len_guard(size):
    """Priming costs time proportional to the corpus, so it is only offered
    where it can pay for itself."""
    return "" if size <= 96 * 1024 * 1024 else "too-large"

def _incompressible(data, cls, probes=8, size=1 << 20):
    """True when a big bundle of video/audio, archives or recompressed JPEGs
    has every one of `probes` evenly spaced 1 MB samples shrink by under 1% at
    zstd level 3. Never used on classes whose content is ordinary data."""
    # GIF/LZW ("image") is left out on purpose: LZMA still finds 0.3-0.6% there
    if cls not in ("media", "archive", "jpeg") or len(data) < 32 * size or zstd is None:
        return False
    c = zstd.ZstdCompressor(level=3)
    step = (len(data) - size) // (probes - 1)
    for k in range(probes):
        chunk = data[k * step:k * step + size]
        if len(c.compress(chunk)) < 0.99 * len(chunk):
            return False
    return True


def tournament(data: bytes, cls: str, tier: str, log=None, portable: bool = False):
    """Run every codec that suits this bundle and keep the smallest result.

    A candidate is only eligible once it has been DECODED back and byte-compared
    against the input. A codec that cannot reproduce the bundle is discarded no
    matter how small its output was - which is how a genuine PPMd library bug
    was caught here rather than in a user's archive."""
    best_id, best_blob, best_prior = "store", data, None
    pk = PRIOR_FOR_CLASS.get(cls)
    cands = candidates_for(cls, tier, len(data))
    if _incompressible(data, cls):
        # video, archives, recompressed JPEGs: on 400 MB of them the full field
        # (zstd-22, LZMA, Brotli) took 40 minutes to save 0.1%. A cheap codec
        # still competes, so anything that does compress is still caught.
        cands = [c for c in cands if c in ("store", "zstd12")] or ["store"]
    for cid in cands:
        if cid == "store":
            continue
        if portable and cid not in PORTABLE_CODECS:
            continue
        needs = CODECS[cid][3]
        prior = None
        if needs:
            ext = _prior_ext(cid)
            prior = _prior(pk, ext) if pk else None
            if not prior:
                continue
        try:
            blob = CODECS[cid][0](data, prior)
        except Exception:
            if log is not None:
                log.append((cid, -1))
            continue
        if blob is None:
            continue
        # ---- prove it decodes before it is allowed to win ----
        try:
            if CODECS[cid][1](blob, prior) != data:
                if log is not None:
                    log.append((cid, -2))
                continue
        except Exception:
            if log is not None:
                log.append((cid, -2))
            continue
        if log is not None:
            log.append((cid, len(blob)))
        if len(blob) < len(best_blob):
            best_id, best_blob, best_prior = cid, blob, (pk if CODECS[cid][3] else None)
    if log is not None:
        log.append(("store", len(data)))
    return best_id, best_blob, best_prior

# ==========================================================================
#  4. THE .crush CONTAINER
# ==========================================================================
@dataclass
class FileEntry:
    p: str; s: int; h: str; m: float; x: int
    cls: str = "generic"
    g: str = ""                      # bundle group; defaults to cls
    ex: bool = True                  # byte-exact (True) or content-exact (False)
    dup: int = -1
    b: int = -1; o: int = 0; l: int = 0
    t: list = field(default_factory=list)

HASH_HEX = 32          # 128 bits - ample for integrity, half the index cost

def _sha(path):
    hs = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hs.update(chunk)
    return hs.hexdigest()[:HASH_HEX]


_ENTRY_DEFAULTS = {"cls": "generic", "g": "", "ex": True, "dup": -1, "b": -1,
                   "o": 0, "l": 0, "t": [], "x": 0o644}

def _pack_entry(e):
    """Drop every field that is at its default. On a big archive this is noise;
    on a single small file the index was costing more than the compression was
    saving, which is how a 15% win turned into a loss."""
    d = asdict(e)
    return {k: v for k, v in d.items() if _ENTRY_DEFAULTS.get(k, object()) != v}

def _unpack_entry(d):
    out = dict(_ENTRY_DEFAULTS)
    out.update(d)
    return out

def _name_key(rel):
    """The name a stored path will have on a case-insensitive, normalising
    file system (NTFS, APFS) - two entries with the same key overwrite each
    other on extraction."""
    import unicodedata
    parts = [c.rstrip(" .") for c in unicodedata.normalize("NFC", rel).split("/")]
    return "/".join(parts).casefold()


def collect(paths, skipped=None):
    """Every file under `paths`, with the name it will be stored as.

    Refuses (rather than silently dropping) what it cannot read: a path that
    does not exist, and a folder that cannot be listed. Links are not followed;
    each one skipped is appended to `skipped` so the caller can say so."""
    files = []
    for p in paths:
        p = os.path.abspath(p)
        if not os.path.lexists(p):
            raise FileNotFoundError(f"nothing to compress at {p!r}: it does not exist")
        if os.path.isfile(p):
            files.append((p, os.path.basename(p)))
        else:
            # dirname of an abspath: "C:\\" stays "C:\\" and "/" stays "/"
            root_parent = os.path.dirname(p)
            def walk_error(e):
                raise OSError(e.errno, f"could not read the folder {e.filename!r} "
                                       f"({e.strerror}); nothing was written") from e
            for dirpath, dirnames, names in os.walk(p, onerror=walk_error):
                for n in sorted(dirnames):
                    if os.path.islink(os.path.join(dirpath, n)) and skipped is not None:
                        skipped.append(os.path.join(dirpath, n))
                for n in sorted(names):
                    fp = os.path.join(dirpath, n)
                    if os.path.islink(fp) or not os.path.isfile(fp):
                        if skipped is not None:
                            skipped.append(fp)
                        continue
                    files.append((fp, os.path.relpath(fp, root_parent).replace(os.sep, "/")))
    # Two inputs called report.txt (or two folders called Photos, or A.txt and
    # a.txt) would be stored under one name, and extraction would keep only
    # the last of them while reporting every one as restored.
    # (Case and Unicode normalisation only count where the file system folds
    # them; on Linux, Makefile and makefile are two real files.)
    seen, dirs = {}, {}
    for fp, rel in files:
        k = _collision_key(rel)
        if k in seen:
            raise ValueError(f"two inputs would be stored under the same name {rel!r}:\n"
                             f"  {seen[k]}\n  {fp}\n"
                             "Rename one, or put them in differently named folders. "
                             "Nothing was written.")
        seen[k] = fp
        parts = k.split("/")
        for j in range(1, len(parts)):
            dirs.setdefault("/".join(parts[:j]), fp)
    for k, fp in seen.items():
        if k in dirs:
            raise ValueError(f"{fp!r} has the same name as a folder holding {dirs[k]!r}; "
                             "nothing was written.")
    return files

BUNDLE_CAP = 192 * 1024 * 1024

# Files bigger than this never pass through memory whole: they are hashed,
# compressed (or stored, if already compressed), verified and restored as
# streams, one chunk at a time, in a bundle of their own. Everything smaller
# goes through the full pipeline. A 3 GB video therefore needs a few MB of RAM,
# not 3 GB - and on an 8 GB machine, a folder of them used to be an
# out-of-memory kill.
BIG_FILE = 256 * 1024 * 1024
_CHUNK = 4 << 20
# classes whose content is already entropy-coded: zstd cannot shrink them, and
# trying would cost a full pass for nothing
_STORE_BIG = {"media", "archive", "jpeg", "png", "gzip", "zipfamily", "pdf"}


def _sha_full(path):
    hs = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            hs.update(chunk)
    return hs.hexdigest()


class _Spill:
    """Transformed payloads waiting for pass 3. Kept in memory up to a budget,
    then written to a temporary file beside the output, so total input is
    bounded by disk, not by RAM. On Linux the file has no name at all
    (O_TMPFILE); on Windows it is delete-on-close but has a directory entry
    while it exists. For an encrypted archive it holds plaintext until the
    archive is finished - see SECURITY.md."""
    MEM = 512 * 1024 * 1024

    def __init__(self, outdir):
        self.outdir, self.mem, self.used, self.f, self.where = outdir, {}, 0, None, {}

    def put(self, k, data):
        if self.used + len(data) <= self.MEM:
            self.mem[k] = data; self.used += len(data)
            return
        if self.f is None:
            self.f = tempfile.TemporaryFile(dir=self.outdir)
        self.f.seek(0, 2)
        self.where[k] = (self.f.tell(), len(data))
        self.f.write(data)

    def size(self, k):
        return len(self.mem[k]) if k in self.mem else self.where[k][1]

    def pop(self, k):
        if k in self.mem:
            d = self.mem.pop(k); self.used -= len(d)
            return d
        off, n = self.where.pop(k)
        self.f.seek(off)
        d = self.f.read(n)
        if len(d) != n:
            raise IOError("the temporary payload file was truncated")
        return d

    def close(self):
        if self.f is not None:
            self.f.close()

ECHO_MIN = 24 * 1024 * 1024

def compress(paths, out_path, tier="balanced", progress=None, harder=False,
             perceptual=None, echo_dedup=True, password=None, kdf_profile=None,
             portable=False):
    if portable:
        # content-exact recomposition and the lossy tier cannot be rebuilt in a
        # browser; refuse quietly rather than produce a page that cannot open
        harder, perceptual = False, None
    t0 = time.time()
    out_abs = os.path.abspath(out_path)
    out_st = None
    if os.path.exists(out_abs):
        if os.path.isdir(out_abs):
            raise IsADirectoryError(f"the output {out_path!r} is a folder")
        with open(out_abs, "rb") as f:
            head = f.read(16)
        if not (head.startswith(MAGIC) or is_encrypted(out_abs)):
            # `crush c notes.txt todo.txt` - the output comes first. Replacing a
            # file that is not an archive is never what was meant.
            raise FileExistsError(f"refusing to replace {out_path!r}: it exists and is "
                                  "not a Crush archive. Nothing was written.")
        out_st = os.stat(out_abs)
    skipped = []
    raw_files = collect(paths, skipped)
    if out_st is not None:
        # the archive being replaced may sit inside a folder being compressed;
        # it is not packed into its own successor
        raw_files = [(f, r) for f, r in raw_files
                     if not os.path.samestat(os.stat(f), out_st)]
    entries, by_hash, total_in = [], {}, 0
    stats = {"classes": {}, "transforms": {}, "codecs": {}, "dedup_bytes": 0, "dedup_files": 0}
    # Measured, not estimated: what plain ZIP (deflate -9 per file, as every ZIP
    # tool does it) would cost for exactly this input. Computed while each file
    # is already in memory, so it costs one extra pass over the bytes.
    zip_equiv = 0
    measure_zip = tier != "quick"

    def tick(stage, i, n, name=""):
        if progress:
            progress({"stage": stage, "i": i, "n": n, "name": name,
                      "elapsed": time.time() - t0})

    # ---- pass 1: identify, dedup, transform ----
    payloads = _Spill(os.path.dirname(out_abs) or ".")
    big, src_of = set(), {}
    for i, (fp, rel) in enumerate(raw_files):
        tick("analyze", i, len(raw_files), rel)
        st0 = os.stat(fp)
        if st0.st_size > BIG_FILE:
            # a big file: hashed now as a stream, read again as a stream in
            # pass 3 (where the stored bytes are hashed once more, and that
            # hash is the one recorded)
            with open(fp, "rb") as f:
                head = f.read(4096)
            cls = sniff(rel, head)
            full_h = _sha_full(fp)
            e = FileEntry(p=rel, s=st0.st_size, h=full_h[:HASH_HEX], m=st0.st_mtime,
                          x=st0.st_mode & 0o777, cls=cls)
            total_in += st0.st_size
            measure_zip = False          # not measured, so not claimed
            if full_h in by_hash:
                e.dup = by_hash[full_h]
                stats["dedup_files"] += 1
                stats["dedup_bytes"] += st0.st_size
                src_of[len(entries)] = fp        # in case its twin changes before pass 3
                entries.append(e)
                continue
            by_hash[full_h] = len(entries)
            entries.append(e)
            big.add(len(entries) - 1)
            src_of[len(entries) - 1] = fp
            c = stats["classes"].setdefault(cls, {"n": 0, "in": 0, "after_transform": 0})
            c["n"] += 1; c["in"] += st0.st_size; c["after_transform"] += st0.st_size
            continue
        # Read ONCE, and hash exactly the bytes that are stored. Hashing the
        # file and then reading it again let a file that changed in between
        # (a log, a mailbox, a sync client) be archived under a hash its stored
        # bytes do not have - and then refused at extraction.
        with open(fp, "rb") as f:
            data = f.read()
        st1 = os.stat(fp)
        if (st0.st_size, st0.st_mtime_ns) != (st1.st_size, st1.st_mtime_ns) or \
                len(data) != st1.st_size:
            stats.setdefault("changed_while_reading", []).append(rel)
        size = len(data)
        full_h = hashlib.sha256(data).hexdigest()
        h = full_h[:HASH_HEX]
        cls = sniff(rel, data[:4096])
        e = FileEntry(p=rel, s=size, h=h, m=st1.st_mtime,
                      x=st1.st_mode & 0o777, cls=cls)
        total_in += size
        # dedup on the FULL digest: a 128-bit prefix collision is ~2^64 work
        if full_h in by_hash:
            e.dup = by_hash[full_h]
            stats["dedup_files"] += 1
            stats["dedup_bytes"] += size
            src_of[len(entries)] = fp
            entries.append(e)
            data = None
            continue
        by_hash[full_h] = len(entries)
        if measure_zip:
            co = zlib.compressobj(9, zlib.DEFLATED, -15)
            zip_equiv += len(co.compress(data)) + len(co.flush()) + 76 + 2 * len(rel)
        payload, chain, exact = apply_transforms(
            data, cls, allow_slow=(tier != "quick"), allow_recompose=harder,
            perceptual=perceptual,
            deep=(tier in ("crush", "obsession", "singularity")), portable=portable)
        # A gzip file whose deflate we cannot rebuild is exactly what it looks
        # like - already compressed - and is stored with the other archives
        # rather than run through a codec tournament it cannot win.
        if cls == "gzip" and not any(st["n"] == "gzip_unwrap" for st in chain):
            cls = e.cls = "archive"
        e.ex = exact
        if not exact:
            stats["recomposed"] = stats.get("recomposed", 0) + 1
        for st in chain:
            stats["transforms"][st["n"]] = stats["transforms"].get(st["n"], 0) + 1
        e.t = chain
        # A law-fitted payload is a different ANIMAL from ordinary compressed
        # text: tiny, structured, and statistically unlike its neighbours. Left
        # in the same solid bundle it changed which codec won for everyone else
        # and cost 89 KB to save 33 KB. It gets its own bundle.
        for st in chain:
            if st["n"] == "prism" and any(m > 1 for m in st["a"].get("modes", [])):
                e.g = cls + "~L"
                break
        entries.append(e)
        plen = len(payload)
        payloads.put(len(entries) - 1, payload)
        data = payload = None
        c = stats["classes"].setdefault(cls, {"n": 0, "in": 0, "after_transform": 0})
        c["n"] += 1; c["in"] += size; c["after_transform"] += plen

    # ---- pass 2: solid bundles grouped by class ----
    groups = {}
    for idx, e in enumerate(entries):
        if e.dup >= 0 or idx in big:
            continue
        groups.setdefault(e.g or e.cls, []).append(idx)
    bundles, assign = [], {}
    for gkey in sorted(groups):
        cls = gkey.split("~")[0]
        members, cur, cur_sz = groups[gkey], [], 0
        members.sort(key=lambda i: (os.path.splitext(entries[i].p)[1], entries[i].p))
        for i in members:
            sz = payloads.size(i)
            if cur and cur_sz + sz > BUNDLE_CAP:
                bundles.append((cls, cur)); cur, cur_sz = [], 0
            cur.append(i); cur_sz += sz
        if cur:
            bundles.append((cls, cur))
    big_bundles = set()
    for i in sorted(big):
        big_bundles.add(len(bundles))
        bundles.append((entries[i].cls, [i]))

    # ---- pass 3: codec tournament per bundle ----
    #
    # With a passphrase, the archive is built into a temporary file that is
    # unlinked the moment it is created (delete-on-close on Windows), then
    # streamed through the cipher into out_path. The plaintext archive therefore
    # never has a name anything can open, and it is gone even if this process is
    # killed mid-run. Compress-then-encrypt is the only workable order:
    # ciphertext does not compress.
    #
    # Either way the finished archive is written under a scratch name beside
    # out_path, flushed to disk, and only then renamed over it. Until that
    # rename, whatever was at out_path (a previous backup) is untouched; a
    # crash, a full disk or a kill leaves it exactly as it was.
    if password is not None and crush_crypt is None:
        raise RuntimeError("encryption needs the 'cryptography' package:\n"
                           "  pip install cryptography argon2-cffi")
    import contextlib
    outdir = os.path.dirname(out_abs) or "."
    fd, part = tempfile.mkstemp(prefix=".crush-part-", suffix=".tmp", dir=outdir)
    final = os.fdopen(fd, "w+b")
    committed = []
    def _drop_part():
        if not committed:
            try:
                os.unlink(part)
            except OSError:
                pass
    guard = contextlib.ExitStack()
    guard.callback(_drop_part)
    guard.callback(payloads.close)          # on failure too, not only on success
    sink = tempfile.TemporaryFile(dir=outdir) if password is not None else final
    with guard, final, sink as out:
        out.write(MAGIC)
        out.write(bytes([FORMAT_VERSION, 0]))
        out.write(b"\x00" * 20)                      # index offset/len/codec
        binfo = []
        for bi, (cls, members) in enumerate(bundles):
            tick("compress", bi, len(bundles), CLASS_LABELS.get(cls, cls))
            if bi in big_bundles:
                i = members[0]
                h1 = entries[i].h
                bd = _write_big(out, src_of[i], entries[i], cls, tier, bi, stats)
                if entries[i].h != h1:
                    # the file changed after pass 1: its copies still hold what
                    # pass 1 read, not what was just stored - each is stored
                    # from its own file instead of pointing at this one
                    for j, ej in enumerate(entries):
                        if ej.dup == i:
                            ej.dup = -1
                            stats["dedup_files"] -= 1
                            stats["dedup_bytes"] -= ej.s
                            big_bundles.add(len(bundles))
                            bundles.append((ej.cls, [j]))
                stats["codecs"][bd["codec"]] = stats["codecs"].get(bd["codec"], 0) + 1
                binfo.append(bd)
                continue
            blob_parts, off = [], 0
            for i in members:
                pl = payloads.pop(i)
                entries[i].b = bi; entries[i].o = off; entries[i].l = len(pl)
                blob_parts.append(pl); off += len(pl)
            raw = b"".join(blob_parts)

            # ---- ECHO: whole-bundle chunk deduplication ------------------
            # Content-defined chunking sees across the entire bundle, past any
            # codec's window. Below the threshold LZMA's own 64 MB dictionary
            # already finds this redundancy, so Crush does not pay for it.
            eh = b""
            einfo = None
            if echo_dedup and len(raw) > ECHO_MIN:
                cs = echo.ChunkStore()
                ids = cs.add(raw)
                if cs.total_unique < len(raw) * 0.95:
                    uniq = b"".join(cs.blobs)
                    hdr = echo.pack_header([len(x) for x in cs.blobs], ids)
                    eh = zstd.ZstdCompressor(level=12).compress(hdr) if zstd \
                         else zlib.compress(hdr, 9)
                    einfo = {"nu": len(cs.blobs), "ni": len(ids),
                             "raw": len(raw), "h": len(eh), "z": bool(zstd)}
                    stats["echo_saved"] = stats.get("echo_saved", 0) + (len(raw) - cs.total_unique)
                    raw = uniq

            trial = []
            cid, comp, prior = tournament(raw, cls, tier, log=trial, portable=portable)
            stats["codecs"][cid] = stats["codecs"].get(cid, 0) + 1
            bd = {"id": bi, "cls": cls, "codec": cid, "off": out.tell(),
                  "clen": len(comp), "rawlen": len(raw), "n": len(members)}
            if prior:
                bd["prior"] = prior
            if einfo:
                bd["echo"] = einfo
            if total_in > 262144:
                bd["trial"] = trial
            binfo.append(bd)
            out.write(eh)
            out.write(comp)
            raw = comp = blob_parts = None

        index = {"app": "Crush", "ver": CRUSH_VERSION, "fmt": FORMAT_VERSION, "tier": tier, "harder": harder,
                 "created": time.time(), "files": [_pack_entry(e) for e in entries],
                 "bundles": binfo, "stats": stats,
                 "total_in": total_in,
                 "zip_equiv": zip_equiv if measure_zip else 0}
        if portable:
            index["portable"] = True
        ib = json.dumps(index, separators=(",", ":")).encode()
        # the frame checksum makes a damaged index refuse to load, instead of
        # extracting files under wrong names or with wrong container metadata
        # (the per-file SHA-256 covers content only)
        ic = (zstd.ZstdCompressor(level=19, write_checksum=True).compress(ib)
              if zstd else zlib.compress(ib, 9))
        ioff = out.tell()
        out.write(ic)
        out.seek(8)
        out.write(struct.pack("<QQI", ioff, len(ic), 1 if zstd else 2))

        if password is not None:
            plain_len = out.seek(0, 2)
            out.seek(0)
            tick("encrypt", 0, 1, "Encrypting")
            def enc_tick(st):
                tick("encrypt", st["i"], max(1, st["n"]), "Encrypting")
            crush_crypt.encrypt_stream(out, final, password,
                                       kdf_profile or crush_crypt.DEFAULT_PROFILE,
                                       progress=enc_tick, total=plain_len)
        final.flush()
        os.fsync(final.fileno())
        final.close()
        if sink is not final:
            sink.close()
        if password is None and os.name != "nt":
            os.chmod(part, 0o644)       # mkstemp made it 0600; encrypted stays 0600
        os.replace(part, out_abs)
        committed.append(True)

    total_out = os.path.getsize(out_path)
    if not measure_zip:
        zip_equiv = 0
    if skipped:
        stats["skipped_links"] = len(skipped)
    return {"in": total_in, "out": total_out, "files": len(raw_files),
            "seconds": time.time() - t0, "stats": stats, "bundles": binfo,
            "zip_equiv": zip_equiv if measure_zip else 0,
            "encrypted": password is not None,
            "ratio": (total_out / total_in) if total_in else 1.0}



class _Region:
    """A file object limited to `n` bytes from where it is positioned."""
    def __init__(self, f, n):
        self.f, self.left = f, n

    def read(self, n=-1):
        n = self.left if n is None or n < 0 else min(n, self.left)
        b = self.f.read(n) if n else b""
        self.left -= len(b)
        return b


def _stream_chunks(f, off, clen, codec):
    """The decoded bytes of one streamed bundle, at most _CHUNK at a time.
    zstd's decompressobj returns everything one input chunk expands to - a
    32 KB frame of a sparse 6 GB disk image is 6 GB in one call - so the
    bounded reader is used instead. A short or over-long stream is caught by
    the caller's length and SHA-256 check."""
    f.seek(off)
    r = _Region(f, clen)
    if codec == "store":
        while r.left:
            b = r.read(_CHUNK)
            if not b:
                raise IOError("the archive ends early")
            yield b
        return
    yield from zstd.ZstdDecompressor().read_to_iter(r, read_size=1 << 20, write_size=_CHUNK)


def _write_big(out, fp, e, cls, tier, bi, stats):
    """Pass 3 for one big file: stream it into the archive, then read back what
    was written, decode it and compare its SHA-256 - the same proof every other
    file gets, done as a stream. Returns the bundle record."""
    start = out.tell()

    def emit(codec):
        out.seek(start); out.truncate()
        hs, n = hashlib.sha256(), 0
        with open(fp, "rb") as f:
            if codec == "store":
                for chunk in iter(lambda: f.read(_CHUNK), b""):
                    hs.update(chunk); out.write(chunk); n += len(chunk)
            else:
                level = {"quick": 3, "balanced": 9, "crush": 15}.get(tier, 17)
                co = zstd.ZstdCompressor(level=level, write_checksum=True).compressobj()
                for chunk in iter(lambda: f.read(_CHUNK), b""):
                    hs.update(chunk); n += len(chunk)
                    out.write(co.compress(chunk))
                out.write(co.flush())
        return hs.hexdigest(), n, out.tell() - start

    codec = "store" if (cls in _STORE_BIG or zstd is None) else "zstd12"
    full_h, n, clen = emit(codec)
    if codec != "store" and clen >= n:
        codec = "store"                                 # never inflate
        full_h, n, clen = emit(codec)
    # prove it: read back exactly what is in the archive and decode it
    out.flush()
    hs, got = hashlib.sha256(), 0
    try:
        for chunk in _stream_chunks(out, start, clen, codec):
            hs.update(chunk); got += len(chunk)
            if got > n:
                break
    except (IOError, zstd.ZstdError):
        got = -1
    out.seek(start + clen)
    if got != n or hs.hexdigest() != full_h:
        raise IOError(f"{e.p}: the archived copy did not read back identically - "
                      "nothing was written (is the disk failing?)")
    if full_h[:HASH_HEX] != e.h or n != e.s:
        stats.setdefault("changed_while_reading", []).append(e.p)
    e.h, e.s, e.b, e.o, e.l = full_h[:HASH_HEX], n, bi, 0, n
    return {"id": bi, "cls": cls, "codec": codec, "off": start, "clen": clen,
            "rawlen": n, "n": 1, "stream": True}


def _restore_big(f, b, e, outps, write_meta):
    """Extract one streamed bundle straight to disk: decode chunk by chunk into a
    scratch file beside the first target while hashing, and rename into place
    only if the SHA-256 and the length match. Further targets (duplicates) are
    copied from the verified scratch file and checked again. Returns a list of
    error strings, one per target (None = written)."""
    n = b["rawlen"]
    d0 = os.path.dirname(outps[0])
    os.makedirs(_long(d0), exist_ok=True)
    try:
        free = shutil.disk_usage(d0).free
    except OSError:
        free = None                  # unknown: the write itself will say if it runs out
    if free is not None and free < n + (64 << 20):
        return [f"not written: it needs {n:,} bytes and the destination has {free:,} free"] * len(outps)
    if b["codec"] not in ("store", "zstd12", "zstd19", "zstd22"):
        return ["not written: unknown codec for a large file"] * len(outps)
    fd, tmp = tempfile.mkstemp(prefix=".crush-x-", suffix=".tmp", dir=_long(d0))
    tmps = [tmp]
    try:
        hs, got = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as g:
            for chunk in _stream_chunks(f, b["off"], b["clen"], b["codec"]):
                got += len(chunk)
                if got > n:                              # a hostile length, or damage
                    raise IOError("decoded more data than the archive records")
                hs.update(chunk); g.write(chunk)
            g.flush(); os.fsync(g.fileno())
        if got != n or hs.hexdigest()[:len(e["h"])] != e["h"] or len(e["h"]) < HASH_HEX:
            return ["not written: it did not match its recorded SHA-256"] * len(outps)
        res = [None]
        for outp in outps[1:]:
            d = os.path.dirname(outp)
            os.makedirs(_long(d), exist_ok=True)
            fd2, t2 = tempfile.mkstemp(prefix=".crush-x-", suffix=".tmp", dir=_long(d))
            tmps.append(t2)
            h2 = hashlib.sha256()
            with open(tmp, "rb") as src, os.fdopen(fd2, "wb") as dst:
                for chunk in iter(lambda: src.read(_CHUNK), b""):
                    h2.update(chunk); dst.write(chunk)
                dst.flush(); os.fsync(dst.fileno())
            res.append(None if h2.hexdigest()[:len(e["h"])] == e["h"]
                       else "not written: its copy did not match its recorded SHA-256")
        for k, (t, outp, r) in enumerate(zip(tmps, outps, res)):
            if r is None:
                # one target that cannot be replaced (a folder in the way, a
                # file open in another program) must not mark the ones
                # already in place as not restored
                try:
                    write_meta(t)
                    os.replace(t, _long(outp))
                except OSError as ex:
                    res[k] = f"could not be written: {ex.strerror or ex}"
        return res
    finally:
        for t in tmps:
            try:
                os.unlink(t)
            except OSError:
                pass


def make_share_page(archive_path, html_path=None, password=None, allow_partial=False):
    """Wrap an archive into one self-opening .html file (share/tools/make_share.py).
    Best made from an archive compressed with portable=True."""
    tools = os.path.join(ENGINE_DIR, "..", "share", "tools")
    if not os.path.isdir(tools):
        raise RuntimeError("the share/ folder is missing from this Crush install")
    if tools not in sys.path:
        sys.path.insert(0, os.path.abspath(tools))
    import make_share
    return make_share.make_share(archive_path, html_path, password=password,
                                 allow_partial=allow_partial)


def _bundle_raw(f, b):
    """Read one bundle back: undo Echo, then the codec."""
    f.seek(b["off"])
    ei = b.get("echo")
    eh = f.read(ei["h"]) if ei else b""
    comp = f.read(b["clen"])
    prior = None
    if b.get("prior"):
        ext = _prior_ext(b["codec"])
        prior = _prior(b["prior"], ext)
        if not prior:
            raise RuntimeError(
                f"this archive needs the shared prior '{b['prior']}{ext}', "
                f"which is missing from {PRIORS_DIR}")
    rawlen = b.get("rawlen")
    if b["codec"] in ("crushcm", "crushcm_p"):
        # the helper trusts the 64-bit length in its stream header (it sized its
        # buffer from the low 32 bits and looped on all 64); a damaged or
        # hostile header must never reach it
        if len(comp) < 12 or comp[:4] != b"CCM1" or (rawlen is not None and (
                int.from_bytes(comp[4:12], "little") != rawlen or rawlen > CRUSHCM_MAX)):
            raise ValueError("crushcm stream header does not match the index")
    if b["codec"] == "ppmd" and rawlen is not None:
        # pyppmd 1.3.1, asked for more output than its input holds, returns while
        # its decoder thread still runs: a use-after-free that kills the process
        if len(comp) < 8 or int.from_bytes(comp[:8], "little") != rawlen:
            raise ValueError("PPMd stream header does not match the index")
    raw = CODECS[b["codec"]][1](comp, prior)
    if rawlen is not None and len(raw) != rawlen:
        raise ValueError("data block decoded to the wrong length")
    if ei:
        hdr = (zstd.ZstdDecompressor().decompress(eh, max_output_size=1 << 30)
               if ei.get("z") else zlib.decompress(eh))
        lens, ids = echo.unpack_header(hdr)
        # the header is untrusted: a few hundred bytes of ids can ask for
        # gigabytes of output, so the total is checked before anything is built
        if sum(lens) != len(raw) or any(not (0 <= j < len(lens)) for j in ids) or \
                ("raw" in ei and sum(lens[j] for j in ids) != ei["raw"]):
            raise ValueError("Echo header does not match its data block")
        blobs, pos = [], 0
        for L in lens:
            blobs.append(raw[pos:pos + L]); pos += L
        raw = b"".join(blobs[j] for j in ids)
    return raw

def _open_archive(path, password=None):
    """Open an archive for reading, decrypting on the fly when it is encrypted.

    Returns a seekable binary file object. For an encrypted archive the
    plaintext is produced in memory, a chunk at a time, only for the regions
    actually read - it is never written to disk."""
    if is_encrypted(path):
        if password is None:
            raise crush_crypt.WrongPassword(
                "this archive is encrypted - a passphrase is required")
        # verify_all authenticates EVERY chunk up front rather than only the
        # ones this particular operation happens to touch. Listing an archive,
        # or extracting two files out of a thousand, reads a small part of it;
        # without this, the rest could have been replaced with anything and
        # Crush would report the archive as healthy. One extra pass over the
        # ciphertext at roughly a gigabyte a second is a cheap price for the
        # guarantee actually being the guarantee.
        return crush_crypt.EncryptedReader(path, password, verify_all=True)
    return open(path, "rb")


def read_index(path, password=None):
    with _open_archive(path, password) as f:
        if f.read(6) != MAGIC:
            raise ValueError("not a .crush file")
        hdr = f.read(22)
        end = f.seek(0, 2)
        if len(hdr) != 22:
            raise ValueError("this archive is damaged: its header is incomplete")
        ioff, ilen, icodec = struct.unpack("<QQI", hdr[2:])
        if icodec not in (1, 2) or ioff < 28 or ilen == 0 or ioff + ilen > end:
            raise ValueError("this archive is damaged or incomplete: its index is "
                             "missing or out of range")
        f.seek(ioff)
        ic = f.read(ilen)
    try:
        ib = (zstd.ZstdDecompressor().decompress(ic, max_output_size=1 << 30)
              if icodec == 1 else zlib.decompress(ic))
        idx = json.loads(ib)
        idx["files"] = [_unpack_entry(f) for f in idx["files"]]
        for b in idx["bundles"]:
            b.setdefault("prior", None); b.setdefault("echo", None); b.setdefault("trial", [])
        n_files, num = len(idx["files"]), (int, float)
        for e in idx["files"]:
            if not isinstance(e["p"], str) or not isinstance(e["h"], str) or \
                    not all(type(e[k]) is int for k in ("s", "b", "o", "l", "dup", "x")) or \
                    not isinstance(e["m"], num) or isinstance(e["m"], bool) or \
                    not (-1 <= e["dup"] < n_files) or not isinstance(e["t"], list):
                raise ValueError
    except Exception:
        raise ValueError("this archive's index is damaged - nothing in it can be "
                         "trusted, so nothing was extracted") from None
    return idx

def _safe_join(dest, stored):
    """Join a stored path onto the destination, refusing anything that would
    escape it. Stored paths come out of an archive, and an archive is a file
    someone else may have made."""
    root = os.path.abspath(dest)
    out = os.path.abspath(os.path.join(root, stored.replace("\\", "/").lstrip("/")))
    if out != root and not out.startswith(root.rstrip(os.sep) + os.sep):
        raise ValueError(f"archive entry escapes the destination: {stored!r}")
    return out


def _dup_source(files, i, limit=64):
    """Follow an entry's deduplication pointer back to the entry that actually
    carries the bytes."""
    seen = 0
    while files[i]["dup"] >= 0 and seen < limit:
        i = files[i]["dup"]
        seen += 1
    return i


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                 *(f"COM{i}" for i in "123456789¹²³"),
                 *(f"LPT{i}" for i in "123456789¹²³")}
_WIN_BAD = set('<>:"|?*') | {chr(c) for c in range(32)}


def _name_problem(stored):
    """Why this stored name cannot be written faithfully here, or None.

    These are the names Windows would not refuse but would silently change:
    a reserved device name (CON, NUL, COM1 - the bytes go to the device), a
    trailing dot or space (dropped, so 'a.' lands on 'a'), and ':' (an
    alternate data stream of another file). Names made on Linux or macOS can
    carry any of them."""
    if os.name != "nt":
        return None
    for c in stored.replace("\\", "/").split("/"):
        if c in ("", ".", ".."):
            continue
        if any(ch in _WIN_BAD for ch in c):
            return "its name contains a character Windows does not allow (< > : \" | ? *)"
        if c[-1] in " .":
            return "its name ends in a dot or a space, which Windows would silently drop"
        if c.split(".")[0].rstrip(" ").upper() in _WIN_RESERVED:
            return "its name is reserved by Windows for a device (CON, NUL, COM1, ...)"
    return None


def _collision_key(p):
    if os.name == "nt" or sys.platform == "darwin":
        return _name_key(p.replace("\\", "/"))
    return p


def _long(p):
    """Windows: an extended-length path, so MAX_PATH (260) does not apply.
    Only ever applied to a path _name_problem has already passed, because
    \\\\?\\ also switches off the trailing-dot/space stripping."""
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        if p.startswith("\\\\"):                   # \\server\share\... (a NAS, a mapped folder)
            return "\\\\?\\UNC\\" + p[2:]
        return "\\\\?\\" + p
    return p


_FSYNC_MIN = 1 << 20


def _write_restored(outp, data, e):
    """Write one restored file under a scratch name in its own folder, then
    rename it into place: an interrupted or failed write never leaves a
    truncated file under the real name, and never destroys a file that was
    already there. os.replace also replaces a link at outp rather than writing
    through it."""
    d = os.path.dirname(outp)
    os.makedirs(_long(d), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".crush-x-", suffix=".tmp", dir=_long(d))
    try:
        with os.fdopen(fd, "wb") as g:
            g.write(data)
            if len(data) >= _FSYNC_MIN:
                # big files are where a power cut can leave a hole; for small
                # ones fsync cost 45x the whole extraction (3,000 files: 0.2 s
                # -> 9.6 s) and the rename already covers kill and disk-full
                g.flush()
                os.fsync(g.fileno())
        if os.name != "nt":
            os.chmod(tmp, (e.get("x", 0o644) & 0o777) | 0o400)
        try:
            os.utime(tmp, (e["m"], e["m"]))
        except (OSError, OverflowError, ValueError):
            pass                  # a date this system cannot hold never costs the data
        os.replace(tmp, _long(outp))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def extract(path, dest, progress=None, verify=True, select=None, password=None,
            overwrite=False):
    """Restore an archive.

    select=None restores everything. Otherwise select is an iterable of stored
    paths (the `p` field of read_index()["files"]) and only those are written.

    Nothing is written until every target path has been checked: an entry that
    would escape `dest` aborts the whole run, and if any target already exists
    the run stops with FileExistsError unless overwrite=True. Each file is
    written to a scratch name and renamed into place only after it has matched
    its SHA-256. A duplicate is written from its verified twin's bytes in
    memory, never copied back off the disk. A file that cannot be decoded or
    written is reported in `failed` with the reason in `notes`; the rest of the
    archive is still restored.
    """
    idx = read_index(path, password)
    files = idx["files"]
    t0 = time.time()

    if select is None:
        want = set(range(len(files)))
    else:
        wanted_paths = set(select)
        want = {i for i, e in enumerate(files) if e["p"] in wanted_paths}
        missing = wanted_paths - {files[i]["p"] for i in want}
        if missing:
            raise KeyError("not in this archive: " + ", ".join(sorted(missing)[:5]))

    failed, notes = [], {}
    def fail(i, why):
        failed.append(files[i]["p"])
        notes.setdefault(why, []).append(files[i]["p"])

    # ---- plan every target before writing anything ----
    root = os.path.abspath(dest)
    if os.path.lexists(root) and not os.path.isdir(root):
        # not a FileExistsError: "replace them" would not help
        raise NotADirectoryError(f"the destination {dest!r} is a file, not a folder; "
                                 "nothing was written")
    real_root = os.path.realpath(root)
    plan, seen, existing = {}, {}, []
    for i in sorted(want):
        e = files[i]
        outp = _safe_join(root, e["p"])            # escape -> ValueError, nothing written
        why = _name_problem(e["p"])
        if why:
            fail(i, f"not written: {why}"); continue
        k = _collision_key(os.path.relpath(outp, root).replace(os.sep, "/"))
        if k in seen:
            fail(i, "not written: another file in this archive has the same name on this "
                    f"computer ({files[seen[k]]['p']!r})"); continue
        seen[k] = i
        plan[i] = outp
    file_keys = set(seen)
    for i in list(plan):
        parts = _collision_key(os.path.relpath(plan[i], root).replace(os.sep, "/")).split("/")
        if any("/".join(parts[:j]) in file_keys for j in range(1, len(parts))):
            fail(i, "not written: a file in this archive has the same name as its folder")
            del plan[i]
            continue
        parent = os.path.dirname(plan[i])
        probe = parent
        while not os.path.lexists(probe) and probe != root:
            probe = os.path.dirname(probe)
        rp = os.path.realpath(probe)
        if rp != real_root and not rp.startswith(real_root.rstrip(os.sep) + os.sep):
            fail(i, "not written: a folder in the destination is a link that leads outside it")
            del plan[i]
            continue
        if os.path.lexists(plan[i]):
            existing.append(files[i]["p"])
    if existing and not overwrite:
        raise FileExistsError(
            f"{len(existing)} file(s) already exist in {dest}, for example "
            + ", ".join(repr(x) for x in existing[:3])
            + ". Nothing was written. Choose an empty folder, or allow replacing them.")

    # sources to decode, and which planned targets each one fills
    targets = {}
    for i in plan:
        s = _dup_source(files, i)
        if files[s]["dup"] >= 0:
            fail(i, "not written: its duplicate chain is broken"); continue
        targets.setdefault(s, []).append(i)
    by_bundle = {}
    for s in sorted(targets):
        by_bundle.setdefault(files[s]["b"], []).append(s)

    os.makedirs(_long(root), exist_ok=True)
    written = verified = 0
    with _open_archive(path, password) as f:
        for n_done, (bi, members) in enumerate(sorted(by_bundle.items())):
            try:
                b = next(x for x in idx["bundles"] if x["id"] == bi)
                if progress:
                    progress({"stage": "extract", "i": n_done, "n": len(by_bundle),
                              "name": CLASS_LABELS.get(b["cls"], b["cls"]),
                              "elapsed": time.time() - t0})
                if b.get("stream"):
                    for s in members:
                        tg = [i for i in targets[s] if files[i]["h"] == files[s]["h"]]
                        for i in targets[s]:
                            if i not in tg:
                                fail(i, "not written: it did not match its recorded SHA-256")
                        if not tg:
                            continue
                        def meta(t, e=files[s]):
                            if os.name != "nt":
                                os.chmod(t, (e.get("x", 0o644) & 0o777) | 0o400)
                            try:
                                os.utime(t, (e["m"], e["m"]))
                            except (OverflowError, ValueError):
                                pass
                        try:
                            res = _restore_big(f, b, files[s], [plan[i] for i in tg], meta)
                        except Exception as ex:
                            res = [f"not written: this part of the archive could not be "
                                   f"decoded ({type(ex).__name__}: {ex})"] * len(tg)
                        for i, r in zip(tg, res):
                            if r is None:
                                written += 1
                                verified += 1 if verify else 0
                            else:
                                fail(i, r)
                    continue
                raw = _bundle_raw(f, b)
            except Exception as ex:
                # one damaged region of the archive costs the files in it, not
                # every file after it
                for s in members:
                    for i in targets[s]:
                        fail(i, f"not written: this part of the archive could not be "
                                f"decoded ({type(ex).__name__}: {ex})")
                continue
            for s in members:
                e = files[s]
                try:
                    data = invert_transforms(raw[e["o"]:e["o"] + e["l"]], e["t"])
                except (zpin.DeflateUnavailable, _jpeg.Unavailable) as ex:
                    for i in targets[s]:
                        fail(i, str(ex))
                    continue
                except Exception as ex:
                    for i in targets[s]:
                        fail(i, f"not written: it could not be decoded ({type(ex).__name__})")
                    continue
                ok = True
                if verify:
                    if e.get("ex", True):
                        # stored length is 32 hex (new) or 64 (old archives)
                        stored_h = e["h"]
                        ok = len(stored_h) >= HASH_HEX and \
                            hashlib.sha256(data).hexdigest()[:len(stored_h)] == stored_h
                    else:
                        ok = False
                        for st in reversed(e["t"]):
                            T = TRANSFORMS.get(st["n"])
                            if T is not None and not getattr(T, "exact", True):
                                try:
                                    ok = bool(T.verify_content(data, st["a"]))
                                except Exception:
                                    ok = False
                                break
                for i in targets[s]:
                    ti = files[i]
                    # a duplicate must carry the same recorded hash as the twin
                    # whose bytes it receives (the share page checks the same)
                    if not ok or ti["h"] != e["h"]:
                        fail(i, "not written: it did not match its recorded SHA-256")
                        continue
                    try:
                        _write_restored(plan[i], data, ti)
                    except OSError as ex:
                        fail(i, f"could not be written: {ex.strerror or ex}")
                        continue
                    written += 1
                    verified += 1 if verify else 0
                data = None
            raw = None

    return {"written": written, "verified": verified, "failed": failed,
            "notes": [{"why": k, "files": v} for k, v in notes.items()],
            "selected": len(want), "total": len(files),
            "partial": select is not None,
            "seconds": time.time() - t0}

# ==========================================================================
def _cli_parser():
    import argparse
    ap = argparse.ArgumentParser(prog="crush", description="Crush - content-aware compression")
    ap.add_argument("--version", action="version", version=f"Crush {CRUSH_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("c"); a.add_argument("out"); a.add_argument("paths", nargs="+")
    a.add_argument("--tier", default="balanced", choices=list(TIERS))
    a.add_argument("--photo", type=float, default=None,
                   help="perceptual photo tier: JPEG XL distance (0.5 = near-transparent, 1.5 = strong)")
    a.add_argument("--audio", default=None,
                   help="perceptual audio tier: Opus bitrate, e.g. 128k")
    a.add_argument("--no-echo", action="store_true", help="disable chunk deduplication")
    a.add_argument("-k", "--password", metavar="PASS",
                   help="encrypt the archive with this passphrase (Argon2id + "
                        "XChaCha20-Poly1305). Prefer CRUSH_PASSWORD in the "
                        "environment: a command line is visible to other users.")
    a.add_argument("--ask", action="store_true",
                   help="prompt for a passphrase (twice) instead of passing it "
                        "on the command line")
    a.add_argument("--kdf", default="balanced", choices=("fast", "balanced", "paranoid"),
                   help="key-stretching cost when encrypting")
    a.add_argument("--portable", action="store_true",
                   help="only transforms and codecs a browser can decode (for share pages)")
    a.add_argument("--share", action="store_true",
                   help="also write OUT.html: a self-opening page that extracts the archive in "
                        "any modern browser, offline, with nothing installed (implies --portable)")
    a.add_argument("--harder", action="store_true",
                   help="allow content-exact container recomposition (contents bit-identical, "
                        "zip-family containers re-packed)")
    b = sub.add_parser("x"); b.add_argument("archive"); b.add_argument("dest")
    b.add_argument("-k", "--password", metavar="PASS")
    b.add_argument("--overwrite", action="store_true",
                   help="replace files that already exist in DEST")
    b.add_argument("--only", nargs="+", metavar="PATH",
                   help="restore only these stored paths (see the 'l' command)")
    l = sub.add_parser("l"); l.add_argument("archive")
    l.add_argument("-k", "--password", metavar="PASS")
    return ap


def _cli(args):
    # A passphrase on the command line is visible in `ps` to every other user on
    # the machine, so the environment variable is offered and preferred, and an
    # interactive prompt is used when neither is given but a terminal is.
    pw = getattr(args, "password", None) or os.environ.get("CRUSH_PASSWORD")
    if args.cmd in ("x", "l") and not pw and is_encrypted(args.archive) and sys.stdin.isatty():
        import getpass
        pw = getpass.getpass("Passphrase: ")
    if args.cmd == "c" and pw and not os.environ.get("CRUSH_PASSWORD") \
            and sys.stdin.isatty() and getattr(args, "password", None):
        print("Warning: a passphrase given on the command line is visible to other\n"
              "users of this machine in the process list. CRUSH_PASSWORD, or the\n"
              "interactive prompt, avoids that.", file=sys.stderr)
    if args.cmd == "c" and not pw and sys.stdin.isatty() and getattr(args, "ask", False):
        import getpass
        pw = getpass.getpass("Passphrase: ")
        if pw != getpass.getpass("Confirm:    "):
            print("The passphrases do not match; nothing was written.", file=sys.stderr)
            sys.exit(1)
    if args.cmd == "c":
        def pr(s): print(f"\r  {s['stage']:<9} {s['i']+1}/{s['n']}  {s['name'][:44]:<44}", end="", flush=True)
        perc = {}
        if args.photo: perc["photo"] = args.photo
        if args.audio: perc["audio"] = args.audio
        r = compress(args.paths, args.out, args.tier, pr, harder=args.harder,
                     perceptual=perc or None, echo_dedup=not args.no_echo,
                     password=pw, kdf_profile=args.kdf,
                     portable=args.portable or args.share)
        lock = "  [encrypted]" if r.get("encrypted") else ""
        print(f"\n{r['in']:,} -> {r['out']:,} bytes  ({100*r['ratio']:.2f}%)  "
              f"in {r['seconds']:.1f}s{lock}")
        st = r.get("stats", {})
        if st.get("skipped_links"):
            print(f"  NOT INCLUDED: {st['skipped_links']} link(s) or special file(s) - "
                  "links are not followed", file=sys.stderr)
        for p in st.get("changed_while_reading", [])[:10]:
            print(f"  CHANGED WHILE READING (archived as read): {p}", file=sys.stderr)
        if args.share:
            page = make_share_page(args.out, password=pw)
            print(f"share page: {page} ({os.path.getsize(page):,} bytes)")
    elif args.cmd == "x":
        try:
            r = extract(args.archive, args.dest, select=args.only, password=pw,
                        overwrite=args.overwrite)
        except FileExistsError as ex:
            print(ex, "\n(--overwrite replaces them)", file=sys.stderr)
            sys.exit(3)
        scope = f" of {r['total']}" if r["partial"] else ""
        print(f"{r['written']} files{scope}, {r['verified']} verified, "
              f"{len(r['failed'])} failed, {r['seconds']:.1f}s")
        if r["failed"]:
            # A script that restores a backup must be able to tell that it did
            # not. Exiting 0 with files missing is how silent data loss happens.
            for bad in r["failed"][:10]:
                print(f"  NOT RESTORED: {bad}", file=sys.stderr)
            for n in r.get("notes", []):
                print(f"  {len(n['files'])} file(s): {n['why']}",
                      file=sys.stderr)
            sys.exit(2)
    else:
        idx = read_index(args.archive, pw)
        for e in idx["files"]:
            print(f"{e['s']:>12,}  {e['cls']:<10} {e['p']}")


if __name__ == "__main__":
    _args = _cli_parser().parse_args()
    try:
        _cli(_args)
    except KeyboardInterrupt:
        print("\ninterrupted - nothing was left half-written", file=sys.stderr)
        sys.exit(130)
    except Exception as _ex:
        # a damaged archive, a missing file or a full disk is a message for the
        # user, not a traceback; CRUSH_DEBUG=1 shows the traceback
        if os.environ.get("CRUSH_DEBUG"):
            raise
        print(f"\ncrush: {_ex}", file=sys.stderr)
        sys.exit(1)
