"""
genome.py - Crush v4 domain codec for genomic and line-oriented scientific data.
================================================================================

Three transforms, in the style of engine/crush_engine.py:

    Fastq    FASTQ reads       -> identifiers / lengths / bases / qualities
    Fasta    FASTA reference   -> headers / layout / case mask / bases
    Tabular  VCF, SAM, CSV, TSV-> one stream per column, numeric columns
                                  reduced to integers

The single biggest idea is the same in all three: a genomic file is several
completely different data sources that a text format has *interleaved*.  A
general-purpose compressor sees one stream whose statistics change every few
bytes.  Split them apart and each one becomes easy.

On top of the split:

* Bases are a 4-symbol alphabet, so they are 2-bit packed (4x before any
  compressor runs), with non-ACGT characters held in an exception list rather
  than polluting the packed stream.  Whether packing WINS is measured, not
  assumed: packing destroys byte-aligned LZ matching, and in a file with
  sequencing coverage the cross-read repeats an LZ window can find are
  sometimes worth more than the 4x.  Both are built; the smaller wins.
* Quality strings are strongly autocorrelated along the read and decline with
  cycle number, so the read x cycle matrix is optionally transposed (each
  stream then being one cycle, which is nearly stationary) or differenced.
* Read identifiers are templated.  The literal skeleton is stored once and
  each numeric field becomes its own integer stream, delta-coded.
* A high-order context model over the bases comes almost free: PPMd order-16
  over a 4-symbol alphabet *is* an order-16 DNA context model, and it is a
  fast C implementation rather than a Python arithmetic coder.  It is one of
  the per-stream codec candidates in pack().

Two levels of use:

    Fastq.forward / inverse   pure reversible transform; the engine's codec
                              tournament then compresses the whole payload
    pack / unpack             the same split, but every stream gets its own
                              codec chosen by measurement.  Streams this
                              different should not share one model, and the
                              numbers in RESULTS.md show how much that is
                              worth.

Every transform verifies its own inverse byte-for-byte before returning.
"""

from __future__ import annotations

import bz2
import lzma
import os
import re
import struct
import numpy as np

from crush_vector import (pack_streams, unpack_streams, _put_varint, _get_varint,
                        _enc_body, _dec_body, _Probe, _u,
                        M_SPLIT, M_IDELTA1, M_IDELTA2, M_XOR1)

try:
    import zstandard as zstd
except ImportError:          # pragma: no cover
    zstd = None
try:
    import brotli
except ImportError:          # pragma: no cover
    brotli = None
try:
    import pyppmd
except ImportError:          # pragma: no cover
    pyppmd = None

MAGIC = b"GN01"
K_FASTQ, K_FASTA, K_TAB = 1, 2, 3
NL = 10


# ------------------------------------------------------------------ helpers

def _lines(a):
    nl = np.flatnonzero(a == NL)
    starts = np.empty(nl.size + 1, dtype=np.int64)
    starts[0] = 0
    starts[1:] = nl + 1
    ends = np.empty(nl.size + 1, dtype=np.int64)
    ends[:nl.size] = nl
    ends[nl.size] = a.size
    return starts, ends


def _gather(a, starts, lens):
    """Concatenate a[s:s+l] for every (s, l), vectorised."""
    tot = int(lens.sum())
    if tot == 0:
        return np.empty(0, dtype=a.dtype)
    off = np.zeros(lens.size, dtype=np.int64)
    np.cumsum(lens[:-1], out=off[1:])
    idx = np.repeat(starts - off, lens) + np.arange(tot, dtype=np.int64)
    return a[idx]


def _scatter(out, dest, lens, src):
    tot = int(lens.sum())
    if tot == 0:
        return
    off = np.zeros(lens.size, dtype=np.int64)
    np.cumsum(lens[:-1], out=off[1:])
    idx = np.repeat(dest - off, lens) + np.arange(tot, dtype=np.int64)
    out[idx] = src


# ------------------------------------------------------------- container

def emit(kind, hdr, groups):
    out = bytearray(MAGIC)
    out.append(kind)
    _put_varint(out, len(hdr))
    out += hdr
    _put_varint(out, len(groups))
    for g in groups:
        _put_varint(out, len(g))
    flat = [s for g in groups for s in g]
    return bytes(out) + pack_streams(flat)


def parse(payload):
    b = payload
    if b[:4] != MAGIC:
        raise ValueError("not a genome payload")
    kind = b[4]
    off = 5
    hl, off = _get_varint(b, off)
    hdr = bytes(b[off:off + hl]); off += hl
    ng, off = _get_varint(b, off)
    sizes = []
    for _ in range(ng):
        s, off = _get_varint(b, off)
        sizes.append(s)
    flat, off = unpack_streams(b, off)
    groups, i = [], 0
    for s in sizes:
        groups.append(flat[i:i + s]); i += s
    return kind, hdr, groups


# --------------------------------------------------------- integer streams

def _ints_stream(vals, probe, modes=(M_SPLIT, M_IDELTA1, M_IDELTA2, M_XOR1)):
    u = np.ascontiguousarray(vals.astype(np.int64)).view(np.uint64).astype(_u(8))
    best = None
    for mo in modes:
        s, c, x = _enc_body(u, mo, _u(8))
        sc = probe(s)
        if best is None or sc < best[0]:
            best = (sc, mo, s, c)
    _, mo, s, c = best
    hdr = bytearray()
    hdr.append(mo)
    _put_varint(hdr, len(c))
    for cv in c:
        hdr.append(0 if cv < 0 else 1)
        if cv >= 0:
            hdr.append(cv)
    return bytes(hdr) + pack_streams(s)


