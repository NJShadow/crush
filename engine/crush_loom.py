#!/usr/bin/env python3
# ===========================================================================
#  LOOM - automatic structure discovery in unknown binary data
#  ---------------------------------------------------------------------------
#  Crush knows what to do with a JSON file or a WAV file because somebody wrote
#  a parser for it.  For a sensor dump, a game asset, a proprietary record file
#  or a serialised tensor there is no parser, and the engine falls back to
#  guessing a fixed byte-transpose width (2/4/8) or to treating the file as an
#  opaque stream.
#
#  Almost every such file is really an ARRAY OF RECORDS, and almost all of its
#  redundancy runs DOWN THE COLUMNS rather than along the byte stream.  Loom
#  discovers that layout with no format knowledge:
#
#     1. stride detection      - where does the record array start, how long is
#                                a record, where does it end
#     2. column typing         - constant / enum / counter / int plane / float
#                                plane / text / noise, measured not guessed
#     3. field regrouping      - adjacent byte planes that are really one wider
#                                little- or big-endian value are coded as that
#                                value (modular delta + zigzag) instead of as
#                                independent planes
#     4. per-field coding      - fields are binned by measured character and
#                                each bin gets its own codec
#     5. variable-length       - if no stride fits, look for a record delimiter
#                                and shred left-aligned on that instead
#
#  Everything Loom decides is decided by MEASUREMENT, and the final choice is
#  made by actually compressing the candidates.  If the structured path loses
#  to plain compression Loom says so and emits the plain path.
#
#  API mirrors the transforms in crush_engine.py:
#      forward(data)          -> (payload, aux)      reversible byte transform
#      inverse(payload, aux)  -> data
#      compress(data)         -> blob                transform + per-field codecs
#      decompress(blob)       -> data
# ===========================================================================
from __future__ import annotations

import json
import lzma
import math
import struct
import zlib
from typing import Optional

import numpy as np

try:
    import zstandard as zstd
except ImportError:                                          # pragma: no cover
    zstd = None
try:
    import brotli
except ImportError:                                          # pragma: no cover
    brotli = None


VERSION = 4
MAGIC = b"LOOM"

# ---- tuning ---------------------------------------------------------------
MAX_STRIDE = 4096          # largest record size we will consider
MIN_STRIDE = 2
SEG_SAMPLE = 1 << 20       # bytes fed to the expensive column/field analysis
SEG_ROWS = 16384           # ... and never more rows than this
CAND_SAMPLE = 1 << 22      # bytes fed to the cheap stride search
TRIAL_SAMPLE = 1 << 20     # bytes fed to the "actually compress it" trial
FIELD_OVERHEAD_BITS = 48.0  # discourages shredding into needless fields
MIN_BODY = 4096
N_STRIDES = 4              # strides carried into the full analysis

# field modes
M_RAW = 0      # planes as-is
M_DELTA = 1    # modular delta + zigzag on the whole value, then planes
M_XOR = 2      # per-plane xor with the previous row

# endianness
E_LE = 0
E_BE = 1


# ===========================================================================
#  small numeric helpers
# ===========================================================================
def _entropy_bits_from_counts(counts: np.ndarray, n: int) -> np.ndarray:
    """counts: (G, 256) int64 histogram per group.  Returns bits per group
    (total, not per symbol) including a crude MDL model cost so that a wider
    stride is not automatically 'better' just because it has more columns."""
    if n <= 0:
        return np.zeros(counts.shape[0], dtype=np.float64)
    c = counts.astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        lg = np.where(c > 0, np.log2(np.maximum(c, 1.0)), 0.0)
    tot = c.sum(axis=1)
    tot = np.maximum(tot, 1.0)
    bits = tot * np.log2(tot) - (c * lg).sum(axis=1)
    k = (counts > 0).sum(axis=1).astype(np.float64)
    model = np.maximum(k - 1.0, 0.0) * 0.5 * math.log2(max(n, 2))
    return bits + model


_OFFS_CACHE = {}


def _col_counts(arr2d: np.ndarray) -> np.ndarray:
    """arr2d: (R, G) uint8.  Returns (G, 256) per-column histogram.

    One bincount over the whole grid rather than G separate ones - the
    difference is three orders of magnitude when G is a few thousand."""
    r, g = arr2d.shape
    if r == 0:
        return np.zeros((g, 256), dtype=np.int64)
    offs = _OFFS_CACHE.get(g)
    if offs is None or offs.shape[0] != g:
        offs = (np.arange(g, dtype=np.intp) * 256)
        _OFFS_CACHE.clear()
        _OFFS_CACHE[g] = offs
    idx = np.empty((r, g), dtype=np.intp)
    np.add(arr2d, offs[None, :], out=idx, casting="unsafe")
    return np.bincount(idx.ravel(), minlength=g * 256).reshape(g, 256)


