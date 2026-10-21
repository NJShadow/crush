"""crushchan core: packet format, erasure codes, text encodings.

Everything in here is integer-only and mirrored exactly by js/src/core.js.
Cross-checked by tests/test_core_cross.py (Python encoder -> JS decoder and back).

Packet (big-endian), identical for every channel:

  off len  field
  0   1    magic 0xC5
  1   1    kind: 0x11 = GF(2) systematic fountain, 0x12 = Reed-Solomon GF(256)
  2   4    session id = SHA-256(data)[0:4]
  6   4    L = data length in bytes (the archive, not counting the hash)
  10  2    T = symbol size in bytes
  12  2    fountain: block index      | RS: K (1 byte), N (1 byte)
  14  4    fountain: ESI (u32)        | RS: block (u16), shard index (u16)
  18  T    symbol payload
  18+T 4   CRC-32 (zlib polynomial) of bytes [0, 18+T)

Source = data || SHA-256(data), zero-padded to a whole number of symbols.
Ktot = ceil((L+32)/T) symbols are split into B blocks of equal size Kb
(the tail of the last block is known-zero padding, which decoders know for free).
The SHA-256 is checked after decoding: nothing is returned unless it matches.
"""
import hashlib
import struct
import zlib

MAGIC = 0xC5
KIND_FOUNTAIN = 0x11
KIND_RS = 0x12
HDR = 18
TRL = 4
OVERHEAD = HDR + TRL
KMAX_FOUNTAIN = 1024          # symbols per fountain block
MAX_L = 1 << 28               # 256 MiB hard bound on claimed size
MIN_T, MAX_T = 8, 8192
MAX_SYMBOLS = 1 << 20          # hostile-input bound on ceil((L+32)/T)
M32 = 0xFFFFFFFF


# ---------------------------------------------------------------- PRNG
def mix32(x):
    """lowbias32 (Chris Wellons). Integer only; JS uses Math.imul."""
    x &= M32
    x ^= x >> 16
    x = (x * 0x7FEB352D) & M32
    x ^= x >> 15
    x = (x * 0x846CA68B) & M32
    x ^= x >> 16
    return x


def row_seed(session, block, esi):
    s = mix32((session ^ mix32((block * 0x85EBCA6B + 0x632BE5AB) & M32)) & M32)
    s = mix32((s + (esi * 0x9E3779B9 & M32)) & M32)
    return s or 0x6D2B79F5


def row_words(seed, n):
    """Counter-mode lowbias32. (A plain xorshift is GF(2)-linear in its seed,
    so all rows would span only 32 dimensions - measured: rank 32 of 782.)"""
    return [mix32((seed + i * 0x9E3779B9) & M32) for i in range(n)]


def fountain_row(session, block, esi, kb):
    """Coefficient bitset (Python int, bit j = source symbol j) for one ESI."""
    if esi < kb:
        return 1 << esi
    nw = (kb + 31) // 32
    words = row_words(row_seed(session, block, esi), nw)
    v = 0
    for i, w in enumerate(words):
        v |= w << (32 * i)
    v &= (1 << kb) - 1
    if v == 0:
        v = 1 << (esi % kb)
    return v


# ---------------------------------------------------------------- layout
def layout(L, T, kind=KIND_FOUNTAIN, K_rs=None):
    ktot = (L + 32 + T - 1) // T
    if kind == KIND_FOUNTAIN:
        B = (ktot + KMAX_FOUNTAIN - 1) // KMAX_FOUNTAIN
        kb = (ktot + B - 1) // B
    else:
        kb = K_rs
        B = (ktot + kb - 1) // kb
    return ktot, B, kb


def source_symbols(data, T, kind=KIND_FOUNTAIN, K_rs=None):
    L = len(data)
    src = data + hashlib.sha256(data).digest()
    ktot, B, kb = layout(L, T, kind, K_rs)
    src = src + b"\0" * (B * kb * T - len(src))
    return [[src[(b * kb + j) * T:(b * kb + j + 1) * T] for j in range(kb)] for b in range(B)]


