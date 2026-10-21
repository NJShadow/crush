#!/usr/bin/env python3
# ============================================================================
#  crush_zpin - a deflate that will produce the same bytes forever
#  ---------------------------------------------------------------------------
#  Crush's container transforms (zip/docx, PNG, PDF, gzip) store the
#  DECOMPRESSED contents of a stream plus the deflate settings that rebuild
#  the original compressed bytes exactly. Extraction re-runs deflate with those
#  settings, so the deflate that runs at extraction must be the very same
#  program that proved them at compression.
#
#  Python's zlib module is not that promise. Python 3.14 on Windows ships
#  zlib-ng instead of zlib, and zlib-ng emits different bytes for the same
#  settings (measured: 11 of 15 level/strategy pairs differ on a 216 KB
#  file). Relying on it would mean an archive made today could fail its
#  checksum after a Python upgrade.
#
#  So Crush carries its own deflate: unmodified zlib 1.3.1, built as
#  crushz.dll / libcrushz.so from engine/crushz/. Every backend - the pinned
#  library or Python's own zlib - must pass a known-answer test against output
#  recorded from reference zlib before it is trusted. If nothing passes, the
#  container transforms are simply not applied at compression time, and
#  extraction of an archive that needs them stops with a plain explanation
#  instead of a checksum error.
# ============================================================================
from __future__ import annotations
import ctypes, hashlib, os, sys, zlib
from typing import Optional, Sequence

# The settings space. The ORDER of this list is part of the archive format:
# archives store an index into it. Never reorder or insert - only append.
PARAMS = []
for _lvl in (6, 9, 1, 8, 7, 5, 4, 3, 2):
    for _strat in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED, zlib.Z_HUFFMAN_ONLY,
                   zlib.Z_RLE, zlib.Z_FIXED):
        for _mem in (8, 9, 7, 6, 5, 4, 3, 2, 1):
            PARAMS.append((_lvl, _strat, _mem))

# The settings real producers overwhelmingly use. Measured on 12,131 rebuilt
# streams from 448 PDFs and 560 gzip files: default strategy with memLevel 8 or
# 9 covered 12,122 of them (99.9%). A cheap search for callers that cannot
# afford the full 405-way search on every miss.
COMMON = [i for i, (l, s, m) in enumerate(PARAMS)
          if s == zlib.Z_DEFAULT_STRATEGY and m in (8, 9)] + \
         [i for i, (l, s, m) in enumerate(PARAMS)
          if s == zlib.Z_FILTERED and m == 8 and l in (6, 9)]

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIBNAME = "crushz.dll" if sys.platform.startswith("win") else "libcrushz.so"

class DeflateUnavailable(RuntimeError):
    pass

# ---- known-answer test ------------------------------------------------------
# A fixed, generated input (no files involved) and a digest of reference zlib's
# output for a spread of settings. Recorded from zlib 1.3.1 and cross-checked
# against zlib 1.3; zlib-ng fails it.
def _kat_input() -> bytes:
    words = [b"the", b"crush", b"stream", b"object", b"endobj", b"0 0 R", b"/Type",
             b"\n", b"  ", b"<<", b">>", b"BT", b"ET", b"Tf", b"1.000", b"q Q"]
    x, out = 0x2545F491, bytearray()
    while len(out) < 60000:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out += words[(x >> 16) % len(words)] + b" "
        if (x >> 8) % 7 == 0:
            out += bytes([(x >> 3) & 0xFF, (x >> 11) & 0xFF])
    return bytes(out)

# small enough to run at every startup (~0.1 s), broad enough that zlib-ng
# and every other non-reference deflate fails it
_KAT_SET = [i for i, (l, s, m) in enumerate(PARAMS)
            if (l in (1, 6, 9) and m == 8) or (l in (3, 7) and s == 0 and m in (1, 9))]
_KAT_DIGEST = "97bb08e19d712f0607e69c514e391400a504855168a40ccf3b576fda0ef5d9a8"

def _kat_digest(fn) -> str:
    data = _kat_input()
    h = hashlib.sha256()
    for i in _KAT_SET:
        for wbits in (-15, 15):
            h.update(fn(i, data, wbits))
    return h.hexdigest()

