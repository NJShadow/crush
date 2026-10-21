#!/usr/bin/env python3
# ============================================================================
#  ORACLE - program synthesis for compression
#  ---------------------------------------------------------------------------
#  Every compressor in the mainstream asks the same question: "what earlier part
#  of this file does this part look like?" That question has a ceiling, and the
#  industry has been sitting near it for twenty years.
#
#  Oracle asks a different question: "what PROGRAM would produce these bytes?"
#
#  That is the Kolmogorov definition of information - the length of the shortest
#  program that outputs the data. It is uncomputable in general, which is why
#  nobody ships it. But it is perfectly computable over a restricted language,
#  and an enormous amount of real-world data lives inside one: counters,
#  timestamps, auto-increment keys, sensor ramps, index tables, generated
#  identifiers, padding, test fixtures, synthetic datasets, log lines emitted by
#  a loop.
#
#  For that data the answer is not "compress it well". The answer is that the
#  file never contained much information in the first place, and 40 bytes of
#  program reproduces 40 megabytes of it exactly.
#
#  Oracle searches a small language of generators:
#      constant            every byte the same
#      periodic            one block repeated
#      polynomial          v[i] = c0 + c1*i + c2*i^2 + c3*i^3   (exact, integer)
#      linear recurrence   v[n] = a1*v[n-1] + ... + ak*v[n-k] + b
#      congruential        v[n+1] = (a*v[n] + c) mod 2^w        (an LCG stream)
#      templated text      line i = literal skeleton + fields that are
#                          themselves polynomials in i
#
#  Everything it emits is regenerated and byte-compared before it is accepted,
#  so a wrong guess costs nothing but the time to check it.
# ============================================================================
from __future__ import annotations
import struct, re, json
from fractions import Fraction

import numpy as np

# --------------------------------------------------------------------------
DTYPES = [("<u1", 1), ("<u2", 2), ("<u4", 4), ("<u8", 8),
          ("<i2", 2), ("<i4", 4), ("<i8", 8), (">u2", 2), (">u4", 4)]
MAX_DEG = 3
MAX_REC = 4


def _period(data: bytes) -> int:
    """Smallest p such that data is a prefix of block*k. KMP failure function."""
    n = len(data)
    f = [0] * (n + 1)
    k = 0
    for i in range(1, n):
        while k and data[i] != data[k]:
            k = f[k]
        if data[i] == data[k]:
            k += 1
        f[i + 1] = k
    return n - f[n]


def _fit_poly(vals, deg_max=MAX_DEG):
    """Exact integer polynomial fit via finite differences. Returns the initial
    difference column if v[i] is a polynomial in i of degree <= deg_max."""
    cur = list(vals[:deg_max + 2])
    if len(cur) < deg_max + 2:
        return None
    col = [cur[0]]
    work = cur
    for d in range(deg_max + 1):
        work = [work[i + 1] - work[i] for i in range(len(work) - 1)]
        if not work:
            return None
        col.append(work[0])
        if all(x == 0 for x in work):
            return col[:d + 1] or [cur[0]]
    return None


def _gen_poly(col, n, dtype):
    """Regenerate n values from an initial finite-difference column."""
    w = np.dtype(dtype).itemsize * 8
    mask = (1 << w) - 1
    order = len(col)
    out = np.zeros(n, dtype=np.int64)
    diffs = np.array(col, dtype=np.int64)
    # cumulative-sum reconstruction, one difference order at a time
    cur = np.zeros(n, dtype=np.int64)
    cur[:] = diffs[-1]
    for d in range(order - 2, -1, -1):
        cur = np.cumsum(cur) - cur + cur          # identity, keeps dtype
        cur = np.concatenate(([diffs[d]], diffs[d] + np.cumsum(cur[:-1])))
    out = cur
    signed = np.dtype(dtype).kind == "i"
    if signed:
        return out.astype(dtype)
    return (out & mask).astype(dtype)


