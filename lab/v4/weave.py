#!/usr/bin/env python3
# ============================================================================
#  WEAVE - getting the most out of inter-file redundancy in a solid bundle
#  ---------------------------------------------------------------------------
#  A solid archive is one long stream. Every compressor that reads it has a
#  finite window and a memory that decays with distance: a match 40 MB back
#  costs more bits to encode than the same match 40 KB back, and a match beyond
#  the dictionary costs everything, because it is not a match at all.
#
#  So the ORDER of files inside the stream is a real, free parameter, and every
#  mainstream archiver throws it away. 7-Zip sorts by extension then name.
#  That is a proxy for similarity, and a bad one: `utils.py` and `util.py` sort
#  together by luck, two builds of the same binary called `app` and `zdemo`
#  do not.
#
#  WEAVE does it directly.
#
#    1. SIGNATURE  Each file gets a content-defined sample of its 24-byte
#       shingles, chosen by a rolling gear hash so the sample survives
#       insertion and deletion, at a rate auto-tuned to the set's median file
#       size so a 3 KB source file and a 6 MB binary are both resolved. That
#       is a fixed-rate MinHash:
#       the intersection of two signatures divided by their union is an
#       unbiased estimate of Jaccard similarity, and divided by the smaller one
#       it estimates containment, which is the question that actually matters
#       when a small file is a subset of a large one.
#
#    2. ORDER      Similar files are made adjacent. Files are clustered on
#       strong edges, ordered inside each cluster by a greedy nearest-neighbour
#       walk refined with neighbour-list 2-opt, and the clusters themselves are
#       then ordered by the same walk over their medoids. The result is a path
#       through the similarity graph that maximises total adjacent similarity.
#
#    3. DELTA      Where similarity is extreme - successive revisions, two
#       builds of one binary, a vendored library at two versions - the file is
#       stored as an explicit delta against a reference chosen per cluster.
#       Two encoders are available and WEAVE measures both: an explicit
#       copy/insert delta whose literals stay uncompressed so the surrounding
#       solid codec can still model them, and a Zstandard stream with the
#       reference as a raw-content preset dictionary.
#
#    4. NEVER LOSE Ordering and delta are both proposals. WEAVE probes the
#       arrangements with a fast codec, keeps whichever is smaller, and records
#       the decision. On material where neither helps it emits the original
#       order with no deltas and costs only the signature pass.
#
#  Everything here is pure Python + numpy + zstandard, importable, and has no
#  dependency on the rest of Crush.
# ============================================================================
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    import zstandard as zstd
except ImportError:                                       # pragma: no cover
    zstd = None
import zlib

# ---------------------------------------------------------------------------
# signature parameters. These are part of the on-disk decision, not the on-disk
# format: an archive stores the resulting order and deltas, never the sketches,
# so these can be retuned freely without breaking old archives.
# ---------------------------------------------------------------------------
SHINGLE = 16                 # bytes of context behind each sampled position
SAMPLE_BITS = 6              # default: keep 1 position in 2**6 = 64
SCAN_CAP = 1 << 20           # bytes actually read per file (head + tail halves)
SKETCH_CAP = 3072            # hard ceiling on signature length
TARGET_SAMPLES = 800         # what auto_bits aims for on a typical file

_rng = np.random.RandomState(0xC0FFEE)
GEAR = _rng.randint(0, 1 << 62, size=256, dtype=np.int64).astype(np.uint64)
_U64 = np.uint64


def auto_bits(sizes: Sequence[int], cap: int = SCAN_CAP) -> int:
    """Pick one sampling rate for the whole set.

    The rate has to be identical across files or the signatures are not
    comparable, but the right rate depends on how big the files are: 1 in 64 is
    generous for a 6 MB binary and starves a 3 KB source file. Aim for roughly
    TARGET_SAMPLES signature values on the median file and use that everywhere.
    Note this is a decision about the median, so a set mixing 3 KB and 6 MB
    files resolves the small ones a little coarsely - which is the right
    trade, because it is the small ones that are cheap to sketch densely."""
    if not sizes:
        return SAMPLE_BITS
    med = float(np.median([min(int(s), cap) for s in sizes]))
    if med <= 0:
        return 3
    b = int(round(np.log2(max(med / TARGET_SAMPLES, 1.0))))
    return max(2, min(12, b))


# ============================================================================
#  1. SIGNATURES
# ============================================================================

def scan_bytes(data: bytes, cap: int = SCAN_CAP) -> bytes:
    """The bytes we are willing to look at. Head and tail halves for anything
    over the cap, so a large file whose head is boilerplate still contributes a
    signature from its real content."""
    if len(data) <= cap:
        return data
    h = cap // 2
    return data[:h] + data[-h:]


def _roll(a: np.ndarray) -> np.ndarray:
    """Four-tap gear hash, avalanched.

    The avalanche is not decoration. Without it the taps only ever shift bits
    UPWARDS, so the low bits of the result depend on one byte and nothing else -
    and a selection rule of the form `h & mask == 0` then picks positions by the
    byte value sitting there rather than by content. That bug is invisible
    (it still returns plausible-looking positions, and they are still stable
    under insertion) right up until you notice that a 1 MB file yields a
    signature of length one because no gear constant happens to end in six
    zero bits."""
    g = GEAR[a]
    h = g.copy()
    for k in (1, 2, 3):
        h[k:] ^= np.left_shift(g[:-k], _U64(k * 7))
    return _mix64(h)