# ---- backends ---------------------------------------------------------------
class _Pinned:
    name = "crushz"
    def __init__(self, path):
        lib = ctypes.CDLL(path)
        lib.crushz_version.restype = ctypes.c_char_p
        lib.crushz_deflate.restype = ctypes.c_int
        lib.crushz_deflate.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
        lib.crushz_match.restype = ctypes.c_int
        lib.crushz_match.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                                     ctypes.c_size_t, ctypes.c_int,
                                     ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        self.lib = lib
        self.version = lib.crushz_version().decode()

    def deflate(self, idx: int, raw: bytes, wbits: int) -> bytes:
        lvl, strat, mem = PARAMS[idx]
        cap = len(raw) + (len(raw) >> 3) + 4096
        while True:
            buf = ctypes.create_string_buffer(cap)
            n = ctypes.c_size_t(0)
            rc = self.lib.crushz_deflate(raw, len(raw), lvl, wbits, mem, strat,
                                         buf, cap, ctypes.byref(n))
            if rc == 0:
                return buf.raw[:n.value]
            if rc != 1:
                raise DeflateUnavailable(f"pinned deflate failed (code {rc})")
            cap *= 2

    def match(self, raw: bytes, target: bytes, wbits: int, order: Sequence[int]):
        if not order:
            return None
        arr = (ctypes.c_int * (3 * len(order)))()
        for k, i in enumerate(order):
            lvl, strat, mem = PARAMS[i]
            arr[3*k], arr[3*k+1], arr[3*k+2] = lvl, mem, strat
        k = self.lib.crushz_match(raw, len(raw), target, len(target), wbits, arr, len(order))
        return order[k] if k >= 0 else None

class _PyZlib:
    """Python's own zlib - used only if it proves to be reference zlib."""
    name = "python-zlib"
    version = zlib.ZLIB_RUNTIME_VERSION
    _PROBE = 1 << 18

    def deflate(self, idx, raw, wbits):
        lvl, strat, mem = PARAMS[idx]
        co = zlib.compressobj(lvl, zlib.DEFLATED, wbits, mem, strat)
        return co.compress(raw) + co.flush()

    def match(self, raw, target, wbits, order):
        # deflate is a stream: output for a prefix of the input is a prefix of
        # the output, so a 256 KB probe eliminates nearly every wrong guess.
        survivors = order
        if len(raw) > self._PROBE:
            head, survivors = raw[:self._PROBE], []
            for i in order:
                lvl, strat, mem = PARAMS[i]
                try:
                    out = zlib.compressobj(lvl, zlib.DEFLATED, wbits, mem, strat).compress(head)
                except Exception:
                    continue
                if len(out) < 64 or target.startswith(out):
                    survivors.append(i)
        for i in survivors:
            try:
                if self.deflate(i, raw, wbits) == target:
                    return i
            except Exception:
                continue
        return None

_backend = None
_why = ""

def _select():
    global _backend, _why
    if _backend is not None or _why:
        return _backend
    reasons = []
    path = os.path.join(_HERE, _LIBNAME)
    cands = []
    if os.path.exists(path):
        try:
            cands.append(_Pinned(path))
        except OSError as e:
            reasons.append(f"{_LIBNAME} would not load ({e})")
    else:
        reasons.append(f"{_LIBNAME} is missing from the engine folder")
    cands.append(_PyZlib())
    for b in cands:
        try:
            if _kat_digest(b.deflate) == _KAT_DIGEST:
                _backend = b
                return b
            reasons.append(f"{b.name} {b.version} does not reproduce reference zlib output")
        except Exception as e:
            reasons.append(f"{b.name} failed its self-check ({e})")
    _why = "; ".join(reasons)
    return None

def available() -> bool:
    return _select() is not None

def status() -> str:
    b = _select()
    if b is None:
        return "unavailable: " + _why
    return f"{b.name} (zlib {b.version})"

def deflate(idx: int, raw: bytes, wbits: int) -> bytes:
    b = _select()
    if b is None:
        raise DeflateUnavailable(
            "This archive contains files whose original compressed bytes are rebuilt "
            "with reference zlib, and no reference zlib is available here ("
            + _why + "). Restore engine/" + _LIBNAME + " and extract again.")
    return b.deflate(idx, raw, wbits)

def _zlib_header(lvl, strat, wbits) -> bytes:
    cmf = ((wbits - 8) << 4) | 8
    if strat >= zlib.Z_HUFFMAN_ONLY or lvl < 2: fl = 0
    elif lvl < 6: fl = 1
    elif lvl == 6: fl = 2
    else: fl = 3
    flg = fl << 6
    flg += 31 - ((cmf << 8) + flg) % 31
    return bytes([cmf, flg])