def _ints_decode(blob, n):
    mo = blob[0]
    nc, off = _get_varint(blob, 1)
    const = []
    for _ in range(nc):
        f = blob[off]; off += 1
        if f:
            const.append(blob[off]); off += 1
        else:
            const.append(-1)
    streams, off = unpack_streams(blob, off)
    return _dec_body(streams, const, mo, _u(8), n, b"").view(np.int64)


# ------------------------------------------------------------ base packing

_CODE = np.full(256, 255, dtype=np.uint8)
for _i, _c in enumerate(b"ACGT"):
    _CODE[_c] = _i
_DECODE = np.frombuffer(b"ACGT", dtype=np.uint8)


def pack2bit(seq):
    code = _CODE[seq]
    ok = code != 255
    kept = code[ok]
    n = kept.size
    pad = (-n) % 4
    if pad:
        kept = np.concatenate([kept, np.zeros(pad, dtype=np.uint8)])
    q = kept.reshape(-1, 4)
    packed = (q[:, 0] | (q[:, 1] << 2) | (q[:, 2] << 4) | (q[:, 3] << 6))
    bad = np.flatnonzero(~ok)
    dbad = bad.copy()
    if bad.size > 1:
        dbad[1:] = bad[1:] - bad[:-1]
    return (packed.tobytes(), dbad.astype("<u4").tobytes(),
            seq[~ok].tobytes(), int(n))


def unpack2bit(packed, dbad, badchars, nkept, total):
    p = np.frombuffer(packed, dtype=np.uint8)
    q = np.empty((p.size, 4), dtype=np.uint8)
    q[:, 0] = p & 3
    q[:, 1] = (p >> 2) & 3
    q[:, 2] = (p >> 4) & 3
    q[:, 3] = (p >> 6) & 3
    kept = q.reshape(-1)[:nkept]
    out = np.empty(total, dtype=np.uint8)
    bd = np.frombuffer(dbad, dtype="<u4")
    if bd.size:
        idx = np.add.accumulate(bd.astype(np.int64))
        mask = np.ones(total, dtype=bool)
        mask[idx] = False
        out[idx] = np.frombuffer(badchars, dtype=np.uint8)
        out[mask] = _DECODE[kept]
    else:
        out[:] = _DECODE[kept]
    return out


def _seq_streams(seq, probe, force=None):
    """0 = raw bases, 1 = 2-bit packed.  Both are built; the probe decides,
    unless the caller is exploring both explicitly."""
    if force == 0:
        return 0, [seq.tobytes()], 0
    packed, dbad, badc, nkept = pack2bit(seq)
    if force == 1:
        return 1, [packed, dbad, badc], nkept
    raw = seq.tobytes()
    if probe([packed, dbad, badc]) <= probe([raw]):
        return 1, [packed, dbad, badc], nkept
    return 0, [raw], 0


def _seq_decode(mode, streams, nkept, total):
    if mode == 0:
        return np.frombuffer(streams[0], dtype=np.uint8)
    return unpack2bit(streams[0], streams[1], streams[2], nkept, total)


# ----------------------------------------------------------- id templating

_DLO, _DHI = 48, 57


def _id_template(a, nlines, lstarts, llens, probe):
    """Templated identifiers -> literal skeleton + one integer stream per
    numeric field.  Returns (streams, meta) or None."""
    isd = (a >= _DLO) & (a <= _DHI)
    if not isd.any():
        return None
    d = isd.astype(np.int8)
    edge = np.diff(np.concatenate([[0], d, [0]]))
    rs = np.flatnonzero(edge == 1)
    re_ = np.flatnonzero(edge == -1)
    if rs.size == 0 or rs.size > 40 * nlines:
        return None
    line_of = np.searchsorted(lstarts, rs, side="right") - 1
    cnt = np.bincount(line_of, minlength=nlines)
    if cnt.size != nlines or cnt.min() != cnt.max():
        return None
    nf = int(cnt[0])
    if nf == 0 or nf > 16:
        return None
    ndig = np.zeros(nlines, dtype=np.int64)
    np.add.at(ndig, line_of, (re_ - rs))
    skl = llens - ndig
    if skl.min() != skl.max():
        return None
    L = int(skl[0])
    skel = a[~isd]
    if L:
        sk = skel.reshape(nlines, L)
        if not np.array_equal(sk, np.broadcast_to(sk[0], sk.shape)):
            return None
        skb = sk[0]
    else:
        skb = np.empty(0, dtype=np.uint8)

    # literal-gap layout, read off the first line
    first_len = int(llens[0])
    fd = isd[:first_len]
    gaps, run, i = [], 0, 0
    while i < first_len:
        if fd[i]:
            gaps.append(run); run = 0
            while i < first_len and fd[i]:
                i += 1
        else:
            run += 1; i += 1
    gaps.append(run)
    if len(gaps) != nf + 1:
        return None

    rs2 = rs.reshape(nlines, nf)
    re2 = re_.reshape(nlines, nf)
    widths = (re2 - rs2).astype(np.int64)
    streams, wconst = [], []
    for j in range(nf):
        w = widths[:, j]
        if w.min() == w.max():
            ww = int(w[0])
            wconst.append(ww)
            chars = _gather(a, rs2[:, j], w).reshape(nlines, ww).astype(np.int64)
            vals = np.zeros(nlines, dtype=np.int64)
            for k in range(ww):
                vals = vals * 10 + (chars[:, k] - 48)
        else:
            wconst.append(-1)
            streams.append(w.astype("<u1").tobytes())
            wmax = int(w.max())
            padded = np.zeros((nlines, wmax), dtype=np.int64)
            ch = _gather(a, rs2[:, j], w).astype(np.int64) - 48
            off = np.zeros(nlines, dtype=np.int64)
            np.cumsum(w[:-1], out=off[1:])
            pos = (np.repeat(np.arange(nlines, dtype=np.int64) * wmax +
                             (wmax - w) - off, w) +
                   np.arange(int(w.sum()), dtype=np.int64))
            padded.reshape(-1)[pos] = ch
            vals = np.zeros(nlines, dtype=np.int64)
            for k in range(wmax):
                vals = vals * 10 + padded[:, k]
            # leading zeros would not survive a plain integer round-trip
            wid = np.where(vals > 0, np.floor(np.log10(np.maximum(vals, 1))) + 1, 1)
            if not np.array_equal(wid.astype(np.int64), w):
                return None
        streams.append(_ints_stream(vals, probe))
    meta = {"nf": nf, "L": L, "skel": skb.tobytes(),
            "wconst": wconst, "gaps": gaps}
    return streams, meta


