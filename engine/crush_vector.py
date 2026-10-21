"""
floatcodec.py - Crush v4 domain codec for floating-point / numeric arrays.
============================================================================

A reversible transform in the style of the transforms in engine/crush_engine.py:

    forward(data: bytes) -> (payload, aux) | None
    inverse(payload, aux) -> bytes

The transform does not compress; it *decorrelates*, so that the codec
tournament that runs afterwards (LZMA / zstd / brotli / PPMd) has something it
can model.

What it knows about numeric data
--------------------------------
1. Layout.  A raw array file is (itemsize, endianness, channel count) and
   nothing else.  All three are inferred, then confirmed by measurement.
   De-interleaving is on its own worth a great deal: the generic byte-plane
   transposer in crush_engine assumes stride == itemsize, which is the wrong
   stride for an interleaved multi-channel array.

2. Residual models, chosen per channel by measurement:
     * XOR with the previous value                          (Gorilla)
     * XOR with the best of the previous 4, per 512-sample block, 2-bit
       selector                                             (Chimp128, blocked)
     * monotonic-integer 1st / 2nd difference               (smooth series)
     * monotonic-integer Lorenzo predictor over an inferred 2-D row width
     * plain unsigned difference                            (integer arrays)
   Every predictor runs in the INTEGER domain on the IEEE-754 bit patterns,
   mapped through the standard order-preserving transform.  No arithmetic is
   ever done in floating point, so NaN payloads, -0.0, infinities and
   denormals survive bit-for-bit and nothing depends on the FPU.

3. Bit-level structure:
     * sign / exponent / mantissa split into separate streams
     * global trailing-zero-bit trim (float32 widened to float64, or
       quantised sensor data, carry a fixed number of dead mantissa bits)
     * float64 that is exactly float32, stored as float32 + exception list

4. Decimals.  Exported data ("4181.11") is a decimal with a couple of
   fractional digits that has been through a float.  The integer mantissa is
   small, delta-codes beautifully, and reconstructs the identical double.
   Values that do not reconstruct exactly go into an exception list, so the
   result is exact whether or not the guess was right.

Everything above is *scored* on a sample with a real compressor and the best
combination wins.  forward() finally verifies its own inverse byte-for-byte
and returns None if the transform did not help, so the path can never lose.
"""

from __future__ import annotations

import os
import struct
import numpy as np

try:
    import zstandard as zstd
except ImportError:          # pragma: no cover
    zstd = None

MAGIC = b"FC01"
BLK = 512                    # Chimp reference-selection block


# ---------------------------------------------------------------- primitives

def _u(w):
    return np.dtype(f"<u{w}")


def _put_varint(out, v):
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return


def _get_varint(buf, off):
    v = 0
    s = 0
    while True:
        b = buf[off]
        off += 1
        v |= (b & 0x7F) << s
        if not (b & 0x80):
            return v, off
        s += 7


def pack_streams(streams):
    hdr = bytearray()
    _put_varint(hdr, len(streams))
    for s in streams:
        _put_varint(hdr, len(s))
    return bytes(hdr) + b"".join(streams)


def unpack_streams(buf, off):
    n, off = _get_varint(buf, off)
    lens = []
    for _ in range(n):
        l, off = _get_varint(buf, off)
        lens.append(l)
    out = []
    for l in lens:
        out.append(buf[off:off + l])
        off += l
    return out, off


def zigzag(d, dt):
    bits = dt.itemsize * 8 - 1
    top = (d >> np.uint64(bits)).astype(dt)
    return ((d << dt.type(1)) ^ (dt.type(0) - top)).astype(dt)


def unzigzag(z, dt):
    return ((z >> dt.type(1)) ^ (dt.type(0) - (z & dt.type(1)))).astype(dt)


def mono(u, dt):
    """IEEE-754 bit pattern -> order-preserving unsigned integer."""
    bits = dt.itemsize * 8
    sb = dt.type(1 << (bits - 1))
    allo = dt.type((1 << bits) - 1)
    top = (u >> np.uint64(bits - 1)).astype(bool)
    return (u ^ np.where(top, allo, sb)).astype(dt)