def match(raw: bytes, target: bytes, wbits: int, prefer: Sequence[int] = (),
          only_prefer: bool = False, exhaustive: bool = True) -> Optional[int]:
    """Index into PARAMS whose deflate of `raw` equals `target` exactly, or
    None. `prefer` is tried first - within one file, one producer usually used
    one setting throughout, so the last hit is nearly always the next hit.
    With only_prefer, nothing else is tried (a cheap mode for files where
    the full search has already missed many times)."""
    b = _select()
    if b is None:
        return None
    rest = [] if only_prefer else (list(range(len(PARAMS))) if exhaustive else COMMON)
    order = list(dict.fromkeys(list(prefer) + rest))
    if wbits > 0:
        # A zlib-wrapped stream announces its window and level class in the
        # first two bytes; only settings that write that exact header can match.
        if len(target) < 2 or not (9 <= wbits <= 15):
            return None
        order = [i for i in order
                 if _zlib_header(PARAMS[i][0], PARAMS[i][1], wbits) == target[:2]]
    return b.match(raw, target, wbits, order)

def selftest() -> str:
    return status()


# ============================================================================
#  Deflate that zlib did not write: the preflate helper
#  ---------------------------------------------------------------------------
#  When no zlib setting rebuilds a stream (GNU gzip, Chrome, Office, Adobe,
#  7-Zip...), crushpf re-parses it with preflate and returns the decompressed
#  data plus a small correction record. It runs as a separate process so a
#  hostile stream or record can never take Crush down with it, and it proves
#  every record (re-encode and compare) before reporting it.
# ============================================================================
import struct as _struct
import subprocess as _sp

_PF_NAME = "crushpf.exe" if sys.platform.startswith("win") else "crushpf"
PF_PATH = os.path.join(_HERE, _PF_NAME)
_PF_MAX_STREAM = 256 << 20            # larger streams are not attempted

def pf_available() -> bool:
    return os.path.exists(PF_PATH)

def _pf_run(mode: str, body: bytes, timeout: float):
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
    r = _sp.run([PF_PATH, mode], input=body, capture_output=True,
                timeout=timeout, **kw)
    return r.stdout

def _read_u64(b, p):
    return _struct.unpack_from("<Q", b, p)[0], p + 8

def pf_decode(streams, timeout: float = 600.0):
    """streams: list of raw-deflate byte strings. Returns a list with, per
    stream, (unpacked, correction) or None. Never raises for a bad stream -
    a crash, a timeout or a refusal all simply mean None."""
    out = [None] * len(streams)
    todo = [i for i, s in enumerate(streams) if 0 < len(s) <= _PF_MAX_STREAM]
    if not todo or not pf_available():
        return out
    body = b"".join(_struct.pack("<Q", len(streams[i])) + streams[i] for i in todo)
    try:
        res = _pf_run("d", body, timeout)
    except (OSError, _sp.SubprocessError):
        return out
    p = 0
    try:
        for i in todo:
            ok = res[p]; p += 1
            if not ok:
                continue
            n, p = _read_u64(res, p); un = res[p:p + n]; p += n
            m, p = _read_u64(res, p); diff = res[p:p + m]; p += m
            if len(un) != n or len(diff) != m:
                break
            out[i] = (bytes(un), bytes(diff))
    except (IndexError, _struct.error):
        pass                                   # helper died part-way: keep what arrived
    return out

def pf_reencode(pairs, timeout: float = 3600.0):
    """pairs: list of (correction, unpacked). Returns the rebuilt deflate
    streams, or raises DeflateUnavailable naming what is missing."""
    if not pairs:
        return []
    if not pf_available():
        raise DeflateUnavailable(
            "This archive contains files whose original compressed bytes were "
            "written by a deflate other than zlib, and rebuilding them needs engine/"
            + _PF_NAME + ", which is missing. Restore it and extract again.")
    body = b"".join(_struct.pack("<Q", len(d)) + d + _struct.pack("<Q", len(u)) + u
                    for d, u in pairs)
    try:
        res = _pf_run("r", body, timeout)
    except (OSError, _sp.SubprocessError) as e:
        raise DeflateUnavailable(f"{_PF_NAME} could not run ({e})")
    out, p = [], 0
    try:
        for _ in pairs:
            ok = res[p]; p += 1
            if not ok:
                raise DeflateUnavailable("a stored deflate correction record was rejected")
            n, p = _read_u64(res, p)
            out.append(bytes(res[p:p + n])); p += n
    except (IndexError, _struct.error):
        raise DeflateUnavailable(f"{_PF_NAME} stopped before finishing")
    return out