def _fit_linrec(vals, order):
    """Solve v[n] = a1 v[n-1] + ... + ak v[n-k] + b exactly over the rationals,
    then require an integer solution."""
    k = order
    need = 2 * k + 2
    if len(vals) < need:
        return None
    rows, rhs = [], []
    for i in range(k, k + k + 1):
        rows.append([Fraction(vals[i - j - 1]) for j in range(k)] + [Fraction(1)])
        rhs.append(Fraction(vals[i]))
    m = len(rows)
    ncol = k + 1
    # Gaussian elimination
    aug = [rows[i] + [rhs[i]] for i in range(m)]
    piv_row = 0
    pivots = []
    for c in range(ncol):
        p = None
        for r in range(piv_row, m):
            if aug[r][c] != 0:
                p = r; break
        if p is None:
            continue
        aug[piv_row], aug[p] = aug[p], aug[piv_row]
        pv = aug[piv_row][c]
        aug[piv_row] = [x / pv for x in aug[piv_row]]
        for r in range(m):
            if r != piv_row and aug[r][c] != 0:
                f = aug[r][c]
                aug[r] = [a - f * b for a, b in zip(aug[r], aug[piv_row])]
        pivots.append(c)
        piv_row += 1
        if piv_row == m:
            break
    sol = [Fraction(0)] * ncol
    for i, c in enumerate(pivots):
        sol[c] = aug[i][ncol]
    if any(x.denominator != 1 for x in sol):
        return None
    if any(abs(x.numerator) > 1 << 40 for x in sol):
        return None
    return [int(x) for x in sol]


def _gen_linrec(coef, init, n, dtype):
    k = len(coef) - 1
    b = coef[-1]
    a = coef[:k]
    w = np.dtype(dtype).itemsize * 8
    mask = (1 << w) - 1
    signed = np.dtype(dtype).kind == "i"
    out = list(init)
    for i in range(len(init), n):
        v = b
        for j in range(k):
            v += a[j] * out[i - j - 1]
        v &= mask
        if signed and v >= (1 << (w - 1)):
            v -= (1 << w)
        out.append(v)
    return np.array(out[:n], dtype=dtype)


def _fit_lcg(vals, dtype):
    """x[n+1] = (a*x[n] + c) mod 2^w, recovered from three consecutive terms."""
    w = np.dtype(dtype).itemsize * 8
    mod = 1 << w
    if len(vals) < 4:
        return None
    x0, x1, x2 = int(vals[0]) % mod, int(vals[1]) % mod, int(vals[2]) % mod
    d1 = (x1 - x0) % mod
    d2 = (x2 - x1) % mod
    if d1 % 2 == 0:
        return None                                   # not invertible mod 2^w
    inv = pow(d1, -1, mod)
    a = (d2 * inv) % mod
    c = (x1 - a * x0) % mod
    return a, c


def _gen_lcg(a, c, seed, n, dtype):
    w = np.dtype(dtype).itemsize * 8
    mod = 1 << w
    out = np.empty(n, dtype=np.uint64)
    x = seed % mod
    for i in range(n):
        out[i] = x
        x = (a * x + c) % mod
    return out.astype(dtype)


# --------------------------------------------------------------------------
#  Templated text
# --------------------------------------------------------------------------
_NUM = re.compile(rb"\d+")

def _fit_template(data: bytes):
    """Line-structured text where the literal parts never change and every
    numeric field is a polynomial in the line number."""
    if len(data) > 64 << 20 or b"\n" not in data:
        return None
    lines = data.split(b"\n")
    trailing = b""
    if lines and lines[-1] == b"":
        lines = lines[:-1]
        trailing = b"\n"
    else:
        return None                                   # keep it simple & exact
    n = len(lines)
    if n < 8:
        return None
    skel0 = _NUM.split(lines[0])
    fields = [[int(m.group())] for m in _NUM.finditer(lines[0])]
    widths = [[len(m.group())] for m in _NUM.finditer(lines[0])]
    if not fields:
        return None
    for ln in lines[1:]:
        sk = _NUM.split(ln)
        if sk != skel0:
            return None
        ms = list(_NUM.finditer(ln))
        if len(ms) != len(fields):
            return None
        for i, m in enumerate(ms):
            fields[i].append(int(m.group()))
            widths[i].append(len(m.group()))
    models = []
    for i, col in enumerate(fields):
        wset = set(widths[i])
        pad = widths[i][0] if len(wset) == 1 and str(col[0]).__len__() != widths[i][0] else 0
        col_p = _fit_poly(col)
        if col_p is None:
            return None
        models.append({"c": col_p, "w": pad})
    return {"skel": [s.decode("latin-1") for s in skel0],
            "f": models, "n": n, "t": trailing.decode("latin-1")}