def _id_rebuild(streams, meta, nlines):
    nf, L = meta["nf"], meta["L"]
    skel = np.frombuffer(meta["skel"], dtype=np.uint8)
    gaps = meta["gaps"]
    si = 0
    fields = []
    for j in range(nf):
        wc = meta["wconst"][j]
        if wc < 0:
            w = np.frombuffer(streams[si], dtype="<u1").astype(np.int64); si += 1
        else:
            w = np.full(nlines, wc, dtype=np.int64)
        vals = _ints_decode(streams[si], nlines); si += 1
        fields.append((w, vals))
    lens = np.full(nlines, L, dtype=np.int64)
    for w, _ in fields:
        lens = lens + w
    out = np.empty(int(lens.sum()), dtype=np.uint8)
    lstart = np.zeros(nlines, dtype=np.int64)
    np.cumsum(lens[:-1], out=lstart[1:])
    cur = lstart.copy()
    gpos = 0
    for j in range(nf):
        g = gaps[j]
        if g:
            _scatter(out, cur, np.full(nlines, g, dtype=np.int64),
                     np.tile(skel[gpos:gpos + g], nlines))
            cur = cur + g
            gpos += g
        w, vals = fields[j]
        wmax = int(w.max())
        v = vals.copy()
        chars = np.empty((nlines, wmax), dtype=np.uint8)
        for k in range(wmax - 1, -1, -1):
            chars[:, k] = (v % 10) + 48
            v //= 10
        take = _gather(chars.reshape(-1),
                       np.arange(nlines, dtype=np.int64) * wmax + (wmax - w), w)
        _scatter(out, cur, w, take)
        cur = cur + w
    g = gaps[nf]
    if g:
        _scatter(out, cur, np.full(nlines, g, dtype=np.int64),
                 np.tile(skel[gpos:gpos + g], nlines))
    return out, lens


# ----------------------------------------------------------------- quality

def _qual_streams(q, rlen, nreads, probe):
    cands = []
    raw = q.tobytes()
    cands.append((probe([raw]), 0, [raw]))
    d = q.copy()
    if q.size > 1:
        d[1:] = (q[1:].astype(np.int16) - q[:-1].astype(np.int16)).astype(np.uint8)
    s8 = d.view(np.int8).astype(np.int16)
    dz = (((s8 << 1) ^ (s8 >> 7)) & 0xFF).astype(np.uint8)
    cands.append((probe([dz.tobytes()]), 1, [dz.tobytes()]))
    if rlen and nreads and rlen * nreads == q.size and 1 < rlen <= 4096:
        t = np.ascontiguousarray(q.reshape(nreads, rlen).T).tobytes()
        cands.append((probe([t]), 2, [t]))
    cands.sort(key=lambda z: z[0])
    return cands[0][1], cands[0][2]


def _qual_decode(mode, streams, rlen, nreads, total):
    if mode == 0:
        return np.frombuffer(streams[0], dtype=np.uint8)
    if mode == 1:
        dz = np.frombuffer(streams[0], dtype=np.uint8)
        d = ((dz >> 1) ^ (np.uint8(0) - (dz & 1))).astype(np.uint8)
        return np.add.accumulate(d, dtype=np.uint8)
    t = np.frombuffer(streams[0], dtype=np.uint8).reshape(rlen, nreads)
    return np.ascontiguousarray(t.T).reshape(-1)


# =========================================================================
#  FASTQ
# =========================================================================

