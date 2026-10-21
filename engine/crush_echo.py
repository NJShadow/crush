#!/usr/bin/env python3
# ============================================================================
#  ECHO - content-defined chunking and cross-file deduplication
#  ---------------------------------------------------------------------------
#  Every mainstream archiver has the same blind spot: its idea of "have I seen
#  this before?" is bounded by a sliding window. 7-Zip's is 64 MB by default.
#  Beyond that distance, two identical megabytes are compressed twice.
#
#  Real folders are full of exactly that. Report_v1 through Report_v7. A photo
#  library where the same image sits in two albums. A backup taken on Monday
#  and again on Friday. A source tree with a vendored copy of a dependency. In
#  every case the second copy costs almost nothing to store - if the archiver
#  can see across the whole input rather than through a window.
#
#  Echo splits every file at boundaries determined by CONTENT, not by position,
#  using a rolling hash over an 8-byte window. That is what makes it survive
#  insertion: add a paragraph to page one of a document and a fixed-block
#  scheme re-aligns every block after it, while content-defined boundaries snap
#  straight back and only the one changed chunk is new.
#
#  Chunks are then keyed by BLAKE2b and stored once for the entire archive, at
#  unlimited distance. A 400 MB folder of document revisions collapses to the
#  size of one revision plus the edits.
# ============================================================================
from __future__ import annotations
import hashlib
import numpy as np

# fixed gear table - part of the format, must never change
_rng = np.random.RandomState(0x5EED)
GEAR = _rng.randint(0, 1 << 62, size=256, dtype=np.int64).astype(np.uint64)

TAPS = 8
MIN_CHUNK = 2 << 10
AVG_BITS = 13                  # ~8 KB average
MAX_CHUNK = 64 << 10
MASK = np.uint64((1 << AVG_BITS) - 1)
_SLICE = 4 << 20


def boundaries(data: bytes):
    """Content-defined cut points via an 8-tap rolling gear hash.

    The hash at position i mixes the previous 8 bytes, each through its own
    multiplier, so it is computable as eight vectorised shifts instead of a
    byte-at-a-time loop - which is the difference between 4 seconds and 4
    minutes on a large file."""
    n = len(data)
    if n <= MIN_CHUNK * 2:
        return [(0, n)]
    # In 4 MiB slices: the whole-input version held three uint64 arrays of the
    # input's length at once - 24 bytes per byte, 4.8 GB for one 200 MB bundle,
    # which is an out-of-memory kill. The hash at i reads only bytes i-7..i, so
    # a 7-byte overlap gives exactly the same hits (and the decoder never sees
    # cut points anyway: it reads lengths and ids).
    a = np.frombuffer(data, dtype=np.uint8)
    parts = []
    for s0 in range(0, n, _SLICE):
        lo = max(0, s0 - (TAPS - 1))
        g = GEAR[a[lo:min(n, s0 + _SLICE)]]
        h = g.copy()
        for k in range(1, TAPS):
            h[k:] ^= np.left_shift(g[:-k], np.uint64(k * 3))
        hit = np.flatnonzero((h & MASK) == 0) + lo
        parts.append(hit[hit >= s0])
        del g, h
    hits = np.concatenate(parts)

    cuts = []
    last = 0
    for pos in hits:
        p = int(pos)
        if p - last < MIN_CHUNK:
            continue
        cuts.append((last, p))
        last = p
    # enforce the maximum
    out = []
    for (s, e) in cuts:
        while e - s > MAX_CHUNK:
            out.append((s, s + MAX_CHUNK)); s += MAX_CHUNK
        if e > s:
            out.append((s, e))
    s = last
    while n - s > MAX_CHUNK:
        out.append((s, s + MAX_CHUNK)); s += MAX_CHUNK
    if n > s:
        out.append((s, n))
    return out


class ChunkStore:
    """Global, whole-archive chunk table."""

    def __init__(self):
        self.index = {}            # digest -> ordinal
        self.blobs = []            # unique chunk bytes, in first-seen order
        self.total_in = 0
        self.total_unique = 0

    def add(self, data: bytes):
        """Returns the chunk-id list for this payload."""
        ids = []
        for (s, e) in boundaries(data):
            piece = data[s:e]
            d = hashlib.blake2b(piece, digest_size=16).digest()
            self.total_in += len(piece)
            j = self.index.get(d)
            if j is None:
                j = len(self.blobs)
                self.index[d] = j
                self.blobs.append(piece)
                self.total_unique += len(piece)
            ids.append(j)
        return ids

    @property
    def saved(self):
        return self.total_in - self.total_unique


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


def pack_header(blob_lens, ids) -> bytes:
    """Chunk table: the length of each unique chunk, then the id sequence that
    rebuilds the original stream from them."""
    out = bytearray(_uv(len(blob_lens)))
    for L in blob_lens:
        out += _uv(L)
    out += _uv(len(ids))
    out += encode_ids(ids)
    return bytes(out)


def unpack_header(buf: bytes):
    r = _R(buf)
    nu = r.uv()
    lens = [r.uv() for _ in range(nu)]
    ni = r.uv()
    return lens, decode_ids(buf[r.i:], ni)


def encode_ids(ids) -> bytes:
    """Delta+varint. Runs of consecutive ids - which is what an unchanged
    region looks like - cost about one byte each."""
    out = bytearray()
    prev = 0
    for i in ids:
        d = i - prev
        z = (d << 1) ^ (d >> 63) if d < 0 else (d << 1)
        while True:
            b = z & 0x7F
            z >>= 7
            out.append(b | (0x80 if z else 0))
            if not z:
                break
        prev = i
    return bytes(out)


def decode_ids(buf: bytes, count: int):
    ids = []
    i = 0
    prev = 0
    for _ in range(count):
        r = s = 0
        while True:
            c = buf[i]; i += 1
            r |= (c & 0x7F) << s
            if not (c & 0x80):
                break
            s += 7
        d = (r >> 1) ^ -(r & 1)
        prev += d
        ids.append(prev)
    return ids
