#!/usr/bin/env python3
# ============================================================================
#  crush_jpeg - lossless JPEG recompression through the crushjpg helper
#  ---------------------------------------------------------------------------
#  A JPEG's picture data is Huffman-coded DCT coefficients. crushjpg re-codes
#  the very same coefficients with a much stronger model - Brunsli (fast), or
#  jcm, Crush's own context-mixing model (stronger) - and rebuilds the
#  original file bit for bit. No external tool is needed; this
#  replaces the old "drop cjxl into tools/" requirement, and cjxl, if
#  present, still competes.
#
#  The helper is a separate process: the input at compression time may be any
#  file, and at extraction the packed data comes from an archive. A crash or
#  hang costs one image its recompression, never the whole run.
# ============================================================================
from __future__ import annotations
import os, struct, subprocess, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "crushjpg.exe" if sys.platform.startswith("win") else "crushjpg"
PATH = os.path.join(_HERE, NAME)
MAX_JPEG = 256 << 20


class Unavailable(RuntimeError):
    pass


def available() -> bool:
    return os.path.exists(PATH)


def _run(mode: str, blobs, timeout: float):
    body = b"".join(struct.pack("<Q", len(b)) + b for b in blobs)
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
    r = subprocess.run([PATH, mode], input=body, capture_output=True,
                       timeout=timeout, **kw)
    out, p = [], 0
    res = r.stdout
    try:
        for _ in blobs:
            ok = res[p]; p += 1
            if not ok:
                out.append(None)
                continue
            n = struct.unpack_from("<Q", res, p)[0]; p += 8
            if p + n > len(res):
                break
            out.append(bytes(res[p:p + n])); p += n
    except (IndexError, struct.error):
        pass
    out += [None] * (len(blobs) - len(out))       # helper stopped early
    return out


def pack(jpeg: bytes, strong: bool = False, timeout: float = 900.0):
    """Recompressed bytes, or None if this JPEG cannot be handled. strong
    selects jcm (Crush's context-mixing model) over Brunsli. Either way the
    caller must still invert and compare - Crush's framework always does."""
    if not available() or not jpeg or len(jpeg) > MAX_JPEG:
        return None
    try:
        return _run("E" if strong else "e", [jpeg], timeout)[0]
    except (OSError, subprocess.SubprocessError):
        return None


def unpack(packed: bytes, timeout: float = 1800.0) -> bytes:
    if not available():
        raise Unavailable("This archive contains JPEGs recompressed by Crush, and "
                          "rebuilding them needs engine/" + NAME + ", which is missing. "
                          "Restore it and extract again.")
    try:
        r = _run("d", [packed], timeout)[0]
    except (OSError, subprocess.SubprocessError) as e:
        raise Unavailable(f"{NAME} could not run ({e})")
    if r is None:
        raise Unavailable("a recompressed JPEG could not be rebuilt")
    return r


def pack_many(jpegs, strong: bool = False):
    """Batch form of pack(): one helper process for all of them."""
    if not available() or not jpegs:
        return [None] * len(jpegs)
    todo = [i for i, d in enumerate(jpegs) if d and len(d) <= MAX_JPEG]
    out = [None] * len(jpegs)
    try:
        res = _run("E" if strong else "e", [jpegs[i] for i in todo],
                   120.0 + sum(len(jpegs[i]) for i in todo) / (50_000 if strong else 400_000))
    except (OSError, subprocess.SubprocessError):
        return out
    for i, r in zip(todo, res):
        out[i] = r
    return out


def unpack_many(packed):
    if not packed:
        return []
    if not available():
        raise Unavailable("This archive contains JPEGs recompressed by Crush, and "
                          "rebuilding them needs engine/" + NAME + ", which is missing. "
                          "Restore it and extract again.")
    try:
        res = _run("d", list(packed), 600.0 + sum(len(p) for p in packed) / 20_000)
    except (OSError, subprocess.SubprocessError) as e:
        raise Unavailable(f"{NAME} could not run ({e})")
    if any(r is None for r in res):
        raise Unavailable("a recompressed JPEG could not be rebuilt")
    return res


def find_jpegs(d: bytes, min_len: int = 2048):
    """(start, end) of every complete baseline/progressive JPEG embedded in d:
    SOI, marker segments, entropy-coded data, EOI. Structure only - the helper
    decides whether each one is really a JPEG it can rebuild."""
    out, i, n = [], 0, len(d)
    while True:
        i = d.find(b"\xff\xd8\xff", i)
        if i < 0:
            return out
        p, ok = i + 2, False
        while p + 4 <= n and d[p] == 0xFF:
            m = d[p + 1]
            if m == 0xD9:
                ok, p = True, p + 2
                break
            if m == 0x01 or 0xD0 <= m <= 0xD7 or m == 0xFF:
                p += 1 if m == 0xFF else 2
                continue
            p += 2 + int.from_bytes(d[p + 2:p + 4], "big")
            if m == 0xDA:                       # entropy-coded data follows
                while p + 1 < n and not (d[p] == 0xFF and d[p + 1] != 0 and not 0xD0 <= d[p + 1] <= 0xD7):
                    p += 1
        if ok and p - i >= min_len:
            out.append((i, p))
            i = p
        else:
            i += 3