def pack(kind, session, L, T, f12, f14, payload):
    assert len(payload) == T
    h = struct.pack(">BBIIH", MAGIC, kind, session, L, T) + f12 + f14 + payload
    return h + struct.pack(">I", zlib.crc32(h) & M32)


def parse(pkt):
    """Validate and parse one packet. Returns dict or None. Never trusts sizes."""
    if len(pkt) < OVERHEAD + MIN_T or pkt[0] != MAGIC:
        return None
    kind = pkt[1]
    if kind not in (KIND_FOUNTAIN, KIND_RS):
        return None
    session, L, T = struct.unpack(">IIH", pkt[2:12])
    if not (MIN_T <= T <= MAX_T) or L > MAX_L or len(pkt) != OVERHEAD + T or (L + 32 + T - 1) // T > MAX_SYMBOLS:
        return None
    if struct.unpack(">I", pkt[-4:])[0] != (zlib.crc32(pkt[:-4]) & M32):
        return None
    d = dict(kind=kind, session=session, L=L, T=T, payload=bytes(pkt[HDR:HDR + T]))
    if kind == KIND_FOUNTAIN:
        d["block"], d["esi"] = struct.unpack(">HI", pkt[12:18])
        ktot, B, kb = layout(L, T)
        if d["block"] >= B:
            return None
        d["B"], d["kb"] = B, kb
    else:
        K, N, blk, idx = struct.unpack(">BBHH", pkt[12:18])
        if K < 1 or N < K or idx >= N:
            return None
        ktot, B, kb = layout(L, T, KIND_RS, K)
        if blk >= B:
            return None
        d.update(K=K, N=N, block=blk, esi=idx, B=B, kb=K)
    return d


# ---------------------------------------------------------------- fountain
def fountain_packet(data, T, block, esi, _cache={}):
    key = (id(data), len(data), T)
    if key not in _cache:
        _cache.clear()
        _cache[key] = (source_symbols(data, T), int.from_bytes(hashlib.sha256(data).digest()[:4], "big"))
    blocks, session = _cache[key]
    kb = len(blocks[0])
    row = fountain_row(session, block, esi, kb)
    acc = 0
    j = 0
    r = row
    while r:
        low = r & -r
        j = low.bit_length() - 1
        acc ^= int.from_bytes(blocks[block][j], "little")
        r ^= low
    payload = acc.to_bytes(T, "little")
    return pack(KIND_FOUNTAIN, session, len(data), T, struct.pack(">H", block), struct.pack(">I", esi), payload)


def fountain_schedule(data, T):
    """Infinite packet sequence: all source symbols first (round-robin across
    blocks), then repair symbols forever, interleaved across blocks."""
    ktot, B, kb = layout(len(data), T)
    esi = 0
    while True:
        for b in range(B):
            yield fountain_packet(data, T, b, esi)
        esi += 1


class FountainBlockDecoder:
    """Online Gaussian elimination over GF(2). Rows are Python ints."""

    def __init__(self, session, block, kb, known_zero_from):
        self.session, self.block, self.kb = session, block, kb
        self.piv = {}      # lowest bit -> (coef, payload_int)
        self.rank = 0
        for j in range(known_zero_from, kb):   # padding symbols are known zeros
            self._add(1 << j, 0)
        self.received = 0

    def _add(self, coef, val):
        piv = self.piv
        while coef:
            b = (coef & -coef).bit_length() - 1
            p = piv.get(b)
            if p is None:
                piv[b] = (coef, val)
                self.rank += 1
                return True
            coef ^= p[0]
            val ^= p[1]
        return False

    def add(self, esi, payload):
        self.received += 1
        return self._add(fountain_row(self.session, self.block, esi, self.kb), int.from_bytes(payload, "little"))

    def done(self):
        return self.rank == self.kb

    def solve(self, T):
        sol = [0] * self.kb
        for b in range(self.kb - 1, -1, -1):
            coef, val = self.piv[b]
            r = coef ^ (1 << b)
            while r:
                low = r & -r
                val ^= sol[low.bit_length() - 1]
                r ^= low
            sol[b] = val
        return [s.to_bytes(T, "little") for s in sol]