def unmono(m, dt):
    bits = dt.itemsize * 8
    sb = dt.type(1 << (bits - 1))
    allo = dt.type((1 << bits) - 1)
    top = (m >> np.uint64(bits - 1)).astype(bool)
    return (m ^ np.where(top, sb, allo)).astype(dt)


def planes_fwd(u, nplanes=None):
    """Byte-plane split.  A plane with a single distinct value collapses."""
    W = u.dtype.itemsize
    nplanes = W if nplanes is None else nplanes
    b = u.view(np.uint8).reshape(-1, W)
    streams, const = [], []
    for p in range(nplanes):
        col = np.ascontiguousarray(b[:, p])
        if col.size and col.max() == col.min():
            const.append(int(col[0]))
            streams.append(b"")
        else:
            const.append(-1)
            streams.append(col.tobytes())
    return streams, const


def planes_inv(streams, const, n, W):
    b = np.zeros((n, W), dtype=np.uint8)
    for p in range(min(len(streams), W)):
        c = const[p] if p < len(const) else -1
        if c >= 0:
            b[:, p] = c
        elif streams[p]:
            b[:, p] = np.frombuffer(streams[p], dtype=np.uint8, count=n)
    return b.reshape(-1).view(_u(W))


# ------------------------------------------------------------------- modes
M_RAW = 0
M_SPLIT = 1        # byte planes
M_XOR1 = 2         # xor with previous
M_XORK = 3         # xor with best of previous 1..4, per block (Chimp128-lite)
M_ODELTA1 = 4      # monotonic-int 1st difference
M_ODELTA2 = 5      # monotonic-int 2nd difference
M_LORENZO = 6      # monotonic-int 2-D Lorenzo
M_IDELTA1 = 7      # plain unsigned 1st difference
M_IDELTA2 = 8      # plain unsigned 2nd difference
M_SEM = 9          # sign / exponent / mantissa split
M_TRUNC = 20       # trailing-zero-bit trim, then a sub-mode
M_DECIMAL = 21     # decimal mantissa, then a sub-mode
M_NARROW = 22      # float64 exactly representable as float32, then a sub-mode

INT_MODES = (M_SPLIT, M_IDELTA1, M_IDELTA2, M_XOR1)
FLOAT_MODES = (M_SPLIT, M_XOR1, M_XORK, M_ODELTA1, M_ODELTA2,
               M_IDELTA1, M_IDELTA2, M_SEM)


def _sigwidth(r, W):
    """Index of the highest non-zero byte + 1, per element."""
    b = r.view(np.uint8).reshape(-1, W)
    w = np.zeros(r.size, dtype=np.uint8)
    for p in range(W):
        np.copyto(w, p + 1, where=b[:, p] != 0)
    return w


# --------------------------------------------------------------- sub-codecs