def _gen_template(prog):
    skel = [s.encode("latin-1") for s in prog["skel"]]
    n = prog["n"]
    cols = []
    for m in prog["f"]:
        vals = _gen_poly_py(m["c"], n)
        w = m["w"]
        cols.append([str(v).rjust(w, "0").encode() if w else str(v).encode() for v in vals])
    out = []
    for i in range(n):
        parts = []
        for j, s in enumerate(skel):
            parts.append(s)
            if j < len(cols):
                parts.append(cols[j][i])
        out.append(b"".join(parts))
    return b"\n".join(out) + prog["t"].encode("latin-1")


def _gen_poly_py(col, n):
    """Exact big-integer regeneration from a finite-difference column."""
    order = len(col)
    cur = list(col)
    out = []
    for _ in range(n):
        out.append(cur[0])
        for d in range(order - 1):
            cur[d] += cur[d + 1]
    return out


def _gen_poly_np(col, n):
    """Same reconstruction in NumPy - successive cumulative sums, one per
    difference order. Turns an 11-second Python loop into 20 milliseconds.
    Overflow past int64 simply produces a wrong array, which the mandatory
    byte-comparison then rejects, so it cannot produce a bad archive."""
    order = len(col)
    if order == 1:
        return np.full(n, col[0], dtype=np.int64)
    cur = np.full(n, col[-1], dtype=np.int64)
    for d in range(order - 2, -1, -1):
        cur = np.concatenate(([col[d]], col[d] + np.cumsum(cur[:n - 1])))
    return cur


def _fit_modlin(vals):
    """v[i] = (c0 + c1*i) mod M - the wrapping counter, which is everywhere in
    real data (ring buffers, cyclic ids, i %% 1000) and which no polynomial fit
    will ever match."""
    if len(vals) < 12:
        return None
    d = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
    uniq = sorted(set(d))
    if len(uniq) != 2:
        return None
    hi, lo = uniq[1], uniq[0]
    step = hi
    M = hi - lo
    if M <= 1 or step <= 0 or step >= M:
        return None
    c0 = vals[0]
    for i, v in enumerate(vals):
        if (c0 + step * i) % M != v % M or v != (c0 + step * i) % M:
            return None
    return {"c0": c0, "step": step, "M": M}


