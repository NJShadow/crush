"""Audio modem: Crush archive -> WAV that survives speaker -> air -> microphone.

    python3 -m crushchan.audio encode archive.crush out.wav [--repair 0.5] [--mode fast|robust]
    python3 -m crushchan.audio decode in.wav out.crush

Physical layer (one audio frame = one fountain packet of AUDIO_T bytes):
  [chirp 4096 smp 800->7000 Hz] [256 silence] [reference OFDM symbol] [data symbols] [1024 silence]
  OFDM: 48 kHz, FFT 1024 (46.875 Hz bins), cyclic prefix 384 (8 ms, for room echoes),
  96 carriers on bins 32..127 (1.5-6.0 kHz). Differential PSK *in time* per carrier
  (fast: DQPSK, robust: DBPSK), so no channel estimate is needed and slow clock drift
  only adds a tiny per-symbol phase step. Convolutional code K=7 r=1/2 (171,133) with soft
  Viterbi, multiplicative bit interleaver over the whole frame. Every frame carries one
  CRC-32-protected fountain packet; lost/corrupt frames are just erasures, and the
  archive is complete as soon as enough frames arrive (SHA-256 verified).
The decode path uses floating point, but its output only ever reaches the user through
the CRC-32 per packet and the SHA-256 of the whole archive: float differences can make
a frame fail, never make wrong bytes pass.
"""
import argparse, hashlib, math, sys, wave
import numpy as np
from .core import fountain_packet, layout, Receiver, mix32, OVERHEAD

FS = 48000
CHIRP_N = 4096
GAP0 = 256
GAP1 = 1024
AUDIO_T = 200
PKT_BITS = (AUDIO_T + OVERHEAD) * 8
# bits per carrier, FFT size, cyclic prefix, first bin, carriers (all bands ~1.5-6 kHz)
MODES = {
    "fast":   dict(bps=2, nfft=1024, cp=384,  k0=32,  nc=96),    # DQPSK, 29 ms symbols, 8 ms CP
    "robust": dict(bps=1, nfft=2048, cp=1024, k0=64,  nc=192),   # DBPSK, 64 ms symbols, 21 ms CP
    "echo":   dict(bps=1, nfft=4096, cp=2048, k0=128, nc=384),   # DBPSK, 128 ms symbols, 43 ms CP
}


def chirp():
    t = np.arange(CHIRP_N) / FS
    f0, f1 = 800.0, 7000.0
    T = CHIRP_N / FS
    ph = 2 * np.pi * (f0 * t + (f1 - f0) / (2 * T) * t * t)
    w = np.ones(CHIRP_N)
    r = 256
    w[:r] = 0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)
    w[-r:] = w[:r][::-1]
    return np.sin(ph) * w


CHIRP = chirp()


def frame_geometry(mode):
    m = MODES[mode]
    coded = 2 * (PKT_BITS + 6)
    nsym = math.ceil(coded / (m["nc"] * m["bps"]))
    L = nsym * m["nc"] * m["bps"]
    flen = CHIRP_N + GAP0 + (nsym + 1) * (m["nfft"] + m["cp"]) + GAP1
    return nsym, L, flen


def ref_phases(nc):
    return np.array([(mix32(0xA0D10 + k) & 3) * (np.pi / 2) for k in range(nc)])


def interleave_perm(L):
    # multiplicative permutation; P coprime to L
    P = 1735
    while math.gcd(P, L) != 1:
        P += 2
    return (np.arange(L) * P) % L


# ------------------------------------------------------------- convolutional code
G = (0o171, 0o133)


def conv_encode(bits):
    s = 0
    out = []
    for b in list(bits) + [0] * 6:
        s = ((s << 1) | b) & 0x7F
        out.append(bin(s & G[0]).count("1") & 1)
        out.append(bin(s & G[1]).count("1") & 1)
    return out


_NS = 64
_prev = np.zeros((_NS, 2), dtype=np.int64)
_outb = np.zeros((_NS, 2, 2), dtype=np.float64)
for _ns in range(_NS):
    for _j, _b0 in enumerate((0, 1)):
        # next state ns = ((ps << 1) | bit) & 63, bit = ns & 1 ; ps has top bit _b0
        _ps = (_ns >> 1) | (_b0 << 5)
        _prev[_ns, _j] = _ps
        _full = ((_ps << 1) | (_ns & 1)) & 0x7F
        _outb[_ns, _j, 0] = 1 - 2 * (bin(_full & G[0]).count("1") & 1)
        _outb[_ns, _j, 1] = 1 - 2 * (bin(_full & G[1]).count("1") & 1)