def _enc_body(u, mode, dt, param=0):
    """Encode one uint array under a simple mode -> (streams, const, extra)."""
    n = u.size
    W = dt.itemsize

    if mode == M_RAW:
        return [u.tobytes()], [], b""

    if mode == M_SPLIT:
        s, c = planes_fwd(u)
        return s, c, b""

    if mode == M_XOR1:
        r = u.copy()
        if n > 1:
            r[1:] = u[1:] ^ u[:-1]
        s, c = planes_fwd(r)
        return s, c, b""

    if mode == M_XORK:
        # per-block choice of reference lag 1..4
        cand = []
        for j in range(1, 5):
            r = u.copy()
            if n > j:
                r[j:] = u[j:] ^ u[:-j]
            cand.append(r)
        nb = (n + BLK - 1) // BLK
        pad = nb * BLK - n
        cost = np.empty((4, nb), dtype=np.int64)
        for j in range(4):
            w = _sigwidth(cand[j], W).astype(np.int64)
            if pad:
                w = np.concatenate([w, np.zeros(pad, dtype=np.int64)])
            cost[j] = w.reshape(nb, BLK).sum(axis=1)
        sel = np.argmin(cost, axis=0).astype(np.uint8)          # 0..3 => lag 1..4
        idx = np.repeat(sel, BLK)[:n].astype(np.intp)
        r = np.choose(idx, cand)
        s, c = planes_fwd(r)
        return [sel.tobytes()] + s, c, b""

    if mode in (M_ODELTA1, M_ODELTA2, M_IDELTA1, M_IDELTA2):
        m = mono(u, dt) if mode in (M_ODELTA1, M_ODELTA2) else u
        d = m.copy()
        if n > 1:
            d[1:] = m[1:] - m[:-1]
        if mode in (M_ODELTA2, M_IDELTA2) and n > 2:
            e = d.copy()
            e[2:] = d[2:] - d[1:-1]
            d = e
        s, c = planes_fwd(zigzag(d, dt))
        return s, c, b""

    if mode == M_LORENZO:
        w = param
        rows = n // w
        m = mono(u, dt)
        body = m[:rows * w].reshape(rows, w)
        t = body.copy()
        t[:, 1:] = body[:, 1:] - body[:, :-1]
        res = t.copy()
        res[1:, :] = t[1:, :] - t[:-1, :]
        r = m.copy()
        r[:rows * w] = res.reshape(-1)
        if n > rows * w:
            tv = m[rows * w:].copy()
            tv[1:] = tv[1:] - tv[:-1]
            r[rows * w:] = tv
        s, c = planes_fwd(zigzag(r, dt))
        return s, c, struct.pack("<I", w)

    if mode == M_SEM:
        bits = W * 8
        ebits, mbits = (11, 52) if W == 8 else (8, 23)
        sign = (u >> np.uint64(bits - 1)).astype(np.uint8)
        expv = ((u >> np.uint64(mbits)) & dt.type((1 << ebits) - 1))
        man = (u & dt.type((1 << mbits) - 1)).astype(dt)
        out = [np.packbits(sign, bitorder="little").tobytes()]
        if ebits > 8:
            es, ec = planes_fwd(expv.astype(_u(2)))
            out += es
            cc = list(ec)
        else:
            out.append(expv.astype(np.uint8).tobytes())
            cc = [-1]
        nman = (mbits + 7) // 8
        ms, mc = planes_fwd(man, nman)
        return out + ms, cc + list(mc), b""

    raise ValueError(mode)


def _dec_body(streams, const, mode, dt, n, extra):
    W = dt.itemsize

    if mode == M_RAW:
        return np.frombuffer(streams[0], dtype=dt, count=n).copy()

    if mode == M_SPLIT:
        return planes_inv(streams, const, n, W)

    if mode == M_XOR1:
        r = planes_inv(streams, const, n, W)
        return np.bitwise_xor.accumulate(r, dtype=dt)

    if mode == M_XORK:
        sel = np.frombuffer(streams[0], dtype=np.uint8)
        r = planes_inv(streams[1:], const, n, W)
        out = np.empty(n, dtype=dt)
        for bi in range(sel.size):
            s0 = bi * BLK
            s1 = min(s0 + BLK, n)
            if s1 <= s0:
                break
            j = int(sel[bi]) + 1
            for c in range(j):
                lo = s0 + c
                if lo >= s1:
                    break
                seg = r[lo:s1:j]
                acc = np.bitwise_xor.accumulate(seg, dtype=dt)
                if lo - j >= 0:
                    acc ^= out[lo - j]
                out[lo:s1:j] = acc
        return out

    if mode in (M_ODELTA1, M_ODELTA2, M_IDELTA1, M_IDELTA2):
        z = planes_inv(streams, const, n, W)
        d = unzigzag(z, dt)
        if mode in (M_ODELTA2, M_IDELTA2) and n > 1:
            d1 = d.copy()
            d1[1:] = np.add.accumulate(d[1:], dtype=dt)
            d = d1
        m = np.add.accumulate(d, dtype=dt)
        return unmono(m, dt) if mode in (M_ODELTA1, M_ODELTA2) else m

    if mode == M_LORENZO:
        w = struct.unpack("<I", extra)[0]
        rows = n // w
        z = planes_inv(streams, const, n, W)
        r = unzigzag(z, dt)
        res = r[:rows * w].reshape(rows, w)
        body = np.add.accumulate(np.add.accumulate(res, axis=1, dtype=dt),
                                 axis=0, dtype=dt)
        m = np.empty(n, dtype=dt)
        m[:rows * w] = body.reshape(-1)
        if n > rows * w:
            m[rows * w:] = np.add.accumulate(r[rows * w:], dtype=dt)
        return unmono(m, dt)

    if mode == M_SEM:
        bits = W * 8
        ebits, mbits = (11, 52) if W == 8 else (8, 23)
        sign = np.unpackbits(np.frombuffer(streams[0], dtype=np.uint8),
                             bitorder="little")[:n].astype(dt)
        if ebits > 8:
            expv = planes_inv(streams[1:3], const[:2], n, 2).astype(dt)
            rest, crest = streams[3:], const[2:]
        else:
            expv = np.frombuffer(streams[1], dtype=np.uint8, count=n).astype(dt)
            rest, crest = streams[2:], const[1:]
        man = planes_inv(rest, crest, n, W)
        return ((sign << np.uint64(bits - 1)) |
                (expv << np.uint64(mbits)) | man).astype(dt)

    raise ValueError(mode)