def _zigzag(d: np.ndarray, bits: int) -> np.ndarray:
    top = (d >> np.uint64(bits - 1)) & np.uint64(1)
    return ((d << np.uint64(1)) ^ (np.uint64(0) - top))


def _unzigzag(z: np.ndarray, bits: int) -> np.ndarray:
    mask = np.uint64((1 << bits) - 1) if bits < 64 else np.uint64(0xFFFFFFFFFFFFFFFF)
    return ((z >> np.uint64(1)) ^ (np.uint64(0) - (z & np.uint64(1)))) & mask


# ===========================================================================
#  1. STRIDE DETECTION
# ===========================================================================
def _repeat_distance_hist(a: np.ndarray, maxs: int) -> np.ndarray:
    """Histogram of the distance from each 4-gram to the previous occurrence of
    the same 4-gram.  A record array of stride S puts a huge spike at S (and
    smaller ones at 2S, 3S ...) because the low-entropy columns repeat.

    Done without a Python loop: sort positions by gram value, then take
    within-group successive differences."""
    n = a.size
    if n < 4096:
        return np.zeros(maxs + 1, dtype=np.int64)
    m = n - 3
    h = (a[0:m].astype(np.uint32)
         | (a[1:m + 1].astype(np.uint32) << np.uint32(8))
         | (a[2:m + 2].astype(np.uint32) << np.uint32(16))
         | (a[3:m + 3].astype(np.uint32) << np.uint32(24)))
    order = np.argsort(h, kind="stable")
    hs = h[order]
    same = hs[1:] == hs[:-1]
    if not same.any():
        return np.zeros(maxs + 1, dtype=np.int64)
    d = (order[1:].astype(np.int64) - order[:-1].astype(np.int64))[same]
    d = d[(d > 0) & (d <= maxs)]
    if d.size == 0:
        return np.zeros(maxs + 1, dtype=np.int64)
    return np.bincount(d, minlength=maxs + 1).astype(np.int64)


FIXED_CANDIDATES = [2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80,
                    96, 128, 160, 192, 256, 320, 384, 512, 768, 1024, 1536,
                    2048, 3072, 4096]