def _mix64(h: np.ndarray) -> np.ndarray:
    """splitmix64 finaliser - makes every bit of the polynomial hash usable."""
    with np.errstate(over="ignore"):
        h = h ^ (h >> _U64(30))
        h = h * _U64(0xBF58476D1CE4E5B9)
        h = h ^ (h >> _U64(27))
        h = h * _U64(0x94D049BB133111EB)
        h = h ^ (h >> _U64(31))
    return h


def sketch(data: bytes, cap: int = SCAN_CAP, bits: int = SAMPLE_BITS) -> np.ndarray:
    """Fixed-rate content-defined MinHash signature.

    A four-tap gear hash over the byte stream picks roughly one position in
    2**SAMPLE_BITS. Because the choice depends only on the bytes at that
    position, inserting a paragraph at the front shifts no decision after it -
    which is the whole point, and the reason a sample-every-Nth-byte scheme
    cannot work.

    At each chosen position a strong 64-bit hash of the preceding SHINGLE bytes
    is emitted. The returned array is sorted and unique; because the sampling
    rate is fixed rather than bottom-k, intersection over union across two of
    these arrays is an unbiased Jaccard estimate even when the two files differ
    in size by three orders of magnitude."""
    b = scan_bytes(data, cap)
    n = len(b)
    if n < SHINGLE + 8:
        # too short to shingle: hash it whole so identical tiddlers still match
        if n == 0:
            return np.zeros(0, dtype=np.uint64)
        v = np.array([(zlib.crc32(b) * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF],
                     dtype=np.uint64)
        return np.unique(_mix64(v))

    a = np.frombuffer(b, dtype=np.uint8)
    mask = _U64((1 << bits) - 1)
    h = _roll(a)
    pos = np.flatnonzero((h & mask) == 0)
    pos = pos[pos >= SHINGLE]
    if pos.size == 0:
        pos = np.array([n - 1], dtype=np.int64)
    if pos.size > SKETCH_CAP * 2:
        # A pathological file (long runs of one byte) can trip the mask
        # everywhere. Thin deterministically by value, not by position, so the
        # thinning is still content-defined.
        pos = pos[:: max(1, pos.size // (SKETCH_CAP * 2))]

    # strong hash of the SHINGLE bytes ending at each chosen position
    idx = pos[:, None] - np.arange(SHINGLE, 0, -1, dtype=np.int64)[None, :]
    win = a[idx].astype(np.uint64)
    acc = np.zeros(pos.size, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for j in range(SHINGLE):
            acc = acc * _U64(0x100000001B3) + win[:, j]
    vals = np.unique(_mix64(acc))
    if vals.size > SKETCH_CAP:
        vals = vals[:SKETCH_CAP]          # bottom-k truncation, sorted already
    return vals


def sketch_many(datas: Sequence[bytes], cap: int = SCAN_CAP,
                bits: Optional[int] = None) -> List[np.ndarray]:
    if bits is None:
        bits = auto_bits([len(d) for d in datas], cap)
    return [sketch(d, cap, bits) for d in datas]


# ============================================================================
#  2. SIMILARITY GRAPH
# ============================================================================

@dataclass
class SimGraph:
    """Sparse similarity over pairs that share at least one signature value."""
    n: int
    src: np.ndarray            # int32
    dst: np.ndarray            # int32
    jac: np.ndarray            # float32, |A n B| / |A u B|
    con: np.ndarray            # float32, |A n B| / min(|A|,|B|)  (containment)
    nbrs: List[np.ndarray] = field(default_factory=list)   # per node, by weight

    def weight(self, mode: str = "mix") -> np.ndarray:
        if mode == "jaccard":
            return self.jac
        if mode == "containment":
            return self.con
        # ordering wants "will these two help each other", which is closer to
        # containment; delta wants near-identity, which is Jaccard. The mix
        # sits between and is what the ordering pass uses by default.
        return np.maximum(self.jac, 0.5 * self.con).astype(np.float32)


# A signature value shared by a very large number of files (a licence header, a
# run of zero bytes) generates a quadratic number of pairs for very little
# information. Groups are dropped largest-first, and only when the pair count
# would otherwise blow the budget - dropping them unconditionally is wrong,
# because in a set of a hundred revisions of one file the values shared by all
# hundred are exactly the signal.
MAX_PAIRS = 4_000_000


def build_graph(sketches: Sequence[np.ndarray], min_shared: int = 1,
                knn: int = 24) -> SimGraph:
    """Inverted index over signature values -> candidate pairs -> similarities.

    Never forms an n x n matrix; on 2500 files this is a few tens of
    milliseconds and the memory is proportional to the number of pairs that
    actually share content."""
    n = len(sketches)
    sizes = np.array([max(int(s.size), 1) for s in sketches], dtype=np.float32)
    if n < 2:
        return SimGraph(n, np.zeros(0, np.int32), np.zeros(0, np.int32),
                        np.zeros(0, np.float32), np.zeros(0, np.float32),
                        [np.zeros(0, np.int32) for _ in range(n)])

    owners = np.concatenate([np.full(s.size, i, dtype=np.int32)
                             for i, s in enumerate(sketches)]) if n else np.zeros(0, np.int32)
    values = np.concatenate(list(sketches)) if n else np.zeros(0, np.uint64)
    if values.size == 0:
        return SimGraph(n, np.zeros(0, np.int32), np.zeros(0, np.int32),
                        np.zeros(0, np.float32), np.zeros(0, np.float32),
                        [np.zeros(0, np.int32) for _ in range(n)])

    order = np.argsort(values, kind="stable")
    values = values[order]
    owners = owners[order]
    # group boundaries of equal values
    brk = np.flatnonzero(np.r_[True, values[1:] != values[:-1], True])
    counts = np.diff(brk)
    keep = counts >= 2
    starts = brk[:-1][keep]
    cnts = counts[keep]

    # Drop the widest groups first, but only as far as the pair budget demands.
    if cnts.size:
        pairs = cnts.astype(np.int64) * (cnts.astype(np.int64) - 1) // 2
        if pairs.sum() > MAX_PAIRS:
            o = np.argsort(cnts, kind="stable")            # narrowest first
            csum = np.cumsum(pairs[o])
            take = o[csum <= MAX_PAIRS]
            starts, cnts = starts[take], cnts[take]

    lo_l, hi_l = [], []
    for s, c in zip(starts.tolist(), cnts.tolist()):
        grp = owners[s:s + c]
        if grp.size > 1:
            ii, jj = np.triu_indices(grp.size, 1)
            lo_l.append(grp[ii]); hi_l.append(grp[jj])
    if not lo_l:
        return SimGraph(n, np.zeros(0, np.int32), np.zeros(0, np.int32),
                        np.zeros(0, np.float32), np.zeros(0, np.float32),
                        [np.zeros(0, np.int32) for _ in range(n)])
    lo = np.concatenate(lo_l).astype(np.int64)
    hi = np.concatenate(hi_l).astype(np.int64)
    a = np.minimum(lo, hi); b = np.maximum(lo, hi)
    key = a * n + b
    key, inter = np.unique(key, return_counts=True)
    src = (key // n).astype(np.int32)
    dst = (key % n).astype(np.int32)
    inter = inter.astype(np.float32)
    si, sj = sizes[src], sizes[dst]
    jac = inter / np.maximum(si + sj - inter, 1.0)
    con = inter / np.maximum(np.minimum(si, sj), 1.0)
    np.clip(jac, 0.0, 1.0, out=jac)
    np.clip(con, 0.0, 1.0, out=con)

    g = SimGraph(n, src, dst, jac.astype(np.float32), con.astype(np.float32))
    g.nbrs = _knn_lists(g, knn)
    return g


def _knn_lists(g: SimGraph, knn: int) -> List[np.ndarray]:
    w = g.weight()
    src = np.concatenate([g.src, g.dst])
    dst = np.concatenate([g.dst, g.src])
    ww = np.concatenate([w, w])
    o = np.lexsort((-ww, src))
    src, dst, ww = src[o], dst[o], ww[o]
    out = [np.zeros(0, dtype=np.int32) for _ in range(g.n)]
    if src.size:
        bnd = np.flatnonzero(np.r_[True, src[1:] != src[:-1], True])
        for a, b in zip(bnd[:-1], bnd[1:]):
            out[int(src[a])] = dst[a:min(b, a + knn)].copy()
    return out


def dense_sim(g: SimGraph, mode: str = "mix") -> np.ndarray:
    """Only for diagnostics and tiny sets - O(n^2) memory."""
    S = np.zeros((g.n, g.n), dtype=np.float32)
    w = g.weight(mode)
    S[g.src, g.dst] = w
    S[g.dst, g.src] = w
    return S


def adjacent_similarity(g: SimGraph, order: Sequence[int], mode: str = "mix") -> float:
    """Total similarity of consecutive pairs - the objective the ordering pass
    is maximising, reported so a run can be judged without compressing."""
    if g.n < 2:
        return 0.0
    w = g.weight(mode)
    lut: Dict[int, float] = {}
    for s, d, x in zip(g.src.tolist(), g.dst.tolist(), w.tolist()):
        lut[s * g.n + d] = x
    tot = 0.0
    o = list(order)
    for u, v in zip(o[:-1], o[1:]):
        a, b = (u, v) if u < v else (v, u)
        tot += lut.get(a * g.n + b, 0.0)
    return tot


# ============================================================================
#  3. ORDERING
# ============================================================================

def _ext_key(name: str) -> Tuple[str, str]:
    base = os.path.basename(name)
    i = base.rfind(".")
    return (base[i:].lower() if i > 0 else "", base)


def greedy_nn(nodes: List[int], nbrs: List[np.ndarray], wmap: Dict[int, float],
              n: int, start: Optional[int] = None) -> List[int]:
    """Nearest-neighbour walk restricted to the k-NN lists.

    When a node's neighbours are all used the walk has to jump; it jumps to the
    next unused node in the caller's tie-break order rather than scanning the
    whole graph, which keeps this linear."""
    if not nodes:
        return []
    inset = set(nodes)
    used = set()
    remaining = list(nodes)
    cur = start if start is not None and start in inset else nodes[0]
    out = [cur]
    used.add(cur)
    ptr = 0
    while len(out) < len(nodes):
        best, bw = -1, -1.0
        for c in nbrs[cur]:
            c = int(c)
            if c in used or c not in inset:
                continue
            a, b = (cur, c) if cur < c else (c, cur)
            x = wmap.get(a * n + b, 0.0)
            if x > bw:
                bw, best = x, c
        if best < 0:
            while ptr < len(remaining) and remaining[ptr] in used:
                ptr += 1
            if ptr >= len(remaining):
                break
            best = remaining[ptr]
        out.append(best)
        used.add(best)
        cur = best
    return out


def two_opt(order: List[int], nbrs: List[np.ndarray], wmap: Dict[int, float],
            n: int, budget_s: float = 2.0) -> List[int]:
    """Neighbour-list 2-opt on the adjacent-similarity objective.

    Full 2-opt is O(n^2) per sweep, which is unaffordable on a few thousand
    files. Restricting candidate reconnections to each node's k nearest
    neighbours is the standard fix and captures essentially all of the gain,
    because a reconnection to a file you are not similar to cannot help."""
    m = len(order)
    if m < 4:
        return order
    pos = {v: i for i, v in enumerate(order)}

    def w(u, v):
        a, b = (u, v) if u < v else (v, u)
        return wmap.get(a * n + b, 0.0)

    t0 = time.time()
    improved = True
    while improved and time.time() - t0 < budget_s:
        improved = False
        for i in range(m - 1):
            if time.time() - t0 > budget_s:
                break
            a, b = order[i], order[i + 1]
            wab = w(a, b)
            for c in nbrs[a]:
                c = int(c)
                j = pos.get(c, -1)
                if j <= i + 1 or j >= m - 1:
                    continue
                d = order[j + 1]
                gain = (w(a, c) + w(b, d)) - (wab + w(c, d))
                if gain > 1e-6:
                    order[i + 1:j + 1] = order[i + 1:j + 1][::-1]
                    for t in range(i + 1, j + 1):
                        pos[order[t]] = t
                    improved = True
                    break
    return order


def connected_clusters(g: SimGraph, thresh: float, mode: str = "containment") -> List[List[int]]:
    """Union-find over edges above `thresh`. These are the near-duplicate
    families: the unit a delta reference is chosen for."""
    parent = list(range(g.n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    w = g.weight(mode)
    sel = np.flatnonzero(w >= thresh)
    for e in sel.tolist():
        ra, rb = find(int(g.src[e])), find(int(g.dst[e]))
        if ra != rb:
            parent[rb] = ra
    groups: Dict[int, List[int]] = {}
    for i in range(g.n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def order_files(sketches: Sequence[np.ndarray], names: Sequence[str],
                g: Optional[SimGraph] = None,
                groups: Optional[Sequence] = None,
                two_opt_budget: float = 2.0,
                cluster_thresh: float = 0.45,
                strategy: str = "walk") -> List[int]:
    """The ordering pass.

    `strategy="walk"` (the default, and the one that measures best) is a single
    greedy nearest-neighbour walk over the whole partition followed by
    neighbour-list 2-opt. Files with no measurable relative are picked up by the
    walk's fallback in the incoming order, so Crush's existing (extension, name)
    sort survives as the tie-break where similarity says nothing - which is the
    right default, since a name is a weak signal but not a worthless one.

    `strategy="cluster"` lays out near-duplicate families as units and then
    orders the families. It is kept because it is the arrangement a delta pass
    wants and because it scales without the walk's global state, but on the
    material measured here it is a fraction of a percent behind the plain walk.

    `groups` (a per-file label, typically Crush's file class) is a hard
    partition: ordering happens inside a group, never across it, because Crush
    bundles by class anyway."""
    n = len(sketches)
    if n < 2:
        return list(range(n))
    if g is None:
        g = build_graph(sketches)
    w = g.weight()
    wmap = {int(s) * n + int(d): float(x)
            for s, d, x in zip(g.src, g.dst, w)}

    if groups is None:
        groups = [0] * n
    part: Dict[object, List[int]] = {}
    for i, gk in enumerate(groups):
        part.setdefault(gk, []).append(i)

    out: List[int] = []
    for gk in sorted(part, key=lambda x: str(x)):
        idx = sorted(part[gk], key=lambda i: _ext_key(names[i]))
        if len(idx) < 3:
            out.extend(idx)
            continue
        if strategy == "walk":
            walk = greedy_nn(idx, g.nbrs, wmap, n, start=idx[0])
            if len(walk) >= 4:
                walk = two_opt(walk, g.nbrs, wmap, n, budget_s=two_opt_budget)
            out.extend(walk)
            continue

        sub = set(idx)
        clusters = [[i for i in c if i in sub]
                    for c in connected_clusters(g, cluster_thresh)]
        clusters = [c for c in clusters if c]
        laid = []
        for c in clusters:
            if len(c) == 1:
                laid.append(c)
                continue
            c = sorted(c, key=lambda i: _ext_key(names[i]))
            walk = greedy_nn(c, g.nbrs, wmap, n, start=c[0])
            if len(walk) >= 4:
                walk = two_opt(walk, g.nbrs, wmap, n,
                               budget_s=two_opt_budget * len(walk) / max(len(idx), 1))
            laid.append(walk)
        laid.sort(key=lambda c: (-len(c), _ext_key(names[c[0]])))
        if len(laid) > 1:
            laid = _chain_clusters(laid, g, wmap, n)
        for c in laid:
            out.extend(c)
    return out


def _chain_clusters(clusters: List[List[int]], g: SimGraph,
                    wmap: Dict[int, float], n: int) -> List[List[int]]:
    """Greedy walk over clusters, scored by the best edge between the tail of
    one and the head of the next. Quadratic in the number of clusters, so it is
    capped; above the cap the (already sensible) size/name order stands."""
    m = len(clusters)
    if m > 600:
        return clusters
    ends = [(c[0], c[-1]) for c in clusters]

    def link(i, j):
        best = 0.0
        for u in (ends[i][1], ends[i][0]):
            for v in (ends[j][0], ends[j][1]):
                a, b = (u, v) if u < v else (v, u)
                x = wmap.get(a * n + b, 0.0)
                if x > best:
                    best = x
        return best

    used = [False] * m
    cur = 0
    used[0] = True
    order = [0]
    for _ in range(m - 1):
        best, bw = -1, -1.0
        for j in range(m):
            if used[j]:
                continue
            x = link(cur, j)
            if x > bw:
                bw, best = x, j
        used[best] = True
        order.append(best)
        cur = best
    return [clusters[i] for i in order]


# ============================================================================
#  4. DELTA
# ============================================================================

DELTA_LZ = 0                 # explicit copy/insert, literals left uncompressed
DELTA_ZSTD = 1               # zstd with the reference as a raw preset dictionary

# LZ delta: content-defined anchors, so an insertion near the front of the file
# does not knock every later anchor out of alignment.
ANCHOR_BITS = 3              # one anchor per 8 bytes of reference, on average
ANCHOR_WIN = 12              # bytes hashed per anchor
MIN_MATCH = 16              # shorter than this and the op costs more than it saves


def _anchor_hashes(a: np.ndarray, bits: int, win: int):
    """Content-defined anchor positions and a strong hash of the `win` bytes
    ending at each."""
    if a.size < win + 8:
        return np.zeros(0, np.int64), np.zeros(0, np.uint64)
    h = _roll(a)
    pos = np.flatnonzero((h & _U64((1 << bits) - 1)) == 0)
    pos = pos[pos >= win]
    if pos.size == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.uint64)
    idx = pos[:, None] - np.arange(win, 0, -1, dtype=np.int64)[None, :]
    w = a[idx].astype(np.uint64)
    acc = np.zeros(pos.size, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for j in range(win):
            acc = acc * _U64(0x100000001B3) + w[:, j]
    return pos.astype(np.int64), _mix64(acc)


def _match_len_fwd(A: np.ndarray, i: int, B: np.ndarray, j: int, limit: int) -> int:
    """Length of the common prefix of A[i:] and B[j:], in blocks."""
    n = min(A.size - i, B.size - j, limit)
    off = 0
    step = 1 << 12
    while off < n:
        k = min(step, n - off)
        d = np.not_equal(A[i + off:i + off + k], B[j + off:j + off + k])
        w = np.flatnonzero(d)
        if w.size:
            return off + int(w[0])
        off += k
        step = min(step * 4, 1 << 20)
    return n


def _match_len_bwd(A: np.ndarray, i: int, B: np.ndarray, j: int, limit: int) -> int:
    n = min(i, j, limit)
    if n <= 0:
        return 0
    d = np.not_equal(A[i - n:i][::-1], B[j - n:j][::-1])
    w = np.flatnonzero(d)
    return int(w[0]) if w.size else n


def lz_delta_encode(ref: bytes, tgt: bytes) -> bytes:
    """An explicit copy/insert delta of `tgt` against `ref`.

    This is the bsdiff shape without the suffix array: anchors in the reference
    are indexed by a content-defined rolling hash, every anchor in the target
    is looked up once, and a hit is extended both ways with vectorised
    comparisons - so the Python loop runs once per MATCH, not once per byte,
    and a pair of near-identical 6 MB binaries costs a few hundred iterations.

    The output is a varint op stream followed by the raw literal bytes and is
    deliberately NOT entropy-coded. In a solid archive that is the point: the
    literals go into the same LZMA stream as everything else and are modelled
    there, where a self-contained zstd delta would arrive as an opaque
    incompressible blob and poison its neighbours."""
    A = np.frombuffer(ref, dtype=np.uint8)
    B = np.frombuffer(tgt, dtype=np.uint8)
    rpos, rhash = _anchor_hashes(A, ANCHOR_BITS, ANCHOR_WIN)
    ops = bytearray()
    lits = bytearray()
    if rpos.size == 0:
        return _pack_lz(tgt)

    o = np.argsort(rhash, kind="stable")
    rhash_s, rpos_s = rhash[o], rpos[o]
    tpos, thash = _anchor_hashes(B, ANCHOR_BITS, ANCHOR_WIN)

    lo = np.searchsorted(rhash_s, thash, "left")
    hi = np.searchsorted(rhash_s, thash, "right")
    hit = lo < hi

    cursor = 0                     # next target byte not yet emitted
    last_ref = 0                   # for delta-coded copy offsets
    n_ops = 0
    for t in range(tpos.size):
        if not hit[t]:
            continue
        b = int(tpos[t])
        if b < cursor:
            continue
        # take the first candidate; near-duplicates rarely need more
        a = int(rpos_s[lo[t]])
        fwd = _match_len_fwd(A, a, B, b, 1 << 30)
        if fwd < MIN_MATCH:
            continue
        bwd = _match_len_bwd(A, a, B, b, min(a, b - cursor))
        a -= bwd
        b -= bwd
        ln = fwd + bwd
        litlen = b - cursor
        lits += tgt[cursor:b]
        d = a - last_ref
        ops += _uv(litlen) + _uv(ln) + _uv((d << 1) ^ (d >> 63) if d < 0 else (d << 1))
        n_ops += 1
        last_ref = a + ln
        cursor = b + ln
    lits += tgt[cursor:]
    tail = len(tgt) - cursor
    header = _uv(len(tgt)) + _uv(n_ops) + _uv(tail) + _uv(len(ops))
    return b"\x01" + header + bytes(ops) + bytes(lits)


def _pack_lz(tgt):
    """Degenerate delta: everything is a literal."""
    return b"\x01" + _uv(len(tgt)) + _uv(0) + _uv(len(tgt)) + _uv(0) + bytes(tgt)


def lz_delta_decode(ref: bytes, blob: bytes) -> bytes:
    assert blob[:1] == b"\x01"
    r = _R(blob)
    r.i = 1
    out_len = r.uv()
    n_ops = r.uv()
    tail = r.uv()
    ops_len = r.uv()
    ops = _R(blob)
    ops.i = r.i
    lit_off = r.i + ops_len
    out = bytearray()
    last_ref = 0
    for _ in range(n_ops):
        litlen = ops.uv()
        ln = ops.uv()
        z = ops.uv()
        d = (z >> 1) ^ -(z & 1)
        if litlen:
            out += blob[lit_off:lit_off + litlen]
            lit_off += litlen
        a = last_ref + d
        out += ref[a:a + ln]
        last_ref = a + ln
    if tail:
        out += blob[lit_off:lit_off + tail]
    assert len(out) == out_len, (len(out), out_len)
    return bytes(out)


def _window_log_for(nbytes: int) -> int:
    wl = 17
    while (1 << wl) < nbytes and wl < 31:
        wl += 1
    return wl


def delta_encode(ref: bytes, tgt: bytes, level: int = 19) -> bytes:
    """Compress `tgt` with `ref` as a raw-content preset dictionary.

    This is an LZ match search of the target against the reference plus the
    target's own history, in one pass - the cheap end of the bsdiff family, and
    for near-duplicates it lands within a few percent of a suffix-array delta
    at a fraction of the cost. The window is widened to span reference plus
    target so a match at the very front of a 40 MB reference is still visible
    from the end of the target."""
    if zstd is None:
        raise RuntimeError("zstandard required for WEAVE deltas")
    d = zstd.ZstdCompressionDict(ref, dict_type=zstd.DICT_TYPE_RAWCONTENT)
    params = zstd.ZstdCompressionParameters.from_level(
        level, window_log=_window_log_for(len(ref) + len(tgt)), enable_ldm=1)
    c = zstd.ZstdCompressor(compression_params=params, dict_data=d)
    return c.compress(tgt)


def delta_decode(ref: bytes, blob: bytes, out_len: int) -> bytes:
    if blob[:1] == b"\x01":
        return lz_delta_decode(ref, blob)
    d = zstd.ZstdCompressionDict(ref, dict_type=zstd.DICT_TYPE_RAWCONTENT)
    dc = zstd.ZstdDecompressor(dict_data=d)
    return dc.decompress(blob[1:], max_output_size=max(out_len, 1))


def encode_delta(ref: bytes, tgt: bytes, mode: int = DELTA_LZ) -> bytes:
    """One tagged delta blob. Byte 0 is the mode, so `delta_decode` needs no
    out-of-band knowledge of which encoder was used."""
    if mode == DELTA_LZ:
        return lz_delta_encode(ref, tgt)
    return b"\x02" + delta_encode(ref, tgt)


def choose_reference(cluster: List[int], g: SimGraph, sizes: Sequence[int],
                     n: int) -> int:
    """The medoid: the member with the greatest total similarity to the rest,
    breaking ties towards the largest file.

    Chaining v1->v2->v3 gives smaller individual deltas on a linear revision
    history, but it serialises reconstruction and makes every file depend on
    every earlier one. A star against the medoid costs a little more and keeps
    each file one hop from a reference; `bench.py` measures both."""
    if len(cluster) == 1:
        return cluster[0]
    w = g.weight("containment")
    tot = {i: 0.0 for i in cluster}
    inset = set(cluster)
    for s, d, x in zip(g.src.tolist(), g.dst.tolist(), w.tolist()):
        if s in inset and d in inset:
            tot[s] += x
            tot[d] += x
    return max(cluster, key=lambda i: (tot[i], sizes[i]))


# ============================================================================
#  5. PLAN, ASSEMBLE, DISASSEMBLE
# ============================================================================

@dataclass
class WeavePlan:
    order: List[int]                       # emission order (indices into inputs)
    ref_of: Dict[int, int]                 # index -> reference index (delta)
    n: int
    mode: int = DELTA_LZ                   # default delta encoder
    mode_of: Dict[int, int] = field(default_factory=dict)   # per-file override
    stats: Dict = field(default_factory=dict)

    @property
    def n_delta(self) -> int:
        return len(self.ref_of)


def _probe(data: bytes, level: int = 3, window: Optional[int] = None) -> int:
    """Cheap stand-in for the real codec, used to compare two candidate
    arrangements of the same bytes.

    `window` is the intended codec's dictionary size. This matters more than
    anything else about the probe: the entire question a delta answers is
    "can the real codec still see the reference from here", and a probe with an
    unlimited window always answers yes and always rejects the delta. Pass the
    real dictionary size and the probe reproduces the real codec's reach."""
    if zstd is None:
        return len(zlib.compress(data, 6))
    nb = window if window else len(data)
    p = zstd.ZstdCompressionParameters.from_level(
        level, window_log=_window_log_for(max(nb, 1024)), enable_ldm=1)
    return len(zstd.ZstdCompressor(compression_params=p).compress(data))


def plan(datas: Sequence[bytes], names: Optional[Sequence[str]] = None,
         groups: Optional[Sequence] = None,
         *,
         do_order: bool = True,
         do_delta: bool = True,
         delta_thresh: float = 0.60,
         cluster_thresh: float = 0.45,
         two_opt_budget: float = 2.0,
         order_strategy: str = "walk",
         verify_order: bool = True,
         verify_delta: bool = True,
         codec_window: Optional[int] = 64 << 20,
         delta_mode: Optional[int] = None,   # None = measure both, keep the winner
         probe_cap: int = 256 << 20,
         sketches: Optional[Sequence[np.ndarray]] = None,
         graph: Optional[SimGraph] = None) -> WeavePlan:
    """Build an ordering + delta plan for a set of payloads.

    Nothing here is destructive: the plan is a permutation and a map, and
    `assemble` is the only thing that touches bytes.

    `codec_window` is the dictionary size of the codec that will actually
    compress the result - 64 MB for Crush's LZMA2. Every accept/reject decision
    below is made through a probe limited to that window, because whether a
    delta earns its place is entirely a question of whether the real codec
    could have found the same redundancy by itself.

    `verify_order` and `verify_delta` are the never-lose guarantees: each
    proposal is probed against the arrangement it would replace and dropped if
    it does not win."""
    n = len(datas)
    st: Dict = {"n": n}
    t0 = time.time()
    if names is None:
        names = [str(i) for i in range(n)]
    sizes = [len(d) for d in datas]

    if sketches is None:
        sketches = sketch_many(datas)
    st["sketch_s"] = time.time() - t0

    t1 = time.time()
    g = graph if graph is not None else build_graph(sketches)
    st["graph_s"] = time.time() - t1
    st["edges"] = int(g.src.size)

    base_order = list(range(n))
    t2 = time.time()
    if do_order and n > 2:
        new_order = order_files(sketches, names, g=g, groups=groups,
                                two_opt_budget=two_opt_budget,
                                cluster_thresh=cluster_thresh,
                                strategy=order_strategy)
    else:
        new_order = base_order
    st["order_s"] = time.time() - t2
    st["adj_sim_before"] = adjacent_similarity(g, base_order)
    st["adj_sim_after"] = adjacent_similarity(g, new_order)

    # ---- never-lose check on the ordering -------------------------------
    chosen = new_order
    st["order_used"] = do_order
    total = sum(sizes)
    if do_order and verify_order and new_order != base_order and total <= probe_cap:
        t3 = time.time()
        a = _probe(b"".join(datas[i] for i in base_order), window=codec_window)
        b_ = _probe(b"".join(datas[i] for i in new_order), window=codec_window)
        st["probe_s"] = time.time() - t3
        st["probe_before"], st["probe_after"] = a, b_
        if b_ >= a:
            chosen = base_order
            st["order_used"] = False

    # ---- delta ----------------------------------------------------------
    #
    # The comparison that matters is NOT "is the delta smaller than compressing
    # this file alone" - it always is. It is "is the delta smaller than what the
    # real codec gets from having the reference sitting a few kilobytes earlier
    # in the same solid stream", which is a completely different and much
    # harder bar. Getting this wrong is not a small mistake: an earlier version
    # of this file scored deltas against standalone compression and made a set
    # of 90 documents from one template 54% BIGGER, because it replaced 90
    # mutually-compressible files with 90 opaque blobs.
    #
    # So each cluster is probed both ways, and only clusters that win are kept.
    ref_of: Dict[int, int] = {}
    mode_of: Dict[int, int] = {}
    if do_delta and n > 1:
        t4 = time.time()
        clusters = [c for c in connected_clusters(g, delta_thresh, "jaccard")
                    if len(c) > 1]
        st["delta_clusters"] = len(clusters)
        st["delta_cluster_sizes"] = sorted((len(c) for c in clusters), reverse=True)[:8]
        pos = {v: i for i, v in enumerate(chosen)}
        mode_of: Dict[int, int] = {}
        kept, tried = 0, 0
        for c in clusters:
            ref = choose_reference(c, g, sizes, n)
            seq = [i for i in sorted(c, key=lambda x: pos[x]) if pos[i] > pos[ref]]
            if not seq:
                continue
            tried += 1
            modes = (DELTA_LZ, DELTA_ZSTD) if delta_mode is None else (delta_mode,)
            solid = _probe(datas[ref] + b"".join(datas[i] for i in seq),
                           window=codec_window)
            best_mode, best_sz = None, solid
            for m in modes:
                if m == DELTA_ZSTD and zstd is None:
                    continue
                cand = _probe(datas[ref] + b"".join(
                    encode_delta(datas[ref], datas[i], m) for i in seq),
                    window=codec_window)
                if cand < best_sz:
                    best_mode, best_sz = m, cand
            st.setdefault("cluster_probe", []).append(
                (len(seq), solid, best_sz, best_mode))
            if best_mode is None and verify_delta:
                continue
            chosen_mode = best_mode if best_mode is not None else modes[0]
            for i in seq:
                ref_of[i] = ref
                mode_of[i] = chosen_mode
            kept += 1
        st["delta_clusters_kept"] = kept
        st["delta_s"] = time.time() - t4
    st["n_delta"] = len(ref_of)

    # ---- final whole-set never-lose gate --------------------------------
    if ref_of and verify_delta and total <= probe_cap:
        t5 = time.time()
        pl = WeavePlan(order=chosen, ref_of=ref_of, n=n, mode_of=mode_of)
        blob_d, man_d = assemble(datas, pl)
        a = _probe(b"".join(datas[i] for i in chosen), window=codec_window)
        b_ = _probe(blob_d, window=codec_window) + len(man_d)
        st["final_probe"] = (a, b_)
        st["final_probe_s"] = time.time() - t5
        if b_ >= a:
            ref_of, mode_of = {}, {}
            st["n_delta"] = 0
            st["delta_rejected"] = True
    st["total_s"] = time.time() - t0

    return WeavePlan(order=chosen, ref_of=ref_of, n=n, mode_of=mode_of, stats=st)


# ---------------------------------------------------------------------------
#  container: a single solid blob + a compact manifest
# ---------------------------------------------------------------------------

def _uv(x: int) -> bytes:
    out = bytearray()
    while True:
        b = x & 0x7F
        x >>= 7
        out.append(b | (0x80 if x else 0))
        if not x:
            return bytes(out)


class _R:
    __slots__ = ("b", "i")

    def __init__(self, b):
        self.b = b
        self.i = 0

    def uv(self):
        r = s = 0
        while True:
            c = self.b[self.i]
            self.i += 1
            r |= (c & 0x7F) << s
            if not (c & 0x80):
                return r
            s += 7


def assemble(datas: Sequence[bytes], p: WeavePlan) -> Tuple[bytes, bytes]:
    """Lay the payloads out in plan order, substituting deltas.

    Returns (blob, manifest). The manifest is a few bytes per file: the
    original index, the stored length, and - for a delta - which earlier file
    it is against. It is meant to be compressed alongside the archive index."""
    order = p.order
    seen = set()
    parts, man = [], bytearray()
    man += _uv(len(order))
    for i in order:
        ref = p.ref_of.get(i, -1)
        if ref >= 0 and ref not in seen:
            ref = -1                       # reference not yet emitted: store raw
        if ref >= 0:
            blob = encode_delta(datas[ref], datas[i], p.mode_of.get(i, p.mode))
            man += _uv(i) + _uv(1) + _uv(ref) + _uv(len(blob)) + _uv(len(datas[i]))
        else:
            blob = datas[i]
            man += _uv(i) + _uv(0) + _uv(len(blob))
        parts.append(blob)
        seen.add(i)
    return b"".join(parts), bytes(man)


def disassemble(blob: bytes, manifest: bytes) -> List[bytes]:
    """Rebuild every payload, in ORIGINAL index order."""
    r = _R(manifest)
    cnt = r.uv()
    out: Dict[int, bytes] = {}
    off = 0
    for _ in range(cnt):
        i = r.uv()
        kind = r.uv()
        if kind == 1:
            ref = r.uv()
            ln = r.uv()
            orig = r.uv()
            out[i] = delta_decode(out[ref], blob[off:off + ln], orig)
            off += ln
        else:
            ln = r.uv()
            out[i] = blob[off:off + ln]
            off += ln
    return [out[i] for i in range(cnt)]


# ---------------------------------------------------------------------------
#  convenience: the whole thing on a list of paths
# ---------------------------------------------------------------------------

def weave_paths(paths: Sequence[str], **kw) -> Tuple[bytes, bytes, WeavePlan]:
    datas = [open(p, "rb").read() for p in paths]
    p = plan(datas, [os.path.basename(x) for x in paths], **kw)
    blob, man = assemble(datas, p)
    return blob, man, p


if __name__ == "__main__":     # tiny self-test
    import random
    rnd = random.Random(1)
    base = bytes(rnd.getrandbits(8) for _ in range(200000))
    files = [base]
    for k in range(6):
        b = bytearray(base)
        for _ in range(40):
            q = rnd.randrange(len(b) - 10)
            b[q:q + 10] = bytes(rnd.getrandbits(8) for _ in range(10))
        files.append(bytes(b))
    files += [bytes(rnd.getrandbits(8) for _ in range(50000)) for _ in range(4)]
    rnd.shuffle(files)
    pl = plan(files, [f"f{i}" for i in range(len(files))])
    blob, man = assemble(files, pl)
    back = disassemble(blob, man)
    assert back == files, "ROUNDTRIP FAILED"
    print("self-test ok:", pl.stats)
    print("blob", len(blob), "manifest", len(man), "orig", sum(map(len, files)))