class Fastq:
    name = "fastq"

    @staticmethod
    def forward(data: bytes, probe_level: int = 6, force_seq=None):
        if not data or data[0:1] != b"@" or b"\r" in data[:1 << 16]:
            return None
        a = np.frombuffer(data, dtype=np.uint8)
        starts, ends = _lines(a)
        trail_nl = 1 if (a.size and a[-1] == NL) else 0
        nlines = starts.size - (1 if trail_nl else 0)
        if nlines < 8 or nlines % 4:
            return None
        s, e = starts[:nlines], ends[:nlines]
        ids, idl = s[0::4], (e - s)[0::4]
        seqs, seql = s[1::4], (e - s)[1::4]
        plus, plusl = s[2::4], (e - s)[2::4]
        qual, quall = s[3::4], (e - s)[3::4]
        nrec = ids.size
        if not np.all(a[ids] == ord("@")) or not np.all(a[plus] == ord("+")):
            return None
        if not np.array_equal(seql, quall):
            return None

        probe = _Probe(probe_level)
        const_len = int(seql[0]) if seql.min() == seql.max() else -1

        idblob = _gather(a, ids + 1, idl - 1)
        idls = (idl - 1).astype(np.int64)
        idstart = np.zeros(nrec, dtype=np.int64)
        np.cumsum(idls[:-1], out=idstart[1:])
        try:
            tmpl = _id_template(idblob, nrec, idstart, idls, probe)
        except Exception:
            tmpl = None

        plus_empty = bool(np.all(plusl == 1))
        seqchars = _gather(a, seqs, seql)
        qualchars = _gather(a, qual, quall)
        seqmode, sstreams, nkept = _seq_streams(seqchars, probe, force_seq)
        qmode, qstreams = _qual_streams(
            qualchars, const_len if const_len > 0 else 0, nrec, probe)

        hdr = bytearray()
        _put_varint(hdr, len(data))
        _put_varint(hdr, nrec)
        hdr.append(trail_nl)
        _put_varint(hdr, const_len + 1)
        hdr.append(1 if plus_empty else 0)
        hdr.append(seqmode)
        _put_varint(hdr, nkept)
        hdr.append(qmode)

        aux = []
        if const_len < 0:
            aux.append(seql.astype("<u4").tobytes())
        if not plus_empty:
            aux.append(_gather(a, plus + 1, plusl - 1).tobytes())
            aux.append(plusl.astype("<u4").tobytes())

        if tmpl is not None:
            tstreams, tmeta = tmpl
            hdr.append(1)
            m = bytearray()
            m.append(tmeta["nf"])
            _put_varint(m, tmeta["L"])
            m += tmeta["skel"]
            for wc in tmeta["wconst"]:
                m.append(0 if wc < 0 else 1)
                if wc >= 0:
                    m.append(wc)
            for g in tmeta["gaps"]:
                _put_varint(m, g)
            _put_varint(hdr, len(m))
            hdr += m
            idpart = tstreams
        else:
            hdr.append(0)
            idpart = [idblob.tobytes(), idls.astype("<u4").tobytes()]

        payload = emit(K_FASTQ, bytes(hdr), [aux, idpart, sstreams, qstreams])
        try:
            if Fastq.inverse(payload, {}) != data:
                return None
        except Exception:
            return None
        return payload, {"n": len(data), "v": 1}

    @staticmethod
    def inverse(payload: bytes, aux_=None) -> bytes:
        kind, b, groups = parse(payload)
        assert kind == K_FASTQ
        off = 0
        total, off = _get_varint(b, off)
        nrec, off = _get_varint(b, off)
        trail_nl = b[off]; off += 1
        cl, off = _get_varint(b, off)
        const_len = cl - 1
        plus_empty = b[off]; off += 1
        seqmode = b[off]; off += 1
        nkept, off = _get_varint(b, off)
        qmode = b[off]; off += 1
        templated = b[off]; off += 1
        tmeta = None
        if templated:
            ml, off = _get_varint(b, off)
            m = b[off:off + ml]; off += ml
            o = 0
            nf = m[o]; o += 1
            L, o = _get_varint(m, o)
            skel = bytes(m[o:o + L]); o += L
            wconst = []
            for _ in range(nf):
                f = m[o]; o += 1
                if f:
                    wconst.append(m[o]); o += 1
                else:
                    wconst.append(-1)
            gaps = []
            for _ in range(nf + 1):
                g, o = _get_varint(m, o)
                gaps.append(g)
            tmeta = {"nf": nf, "L": L, "skel": skel, "wconst": wconst,
                     "gaps": gaps}

        aux, idpart, sstreams, qstreams = groups
        ai = 0
        if const_len < 0:
            seql = np.frombuffer(aux[ai], dtype="<u4").astype(np.int64); ai += 1
        else:
            seql = np.full(nrec, const_len, dtype=np.int64)
        if plus_empty:
            plusblob = np.empty(0, dtype=np.uint8)
            plusl = np.zeros(nrec, dtype=np.int64)
        else:
            plusblob = np.frombuffer(aux[ai], dtype=np.uint8); ai += 1
            plusl = np.frombuffer(aux[ai], dtype="<u4").astype(np.int64) - 1
            ai += 1

        if templated:
            idblob, idls = _id_rebuild(idpart, tmeta, nrec)
        else:
            idblob = np.frombuffer(idpart[0], dtype=np.uint8)
            idls = np.frombuffer(idpart[1], dtype="<u4").astype(np.int64)

        seqtotal = int(seql.sum())
        seqchars = _seq_decode(seqmode, sstreams, nkept, seqtotal)
        qualchars = _qual_decode(qmode, qstreams,
                                 const_len if const_len > 0 else 0, nrec, seqtotal)

        rec = 1 + idls + 1 + seql + 1 + 1 + plusl + 1 + seql + 1
        rstart = np.zeros(nrec, dtype=np.int64)
        np.cumsum(rec[:-1], out=rstart[1:])
        # the buffer always ends in a newline; a file that does not is that
        # buffer minus its last byte
        out = np.full(int(rec.sum()), NL, dtype=np.uint8)
        out[rstart] = ord("@")
        _scatter(out, rstart + 1, idls, idblob)
        p_seq = rstart + 2 + idls
        _scatter(out, p_seq, seql, seqchars)
        p_plus = p_seq + seql + 1
        out[p_plus] = ord("+")
        if plusblob.size:
            _scatter(out, p_plus + 1, plusl, plusblob)
        p_q = p_plus + 1 + plusl + 1
        _scatter(out, p_q, seql, qualchars)
        res = out.tobytes()
        return res if trail_nl else res[:-1]