# ------------------------------------------------------------ probe / search

class _Probe:
    """Fast size estimate.  A real LZ codec is used, because entropy estimates
    rank these streams wrongly (they ignore the long runs that make byte
    planes cheap)."""

    def __init__(self, level=6):
        self.level = level
        self.c = zstd.ZstdCompressor(level=level) if zstd else None

    def __call__(self, streams):
        blob = b"".join(streams) if not isinstance(streams, bytes) else streams
        if self.c is None:
            import zlib
            return len(zlib.compress(blob, 6))
        return len(self.c.compress(blob))


def _ctz_min(u, dt):
    """Largest t such that every value has its low t bits clear."""
    bits = dt.itemsize * 8
    nz = u[u != 0]
    if nz.size == 0:
        return 0
    low = (nz & (dt.type(0) - nz))
    mn = int(low.min())
    t = mn.bit_length() - 1
    return max(0, min(t, bits - 8))


def _guess_width(m, dt, n):
    """Infer a 2-D row width from the monotonic-int series."""
    smp = m[:min(n, 1 << 19)]
    if smp.size < 4096:
        return None
    base = float(np.mean(zigzag((smp[1:] - smp[:-1]).astype(dt), dt).astype(np.float64)))
    best, bw = None, None
    for w in (2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96,
              100, 120, 128, 160, 192, 200, 240, 256, 300, 320, 360, 384, 400,
              480, 500, 512, 600, 640, 720, 768, 800, 900, 960, 1000, 1024,
              1200, 1280, 1440, 1500, 1600, 1920, 2000, 2048, 2560, 3000, 4096):
        if 8 * w >= smp.size:
            break
        d = zigzag((smp[w:] - smp[:-w]).astype(dt), dt).astype(np.float64)
        v = float(np.mean(d))
        if best is None or v < best:
            best, bw = v, w
    if bw is None or best is None or best > base * 0.85:
        return None
    return bw


def _best_simple(u, dt, is_float, probe):
    """Score every simple mode on `u` (already a sample).  -> (score, mode, param)"""
    modes = list(FLOAT_MODES if is_float else INT_MODES)
    trials = [(mo, 0) for mo in modes]
    if is_float and u.size > 8192:
        w = _guess_width(mono(u, dt), dt, u.size)
        if w:
            trials.append((M_LORENZO, w))
    best = None
    for mo, pa in trials:
        try:
            s, c, x = _enc_body(u, mo, dt, pa)
        except Exception:
            continue
        sc = probe(s) + len(x) + 2 + 2 * len(c)
        if best is None or sc < best[0]:
            best = (sc, mo, pa)
    return best


# --------------------------------------------------------- channel container

def _pack_channel(mode, streams, const, extra):
    hdr = bytearray()
    hdr.append(mode)
    _put_varint(hdr, len(const))
    for c in const:
        hdr.append(0 if c < 0 else 1)
        if c >= 0:
            hdr.append(c)
    _put_varint(hdr, len(extra))
    hdr += extra
    return bytes(hdr) + pack_streams(streams)


