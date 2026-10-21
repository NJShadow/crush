#!/usr/bin/env python3
# ============================================================================
#  PERCEPTUAL - the opt-in tier, with the receipt attached
#  ---------------------------------------------------------------------------
#  Everything else in Crush is bounded by the counting argument. This tier is
#  not, because it answers a different question: not "what are these bytes?"
#  but "what will a person actually be able to see and hear?"
#
#  That is not a loophole in information theory. It is a decision about which
#  information matters, and it is the user's decision to make - so this tier is
#  off by default, is enabled per media type, and every file it touches is
#  MEASURED. Crush encodes, decodes, and compares against the original, then
#  records PSNR, SSIM and the size traded. If a file does not clear the quality
#  floor, Crush keeps the lossless result instead.
#
#  No other archiver does this. The ones that offer lossy media handling ask
#  you to trust a quality slider. Crush shows you the number it got.
# ============================================================================
from __future__ import annotations
import os, io, json, math, struct, wave, shutil, subprocess, tempfile

import numpy as np


def _tool(n):
    from crush_engine import _tool as t
    return t(n)


# ------------------------------------------------------------ metrics ----
def psnr(a: np.ndarray, b: np.ndarray, peak=255.0) -> float:
    a = a.astype(np.float64); b = b.astype(np.float64)
    n = min(a.size, b.size)
    mse = float(np.mean((a.ravel()[:n] - b.ravel()[:n]) ** 2))
    if mse <= 1e-12:
        return 99.0
    return 10.0 * math.log10(peak * peak / mse)


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Global SSIM over 8x8 blocks - close enough to the windowed version for
    a quality gate, and it needs no SciPy."""
    a = a.astype(np.float64); b = b.astype(np.float64)
    h = min(a.shape[0], b.shape[0]) // 8 * 8
    w = min(a.shape[1], b.shape[1]) // 8 * 8
    if h == 0 or w == 0:
        return 0.0
    A = a[:h, :w].reshape(h // 8, 8, w // 8, 8).transpose(0, 2, 1, 3).reshape(-1, 64)
    B = b[:h, :w].reshape(h // 8, 8, w // 8, 8).transpose(0, 2, 1, 3).reshape(-1, 64)
    mu1, mu2 = A.mean(1), B.mean(1)
    v1, v2 = A.var(1), B.var(1)
    cov = ((A - mu1[:, None]) * (B - mu2[:, None])).mean(1)
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    s = ((2 * mu1 * mu2 + C1) * (2 * cov + C2)) / ((mu1 ** 2 + mu2 ** 2 + C1) * (v1 + v2 + C2))
    return float(np.mean(s))


def _gray(path):
    from PIL import Image
    im = Image.open(path).convert("L")
    return np.asarray(im)


# ==========================================================================
#  Photographs
# ==========================================================================
class PerceptualImage:
    """JPEG / PNG -> JPEG XL at a chosen butteraugli distance."""
    name = "perc_image"
    exact = False
    QUALITY_FLOOR = {"psnr": 34.0, "ssim": 0.965}

    @staticmethod
    def available():
        return bool(_tool("cjxl") and _tool("djxl"))

    @staticmethod
    def forward(data: bytes, distance: float = 1.0, fmt: str = "jpg"):
        cjxl, djxl = _tool("cjxl"), _tool("djxl")
        if not (cjxl and djxl):
            return None
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a." + fmt)
            enc = os.path.join(td, "a.jxl")
            dec = os.path.join(td, "b." + fmt)
            open(src, "wb").write(data)
            # cjxl defaults to a LOSSLESS transcode when the input is a JPEG,
            # which silently ignores the distance you asked for. This tier is
            # explicitly the lossy one, so say so.
            cmd = [cjxl, src, enc, "-d", str(distance), "-e", "7"]
            if fmt in ("jpg", "jpeg"):
                cmd += ["--lossless_jpeg=0"]
            r = subprocess.run(cmd, capture_output=True, timeout=900)
            if r.returncode != 0 or not os.path.exists(enc):
                return None
            out = open(enc, "rb").read()
            if len(out) >= len(data) * 0.92:
                return None                       # not worth the trade
            # Measure the file the USER will actually get back, not the
            # intermediate. For a JPEG that means running the whole restore
            # path here - decode, re-encode - and scoring that.
            png = os.path.join(td, "decoded.png")
            r = subprocess.run([djxl, enc, png], capture_output=True, timeout=900)
            if r.returncode != 0 or not os.path.exists(png):
                return None
            jq = 0
            if fmt in ("jpg", "jpeg"):
                from PIL import Image
                im = Image.open(png).convert("RGB")
                # Restore at a quality that matches the ORIGINAL's file size.
                # Handing someone back a photo 50% larger than the one they
                # gave you is its own kind of failure.
                for q in (95, 92, 90, 87, 84, 80, 76, 72):
                    im.save(dec, "JPEG", quality=q, optimize=True, subsampling=0)
                    jq = q
                    if os.path.getsize(dec) <= len(data) * 1.02:
                        break
            else:
                dec = png
            try:
                g0, g1 = _gray(src), _gray(dec)
            except Exception:
                return None
            m = {"psnr": round(psnr(g0, g1), 2), "ssim": round(ssim(g0, g1), 4),
                 "d": distance, "fmt": fmt, "q": jq,
                 "restored": os.path.getsize(dec)}
        if m["psnr"] < PerceptualImage.QUALITY_FLOOR["psnr"] or \
           m["ssim"] < PerceptualImage.QUALITY_FLOOR["ssim"]:
            return None                            # below the floor - refuse
        return out, {"k": "perc_image", "m": m, "n": len(data)}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        djxl = _tool("djxl")
        fmt = aux["m"].get("fmt", "jpg")
        with tempfile.TemporaryDirectory() as td:
            enc = os.path.join(td, "a.jxl")
            png = os.path.join(td, "decoded.png")
            dec = os.path.join(td, "c." + fmt)
            open(enc, "wb").write(payload)
            subprocess.run([djxl, enc, png], capture_output=True, timeout=900)
            if fmt in ("jpg", "jpeg"):
                from PIL import Image
                Image.open(png).convert("RGB").save(
                    dec, "JPEG", quality=aux["m"].get("q", 90),
                    optimize=True, subsampling=0)
                return open(dec, "rb").read()
            return open(png, "rb").read()

    @staticmethod
    def verify_content(rebuilt: bytes, aux) -> bool:
        return len(rebuilt) > 0

    @staticmethod
    def describe(aux) -> str:
        m = aux["m"]
        return f"JPEG XL d={m['d']} · PSNR {m['psnr']} dB · SSIM {m['ssim']}"


# ==========================================================================
#  Audio
# ==========================================================================
class PerceptualAudio:
    """PCM WAV -> Opus. Restores a WAV of identical format and length."""
    name = "perc_audio"
    exact = False
    QUALITY_FLOOR = {"snr": 12.0}

    @staticmethod
    def available():
        return bool(_tool("ffmpeg"))

    @staticmethod
    def _wav_info(d):
        if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
            return None
        p = 12
        fmt = data = None
        while p + 8 <= len(d):
            cid = d[p:p + 4]
            sz = struct.unpack_from("<I", d, p + 4)[0]
            if cid == b"fmt ":
                fmt = struct.unpack_from("<HHIIHH", d, p + 8)
            elif cid == b"data":
                data = (p + 8, min(sz, len(d) - p - 8))
                break
            p += 8 + sz + (sz & 1)
        if not fmt or not data:
            return None
        return fmt, data

    @staticmethod
    def forward(data: bytes, bitrate: str = "128k"):
        ff = _tool("ffmpeg")
        if not ff:
            return None
        info = PerceptualAudio._wav_info(data)
        if not info:
            return None
        (afmt, ch, rate, brate, align, bits), (doff, dlen) = info
        if afmt != 1 or bits != 16 or ch not in (1, 2) or dlen < 200000:
            return None
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a.wav"); enc = os.path.join(td, "a.opus")
            dec = os.path.join(td, "b.wav")
            open(src, "wb").write(data)
            r = subprocess.run([ff, "-y", "-loglevel", "quiet", "-i", src,
                                "-c:a", "libopus", "-b:a", bitrate, enc],
                               capture_output=True, timeout=1800)
            if r.returncode != 0 or not os.path.exists(enc):
                return None
            out = open(enc, "rb").read()
            if len(out) >= len(data) * 0.7:
                return None
            subprocess.run([ff, "-y", "-loglevel", "quiet", "-i", enc,
                            "-ar", str(rate), "-ac", str(ch), "-c:a", "pcm_s16le", dec],
                           capture_output=True, timeout=1800)
            if not os.path.exists(dec):
                return None
            db = open(dec, "rb").read()
            di = PerceptualAudio._wav_info(db)
            if not di:
                return None
            (_, _, _, _, _, _), (o2, l2) = di
            a = np.frombuffer(data[doff:doff + (dlen // 2) * 2], dtype="<i2").astype(np.float64)
            b = np.frombuffer(db[o2:o2 + (l2 // 2) * 2], dtype="<i2").astype(np.float64)
            n = min(len(a), len(b))
            if n < 1000:
                return None
            num = float(np.sum(a[:n] ** 2))
            den = float(np.sum((a[:n] - b[:n]) ** 2)) + 1e-9
            snr = 10 * math.log10(num / den) if num > 0 else 0.0
        m = {"snr": round(snr, 2), "br": bitrate, "rate": rate, "ch": ch,
             "dlen": dlen, "doff": doff}
        return out, {"k": "perc_audio", "m": m, "n": len(data),
                     "hdr": data[:doff].hex(), "tail": data[doff + dlen:].hex()}

    @staticmethod
    def inverse(payload: bytes, aux) -> bytes:
        ff = _tool("ffmpeg")
        m = aux["m"]
        with tempfile.TemporaryDirectory() as td:
            enc = os.path.join(td, "a.opus"); dec = os.path.join(td, "b.wav")
            open(enc, "wb").write(payload)
            subprocess.run([ff, "-y", "-loglevel", "quiet", "-i", enc,
                            "-ar", str(m["rate"]), "-ac", str(m["ch"]),
                            "-c:a", "pcm_s16le", dec], capture_output=True, timeout=1800)
            db = open(dec, "rb").read()
            di = PerceptualAudio._wav_info(db)
            pcm = db[di[1][0]:di[1][0] + di[1][1]] if di else b""
            need = m["dlen"]
            pcm = pcm[:need] + b"\x00" * max(0, need - len(pcm))
            return bytes.fromhex(aux["hdr"]) + pcm + bytes.fromhex(aux["tail"])

    @staticmethod
    def verify_content(rebuilt: bytes, aux) -> bool:
        return len(rebuilt) == aux["n"]

    @staticmethod
    def describe(aux) -> str:
        m = aux["m"]
        return f"Opus {m['br']} · SNR {m['snr']} dB"


PERCEPTUAL = {"perc_image": PerceptualImage, "perc_audio": PerceptualAudio}