# =========================================================================
#  FASTA
# =========================================================================

class Fasta:
    name = "fasta"

    @staticmethod
    def forward(data: bytes, probe_level: int = 6, force_seq=None):
        if not data or data[0:1] != b">" or b"\r" in data[:1 << 16]:
            return None
        a = np.frombuffer(data, dtype=np.uint8)
        starts, ends = _lines(a)
        trail_nl = 1 if (a.size and a[-1] == NL) else 0
        nlines = starts.size - (1 if trail_nl else 0)
        if nlines < 4:
            return None
        s, e = starts[:nlines], ends[:nlines]
        L = (e - s).astype(np.int64)
        ishdr = a[s] == ord(">")
        nh = int(ishdr.sum())
        if nh < 1 or nh > nlines // 2:
            return None
        probe = _Probe(probe_level)

        hdrblob = _gather(a, s[ishdr], L[ishdr])
        seqblob = _gather(a, s[~ishdr], L[~ishdr])

        lower = (seqblob >= 97) & (seqblob <= 122)
        up = np.where(lower, seqblob - 32, seqblob).astype(np.uint8)
        chg = np.flatnonzero(np.diff(lower.astype(np.int8)) != 0) + 1
        runs = np.diff(np.concatenate([[0], chg, [lower.size]])).astype(np.int64)
        first_lower = 1 if (lower.size and lower[0]) else 0

        seqmode, sstreams, nkept = _seq_streams(up, probe, force_seq)

        hdr = bytearray()
        _put_varint(hdr, len(data))
        _put_varint(hdr, nlines)
        hdr.append(trail_nl)
        hdr.append(seqmode)
        _put_varint(hdr, nkept)
        hdr.append(first_lower)

        layout = [np.packbits(ishdr, bitorder="little").tobytes(),
                  L.astype("<u4").tobytes(),
                  hdrblob.tobytes(),
                  runs.astype("<u8").tobytes()]
        payload = emit(K_FASTA, bytes(hdr), [layout, sstreams])
        try:
            if Fasta.inverse(payload, {}) != data:
                return None
        except Exception:
            return None
        return payload, {"n": len(data), "v": 1}

    @staticmethod
    def inverse(payload: bytes, aux_=None) -> bytes:
        kind, b, groups = parse(payload)
        assert kind == K_FASTA
        off = 0
        total, off = _get_varint(b, off)
        nlines, off = _get_varint(b, off)
        trail_nl = b[off]; off += 1
        seqmode = b[off]; off += 1
        nkept, off = _get_varint(b, off)
        first_lower = b[off]; off += 1
        layout, sstreams = groups

        ishdr = np.unpackbits(np.frombuffer(layout[0], dtype=np.uint8),
                              bitorder="little")[:nlines].astype(bool)
        L = np.frombuffer(layout[1], dtype="<u4").astype(np.int64)
        hdrblob = np.frombuffer(layout[2], dtype=np.uint8)
        runs = np.frombuffer(layout[3], dtype="<u8").astype(np.int64)

        seqtotal = int(L[~ishdr].sum())
        up = _seq_decode(seqmode, sstreams, nkept, seqtotal)
        bounds = np.zeros(runs.size + 1, dtype=np.int64)
        np.cumsum(runs, out=bounds[1:])
        lower = np.zeros(seqtotal, dtype=bool)
        sel = np.arange(runs.size) % 2 == (0 if first_lower else 1)
        for i in np.flatnonzero(sel):
            lower[bounds[i]:bounds[i + 1]] = True
        seqblob = np.where(lower, up + 32, up).astype(np.uint8)

        out = np.full(int((L + 1).sum()), NL, dtype=np.uint8)
        lstart = np.zeros(nlines, dtype=np.int64)
        np.cumsum((L + 1)[:-1], out=lstart[1:])
        _scatter(out, lstart[ishdr], L[ishdr], hdrblob)
        _scatter(out, lstart[~ishdr], L[~ishdr], seqblob)
        res = out.tobytes()
        return res if trail_nl else res[:-1]


