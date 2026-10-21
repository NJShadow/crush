#!/usr/bin/env python3
# ============================================================================
#  PRISM - structural decomposition
#  ---------------------------------------------------------------------------
#  A JSON file, a CSV export, a log: to every mainstream compressor these are
#  just a stream of bytes, and it looks for repeats within a sliding window.
#  But the actual redundancy in such a file runs DOWN the columns, not along
#  the rows. Every `"balance": 4821.55` sits hundreds of bytes away from the
#  next one, with an email address and a timestamp in between. LZMA has to
#  bridge that distance with a match reference every single time.
#
#  Prism takes the file apart along its real structural axis. Keys go in one
#  stream, each field's values go in their own stream, punctuation in another,
#  whitespace in another. Then all the timestamps are adjacent. All the ZIP
#  codes are adjacent. All the ids are adjacent and monotonic, so a delta pass
#  reduces them to a run of near-constant small integers.
#
#  This is the idea behind columnar storage formats - Parquet, ORC, Arrow -
#  applied where nobody applies it: to the compressed archive of an ordinary
#  file, with byte-exact reconstruction of the original including every space,
#  every line ending, and every quirk of the producer's formatting.
#
#  Reconstruction is exact by construction: the split is a pure partition of
#  the original bytes and the skeleton replays them in order. It is verified
#  anyway.
# ============================================================================
from __future__ import annotations
import re, json, struct
from collections import Counter

import numpy as np