def _unpack_channel(buf, off):
    mode = buf[off]; off += 1
    nc, off = _get_varint(buf, off)
    const = []
    for _ in range(nc):
        f = buf[off]; off += 1
        if f:
            const.append(buf[off]); off += 1
        else:
            const.append(-1)
    xl, off = _get_varint(buf, off)
    extra = bytes(buf[off:off + xl]); off += xl
    streams, off = unpack_streams(buf, off)
    return mode, const, extra, streams, off


# ---------------------------------------------------------------- wrappers

def _trunc_plan(u, dt, probe):
    """Global trailing-zero-bit trim.  The dead planes are not dropped by hand:
    planes_fwd already collapses any plane with one distinct value to nothing,
    which is exact whatever the residual mode does to the top bits."""
    t = _ctz_min(u, dt)
    if t < 4:
        return None
    v = (u >> np.uint64(t)).astype(dt)
    best = None
    for mo in (M_SPLIT, M_IDELTA1, M_XOR1):
        s, c, x = _enc_body(v, mo, dt)
        sc = probe(s) + 8
        if best is None or sc < best[0]:
            best = (sc, mo)
    return best[0], ("trunc", t, best[1])


def _trunc_encode(u, dt, t, mo):
    v = (u >> np.uint64(t)).astype(dt)
    s, c, x = _enc_body(v, mo, dt)
    inner = _pack_channel(mo, s, c, x)
    return _pack_channel(M_TRUNC, [struct.pack("<B", t) + inner], [], b"")


def _decimal_digits(u, dt):
    W = dt.itemsize
    fdt = np.dtype("<f8" if W == 8 else "<f4")
    v = u.view(fdt)
    fin = np.isfinite(v)
    if fin.size == 0 or fin.mean() < 0.5:
        return None
    vf = v[fin].astype(np.float64)
    ref = v[fin].view(_u(W))
    for d in range(0, 10):
        sc = 10.0 ** d
        with np.errstate(invalid="ignore", over="ignore"):
            m = np.rint(vf * sc)
        if not np.all(np.abs(m) < 9.0e15):
            return None
        back = (m / sc).astype(fdt)
        if float((back.view(_u(W)) == ref).mean()) > 0.995:
            return d
    return None


def _decimal_build(u, dt, d):
    """-> (int64 mantissa array, bad mask) or None."""
    W = dt.itemsize
    fdt = np.dtype("<f8" if W == 8 else "<f4")
    v = u.view(fdt)
    sc = 10.0 ** d
    with np.errstate(invalid="ignore", over="ignore"):
        allm = np.rint(v.astype(np.float64) * sc)
        good = np.isfinite(allm) & (np.abs(allm) < 9.0e15)
    mi = np.zeros(u.size, dtype=np.int64)
    mi[good] = allm[good].astype(np.int64)
    back = np.zeros(u.size, dtype=fdt)
    with np.errstate(invalid="ignore", over="ignore"):
        back[good] = (mi[good].astype(np.float64) / sc).astype(fdt)
    bad = ~good
    bad[good] = back[good].view(_u(W)) != u[good]
    mi[bad] = 0
    return mi, bad


def _decimal_plan(u, dt, probe):
    d = _decimal_digits(u, dt)
    if d is None:
        return None
    mi, bad = _decimal_build(u, dt, d)
    nbad = int(bad.sum())
    if nbad > u.size * 0.02:
        return None
    mu = mi.view(np.uint64).astype(_u(8))
    r = _best_simple(mu, _u(8), False, probe)
    if r is None:
        return None
    sc = r[0] + nbad * (dt.itemsize + 4) + 16
    return sc, ("decimal", d, r[1], r[2])


def _exc_streams(bad, u):
    idx = np.flatnonzero(bad).astype(np.int64)
    didx = idx.copy()
    if idx.size > 1:
        didx[1:] = idx[1:] - idx[:-1]
    return [didx.astype("<u8").tobytes(), u[bad].tobytes()]