# =========================================================================
#  Tabular: VCF / SAM / CSV / TSV
# =========================================================================

_INT_RE = re.compile(rb"^-?(0|[1-9][0-9]*)$")


def _render_fixed(v, k):
    p = 10 ** k
    out = []
    for x in v.tolist():
        neg = x < 0
        ax = -x if neg else x
        ip, fp = divmod(ax, p)
        out.append(("-" if neg else "") + f"{ip}.{fp:0{k}d}")
    return "\n".join(out).encode()


def _col_encode(fields, probe):
    raw = b"\n".join(fields)
    n = len(fields)
    cands = [(probe([raw]), 0, [raw])]

    if n and all(_INT_RE.match(f) for f in fields):
        try:
            v = np.array([int(f) for f in fields], dtype=np.int64)
            if b"\n".join(str(x).encode() for x in v.tolist()) == raw:
                blob = _ints_stream(v, probe)
                cands.append((probe([blob]), 1, [blob]))
        except (ValueError, OverflowError):
            pass

    if n > 1:
        f0 = fields[0]
        dot = f0.rfind(b".")
        if 0 < dot < len(f0) - 1:
            k = len(f0) - dot - 1
            ok = k <= 15
            if ok:
                for f in fields:
                    d = f.rfind(b".")
                    if d < 1 or len(f) - d - 1 != k or \
                       not _INT_RE.match(f[:d]) or not f[d + 1:].isdigit():
                        ok = False
                        break
            if ok:
                try:
                    v = np.array([int(f[:f.rfind(b".")] + f[f.rfind(b".") + 1:])
                                  for f in fields], dtype=np.int64)
                    if _render_fixed(v, k) == raw:
                        blob = _ints_stream(v, probe)
                        cands.append((probe([blob]), 2,
                                      [blob, struct.pack("<B", k)]))
                except (ValueError, OverflowError):
                    pass

    cands.sort(key=lambda z: z[0])
    return cands[0][1], cands[0][2]


def _col_decode(mode, streams, n):
    if mode == 0:
        return streams[0].split(b"\n") if n else []
    if mode == 1:
        v = _ints_decode(streams[0], n)
        return [str(x).encode() for x in v.tolist()]
    k = streams[1][0]
    v = _ints_decode(streams[0], n)
    return _render_fixed(v, k).split(b"\n")


class Tabular:
    """Column-split transform for line-oriented delimited text.

    Splitting on EVERY delimiter and storing the per-row field count keeps the
    split exactly reversible whatever the quoting rules are, because the join
    puts every delimiter back where it was."""

    name = "tabular"

    @staticmethod
    def _detect(data):
        head = data[:1 << 16]
        lines = head.split(b"\n")[:200]
        if len(lines) < 8:
            return None
        best = None
        for d in (b"\t", b","):
            cnt = [l.count(d) for l in lines if l]
            if not cnt:
                continue
            m = max(set(cnt), key=cnt.count)
            if m < 1:
                continue
            frac = sum(1 for c in cnt if c == m) / len(cnt)
            # ragged rows are normal (VCF, ad-hoc CSV); the per-row field count
            # is stored, so a delimiter only has to be the dominant one
            if frac > 0.6 and (best is None or m > best[1]):
                best = (d, m)
        return best[0] if best else None

    @staticmethod
    def forward(data: bytes, probe_level: int = 6, delim=None, **kw):
        if not data or b"\r" in data[:1 << 20]:
            return None
        delim = delim or Tabular._detect(data)
        if delim is None:
            return None
        trail_nl = data.endswith(b"\n")
        body = data[:-1] if trail_nl else data
        lines = body.split(b"\n")
        if len(lines) < 8:
            return None
        pre = 0
        while pre < len(lines) and lines[pre].startswith(b"#"):
            pre += 1
        if pre == 0 and lines[0].count(delim) == lines[1].count(delim) \
           and not any(48 <= c <= 57 for c in lines[0]):
            pre = 1                     # a text header over numeric columns
        preamble = b"\n".join(lines[:pre])
        rows = lines[pre:]
        nrow = len(rows)
        if nrow < 4:
            return None
        split = [r.split(delim) for r in rows]
        nf = np.array([len(x) for x in split], dtype=np.int64)
        maxf = int(nf.max())
        if maxf < 2 or maxf > 512:
            return None
        probe = _Probe(probe_level)

        modes, groups = [], []
        for j in range(maxf):
            fields = [x[j] for x in split if len(x) > j]
            mo, st = _col_encode(fields, probe)
            modes.append(mo)
            groups.append(st)

        hdr = bytearray()
        _put_varint(hdr, len(data))
        hdr.append(delim[0])
        hdr.append(1 if trail_nl else 0)
        _put_varint(hdr, nrow)
        _put_varint(hdr, maxf)
        const_nf = 1 if nf.min() == nf.max() else 0
        hdr.append(const_nf)
        for m in modes:
            hdr.append(m)
        aux = [preamble]
        if not const_nf:
            aux.append(nf.astype("<u4").tobytes())

        payload = emit(K_TAB, bytes(hdr), [aux] + groups)
        try:
            if Tabular.inverse(payload, {}) != data:
                return None
        except Exception:
            return None
        return payload, {"n": len(data), "v": 1}

    @staticmethod
    def inverse(payload: bytes, aux_=None) -> bytes:
        kind, b, groups = parse(payload)
        assert kind == K_TAB
        off = 0
        total, off = _get_varint(b, off)
        delim = bytes([b[off]]); off += 1
        trail_nl = b[off]; off += 1
        nrow, off = _get_varint(b, off)
        maxf, off = _get_varint(b, off)
        const_nf = b[off]; off += 1
        modes = list(b[off:off + maxf]); off += maxf

        aux = groups[0]
        preamble = aux[0]
        if const_nf:
            nf = np.full(nrow, maxf, dtype=np.int64)
        else:
            nf = np.frombuffer(aux[1], dtype="<u4").astype(np.int64)
        cols = []
        for j in range(maxf):
            cnt = int((nf > j).sum())
            cols.append(_col_decode(modes[j], groups[1 + j], cnt))
        out = []
        if preamble:
            out.append(preamble)
        if const_nf:
            for row in zip(*cols):
                out.append(delim.join(row))
        else:
            ptr = [0] * maxf
            for i in range(nrow):
                k = int(nf[i])
                parts = []
                for j in range(k):
                    parts.append(cols[j][ptr[j]])
                    ptr[j] += 1
                out.append(delim.join(parts))
        res = b"\n".join(out)
        return res + b"\n" if trail_nl else res