# ==========================================================================
#  The transform
# ==========================================================================
class Oracle:
    """Finds the program behind the bytes. Exact regeneration or nothing."""
    name = "oracle"
    exact = True

    # a program is only worth it if it is dramatically smaller
    MIN_INPUT = 4096

    @staticmethod
    def analyze(data: bytes):
        n = len(data)
        if n < Oracle.MIN_INPUT:
            return None

        # ---- 1. constant ------------------------------------------------
        if data == data[:1] * n:
            return {"k": "const", "b": data[0], "n": n}

        # ---- 2. periodic ------------------------------------------------
        p = _period(data)
        if p < n // 4 and p <= 1 << 20:
            if (data[:p] * (n // p + 1))[:n] == data:
                return {"k": "period", "n": n, "_blk": data[:p]}

        # ---- 3. numeric generators --------------------------------------
        for dt, w in DTYPES:
            if n < w * 16:
                continue
            cnt = n // w
            tail = data[cnt * w:]
            if len(tail) > 64:
                continue
            try:
                arr = np.frombuffer(data[:cnt * w], dtype=dt)
            except Exception:
                continue
            head = [int(x) for x in arr[:64]]

            col = _fit_poly(head)
            if col is not None and len(col) <= MAX_DEG + 1:
                prog = {"k": "poly", "dt": dt, "c": col, "n": cnt, "_tail": tail}
                if Oracle._verify(prog, data):
                    return prog

            ml = _fit_modlin(head)
            if ml:
                prog = {"k": "modlin", "dt": dt, "n": cnt, "_tail": tail, **ml}
                if Oracle._verify(prog, data):
                    return prog

            for order in range(1, MAX_REC + 1):
                if len(head) < 2 * order + 2:
                    break
                coef = _fit_linrec(head, order)
                if coef is None:
                    continue
                prog = {"k": "linrec", "dt": dt, "a": coef,
                        "i": [int(x) for x in arr[:order]], "n": cnt, "_tail": tail}
                if Oracle._verify(prog, data):
                    return prog

            lc = _fit_lcg(head, dt)
            if lc:
                prog = {"k": "lcg", "dt": dt, "a": lc[0], "c": lc[1],
                        "s": int(arr[0]), "n": cnt, "_tail": tail}
                if Oracle._verify(prog, data):
                    return prog

        # ---- 4. templated text ------------------------------------------
        try:
            t = _fit_template(data)
        except Exception:
            t = None
        if t:
            prog = {"k": "template", **t}
            try:
                if Oracle.generate(prog) == data:
                    return prog
            except Exception:
                pass
        return None

    @staticmethod
    def generate(prog) -> bytes:
        k = prog["k"]
        if k == "const":
            return bytes([prog["b"]]) * prog["n"]
        if k == "period":
            blk = prog["_blk"]
            n = prog["n"]
            return (blk * (n // len(blk) + 1))[:n]
        if k == "modlin":
            n = prog["n"]
            i = np.arange(n, dtype=np.int64)
            vals = (prog["c0"] + prog["step"] * i) % prog["M"]
            return vals.astype(prog["dt"]).tobytes() + prog["_tail"]
        if k == "poly":
            arr = _gen_poly_np(prog["c"], prog["n"])
            dt = np.dtype(prog["dt"])
            w = dt.itemsize * 8
            mask = (1 << w) - 1
            if dt.kind == "i":
                m = arr & mask
                half = 1 << (w - 1)
                m = np.where(m >= half, m - (1 << w), m)
                return m.astype(prog["dt"]).tobytes() + prog["_tail"]
            return (arr & mask).astype(prog["dt"]).tobytes() + prog["_tail"]
        if k == "poly_slow":
            vals = _gen_poly_py(prog["c"], prog["n"])
            dt = np.dtype(prog["dt"])
            w = dt.itemsize * 8
            mask = (1 << w) - 1
            if dt.kind == "i":
                half = 1 << (w - 1)
                vals = [((v & mask) - (1 << w)) if (v & mask) >= half else (v & mask) for v in vals]
            else:
                vals = [v & mask for v in vals]
            return np.array(vals, dtype=prog["dt"]).tobytes() + prog["_tail"]
        if k == "linrec":
            return _gen_linrec(prog["a"], prog["i"], prog["n"], prog["dt"]).tobytes() + prog["_tail"]
        if k == "lcg":
            return _gen_lcg(prog["a"], prog["c"], prog["s"], prog["n"], prog["dt"]).tobytes() + prog["_tail"]
        if k == "template":
            return _gen_template(prog)
        raise ValueError("unknown program kind " + k)

    @staticmethod
    def _verify(prog, data) -> bool:
        """Check a cheap prefix before regenerating the whole thing."""
        n = prog.get("n", 0)
        if n > 20000:
            small = dict(prog); small["n"] = 8192; small["_tail"] = b""
            try:
                out = Oracle.generate(small)
            except Exception:
                return False
            if not data.startswith(out[:len(out)]):
                return False
        try:
            return Oracle.generate(prog) == data
        except Exception:
            return False

    # ---- transform interface --------------------------------------------
    @staticmethod
    def forward(data: bytes):
        prog = Oracle.analyze(data)
        if prog is None:
            return None
        # binary pieces travel in the payload where a codec can still work on
        # them; only the symbolic part goes into the index
        payload = b""
        meta = {k: v for k, v in prog.items() if not k.startswith("_")}
        if "_blk" in prog:
            payload += prog["_blk"]; meta["_blk_n"] = len(prog["_blk"])
        if prog.get("_tail"):
            payload += prog["_tail"]; meta["_tail_n"] = len(prog["_tail"])
        blob = json.dumps(meta, separators=(",", ":")).encode()
        if len(blob) + len(payload) >= len(data) * 0.5:
            return None
        return payload, {"p": blob.decode()}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        prog = json.loads(aux["p"])
        o = 0
        if "_blk_n" in prog:
            prog["_blk"] = payload[o:o + prog["_blk_n"]]; o += prog["_blk_n"]
        prog["_tail"] = payload[o:o + prog.get("_tail_n", 0)]
        return Oracle.generate(prog)

    @staticmethod
    def describe(aux) -> str:
        p = json.loads(aux["p"])
        return {"const": "constant fill", "period": "repeated block",
                "poly": "polynomial sequence", "linrec": "linear recurrence",
                "lcg": "congruential generator",
                "template": "templated text"}.get(p["k"], p["k"])