def _decimal_encode(u, dt, d, mo, pa):
    mi, bad = _decimal_build(u, dt, d)
    nbad = int(bad.sum())
    if nbad > u.size * 0.05:
        return None
    mu = mi.view(np.uint64).astype(_u(8))
    s, c, x = _enc_body(mu, mo, _u(8), pa)
    inner = _pack_channel(mo, s, c, x)
    body = struct.pack("<BI", d, nbad) + pack_streams(_exc_streams(bad, u)) + inner
    return _pack_channel(M_DECIMAL, [body], [], b"")


def _narrow_build(u):
    v = u.view("<f8")
    with np.errstate(over="ignore", invalid="ignore"):
        s32 = v.astype("<f4")
        back = s32.astype("<f8")
    ok = back.view(_u(8)) == u
    s32 = s32.copy()
    s32[~ok] = 0.0
    return s32, ~ok


def _narrow_plan(u, dt, probe):
    if dt.itemsize != 8:
        return None
    s32, bad = _narrow_build(u)
    nbad = int(bad.sum())
    if nbad > u.size * 0.02:
        return None
    r = _best_simple(s32.view(_u(4)), _u(4), True, probe)
    if r is None:
        return None
    return r[0] + nbad * 12 + 16, ("narrow", r[1], r[2])


def _narrow_encode(u, mo, pa):
    s32, bad = _narrow_build(u)
    nbad = int(bad.sum())
    if nbad > u.size * 0.05:
        return None
    s, c, x = _enc_body(s32.view(_u(4)), mo, _u(4), pa)
    inner = _pack_channel(mo, s, c, x)
    body = struct.pack("<I", nbad) + pack_streams(_exc_streams(bad, u)) + inner
    return _pack_channel(M_NARROW, [body], [], b"")


# ------------------------------------------------------------ channel codec

def _plan_channel(smp, dt, probe):
    """Score every family on the sample -> plan tuple."""
    plans = []
    r = _best_simple(smp, dt, True, probe)
    if r:
        plans.append((r[0], ("simple", r[1], r[2])))
    for f in (_trunc_plan, _decimal_plan, _narrow_plan):
        try:
            p = f(smp, dt, probe)
        except Exception:
            p = None
        if p:
            plans.append(p)
    plans.sort(key=lambda z: z[0])
    return plans[0] if plans else (1 << 62, ("simple", M_SPLIT, 0))


def _encode_channel(u, dt, plan, probe):
    kind = plan[0]
    try:
        if kind == "simple":
            mo, pa = plan[1], plan[2]
            if mo == M_LORENZO and u.size // max(1, pa) < 4:
                mo, pa = M_SPLIT, 0
            s, c, x = _enc_body(u, mo, dt, pa)
            return _pack_channel(mo, s, c, x)
        if kind == "trunc":
            t, mo = plan[1], plan[2]
            if _ctz_min(u, dt) >= t:
                return _trunc_encode(u, dt, t, mo)
        if kind == "decimal":
            b = _decimal_encode(u, dt, plan[1], plan[2], plan[3])
            if b is not None:
                return b
        if kind == "narrow":
            b = _narrow_encode(u, plan[1], plan[2])
            if b is not None:
                return b
    except Exception:
        pass
    s, c, x = _enc_body(u, M_SPLIT, dt)
    return _pack_channel(M_SPLIT, s, c, x)