# -------------------------------------------------------------- varints ---
def _uv(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


class _R:
    __slots__ = ("b", "i")
    def __init__(self, b): self.b = b; self.i = 0
    def uv(self):
        r = s = 0
        while True:
            c = self.b[self.i]; self.i += 1
            r |= (c & 0x7F) << s
            if not (c & 0x80):
                return r
            s += 7
    def take(self, n):
        v = self.b[self.i:self.i + n]; self.i += n; return v


def _pack(streams) -> bytes:
    """Length-prefixed concatenation. Streams stay contiguous so the codec sees
    each column as one run instead of a thousand scattered fragments."""
    head = _uv(len(streams)) + b"".join(_uv(len(s)) for s in streams)
    return head + b"".join(streams)


def _unpack(buf: bytes):
    r = _R(buf)
    n = r.uv()
    lens = [r.uv() for _ in range(n)]
    return [r.take(L) for L in lens]


# ---------------------------------------------------- numeric columns -----
_INTCOL = re.compile(rb"^-?\d{1,18}$")

def _try_int_column(vals):
    """If a column is all integers, store it as delta-of-integers instead of
    text. An auto-increment id column collapses to a run of 1s."""
    if len(vals) < 16:
        return None
    for v in vals:
        if not _INTCOL.match(v):
            return None
        if len(v) > 1 and (v[0:1] == b"0" or v[0:2] == b"-0"):
            return None                     # zero padding must survive verbatim
    a = np.array([int(v) for v in vals], dtype=np.int64)
    d = np.diff(a, prepend=np.int64(0))
    z = ((d << 1) ^ (d >> 63)).astype(np.uint64)      # zigzag
    out = bytearray()
    for v in z:
        out += _uv(int(v))
    return bytes(out)


def _from_int_column(buf, count):
    r = _R(buf)
    z = np.fromiter((r.uv() for _ in range(count)), dtype=np.uint64, count=count)
    d = ((z >> np.uint64(1)).astype(np.int64)) ^ -(z & np.uint64(1)).astype(np.int64)
    a = np.cumsum(d)
    return [str(int(x)).encode() for x in a]


# --------------------------------------------------- column LAW fitting ---
# Splitting a file into columns puts like next to like, which is most of the
# win. But a column is often not merely similar to itself - it IS a law. A
# timestamp column that steps by exactly 15, a device id that cycles 0..799, a
# reading that repeats every 140 rows: LZMA has to re-derive that pattern from
# the bytes, and pays for it. Fitting the law directly costs a few dozen bytes
# for the whole column. Everything here is regenerated and compared against the
# original values before it is allowed to win.

def _fit_cycle(vals, maxp=4096):
    """Smallest period p with vals[i] == vals[i % p], found by the KMP failure
    function on the value list. Works on the raw field bytes, so it catches
    cycling strings and decimals, not only integers."""
    n = len(vals)
    if n < 32:
        return None
    f = [0] * (n + 1)
    k = 0
    for i in range(1, n):
        while k and vals[i] != vals[k]:
            k = f[k]
        if vals[i] == vals[k]:
            k += 1
        f[i + 1] = k
    p = n - f[n]
    if p > maxp or p >= n // 3:
        return None
    for i in range(n):
        if vals[i] != vals[i % p]:
            return None
    return p


def _fit_intpoly(ints, maxdeg=3):
    """Exact integer polynomial via finite differences."""
    if len(ints) < maxdeg + 3:
        return None
    cur = list(ints[:maxdeg + 2])
    col = [cur[0]]
    work = cur
    for dg in range(maxdeg + 1):
        work = [work[i + 1] - work[i] for i in range(len(work) - 1)]
        if not work:
            return None
        col.append(work[0])
        if all(x == 0 for x in work):
            return col[:dg + 1] or [cur[0]]
    return None


def _gen_intpoly(col, n):
    cur = list(col)
    out = []
    order = len(cur)
    for _ in range(n):
        out.append(cur[0])
        for d in range(order - 1):
            cur[d] += cur[d + 1]
    return out


def _zig(v):
    return (v << 1) if v >= 0 else ((-v << 1) - 1)

def _unzig(z):
    return (z >> 1) if not (z & 1) else -((z + 1) >> 1)


def _sel_size(b: bytes) -> int:
    """Score a candidate by how small it will actually END UP, not how small it
    is raw. A delta-varint column is bigger raw than a cyclic one but can beat
    it after entropy coding, and choosing on raw bytes silently picked the worse
    option on real logs."""
    try:
        import zstandard as _z
        return len(_z.ZstdCompressor(level=3).compress(b))
    except Exception:
        import zlib
        return len(zlib.compress(b, 1))


def _law_body(vals, k, kind):
    """Encode k literal leading values, then a law over the rest.

    The k prefix exists for one reason: a CSV header row. With 'ts' sitting at
    the top of a column of integers, every fit fails and the column falls back
    to raw - which is exactly what was happening. Holding back one value costs
    a few bytes and rescues the entire column."""
    rest = vals[k:]
    if len(rest) < 32:
        return None
    body = bytearray(_uv(k))
    for v in vals[:k]:
        body += _uv(len(v)) + v
    if kind == "cycle":
        p = _fit_cycle(rest)
        if not p:
            return None
        body += _uv(p)
        for v in rest[:p]:
            body += _uv(len(v)) + v
        return bytes(body)
    if kind == "poly":
        for v in rest:
            if not _INTCOL.match(v) or (len(v) > 1 and (v[0:1] == b"0" or v[0:2] == b"-0")):
                return None
        ints = [int(v) for v in rest]
        col = _fit_intpoly(ints)
        if col is None:
            return None
        pred = _gen_intpoly(col, len(ints))
        res = [a - b for a, b in zip(ints, pred)]
        if sum(1 for r in res if r) > len(res) // 4:
            return None
        body += _uv(len(col))
        for c in col:
            body += _uv(_zig(c))
        nz = sum(1 for r in res if r)
        body += _uv(nz)
        prev = -1
        for i, r in enumerate(res):
            if r:
                body += _uv(i - prev - 1) + _uv(_zig(r)); prev = i
        return bytes(body)
    return None


def _emit_column(vals, laws=True):
    """Return (mode, bytes) for one column.
       0 = raw   1 = integer deltas   2 = k literals + cycle   3 = k literals + polynomial"""
    plain = bytearray()
    for v in vals:
        plain += _uv(len(v)) + v
    plain = bytes(plain)
    best_mode, best, best_score = 0, plain, _sel_size(plain)

    ic = _try_int_column(vals)
    if ic is not None:
        sc = _sel_size(ic)
        if sc < best_score:
            best_mode, best, best_score = 1, ic, sc

    if not laws:
        return best_mode, best
    for mode, kind in ((2, "cycle"), (3, "poly")):
        for k in (0, 1):
            try:
                body = _law_body(vals, k, kind)
            except Exception:
                body = None
            if not body:
                continue
            sc = _sel_size(body)
            if sc >= best_score:
                continue
            if _read_column(mode, body, len(vals)) != vals:
                continue                       # never accept an unproven law
            best_mode, best, best_score = mode, body, sc
    return best_mode, best


def _read_column(mode, buf, count):
    if mode == 1:
        return _from_int_column(buf, count)
    if mode in (2, 3):
        r = _R(buf)
        k = r.uv()
        lead = [r.take(r.uv()) for _ in range(k)]
        m = count - k
        if mode == 2:
            p = r.uv()
            base = [r.take(r.uv()) for _ in range(p)]
            return lead + [base[i % p] for i in range(m)]
        order = r.uv()
        col = [_unzig(r.uv()) for _ in range(order)]
        out = _gen_intpoly(col, m)
        nz = r.uv()
        prev = -1
        for _ in range(nz):
            gap = r.uv(); val = _unzig(r.uv())
            i = prev + 1 + gap
            out[i] += val
            prev = i
        return lead + [str(v).encode() for v in out]
    r = _R(buf)
    return [r.take(r.uv()) for _ in range(count)]


# ==========================================================================
#  JSON
# ==========================================================================
_WS = b" \t\r\n"

def _tok_json(b: bytes):
    toks = []
    i, n = 0, len(b)
    while i < n:
        c = b[i]
        if c in _WS:
            j = i
            while j < n and b[j] in _WS:
                j += 1
            toks.append(("W", b[i:j])); i = j
        elif c in b"{}[],:":
            toks.append(("P", b[i:i + 1])); i += 1
        elif c == 0x22:
            j = i + 1
            while j < n:
                if b[j] == 0x5C:
                    j += 2; continue
                if b[j] == 0x22:
                    j += 1; break
                j += 1
            else:
                return None
            toks.append(("S", b[i:j])); i = j
        elif c in b"-0123456789":
            j = i
            while j < n and b[j] in b"-+.eE0123456789":
                j += 1
            toks.append(("N", b[i:j])); i = j
        elif b[i:i + 4] == b"true":
            toks.append(("L", b"true")); i += 4
        elif b[i:i + 5] == b"false":
            toks.append(("L", b"false")); i += 5
        elif b[i:i + 4] == b"null":
            toks.append(("L", b"null")); i += 4
        else:
            return None
        if len(toks) > 40_000_000:
            return None
    return toks


def _json_split(data: bytes, laws=True):
    toks = _tok_json(data)
    if not toks or len(toks) < 64:
        return None
    # a string token is a KEY when the next non-whitespace token is ':'
    kinds = []
    nxt = None
    for idx in range(len(toks) - 1, -1, -1):
        k, v = toks[idx]
        if k == "S" and nxt == b":":
            kinds.append("K")
        else:
            kinds.append(k)
        if k != "W":
            nxt = v if k == "P" else None
    kinds.reverse()

    keydict, keyid = [], {}
    cur_key = -1                    # -1 = array element / no key
    skel = bytearray()
    punct = bytearray()
    ws = bytearray()
    keyseq = bytearray()
    cols = {}                       # colid -> list of value bytes
    stack = []

    for (k, v), kind in zip(toks, kinds):
        if kind == "W":
            skel.append(ord("W")); ws += _uv(len(v)) + v
        elif kind == "P":
            skel.append(ord("P")); punct.append(v[0])
            if v == b"{":
                stack.append(cur_key); cur_key = -1
            elif v == b"}":
                cur_key = stack.pop() if stack else -1
            elif v == b"[":
                stack.append(cur_key)
            elif v == b"]":
                cur_key = stack.pop() if stack else -1
        elif kind == "K":
            if v not in keyid:
                keyid[v] = len(keydict); keydict.append(v)
            cur_key = keyid[v]
            skel.append(ord("K")); keyseq += _uv(cur_key)
        else:
            skel.append(ord(kind))
            cols.setdefault(cur_key, []).append(v)

    if not cols or len(keydict) > 20000:
        return None

    order = sorted(cols)
    modes, streams, counts = [], [], []
    for cid in order:
        m, s = _emit_column(cols[cid], laws)
        modes.append(m); streams.append(s); counts.append(len(cols[cid]))

    kd = bytearray()
    for kb in keydict:
        kd += _uv(len(kb)) + kb

    payload = _pack([bytes(skel), bytes(punct), bytes(ws), bytes(keyseq), bytes(kd)] + streams)
    aux = {"k": "json", "order": order, "modes": modes, "counts": counts,
           "nkeys": len(keydict)}
    return payload, aux


def _json_join(payload: bytes, aux) -> bytes:
    parts = _unpack(payload)
    skel, punct, ws, keyseq, kd = parts[:5]
    streams = parts[5:]
    order, modes, counts = aux["order"], aux["modes"], aux["counts"]
    colvals = {}
    for i, cid in enumerate(order):
        colvals[cid] = _read_column(modes[i], streams[i], counts[i])
    colpos = {cid: 0 for cid in order}

    rk = _R(kd)
    keydict = [rk.take(rk.uv()) for _ in range(aux["nkeys"])]
    rw, rp, rq = _R(ws), _R(punct), _R(keyseq)

    out = bytearray()
    cur_key = -1
    stack = []
    for code in skel:
        c = chr(code)
        if c == "W":
            out += rw.take(rw.uv())
        elif c == "P":
            ch = rp.take(1); out += ch
            if ch == b"{":
                stack.append(cur_key); cur_key = -1
            elif ch == b"}":
                cur_key = stack.pop() if stack else -1
            elif ch == b"[":
                stack.append(cur_key)
            elif ch == b"]":
                cur_key = stack.pop() if stack else -1
        elif c == "K":
            cur_key = rq.uv()
            out += keydict[cur_key]
        else:
            v = colvals[cur_key][colpos[cur_key]]
            colpos[cur_key] += 1
            out += v
    return bytes(out)


# ==========================================================================
#  Delimited text  (CSV / TSV / logs)
# ==========================================================================
def _pick_delim(sample: bytes):
    lines = sample.split(b"\n")[:200]
    if len(lines) < 8:
        return None
    best, bestscore = None, 0
    for d in (b",", b"\t", b"|", b";", b" "):
        counts = [ln.count(d) for ln in lines if ln]
        if not counts:
            continue
        c = Counter(counts)
        mode, freq = c.most_common(1)[0]
        if mode == 0:
            continue
        score = freq * mode
        if score > bestscore:
            best, bestscore = d, score
    return best


def _delim_split(data: bytes, laws=True):
    if len(data) < 4096:
        return None
    delim = _pick_delim(data[:1 << 18])
    if delim is None:
        return None
    # keep line terminators exact
    crlf = data.count(b"\r\n")
    lines = data.split(b"\n")
    trailing_nl = lines[-1] == b""
    if trailing_nl:
        lines = lines[:-1]
    if len(lines) < 16:
        return None

    ncols = Counter(ln.count(delim) for ln in lines).most_common(1)[0][0] + 1
    if ncols < 2 or ncols > 4096:
        return None

    cols = [[] for _ in range(ncols)]
    widths = bytearray()
    extra = bytearray()          # rows whose field count differs
    eol = bytearray()            # 1 bit per line: had a \r before \n
    nreg = 0
    for ln in lines:
        had_cr = ln.endswith(b"\r")
        body = ln[:-1] if had_cr else ln
        eol.append(1 if had_cr else 0)
        f = body.split(delim)
        if len(f) == ncols:
            widths.append(0)
            for i in range(ncols):
                cols[i].append(f[i])
            nreg += 1
        else:
            widths.append(1)
            extra += _uv(len(body)) + body
    if nreg < len(lines) * 0.5:
        return None

    modes, streams, counts = [], [], []
    for c in cols:
        m, s = _emit_column(c, laws)
        modes.append(m); streams.append(s); counts.append(len(c))

    payload = _pack([bytes(widths), bytes(extra), bytes(eol)] + streams)
    aux = {"k": "delim", "d": delim.decode("latin-1"), "n": ncols,
           "modes": modes, "counts": counts, "lines": len(lines),
           "tnl": trailing_nl}
    return payload, aux


def _delim_join(payload: bytes, aux) -> bytes:
    parts = _unpack(payload)
    widths, extra, eol = parts[:3]
    streams = parts[3:]
    delim = aux["d"].encode("latin-1")
    cols = [_read_column(aux["modes"][i], streams[i], aux["counts"][i])
            for i in range(aux["n"])]
    pos = 0
    rx = _R(extra)
    out = []
    for i in range(aux["lines"]):
        if widths[i] == 0:
            body = delim.join(c[pos] for c in cols)
            pos += 1
        else:
            body = rx.take(rx.uv())
        if eol[i]:
            body += b"\r"
        out.append(body)
    res = b"\n".join(out)
    if aux["tnl"]:
        res += b"\n"
    return res


# ==========================================================================
#  XML / HTML
# ==========================================================================
def _xml_split(data: bytes):
    if len(data) < 4096 or data.count(b"<") < 32:
        return None
    tags, text, skel = bytearray(), bytearray(), bytearray()
    i, n = 0, len(data)
    ntok = 0
    while i < n:
        j = data.find(b"<", i)
        if j < 0:
            seg = data[i:]
            text += _uv(len(seg)) + seg
            skel.append(1); i = n; ntok += 1
            break
        if j > i:
            seg = data[i:j]
            text += _uv(len(seg)) + seg
            skel.append(1); ntok += 1
        k = data.find(b">", j)
        if k < 0:
            seg = data[j:]
            text += _uv(len(seg)) + seg
            skel.append(1); ntok += 1
            break
        seg = data[j:k + 1]
        tags += _uv(len(seg)) + seg
        skel.append(0); ntok += 1
        i = k + 1
    if ntok < 32:
        return None
    payload = _pack([bytes(skel), bytes(tags), bytes(text)])
    return payload, {"k": "xml", "n": ntok}


def _xml_join(payload: bytes, aux) -> bytes:
    skel, tags, text = _unpack(payload)
    rt, rx = _R(tags), _R(text)
    out = bytearray()
    for c in skel:
        out += (rt.take(rt.uv()) if c == 0 else rx.take(rx.uv()))
    return bytes(out)


# ==========================================================================
#  The transform
# ==========================================================================
_PROBE_CAP = 6 << 20

def _probe(d: bytes) -> int:
    """Approximate the TOURNAMENT, not one codec.

    Crush compresses each bundle with several codecs and keeps the smallest, so
    a gate that consults only one of them can be flatly wrong. On a real server
    log, lzma -9e made the law-fitted payload 11% WORSE while lzma -6, zstd-19
    and the text-tuned LZMA filter all made it better - and the tournament's
    actual winner was 2.5% better with laws on. Probing with the min of two
    dissimilar codecs tracks the outcome; probing with one does not."""
    d = d[:_PROBE_CAP]
    import lzma as _l
    best = len(_l.compress(d, preset=6))
    try:
        import zstandard as _z
        best = min(best, len(_z.ZstdCompressor(level=19).compress(d)))
    except Exception:
        pass
    return best


class Prism:
    name = "prism"
    exact = True

    SPLITTERS = [("json", _json_split), ("delim", _delim_split), ("xml", _xml_split)]
    JOINERS = {"json": _json_join, "delim": _delim_join, "xml": _xml_join}

    @staticmethod
    def forward(data: bytes):
        if len(data) < 4096:
            return None
        head = data.lstrip()[:1]
        cands = []
        if head in (b"{", b"["):
            cands = ["json", "delim"]
        elif head == b"<":
            cands = ["xml", "delim"]
        else:
            cands = ["delim", "json", "xml"]
        for name in cands:
            fn = dict(Prism.SPLITTERS)[name]
            try:
                r = fn(data)
            except Exception:
                r = None
            if not r:
                continue
            # Column laws are chosen per column, which cannot see what the codec
            # will make of the payload AS A WHOLE - on one real server log that
            # myopia cost 11%. So build it both ways and measure.
            if name in ("json", "delim") and any(m > 1 for m in r[1].get("modes", [])):
                try:
                    r0 = fn(data, laws=False)
                except Exception:
                    r0 = None
                # Require a DECISIVE margin. Column laws change the payload's
                # character enough that the bundle's winning codec can flip, and
                # a 2% per-file gain can become a 5% bundle-level loss. Where the
                # law is real - a pure generated column - the margin is 90%+, so
                # demanding 5% costs nothing there and removes the coin-flips.
                if r0 and _probe(r[0]) >= _probe(r0[0]) * 0.95:
                    return r0
            return r
        return None

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        return Prism.JOINERS[aux["k"]](payload, aux)

    @staticmethod
    def describe(aux) -> str:
        k = aux.get("k")
        if k == "json":
            return f"JSON shredded into {len(aux['order'])} value columns"
        if k == "delim":
            return f"delimited text shredded into {aux['n']} columns"
        return "markup split into tag and text streams"