def _stride_candidates(data: bytes, maxs: int) -> list:
    a = np.frombuffer(data[:CAND_SAMPLE], dtype=np.uint8)
    hist = _repeat_distance_hist(a, maxs)
    cands = set()
    if hist.size and hist[MIN_STRIDE:].sum() > 0:
        h = hist.copy()
        h[:MIN_STRIDE] = 0
        # top peaks, and for each peak its small divisors (harmonics of the
        # true fundamental show up as strong as the fundamental itself)
        top = np.argsort(h)[::-1][:16]
        for s in top:
            s = int(s)
            if h[s] <= 0:
                continue
            cands.add(s)
            for f in (2, 3, 4, 5, 6, 8, 12, 16):
                if s % f == 0 and s // f >= MIN_STRIDE:
                    cands.add(s // f)
    for s in FIXED_CANDIDATES:
        if MIN_STRIDE <= s <= maxs:
            cands.add(s)
    return sorted(c for c in cands if MIN_STRIDE <= c <= maxs)


def _mdl_score(a: np.ndarray, s: int) -> float:
    """Bits per byte of the column-wise order-0 model at stride s (with MDL
    model cost).  Cheap, and good enough to shortlist."""
    r = a.size // s
    if r < 8:
        return 1e18
    g = a[: r * s].reshape(r, s)
    counts = _col_counts(g)
    bits = _entropy_bits_from_counts(counts, r).sum()
    # also the row-delta view, which is what a counter column looks like
    gd = g.copy()
    gd[1:] ^= g[:-1]
    bits2 = _entropy_bits_from_counts(_col_counts(gd), r).sum()
    return min(bits, bits2) / float(r * s)


def _shortlist_strides(data: bytes, maxs: int, keep: int = N_STRIDES) -> list:
    cands = _stride_candidates(data, maxs)
    if not cands:
        return []
    want = min(1 << 21, max(1 << 18, 96 * maxs))
    off = 0
    if len(data) > 4 * want:            # skip a header if there is room
        off = len(data) // 8
    a = np.frombuffer(data[off:off + want], dtype=np.uint8)
    scored = []
    for s in cands:
        if a.size // s < 8:
            continue
        sc = _mdl_score(a, s)
        # gentle preference for the smaller stride among near-ties: a true
        # fundamental is never worse than its harmonic by more than noise
        scored.append((sc * (1.0 + 0.0006 * math.log2(max(s, 2))), s, sc))
    scored.sort()
    out = []
    for _, s, _sc in scored:
        if s not in out:
            out.append(s)
        if len(out) >= keep:
            break
    return out


def _periodic_region(a: np.ndarray, s: int):
    """Locate the [start, end) span over which the file actually repeats with
    period s, so a header/preamble and a trailer can be split off."""
    n = a.size
    if n < 8 * s or s >= n:
        return 0, n
    m = (a[:-s] == a[s:]).astype(np.float32)
    w = int(min(max(4 * s, 2048), max(m.size // 8, 1)))
    if w < 1 or m.size < 2 * w:
        return 0, n
    cs = np.concatenate(([0.0], np.cumsum(m, dtype=np.float64)))
    avg = (cs[w:] - cs[:-w]) / w                  # avg[i] covers [i, i+w)
    if avg.size < 4:
        return 0, n
    hi = float(np.percentile(avg, 90))
    lo = float(np.percentile(avg, 10))
    if hi - lo < 0.12:                            # homogeneous file
        return 0, n
    thr = lo + 0.55 * (hi - lo)
    good = avg >= thr
    if not good.any():
        return 0, n
    idx = np.flatnonzero(good)
    b = int(idx[0])
    e = int(min(idx[-1] + w + s, n))
    if e - b < max(MIN_BODY, 16 * s):
        return 0, n
    if (e - b) > 0.97 * n:
        return 0, n
    return b, e


# ===========================================================================
#  2/3. COLUMN TYPING AND FIELD REGROUPING
# ===========================================================================
def _plane_costs(grid: np.ndarray):
    """grid: (R, S) uint8.  Per-column cost in bits for the two decomposable
    modes (raw planes, xor-with-previous-row planes)."""
    r = grid.shape[0]
    raw = _entropy_bits_from_counts(_col_counts(grid), r)
    gx = grid.copy()
    if r > 1:
        gx[1:] ^= grid[:-1]
    xor = _entropy_bits_from_counts(_col_counts(gx), r)
    return raw, xor


def _delta_costs(grid: np.ndarray, w: int, endian: int, u=None) -> np.ndarray:
    """Cost in bits of coding columns [s, s+w) as one w-byte integer with
    modular delta + zigzag, for EVERY start s at once.

    Vectorised: build the (R, S-w+1) value matrix for all starts in one go,
    delta it down the rows, zigzag, then histogram all planes with a single
    bincount per plane.  Doing this per start with a Python loop would be
    ~S times slower and is what makes naive field discovery unusable."""
    r, s_all = grid.shape
    ns = s_all - w + 1
    if ns <= 0 or r < 4:
        return np.full(max(ns, 0), 1e18)
    if u is None:
        u = grid.astype(np.uint64)
    v = np.zeros((r, ns), dtype=np.uint64)
    for i in range(w):
        sh = np.uint64(8 * (i if endian == E_LE else (w - 1 - i)))
        v += u[:, i:i + ns] << sh
    bits = 8 * w
    if bits < 64:
        mask = np.uint64((1 << bits) - 1)
    else:
        mask = np.uint64(0xFFFFFFFFFFFFFFFF)
    d = np.empty_like(v)
    d[0] = v[0]
    d[1:] = (v[1:] - v[:-1]) & mask
    z = _zigzag(d, bits) & mask
    total = np.zeros(ns, dtype=np.float64)
    for i in range(w):
        pl = ((z >> np.uint64(8 * i)) & np.uint64(255)).astype(np.uint8)
        total += _entropy_bits_from_counts(_col_counts(pl), r)
    return total


def _segment_fields(grid: np.ndarray):
    """Dynamic program over the S columns choosing a partition into fields of
    width 1/2/4/8 with a coding mode, minimising estimated bits."""
    r, s = grid.shape
    raw, xor = _plane_costs(grid)
    unit = np.minimum(raw, xor)
    unit_mode = np.where(raw <= xor, M_RAW, M_XOR)

    delta = {}
    u = grid.astype(np.uint64)
    for w in (2, 4, 8):
        if w > s:
            continue
        for endian in (E_LE, E_BE):
            delta[(w, endian)] = _delta_costs(grid, w, endian, u=u)
    del u

    INF = 1e30
    best = np.full(s + 1, INF)
    best[0] = 0.0
    choice = [None] * (s + 1)
    for c in range(1, s + 1):
        # width 1
        v = best[c - 1] + unit[c - 1] + FIELD_OVERHEAD_BITS
        bw, bm, be = 1, int(unit_mode[c - 1]), E_LE
        for w in (2, 4, 8):
            if w > c:
                continue
            st = c - w
            if best[st] >= INF:
                continue
            for endian in (E_LE, E_BE):
                arr = delta.get((w, endian))
                if arr is None or st >= arr.size:
                    continue
                cand = best[st] + float(arr[st]) + FIELD_OVERHEAD_BITS
                if cand < v:
                    v, bw, bm, be = cand, w, M_DELTA, endian
        best[c] = v
        choice[c] = (bw, bm, be)

    fields = []
    c = s
    while c > 0:
        w, m, e = choice[c]
        fields.append((w, m, e))
        c -= w
    fields.reverse()
    return fields, float(best[s])


# ===========================================================================
#  transform of a single field
# ===========================================================================
def _field_forward(sub: np.ndarray, w: int, mode: int, endian: int) -> np.ndarray:
    """sub: (R, w) uint8 -> flat uint8 of length R*w (planes concatenated)."""
    r = sub.shape[0]
    if r == 0:
        return np.zeros(0, dtype=np.uint8)
    if mode == M_RAW:
        return np.ascontiguousarray(sub.T).ravel()
    if mode == M_XOR:
        p = np.ascontiguousarray(sub.T)          # (w, R)
        out = p.copy()
        if r > 1:
            out[:, 1:] ^= p[:, :-1]
        return out.ravel()
    # M_DELTA
    u = sub.astype(np.uint64)
    v = np.zeros(r, dtype=np.uint64)
    for i in range(w):
        sh = np.uint64(8 * (i if endian == E_LE else (w - 1 - i)))
        v += u[:, i] << sh
    bits = 8 * w
    mask = np.uint64((1 << bits) - 1) if bits < 64 else np.uint64(0xFFFFFFFFFFFFFFFF)
    d = np.empty_like(v)
    d[0] = v[0]
    d[1:] = (v[1:] - v[:-1]) & mask
    z = _zigzag(d, bits) & mask
    out = np.empty((w, r), dtype=np.uint8)
    for i in range(w):
        out[i] = ((z >> np.uint64(8 * i)) & np.uint64(255)).astype(np.uint8)
    return out.ravel()


def _field_inverse(flat: np.ndarray, r: int, w: int, mode: int, endian: int) -> np.ndarray:
    """flat: R*w uint8 -> (R, w) uint8."""
    if r == 0:
        return np.zeros((0, w), dtype=np.uint8)
    p = flat.reshape(w, r)
    if mode == M_RAW:
        return np.ascontiguousarray(p.T)
    if mode == M_XOR:
        out = p.copy()
        if r > 1:
            out = np.bitwise_xor.accumulate(out, axis=1)
        return np.ascontiguousarray(out.T)
    bits = 8 * w
    mask = np.uint64((1 << bits) - 1) if bits < 64 else np.uint64(0xFFFFFFFFFFFFFFFF)
    z = np.zeros(r, dtype=np.uint64)
    for i in range(w):
        z += p[i].astype(np.uint64) << np.uint64(8 * i)
    d = _unzigzag(z, bits)
    v = np.zeros(r, dtype=np.uint64)
    np.cumsum(d, dtype=np.uint64, out=v)
    v &= mask
    out = np.empty((r, w), dtype=np.uint8)
    for i in range(w):
        sh = np.uint64(8 * (i if endian == E_LE else (w - 1 - i)))
        out[:, i] = ((v >> sh) & np.uint64(255)).astype(np.uint8)
    return out


# ===========================================================================
#  the stride plan
# ===========================================================================
def _build_stride_plan(data: bytes, s: int, use_region: bool = True):
    a = np.frombuffer(data, dtype=np.uint8)
    n = a.size
    b, e = (_periodic_region(a, s) if use_region else (0, n))
    r = (e - b) // s
    if r < 8:
        return None
    e = b + r * s
    # analyse a sample of the body
    rows_cap = max(8, min(SEG_ROWS, SEG_SAMPLE // s))
    rs = min(r, rows_cap)
    grid = np.ascontiguousarray(a[b:b + rs * s].reshape(rs, s))
    fields, bits = _segment_fields(grid)
    return {"kind": "stride", "S": s, "B": b, "E": e, "R": r,
            "fields": fields, "est_bits_per_byte": bits / max(rs * s, 1)}


def _apply_stride_plan(data: bytes, plan: dict):
    a = np.frombuffer(data, dtype=np.uint8)
    s, b, e, r = plan["S"], plan["B"], plan["E"], plan["R"]
    grid = a[b:e].reshape(r, s)
    parts = []
    sizes = []
    c = 0
    for (w, mode, endian) in plan["fields"]:
        blk = _field_forward(np.ascontiguousarray(grid[:, c:c + w]), w, mode, endian)
        parts.append(blk)
        sizes.append(int(blk.size))
        c += w
    body = np.concatenate(parts) if parts else np.zeros(0, dtype=np.uint8)
    head = data[:b]
    tail = data[e:]
    aux = {"v": VERSION, "kind": "stride", "n": len(data), "S": s, "B": b,
           "E": e, "R": r, "fields": [list(f) for f in plan["fields"]],
           "sizes": sizes, "hlen": len(head), "tlen": len(tail)}
    payload = body.tobytes() + head + tail
    return payload, aux


def _invert_stride(payload: bytes, aux: dict) -> bytes:
    s, b, e, r = aux["S"], aux["B"], aux["E"], aux["R"]
    sizes = aux["sizes"]
    total = sum(sizes)
    flat = np.frombuffer(payload[:total], dtype=np.uint8)
    head = payload[total:total + aux["hlen"]]
    tail = payload[total + aux["hlen"]:total + aux["hlen"] + aux["tlen"]]
    grid = np.empty((r, s), dtype=np.uint8)
    off = 0
    c = 0
    for (w, mode, endian), sz in zip(aux["fields"], sizes):
        blk = flat[off:off + sz]
        grid[:, c:c + w] = _field_inverse(blk, r, w, mode, endian)
        off += sz
        c += w
    return head + grid.tobytes() + tail


# ===========================================================================
#  5. VARIABLE-LENGTH RECORDS (delimiter shred)
# ===========================================================================
MAX_VLEN_COLS = 2048


def _delim_candidates(data: bytes):
    a = np.frombuffer(data[:CAND_SAMPLE], dtype=np.uint8)
    if a.size < 4096:
        return []
    cnt = np.bincount(a, minlength=256).astype(np.float64)
    n = float(a.size)
    out = []
    # a real delimiter is not necessarily the MOST common byte - in a log of
    # ASCII records 'e' and '/' both beat the 0x1E that actually ends the
    # record - so score delimiter-ness, do not rank by frequency
    for v in np.argsort(cnt)[::-1][:32]:
        v = int(v)
        c = cnt[v]
        if c < 64:
            continue
        avg = n / c
        if avg < 4 or avg > MAX_VLEN_COLS:
            continue
        pos = np.flatnonzero(a == v)
        gaps = np.diff(pos)
        if gaps.size < 32 or gaps.max() > MAX_VLEN_COLS:
            continue
        cv = float(gaps.std()) / max(float(gaps.mean()), 1e-9)
        # a delimiter never occurs twice inside one record, so tiny gaps are
        # evidence AGAINST it
        tiny = float((gaps <= 2).mean())
        out.append((cv + 3.0 * tiny, v))
    out.sort()
    return [v for _s, v in out[:3]]


def _build_vlen_plan(data: bytes, delim: int, align: int = 0):
    a = np.frombuffer(data, dtype=np.uint8)
    pos = np.flatnonzero(a == delim)
    if pos.size < 64:
        return None
    ends = pos + 1                       # record includes its delimiter
    starts = np.concatenate(([0], ends[:-1]))
    lens = (ends - starts).astype(np.int64)
    if lens.max() > MAX_VLEN_COLS:
        return None
    return {"kind": "vlen", "delim": delim, "align": align,
            "nrec": int(pos.size), "maxlen": int(lens.max()),
            "bodyend": int(ends[-1])}


def _vlen_layout(lens: np.ndarray, maxlen: int, align: int):
    """Yield (j, boolean mask, offset-within-record) for each shred column."""
    body = lens - 1                                # content, delimiter removed
    for j in range(maxlen - 1):
        if align == 0:
            sel = body > j
            yield sel, j
        else:
            sel = body > j
            yield sel, None                        # offset computed per record


def _apply_vlen_plan(data: bytes, plan: dict):
    a = np.frombuffer(data, dtype=np.uint8)
    delim = plan["delim"]
    align = plan.get("align", 0)
    pos = np.flatnonzero(a == delim)
    ends = pos + 1
    starts = np.concatenate(([0], ends[:-1]))
    lens = (ends - starts).astype(np.int64)
    body = lens - 1
    maxlen = int(lens.max())
    # length stream as two byte planes (maxlen <= MAX_VLEN_COLS fits in u16)
    lz = lens.astype(np.uint16)
    lplanes = np.empty((2, lz.size), dtype=np.uint8)
    lplanes[0] = (lz & np.uint16(255)).astype(np.uint8)
    lplanes[1] = (lz >> np.uint16(8)).astype(np.uint8)
    lenblob = lplanes.ravel().tobytes()
    cols = []
    for j in range(maxlen - 1):
        sel = body > j
        if not sel.any():
            continue
        if align == 0:
            idx = starts[sel] + j
        else:
            idx = starts[sel] + body[sel] - 1 - j
        cols.append(a[idx])
    colblob = np.concatenate(cols).tobytes() if cols else b""
    tail = data[int(ends[-1]):]
    aux = {"v": VERSION, "kind": "vlen", "n": len(data), "delim": delim,
           "align": align, "nrec": int(lens.size), "maxlen": maxlen,
           "lenblob": len(lenblob), "colblob": len(colblob), "tlen": len(tail)}
    return lenblob + colblob + tail, aux


def _invert_vlen(payload: bytes, aux: dict) -> bytes:
    nrec = aux["nrec"]
    lb = aux["lenblob"]
    align = aux.get("align", 0)
    lp = np.frombuffer(payload[:lb], dtype=np.uint8).reshape(2, nrec)
    lens = (lp[0].astype(np.int64) | (lp[1].astype(np.int64) << 8))
    body = lens - 1
    ends = np.cumsum(lens)
    starts = ends - lens
    total = int(ends[-1])
    out = np.empty(total, dtype=np.uint8)
    out[ends - 1] = aux["delim"]
    col = np.frombuffer(payload[lb:lb + aux["colblob"]], dtype=np.uint8)
    off = 0
    for j in range(aux["maxlen"] - 1):
        sel = body > j
        k = int(sel.sum())
        if not k:
            continue
        if align == 0:
            idx = starts[sel] + j
        else:
            idx = starts[sel] + body[sel] - 1 - j
        out[idx] = col[off:off + k]
        off += k
    tail = payload[lb + aux["colblob"]:lb + aux["colblob"] + aux["tlen"]]
    return out.tobytes() + tail


# ===========================================================================
#  public transform API
# ===========================================================================
def _trial_size(blob: bytes) -> int:
    if zstd is None:
        return len(zlib.compress(blob, 6))
    return len(zstd.ZstdCompressor(level=12).compress(blob))


def analyze(data: bytes, verbose: bool = False) -> Optional[dict]:
    """Pick the best structural plan for `data`, or None if nothing beats the
    raw stream.  The decision is made by actually compressing a sample."""
    n = len(data)
    if n < MIN_BODY:
        return None
    maxs = min(MAX_STRIDE, max(2, n // 32))
    strides = _shortlist_strides(data, maxs)
    plans = []
    for i, s in enumerate(strides):
        # region detection is only worth a second full analysis for the
        # front-runner; for the rest one plan each
        variants = (True, False) if (i == 0 and n > 1 << 16) else (True,)
        for use_region in variants:
            p = _build_stride_plan(data, s, use_region=use_region)
            if p is None:
                continue
            key = (p["S"], p["B"], p["E"])
            if any((q.get("S"), q.get("B"), q.get("E")) == key for q in plans):
                continue
            plans.append(p)
    for d in _delim_candidates(data):
        for align in (0, 1):
            p = _build_vlen_plan(data, d, align)
            if p is not None:
                plans.append(p)
    if not plans:
        return None

    # --- trial: actually compress a window of each plan's own body ---------
    best, best_ratio = None, 0.999
    for p in plans:
        try:
            sp, lo, hi = _trial_window(p, n)
            if sp is None:
                continue
            chunk = data[lo:hi]
            pay, _aux = _apply(chunk, sp)
        except Exception:
            continue
        base = _trial_size(chunk)
        sz = _trial_size(pay)
        ratio = sz / max(base, 1)
        if verbose:
            print(f"    trial {p['kind']:6s} key={p.get('S', p.get('delim'))!s:>5} "
                  f"B={p.get('B', 0):>9} {base:>8} -> {sz:>8}  x{ratio:.3f}")
        if ratio < best_ratio:
            best_ratio, best = ratio, p
    if best is None:
        return None
    best = dict(best)
    best["trial_ratio"] = best_ratio
    return best


def _trial_window(plan: dict, n: int):
    """A window of the file that this plan actually covers, plus the plan
    restricted to it.  Comparing each plan on its own body and scoring by
    RATIO keeps plans with different [B,E) spans comparable."""
    p = dict(plan)
    if p["kind"] == "stride":
        s = p["S"]
        rows = max(8, TRIAL_SAMPLE // s)
        lo = p["B"]
        hi = min(p["E"], lo + rows * s)
        r = (hi - lo) // s
        if r < 8:
            return None, 0, 0
        p["B"], p["E"], p["R"] = 0, r * s, r
        return p, lo, lo + r * s
    # vlen: _apply_vlen_plan re-derives the record boundaries from whatever
    # slice it is handed, so the plan needs no rescaling
    return p, 0, min(n, TRIAL_SAMPLE)


def _apply(data: bytes, plan: dict):
    if plan["kind"] == "stride":
        return _apply_stride_plan(data, plan)
    return _apply_vlen_plan(data, plan)


def forward(data: bytes):
    """Reversible structure-aware reordering.  Returns (payload, aux) or None
    when Loom finds no structure worth exploiting."""
    plan = analyze(data)
    if plan is None:
        return None
    if plan["kind"] == "vlen":
        plan = _build_vlen_plan(data, plan["delim"])
        if plan is None:
            return None
    return _apply(data, plan)


def inverse(payload: bytes, aux) -> bytes:
    if aux["kind"] == "stride":
        return _invert_stride(payload, aux)
    if aux["kind"] == "vlen":
        return _invert_vlen(payload, aux)
    if aux["kind"] == "plain":
        return payload
    raise ValueError("unknown loom aux kind")


# ===========================================================================
#  4. PER-FIELD CODING  (container)
# ===========================================================================
def _c_store(d):    return d
def _c_lzma(d):
    return lzma.compress(d, format=lzma.FORMAT_RAW, filters=[
        dict(id=lzma.FILTER_LZMA2, preset=9 | lzma.PRESET_EXTREME,
             dict_size=min(max(1 << 20, 1 << (len(d).bit_length())), 64 << 20))])
def _d_lzma(b, ds):
    return lzma.decompress(b, format=lzma.FORMAT_RAW, filters=[
        dict(id=lzma.FILTER_LZMA2, preset=9 | lzma.PRESET_EXTREME, dict_size=ds)])
def _c_zstd(d):
    p = zstd.ZstdCompressionParameters.from_level(19, window_log=27, enable_ldm=1)
    return zstd.ZstdCompressor(compression_params=p).compress(d)
def _d_zstd(b):
    return zstd.ZstdDecompressor().decompress(b, max_output_size=1 << 31)
def _c_brotli(d):   return brotli.compress(d, quality=11, lgwin=24)


# id -> (encode, decode)
_CODECS = {
    0: ("store", _c_store, lambda b, n: b),
    1: ("lzma", None, None),
    2: ("zstd19", _c_zstd, lambda b, n: _d_zstd(b)),
    3: ("brotli", _c_brotli, lambda b, n: brotli.decompress(b)),
}
_LZMA_DICT = 64 << 20


def _lzma_enc(d):
    return lzma.compress(d, format=lzma.FORMAT_RAW, filters=[
        dict(id=lzma.FILTER_LZMA2, preset=9 | lzma.PRESET_EXTREME,
             dict_size=_LZMA_DICT)])


def _lzma_dec(b, n=0):
    return lzma.decompress(b, format=lzma.FORMAT_RAW, filters=[
        dict(id=lzma.FILTER_LZMA2, preset=9 | lzma.PRESET_EXTREME,
             dict_size=_LZMA_DICT)])


_CODECS[1] = ("lzma", _lzma_enc, _lzma_dec)


def _pick_codec(blob: bytes, quick: bool = False):
    """Smallest of the codec set, verified by decoding it back."""
    if not blob:
        return 0, b""
    best_id, best = 0, blob
    ids = [1, 2] if quick else [1, 2, 3]
    if len(blob) > (48 << 20):
        ids = [2]
    for cid in ids:
        enc = _CODECS[cid][1]
        if enc is None:
            continue
        try:
            out = enc(blob)
        except Exception:
            continue
        if out is None or len(out) >= len(best):
            continue
        try:
            if _CODECS[cid][2](out, len(blob)) != blob:
                continue
        except Exception:
            continue
        best_id, best = cid, out
    return best_id, best


def _bin_of(bits_per_byte: float) -> int:
    if bits_per_byte < 1.0:
        return 0
    if bits_per_byte < 5.0:
        return 1
    return 2


def _field_bin_bits(blk: np.ndarray) -> float:
    if blk.size == 0:
        return 0.0
    c = np.bincount(blk, minlength=256).astype(np.float64)
    tot = c.sum()
    nz = c[c > 0]
    h = np.log2(tot) - float((nz * np.log2(nz)).sum()) / tot
    return h


def _pack_header(meta: dict) -> bytes:
    raw = zlib.compress(json.dumps(meta, separators=(",", ":")).encode(), 9)
    return struct.pack("<I", len(raw)) + raw


def compress(data: bytes, verbose: bool = False) -> bytes:
    """Full Loom path: discover structure, transform, code each field bin with
    its own codec.  Falls back to plain compression whenever that is smaller.
    Output is never larger than a stored copy plus 9 bytes."""
    plan = analyze(data, verbose=verbose)

    # ---- baseline: plain compression of the raw stream --------------------
    pid, pblob = _pick_codec(data)
    plain = MAGIC + bytes([0]) + bytes([pid]) + struct.pack("<I", len(data)) + pblob
    best = plain

    if plan is not None:
        try:
            payload, aux = _apply(data, plan)
        except Exception:
            payload, aux = None, None
        if payload is not None:
            blob = _encode_structured(data, payload, aux, verbose=verbose)
            if blob is not None and len(blob) < len(best):
                best = blob
    return best


def _encode_structured(data: bytes, payload: bytes, aux: dict, verbose=False):
    parts = []          # list of (bin_index, bytes)
    layout = []
    if aux["kind"] == "stride":
        sizes = aux["sizes"]
        off = 0
        flat = np.frombuffer(payload[:sum(sizes)], dtype=np.uint8)
        binbufs = {0: [], 1: [], 2: []}
        for sz in sizes:
            blk = flat[off:off + sz]
            off += sz
            b = _bin_of(_field_bin_bits(blk))
            binbufs[b].append(blk)
            layout.append([b, int(sz)])
        misc = payload[sum(sizes):]
        for b in (0, 1, 2):
            parts.append(np.concatenate(binbufs[b]).tobytes() if binbufs[b] else b"")
        parts.append(misc)
    else:
        lb, cb = aux["lenblob"], aux["colblob"]
        parts = [payload[:lb], payload[lb:lb + cb], payload[lb + cb:], b""]
    streams = []
    total = 0
    for p in parts:
        cid, blob = _pick_codec(p)
        streams.append((cid, len(p), blob))
        total += len(blob)
    meta = {k: v for k, v in aux.items()}
    meta["layout"] = layout
    meta["streams"] = [[c, n, len(b)] for c, n, b in streams]
    hdr = _pack_header(meta)
    out = MAGIC + bytes([1]) + hdr + b"".join(b for _c, _n, b in streams)
    if verbose:
        print(f"    structured: header {len(hdr)} streams "
              f"{[ (parts and len(p)) for p in parts ]} -> "
              f"{[len(b) for _c,_n,b in streams]}")
    return out


def decompress(blob: bytes) -> bytes:
    if blob[:4] != MAGIC:
        raise ValueError("not a loom blob")
    mode = blob[4]
    if mode == 0:
        cid = blob[5]
        n = struct.unpack("<I", blob[6:10])[0]
        return _CODECS[cid][2](blob[10:], n)
    hlen = struct.unpack("<I", blob[5:9])[0]
    meta = json.loads(zlib.decompress(blob[9:9 + hlen]).decode())
    pos = 9 + hlen
    parts = []
    for cid, n, clen in meta["streams"]:
        raw = _CODECS[cid][2](blob[pos:pos + clen], n)
        pos += clen
        parts.append(raw)
    if meta["kind"] == "stride":
        binbufs = [np.frombuffer(parts[i], dtype=np.uint8) for i in range(3)]
        cursors = [0, 0, 0]
        chunks = []
        for b, sz in meta["layout"]:
            chunks.append(binbufs[b][cursors[b]:cursors[b] + sz])
            cursors[b] += sz
        payload = (np.concatenate(chunks).tobytes() if chunks else b"") + parts[3]
    else:
        payload = parts[0] + parts[1] + parts[2]
    aux = {k: v for k, v in meta.items() if k not in ("layout", "streams")}
    return inverse(payload, aux)


# ===========================================================================
if __name__ == "__main__":
    import sys, time
    for path in sys.argv[1:]:
        d = open(path, "rb").read()
        t0 = time.time()
        p = analyze(d, verbose=True)
        t1 = time.time()
        print(path, len(d), "plan:", None if p is None else
              {k: v for k, v in p.items() if k != "fields"},
              f"analysis {t1-t0:.2f}s ({len(d)/1e6/(t1-t0):.1f} MB/s)")