class Receiver:
    """Channel-agnostic receiver. Feed raw packets in any order; duplicates,
    corrupt packets and foreign sessions are ignored. Works for both kinds."""

    def __init__(self):
        self.p = None
        self.blocks = None
        self.result = None
        self.error = None
        self.stats = dict(packets=0, bad=0, foreign=0, dup=0, useful=0)

    def feed(self, pkt):
        if self.result is not None:
            return True
        self.stats["packets"] += 1
        d = parse(pkt)
        if d is None:
            self.stats["bad"] += 1
            return False
        if self.p is None:
            self.p = {k: d[k] for k in ("kind", "session", "L", "T", "B", "kb")}
            if d["kind"] == KIND_RS:
                self.p["N"] = d["N"]
            self.p["ktot"] = (d["L"] + 32 + d["T"] - 1) // d["T"]
            if d["kind"] == KIND_RS:
                self.p["K"] = d["K"]
            self.blocks = {}          # created lazily: a hostile header cannot force big allocations
        elif any(self.p[k] != d[k] for k in ("kind", "session", "L", "T")) or \
                (d["kind"] == KIND_RS and d["N"] != self.p["N"]):
            self.stats["foreign"] += 1
            return False
        blk = self.blocks.get(d["block"])
        if blk is None:
            b, kb, ktot = d["block"], self.p["kb"], self.p["ktot"]
            real = max(0, min(kb, ktot - b * kb))
            blk = self.blocks[b] = (FountainBlockDecoder(d["session"], b, kb, real) if d["kind"] == KIND_FOUNTAIN
                                    else RSBlockDecoder(self.p["K"], self.p["N"], real))
        if blk.add(d["esi"], d["payload"]):
            self.stats["useful"] += 1
        else:
            self.stats["dup"] += 1
        if len(self.blocks) == self.p["B"] and all(b.done() for b in self.blocks.values()):
            T = self.p["T"]
            src = b"".join(b"".join(self.blocks[i].solve(T)) for i in range(self.p["B"]))
            L = self.p["L"]
            data, h = src[:L], src[L:L + 32]
            if hashlib.sha256(data).digest() != h or int.from_bytes(h[:4], "big") != self.p["session"]:
                # forged or corrupted session (CRC-32 alone is not authentication): drop it and listen again
                self.error = "SHA-256 mismatch after decoding: output refused"
                self.p, self.blocks = None, None
                return False
            self.result = data
            return True
        return False

    def progress(self):
        if not self.blocks:
            return 0.0
        return sum(min(b.rank, b.kb) for b in self.blocks.values()) / (self.p["B"] * self.p["kb"])


# ---------------------------------------------------------------- Reed-Solomon GF(256)
GF_EXP = [0] * 512
GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    GF_EXP[_i] = _x
    GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    GF_EXP[_i] = GF_EXP[_i - 255]


def gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def ginv(a):
    return GF_EXP[255 - GF_LOG[a]]


def rs_coef(K, i, j):
    """Systematic generator: shard i<K is source i; shard i>=K is parity with
    Cauchy coefficients 1/(x_i + y_j), x_i = i (K<=i<N<=255... ) y_j = j.
    x in [K,N), y in [0,K): disjoint, so the full matrix [I; C] is MDS."""
    if i < K:
        return 1 if i == j else 0
    return ginv(i ^ j)


_MUL_TABLES = {}


def _mul_table(c):
    t = _MUL_TABLES.get(c)
    if t is None:
        t = bytes(gmul(c, v) for v in range(256))
        _MUL_TABLES[c] = t
    return t