def viterbi(soft, nbits):
    """soft: array of 2*(nbits+6) values, positive = bit 0. Returns nbits bits."""
    n = nbits + 6
    soft = np.asarray(soft[:2 * n], dtype=np.float64).reshape(n, 2)
    metric = np.full(_NS, -1e18)
    metric[0] = 0.0
    dec = np.zeros((n, _NS), dtype=np.int8)
    for t in range(n):
        cand = metric[_prev] + _outb[:, :, 0] * soft[t, 0] + _outb[:, :, 1] * soft[t, 1]
        j = np.argmax(cand, axis=1)
        dec[t] = j
        metric = cand[np.arange(_NS), j]
    s = 0  # tail forces state 0
    bits = np.zeros(n, dtype=np.uint8)
    for t in range(n - 1, -1, -1):
        bits[t] = s & 1
        s = _prev[s, dec[t, s]]
    return bits[:nbits]


# ------------------------------------------------------------- modulator
def bytes_to_bits(b):
    return np.unpackbits(np.frombuffer(b, dtype=np.uint8)).tolist()


def ofdm_symbol(phases, m):
    X = np.zeros(m["nfft"] // 2 + 1, dtype=complex)
    X[m["k0"]:m["k0"] + m["nc"]] = np.exp(1j * phases)
    x = np.fft.irfft(X, m["nfft"])
    return np.concatenate([x[-m["cp"]:], x])


def modulate_packet(pkt, mode="fast"):
    m = MODES[mode]; bps = m["bps"]; NC = m["nc"]
    nsym, L, flen = frame_geometry(mode)
    coded = conv_encode(bytes_to_bits(pkt))
    coded += [0] * (L - len(coded))
    perm = interleave_perm(L)
    inter = np.zeros(L, dtype=np.uint8)
    inter[perm] = coded            # coded bit i goes to slot perm[i]
    ph = ref_phases(NC).copy()
    syms = [ofdm_symbol(ph, m)]
    for s in range(nsym):
        blk = inter[s * NC * bps:(s + 1) * NC * bps]
        if bps == 2:
            b0, b1 = blk[0::2], blk[1::2]
            inc = np.select([(b0 == 0) & (b1 == 0), (b0 == 1) & (b1 == 0), (b0 == 1) & (b1 == 1)], [0, 1, 2], 3) * (np.pi / 2)
        else:
            inc = blk * np.pi
        ph = ph + inc
        syms.append(ofdm_symbol(ph, m))
    body = np.concatenate(syms)
    body = body / (np.sqrt(np.mean(body ** 2)) + 1e-12) * 0.18
    body = np.clip(body, -0.85, 0.85)         # OFDM peaks: hard limit at ~13 dB PAPR
    return np.concatenate([CHIRP * 0.5, np.zeros(GAP0), body, np.zeros(GAP1)])


def encode(data, repair=0.5, mode="fast", lead=0.4):
    """Archive -> float32 signal at 48 kHz. Sends ceil(ktot*(1+repair))+2 frames:
    all source packets then repair packets (fountain), so ~repair fraction of frames can be lost."""
    ktot, B, kb = layout(len(data), AUDIO_T)
    nframes = math.ceil(ktot * (1 + repair)) + 2
    parts = [np.zeros(int(lead * FS))]
    for n in range(nframes):
        parts.append(modulate_packet(fountain_packet(data, AUDIO_T, n % B, n // B), mode))
    parts.append(np.zeros(int(lead * FS)))
    return np.concatenate(parts).astype(np.float32), nframes


def write_wav(path, x, fs=FS):
    y = np.clip(x, -1, 1)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(fs)
        w.writeframes((y * 32767).astype("<i2").tobytes())


def read_wav(path):
    with wave.open(path, "rb") as w:
        fs, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648
    elif sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
    else:
        raise ValueError("unsupported sample width")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, fs


def to_48k(x, fs):
    if fs == FS:
        return x
    from scipy.signal import resample_poly
    g = math.gcd(int(fs), FS)
    return resample_poly(x, FS // g, int(fs) // g)


# ------------------------------------------------------------- demodulator
def find_chirps(x, flen, thresh=0.35):
    """Normalised matched-filter peaks. Returns sample indexes where chirps start."""
    from scipy.signal import fftconvolve
    c = CHIRP[::-1]
    corr = fftconvolve(x, c, mode="valid")
    e = np.sqrt(np.maximum(fftconvolve(x * x, np.ones(CHIRP_N), mode="valid"), 0) * np.sum(CHIRP ** 2)) + 1e-9
    nc = corr / e
    peaks = []
    nxt = 0
    for i in np.nonzero(nc > thresh)[0]:
        if i < nxt:
            continue
        j = i + int(np.argmax(nc[i:i + CHIRP_N]))
        peaks.append(j)
        nxt = j + flen - CHIRP_N // 2
    return peaks


def demod_frame(x, start, mode="fast"):
    m = MODES[mode]; bps = m["bps"]; NC = m["nc"]; NFFT = m["nfft"]; CP = m["cp"]; K0 = m["k0"]
    nsym, L, flen = frame_geometry(mode)
    base = start + CHIRP_N + GAP0
    margin = 24   # start the FFT window a little early inside the CP (tolerates sync error)
    Y = []
    for s in range(nsym + 1):
        o = base + s * (NFFT + CP) + CP - margin
        seg = x[o:o + NFFT]
        if len(seg) < NFFT:
            return None
        Y.append(np.fft.rfft(seg)[K0:K0 + NC])
    Y = np.array(Y)
    z = Y[1:] * np.conj(Y[:-1])
    z /= (np.mean(np.abs(z)) + 1e-12)
    if bps == 2:
        z = z * np.exp(1j * np.pi / 4)
        soft = np.empty((nsym, NC * 2))
        soft[:, 0::2] = z.real
        soft[:, 1::2] = z.imag
    else:
        soft = z.real
    soft = soft.reshape(-1)
    perm = interleave_perm(L)
    deint = soft[perm]
    bits = viterbi(deint, PKT_BITS)
    return np.packbits(bits).tobytes()


def demodulate(x, mode=None, receiver=None, stats=None):
    """mode=None: try every mode on each detected chirp and keep the CRC-valid one."""
    rx = receiver or Receiver()
    modes = [mode] if mode else list(MODES)
    flen_min = min(frame_geometry(m)[2] for m in modes)
    peaks = find_chirps(x, flen_min)
    good = bad = 0
    nxt = 0
    from .core import parse
    for p in peaks:
        if p < nxt:
            continue
        for m in modes:
            pkt = demod_frame(x, p, m)
            if pkt is not None and parse(pkt) is not None:
                rx.feed(pkt); good += 1
                nxt = p + frame_geometry(m)[2] - CHIRP_N // 2
                modes = [m] + [q for q in modes if q != m]
                break
        else:
            bad += 1
        if rx.result is not None:
            break
    if stats is not None:
        stats.update(frames_detected=len(peaks), frames_ok=good, frames_bad=bad)
    return rx


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crushchan.audio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("encode"); e.add_argument("archive"); e.add_argument("wav")
    e.add_argument("--repair", type=float, default=0.5); e.add_argument("--mode", default="echo", choices=list(MODES))
    d = sub.add_parser("decode"); d.add_argument("wav"); d.add_argument("out"); d.add_argument("--mode", default=None, choices=list(MODES), help="default: auto-detect")
    a = ap.parse_args(argv)
    if a.cmd == "encode":
        data = open(a.archive, "rb").read()
        x, nf = encode(data, a.repair, a.mode)
        write_wav(a.wav, x)
        print(f"{len(data):,} bytes -> {nf} frames, {len(x) / FS:.1f} s, {len(data) / (len(x) / FS):.0f} B/s gross ({a.mode})")
    else:
        x, fs = read_wav(a.wav)
        st = {}
        rx = demodulate(to_48k(x, fs), a.mode, stats=st)
        print(st)
        if rx.result is None:
            print(f"NOT DECODED: {rx.progress() * 100:.0f}% of the needed packets received")
            return 2
        open(a.out, "wb").write(rx.result)
        print(f"decoded {len(rx.result):,} bytes, SHA-256 verified {hashlib.sha256(rx.result).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
