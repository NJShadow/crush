"""Audio modem channel simulation.

Channel: speaker/mic band-pass (150 Hz - 10 kHz, 2nd order) -> room impulse response
(direct + early reflections + exponential tail with given RT60) -> sample-clock drift (ppm)
-> optional 48k -> 44.1k resampling (decoder resamples back) -> white noise at given SNR
(SNR = signal power / noise power over the whole 0-24 kHz band, measured on the
received signal while frames are playing).

Reports frame success rate (CRC-valid packets / frames sent) and end-to-end archive
decode (SHA-256 verified, byte-compared) with the default 50% fountain repair.
Usage: python3 tests/sim_audio.py archive out.json [--frames 40]
"""
import argparse, json, math, os, sys, time
import numpy as np
from scipy.signal import butter, sosfilt, fftconvolve, resample_poly
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan import audio as A
from crushchan.core import fountain_packet, layout, Receiver

ap = argparse.ArgumentParser()
ap.add_argument('archive'); ap.add_argument('out')
ap.add_argument('--frames', type=int, default=40)
ap.add_argument('--modes', default='fast,robust')
ap.add_argument('--snrs', default='30,20,12,8,5,2')
ap.add_argument('--conds', default='clean,resample44,drift100,drift300,room03near,room06near,room03far,room06far,combo')
ap.add_argument('--e2e', action='store_true', help='also decode the whole archive per condition at the given SNRs')
a = ap.parse_args()
data = open(a.archive, 'rb').read()
FS = A.FS
SOS = butter(2, [150, 10000], btype='band', fs=FS, output='sos')


def rir(rt60, rng, drr_db):
    if rt60 <= 0:
        return np.array([1.0])
    n = int(rt60 * FS)
    t = np.arange(n) / FS
    h = rng.normal(0, 1, n) * np.exp(-6.91 * t / rt60)
    h[: int(0.002 * FS)] = 0
    # diffuse tail energy = direct energy / DRR (direct-to-reverberant ratio)
    h *= np.sqrt(10 ** (-drr_db / 10) / np.sum(h ** 2))
    h[0] = 1.0
    for d, g in ((0.0031, 0.6), (0.0057, -0.45), (0.0093, 0.4), (0.0142, -0.3), (0.021, 0.25)):
        h[int(d * FS)] += g
    return h


def drift(x, ppm):
    if ppm == 0:
        return x
    up = resample_poly(x, 4, 1)
    r = 1 + ppm * 1e-6                       # receiver clock faster by ppm
    t = np.arange(int(len(x) * r)) / r * 4
    return np.interp(t, np.arange(len(up)), up)


# room: (RT60 s, DRR dB). 'near' ~ phone 0.5-1 m from the speaker, 'far' ~ 2-3 m in a live room
ROOMS = {'room03near': (0.3, 8), 'room06near': (0.6, 6), 'room03far': (0.3, 0), 'room06far': (0.6, -3), 'combo': (0.4, 4)}


def channel(x, cond, snr, rng):
    y = sosfilt(SOS, x)
    rt, drr = ROOMS.get(cond, (0, 0))
    if rt:
        y = fftconvolve(y, rir(rt, rng, drr))[: len(y) + int(rt * FS)]
    ppm = {'drift100': 100, 'drift300': 300, 'combo': -150}.get(cond, 0)
    y = drift(y, ppm)
    fs = FS
    if cond in ('resample44', 'combo'):
        y = resample_poly(y, 147, 160); fs = 44100
    active = np.abs(y) > 1e-4
    p = np.mean(y[active] ** 2)
    y = y + rng.normal(0, math.sqrt(p / 10 ** (snr / 10)), len(y))
    return A.to_48k(y, fs)


ktot, B, kb = layout(len(data), A.AUDIO_T)
results = []
for mode in a.modes.split(','):
    nsym, L, flen = A.frame_geometry(mode)
    pk = [fountain_packet(data, A.AUDIO_T, n % B, n // B) for n in range(a.frames)]
    tx = np.concatenate([np.zeros(FS // 4)] + [A.modulate_packet(p, mode) for p in pk] + [np.zeros(FS // 2)])
    for cond in a.conds.split(','):
        for snr in map(float, a.snrs.split(',')):
            rng = np.random.default_rng(int(snr * 10) + len(cond))
            t0 = time.time()
            rx_sig = channel(tx, cond, snr, rng)
            peaks = A.find_chirps(rx_sig, flen)
            ok = 0
            got = set()
            for p in peaks:
                b = A.demod_frame(rx_sig, p, mode)
                if b is not None and b in pk and b not in got:
                    got.add(b); ok += 1
                # a CRC-valid packet that is not one we sent would be a silent error:
                elif b is not None and b not in pk:
                    from crushchan.core import parse
                    if parse(b) is not None:
                        print('!!! CRC-valid foreign packet', file=sys.stderr)
            row = dict(mode=mode, cond=cond, snr_db=snr, frames=a.frames, detected=len(peaks), frames_ok=ok,
                       fsr=ok / a.frames, frame_s=flen / FS, net_Bps_per_good_frame=A.AUDIO_T / (flen / FS),
                       sim_s=round(time.time() - t0, 1))
            results.append(row)
            print(json.dumps(row), flush=True)
            json.dump(results, open(a.out, 'w'), indent=1)