def _decode_channel(buf, off, dt, n):
    mode, const, extra, streams, off = _unpack_channel(buf, off)
    W = dt.itemsize

    if mode == M_TRUNC:
        body = streams[0]
        t = body[0]
        m2, c2, x2, s2, _ = _unpack_channel(body, 1)
        v = _dec_body(s2, c2, m2, dt, n, x2)
        return (v << np.uint64(t)).astype(dt), off

    if mode == M_DECIMAL:
        body = streams[0]
        d, nbad = struct.unpack("<BI", body[:5])
        exc, o2 = unpack_streams(body, 5)
        m2, c2, x2, s2, _ = _unpack_channel(body, o2)
        mu = _dec_body(s2, c2, m2, _u(8), n, x2)
        mi = mu.view(np.int64)
        fdt = np.dtype("<f8" if W == 8 else "<f4")
        sc = 10.0 ** d
        with np.errstate(invalid="ignore", over="ignore"):
            out = (mi.astype(np.float64) / sc).astype(fdt).view(_u(W)).copy()
        if nbad:
            idx = np.add.accumulate(np.frombuffer(exc[0], dtype="<u8").astype(np.int64))
            out[idx] = np.frombuffer(exc[1], dtype=_u(W))
        return out, off

    if mode == M_NARROW:
        body = streams[0]
        (nbad,) = struct.unpack("<I", body[:4])
        exc, o2 = unpack_streams(body, 4)
        m2, c2, x2, s2, _ = _unpack_channel(body, o2)
        mu = _dec_body(s2, c2, m2, _u(4), n, x2)
        with np.errstate(over="ignore", invalid="ignore"):
            out = mu.view("<f4").astype("<f8").view(_u(8)).copy()
        if nbad:
            idx = np.add.accumulate(np.frombuffer(exc[0], dtype="<u8").astype(np.int64))
            out[idx] = np.frombuffer(exc[1], dtype=_u(8))
        return out, off

    return _dec_body(streams, const, mode, dt, n, extra), off


# ------------------------------------------------------------------ layout

def _sanity(data, W, be, cap=1 << 20):
    """Fraction of values that look like a real physical quantity."""
    dt = np.dtype((">" if be else "<") + f"f{W}")
    nv = min(len(data) // W, cap // W)
    if nv < 64:
        return 0.0, 8.0
    with np.errstate(invalid="ignore", over="ignore"):
        a = np.frombuffer(data, dtype=dt, count=nv).astype(np.float64)
        aa = np.abs(a)
        ok = (aa == 0) | ((aa > 1e-25) & (aa < 1e25))
    frac = float(np.mean(ok))
    ub = np.frombuffer(data, dtype=np.dtype((">" if be else "<") + f"u{W}"),
                       count=nv).astype(_u(W))
    hi = ub.view(np.uint8).reshape(-1, W)[:, W - 1]
    h = np.bincount(hi, minlength=256).astype(np.float64)
    h /= h.sum()
    ent = float(-np.sum(h[h > 0] * np.log2(h[h > 0])))
    return frac, ent


def layout_candidates(data):
    n = len(data)
    out = []
    for W in (8, 4):
        if n % W:
            continue
        for be in (0, 1):
            frac, ent = _sanity(data, W, be)
            out.append((-(frac * 4) + ent, W, be, frac, ent))
    out.sort()
    return out


def _guess_channels(u, dt, cap=1 << 20):
    nv = min(u.size, cap)
    m = mono(u[:nv], dt)
    best, bc = None, 1
    for C in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 16, 20, 24, 32):
        if nv < C * 256:
            continue
        d = zigzag((m[C:] - m[:-C]).astype(dt), dt).astype(np.float64)
        v = float(np.mean(np.log2(d + 1.0))) + 0.02 * C
        if best is None or v < best:
            best, bc = v, C
    return bc


def detect_layout(data, probe=None):
    """Infer (itemsize, endianness, channels), confirmed by measurement."""
    n = len(data)
    if n < 4096:
        return None
    cands = layout_candidates(data)
    if not cands:
        return None
    cands = cands[:2]
    probe = probe or _Probe(3)
    sample_bytes = min(n, 1 << 20)
    best = None
    for _, W, be, frac, ent in cands:
        dt = _u(W)
        raw = data[:sample_bytes - (sample_bytes % W)]
        if be:
            u = np.frombuffer(raw, dtype=np.dtype(f">u{W}")).astype(dt)
        else:
            u = np.frombuffer(raw, dtype=dt)
        if u.size < 256:
            continue
        for C in {1, _guess_channels(u, dt)}:
            rows = u.size // C
            if rows < 256:
                continue
            mat = u[:rows * C].reshape(rows, C)
            tot = 0
            probed = min(C, 4)
            for ci in range(probed):
                col = np.ascontiguousarray(mat[:, ci])
                # the full plan, not just the simple modes: whether a layout is
                # right can hinge entirely on the decimal path firing
                tot += _plan_channel(col, dt, probe)[0]
            tot = tot * C / probed
            if best is None or tot < best[0]:
                best = (tot, W, be, C)
    if best is None:
        return None
    return {"W": best[1], "be": best[2], "C": best[3]}