# ------------------------------------------------------------------ sniffer

def sniff(path, head):
    ext = os.path.splitext(path or "")[1].lower()
    h = head.replace(b"\r\n", b"\n")
    if h[:1] == b"@":
        ls = h.split(b"\n")
        if len(ls) >= 4 and ls[2][:1] == b"+" and len(ls[1]) == len(ls[3]) \
           and len(ls[1]) > 0:
            return "fastq"
        if ext in (".fastq", ".fq"):
            return "fastq"
        if h[:3] in (b"@HD", b"@SQ", b"@RG", b"@PG"):
            return "sam"
    if h[:1] == b">":
        ls = h.split(b"\n")
        if len(ls) >= 3 and ls[1] and \
           all(c in b"ACGTUNRYKMSWBDHVacgtunrykmswbdhv*-" for c in ls[1][:80]):
            return "fasta"
        if ext in (".fasta", ".fa", ".fna", ".faa"):
            return "fasta"
    if h[:16].startswith(b"##fileformat=VCF") or ext == ".vcf":
        return "vcf"
    if ext in (".csv", ".tsv", ".sam"):
        return "tabular"
    if Tabular._detect(h) is not None:
        return "tabular"
    return None


TRANSFORMS = {"fastq": Fastq, "fasta": Fasta,
              "vcf": Tabular, "sam": Tabular, "tabular": Tabular}


def _crlf(data):
    """Uniform CRLF -> LF, reversibly.  Mixed endings are refused."""
    if b"\r" not in data:
        return data, 0
    ncr = data.count(b"\r")
    if ncr and ncr == data.count(b"\r\n") == data.count(b"\n"):
        return data.replace(b"\r\n", b"\n"), 1
    return None, 0


def _put_len(v):
    b = bytearray(); _put_varint(b, v); return bytes(b)


