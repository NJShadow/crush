"""QR helpers: capacity, matrix generation (segno, alphanumeric mode), rendering, decoding (zbar)."""
import functools
import numpy as np
import segno
from PIL import Image

from .core import OVERHEAD, b45encode, b45decode

# number of data codewords per (version, ecl) - computed from segno by probing,
# and checked against the JS encoder (qrcodegen) in tests.


@functools.lru_cache(None)
def alnum_capacity(version, ecl):
    lo, hi = 1, 4296
    while lo < hi:
        mid = (lo + hi + 1) // 2
        try:
            segno.make("A" * mid, mode="alphanumeric", version=version, error=ecl, boost_error=False)
            lo = mid
        except Exception:
            hi = mid - 1
    return lo


def max_packet_bytes(version, ecl):
    c = alnum_capacity(version, ecl)
    return (c // 3) * 2 + (1 if c % 3 == 2 else 0)


def symbol_size(version, ecl):
    """T such that one packet (T + 22 bytes header/CRC) fills the QR code."""
    return max_packet_bytes(version, ecl) - OVERHEAD


def qr_matrix(packet, version, ecl):
    q = segno.make(b45encode(packet), mode="alphanumeric", version=version, error=ecl, boost_error=False)
    return np.array([[bool(v) for v in row] for row in q.matrix], dtype=bool)


def render(mat, module_px=4, quiet=4, invert=False):
    m = np.pad(mat, quiet, constant_values=False)
    img = np.where(m, 0, 255).astype(np.uint8)
    if invert:
        img = 255 - img
    img = np.kron(img, np.ones((module_px, module_px), dtype=np.uint8))
    return Image.fromarray(img, "L")


def zbar_decode(img):
    """Return list of packets (bytes) found by zbar in a PIL image."""
    from pyzbar import pyzbar
    out = []
    for r in pyzbar.decode(img, symbols=[pyzbar.ZBarSymbol.QRCODE]):
        try:
            s = r.data.decode("ascii")
        except UnicodeDecodeError:
            continue
        b = b45decode(s)
        if b is not None:
            out.append(b)
    return out