# ------------------------------------------------------------------ sniffer

TEXTY = set(range(32, 127)) | {9, 10, 13}


def sniff(path, head):
    """Return "numeric_array" when this looks like a raw float / int array."""
    ext = os.path.splitext(path or "")[1].lower()
    if ext in (".txt", ".csv", ".json", ".xml", ".log", ".md", ".py", ".c",
               ".h", ".html", ".fastq", ".fq", ".fasta", ".fa", ".sam",
               ".vcf", ".zip", ".gz", ".png", ".jpg", ".pdf"):
        return None
    if len(head) < 4096:
        return None
    smp = head[:65536]
    if sum(1 for b in smp if b in TEXTY) / len(smp) > 0.95:
        return None
    cands = layout_candidates(head[:1 << 20])
    if not cands:
        return None
    _, W, be, frac, ent = cands[0]
    if frac < 0.90 or ent > 7.0:
        return None
    return "numeric_array"


# --------------------------------------------------------------- transform

class FloatArray:
    """Crush transform: domain codec for float / numeric arrays."""

    name = "floatarray"

    @staticmethod
    def forward(data: bytes, probe_level: int = 6, layout=None):
        n = len(data)
        if n < 4096:
            return None
        probe = _Probe(probe_level)
        lay = layout or detect_layout(data, _Probe(3))
        if lay is None:
            return None
        W, be, C = lay["W"], lay["be"], lay["C"]
        nv = n // W
        if nv < C * 64:
            return None
        dt = _u(W)
        if be:
            u = np.frombuffer(data, dtype=np.dtype(f">u{W}"), count=nv).astype(dt)
        else:
            u = np.frombuffer(data, dtype=dt, count=nv)
        tailbytes = data[nv * W:]
        rows = nv // C
        mat = u[:rows * C].reshape(rows, C)
        rest = u[rows * C:]

        budget = max(1 << 16, min(rows, 1 << 18))
        chans = []
        for ci in range(C):
            col = np.ascontiguousarray(mat[:, ci])
            smp = col[:budget]
            _, plan = _plan_channel(smp, dt, probe)
            chans.append(_encode_channel(col, dt, plan, probe))

        hdr = bytearray(MAGIC)
        hdr.append(W)
        hdr.append(be)
        _put_varint(hdr, C)
        _put_varint(hdr, rows)
        _put_varint(hdr, n)
        payload = (bytes(hdr) + pack_streams([rest.tobytes(), tailbytes]) +
                   b"".join(chans))

        try:
            if FloatArray.inverse(payload, {"n": n}) != data:
                return None
        except Exception:
            return None

        # ---- never lose: if plain compression of the raw bytes is already at
        # least as small, decline the transform entirely.
        guard = _Probe(3)
        cap = 12 << 20
        if guard(payload[:cap]) >= guard(data[:cap]) * 0.995:
            return None
        return payload, {"n": n, "v": 1}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        buf = payload
        if buf[:4] != MAGIC:
            raise ValueError("bad floatarray payload")
        off = 4
        W = buf[off]; off += 1
        be = buf[off]; off += 1
        C, off = _get_varint(buf, off)
        rows, off = _get_varint(buf, off)
        n, off = _get_varint(buf, off)
        extra, off = unpack_streams(buf, off)
        dt = _u(W)
        cols = []
        for _ in range(C):
            col, off = _decode_channel(buf, off, dt, rows)
            cols.append(col)
        if C == 1:
            body = cols[0]
        else:
            body = np.empty(rows * C, dtype=dt)
            for ci, col in enumerate(cols):
                body[ci::C] = col
        rest = np.frombuffer(extra[0], dtype=dt)
        u = np.concatenate([body, rest]) if rest.size else body
        out = u.astype(np.dtype(f">u{W}")).tobytes() if be else u.tobytes()
        return out + extra[1]


def encode(data: bytes):
    return FloatArray.forward(data)


def decode(payload: bytes, aux=None):
    return FloatArray.inverse(payload, aux or {})