class Genome:
    """Dispatcher transform: sniff, normalise line endings, run the right
    sub-transform."""

    name = "genome"

    @staticmethod
    def _whole_records(d: bytes, kind: str):
        """Largest prefix of d that ends on a complete record, plus the ragged
        tail. Real sequencing files are whole, but truncated and streamed ones
        exist, and refusing the whole file over a partial last record threw away
        the entire gain on exactly the files people are most likely to be
        shuffling around."""
        if kind != "fastq":
            return d, b""
        nl = d.rfind(b"\n")
        if nl < 0:
            return d, b""
        # count complete 4-line groups
        n = d.count(b"\n")
        keep = (n // 4) * 4
        if keep == 0:
            return d, b""
        pos, seen = -1, 0
        while seen < keep:
            pos = d.find(b"\n", pos + 1)
            if pos < 0:
                return d, b""
            seen += 1
        return d[:pos + 1], d[pos + 1:]

    @staticmethod
    def forward(data: bytes, path: str = "", **kw):
        d, crlf = _crlf(data)
        if d is None:
            return None
        k = sniff(path, d[:1 << 16])
        if k is None:
            return None
        r = TRANSFORMS[k].forward(d, **kw)
        tail = b""
        if r is None:
            head, tail = Genome._whole_records(d, k)
            if not tail or len(head) < len(d) // 2:
                return None
            r = TRANSFORMS[k].forward(head, **kw)
            if r is None:
                return None
        pay, _ = r
        return (bytes([crlf]) + _put_len(len(tail)) + tail + pay,
                {"n": len(data), "v": 2})

    @staticmethod
    def inverse(payload: bytes, aux=None) -> bytes:
        crlf = payload[0]
        rest = payload[1:]
        if (aux or {}).get("v", 2) >= 2:
            tlen, off = _get_varint(rest, 0)
            tail = rest[off:off + tlen]
            pay = rest[off + tlen:]
        else:
            tail, pay = b"", rest
        k = pay[4]
        if k == K_FASTQ:
            out = Fastq.inverse(pay)
        elif k == K_FASTA:
            out = Fasta.inverse(pay)
        else:
            out = Tabular.inverse(pay)
        out += tail
        return out.replace(b"\n", b"\r\n") if crlf else out


# =========================================================================
#  Per-stream codec selection
# =========================================================================

def _c_lzma(d):
    return lzma.compress(d, format=lzma.FORMAT_RAW, filters=[
        dict(id=lzma.FILTER_LZMA2, preset=9 | lzma.PRESET_EXTREME,
             dict_size=64 << 20)])


def _d_lzma(b):
    return lzma.decompress(b, format=lzma.FORMAT_RAW, filters=[
        dict(id=lzma.FILTER_LZMA2, preset=9 | lzma.PRESET_EXTREME,
             dict_size=64 << 20)])


def _c_ppmd(d):
    if pyppmd is None:
        return None
    enc = pyppmd.Ppmd7Encoder(16, 192 << 20)
    return struct.pack("<Q", len(d)) + enc.encode(d) + enc.flush()


def _d_ppmd(b):
    n = struct.unpack("<Q", b[:8])[0]
    return pyppmd.Ppmd7Decoder(16, 192 << 20).decode(b[8:], n)


STREAM_CODECS = {
    0: (lambda d: d, lambda b: b, "store"),
    1: (lambda d: zstd.ZstdCompressor(level=19, write_content_size=True).compress(d),
        lambda b: zstd.ZstdDecompressor().decompress(b, max_output_size=1 << 31),
        "zstd-19"),
    2: (_c_lzma, _d_lzma, "lzma"),
    3: (lambda d: brotli.compress(d, quality=11, lgwin=24),
        lambda b: brotli.decompress(b), "brotli-11"),
    4: (_c_ppmd, _d_ppmd, "ppmd-16"),
    5: (lambda d: bz2.compress(d, 9), lambda b: bz2.decompress(b), "bzip2"),
}


def _pick_codec(d, big, memo=None):
    key = None
    if memo is not None:
        import hashlib
        key = hashlib.blake2b(d, digest_size=16).digest()
        if key in memo:
            return memo[key]
    ids = [1, 2, 4] if big else [1, 2, 3, 4, 5]
    best = (len(d) + 1, 0, d)
    for i in ids:
        enc, dec, _ = STREAM_CODECS[i]
        try:
            b = enc(d)
        except Exception:
            continue
        if b is None:
            continue
        if len(b) < best[0]:
            best = (len(b), i, b)
    out = (best[1], best[2])
    if memo is not None:
        memo[key] = out
    return out


def pack(data: bytes, path: str = "", log=None):
    """Full domain compression: split, then give every stream its own codec."""
    d0, crlf0 = _crlf(data)
    if d0 is None:
        return None
    k0 = sniff(path, d0[:1 << 16])
    if k0 is None:
        return None
    forces = (0, 1) if k0 in ("fastq", "fasta") else (None,)
    memo = {}
    best = None
    for force in forces:
        d, crlf, k = d0, crlf0, k0
        r = TRANSFORMS[k].forward(d, force_seq=force)
        if r is None:
            return None
        pay = r[0]
        kind, hdr, groups = parse(pay)
        flat = [s for g in groups for s in g]
        out = bytearray(b"GP01")
        out.append(crlf)
        out.append(kind)
        _put_varint(out, len(hdr))
        out += hdr
        _put_varint(out, len(groups))
        for g in groups:
            _put_varint(out, len(g))
        blobs = []
        rec = []
        for s in flat:
            cid, blob = _pick_codec(s, len(s) > (2 << 20), memo)
            rec.append((cid, len(blob), len(s)))
            blobs.append(blob)
        _put_varint(out, len(rec))
        for cid, bl, sl in rec:
            out.append(cid)
            _put_varint(out, bl)
            _put_varint(out, sl)
        res = bytes(out) + b"".join(blobs)
        if log is not None:
            log.append((force, [(STREAM_CODECS[c][2], b, s) for (c, b, s) in rec],
                        len(res)))
        if best is None or len(res) < len(best):
            best = res
        if k not in ("fastq", "fasta"):
            break                       # force_seq is meaningless elsewhere
    return best


def unpack(blob: bytes) -> bytes:
    assert blob[:4] == b"GP01"
    crlf = blob[4]
    kind = blob[5]
    off = 6
    hl, off = _get_varint(blob, off)
    hdr = bytes(blob[off:off + hl]); off += hl
    ng, off = _get_varint(blob, off)
    sizes = []
    for _ in range(ng):
        s, off = _get_varint(blob, off)
        sizes.append(s)
    nrec, off = _get_varint(blob, off)
    rec = []
    for _ in range(nrec):
        cid = blob[off]; off += 1
        bl, off = _get_varint(blob, off)
        sl, off = _get_varint(blob, off)
        rec.append((cid, bl, sl))
    flat = []
    for cid, bl, sl in rec:
        raw = blob[off:off + bl]; off += bl
        flat.append(STREAM_CODECS[cid][1](raw))
    groups, i = [], 0
    for s in sizes:
        groups.append(flat[i:i + s]); i += s
    pay = emit(kind, hdr, groups)
    if kind == K_FASTQ:
        out = Fastq.inverse(pay)
    elif kind == K_FASTA:
        out = Fasta.inverse(pay)
    else:
        out = Tabular.inverse(pay)
    return out.replace(b"\n", b"\r\n") if crlf else out