def rs_encode_block(symbols, K, N):
    """symbols: K bytes objects of length T -> N shards."""
    import numpy as np  # encoder-side convenience only
    T = len(symbols[0])
    src = np.frombuffer(b"".join(symbols), dtype=np.uint8).reshape(K, T)
    out = list(symbols)
    for i in range(K, N):
        acc = np.zeros(T, dtype=np.uint8)
        for j in range(K):
            tab = np.frombuffer(_mul_table(rs_coef(K, i, j)), dtype=np.uint8)
            acc ^= tab[src[j]]
        out.append(acc.tobytes())
    return out


def rs_packets(data, T, K, N):
    assert 1 <= K <= N <= 255
    session = int.from_bytes(hashlib.sha256(data).digest()[:4], "big")
    blocks = source_symbols(data, T, KIND_RS, K)
    pk = []
    for b, syms in enumerate(blocks):
        for i, sh in enumerate(rs_encode_block(syms, K, N)):
            pk.append(pack(KIND_RS, session, len(data), T, struct.pack(">BB", K, N), struct.pack(">HH", b, i), sh))
    return pk


class RSBlockDecoder:
    def __init__(self, K, N, real):
        self.K, self.N, self.kb = K, N, K
        self.real = real          # source symbols >= real are known zeros
        self.zeros = K - real
        self.shards = {}

    @property
    def rank(self):
        return min(self.K, len(self.shards) + self.zeros)

    def add(self, idx, payload):
        if idx in self.shards or self.done() or self.real <= idx < self.K:
            return False
        self.shards[idx] = payload
        return True

    def done(self):
        return len(self.shards) + self.zeros >= self.K

    def solve(self, T):
        K = self.K
        rows, vals = [], []
        for j in range(self.real, K):
            rows.append([1 if c == j else 0 for c in range(K)])
            vals.append(bytes(T))
        for key in sorted(self.shards):
            if len(rows) == K:
                break
            rows.append([rs_coef(K, key, c) for c in range(K)])
            vals.append(self.shards[key])
        vals = [bytearray(v) for v in vals]
        # Gauss-Jordan over GF(256)
        for col in range(K):
            pr = next(r for r in range(col, K) if rows[r][col])
            rows[col], rows[pr] = rows[pr], rows[col]
            vals[col], vals[pr] = vals[pr], vals[col]
            inv = ginv(rows[col][col])
            if inv != 1:
                rows[col] = [gmul(inv, x) for x in rows[col]]
                t = _mul_table(inv)
                vals[col] = bytearray(t[x] for x in vals[col])
            for r in range(K):
                f = rows[r][col]
                if r != col and f:
                    rows[r] = [a ^ gmul(f, b) for a, b in zip(rows[r], rows[col])]
                    t = _mul_table(f)
                    vc = vals[col]
                    vals[r] = bytearray(a ^ t[b] for a, b in zip(vals[r], vc))
        return [bytes(v) for v in vals]


# ---------------------------------------------------------------- Base45 (RFC 9285)
B45 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
B45_IDX = {c: i for i, c in enumerate(B45)}


def b45encode(b):
    out = []
    for i in range(0, len(b) - 1, 2):
        n = b[i] * 256 + b[i + 1]
        c, n = n % 45, n // 45
        d, e = n % 45, n // 45
        out += [B45[c], B45[d], B45[e]]
    if len(b) % 2:
        n = b[-1]
        out += [B45[n % 45], B45[n // 45]]
    return "".join(out)


def b45decode(s):
    out = bytearray()
    n = len(s)
    if n % 3 == 1:
        return None
    try:
        for i in range(0, n - n % 3, 3):
            v = B45_IDX[s[i]] + B45_IDX[s[i + 1]] * 45 + B45_IDX[s[i + 2]] * 2025
            if v > 0xFFFF:
                return None
            out += bytes((v >> 8, v & 255))
        if n % 3 == 2:
            v = B45_IDX[s[-2]] + B45_IDX[s[-1]] * 45
            if v > 0xFF:
                return None
            out.append(v)
    except KeyError:
        return None
    return bytes(out)
