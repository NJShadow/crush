"""Monte-Carlo completion statistics for the fountain stream.

Uses the real encoder/decoder (py/crushchan/core.py) with exact packets; each
displayed frame is independently (a) lost with probability r (camera/receiver
missed it) and (b) decoded with the per-frame QR decode probability measured
by tests/sim_stream.py --rate-only. Receiver joins at a random point.
Usage: python3 tests/mc_stream.py results/stream_rates.json out.json archive...
"""
import json, os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan.core import fountain_packet, layout, Receiver

rates = json.load(open(sys.argv[1]))['results']
out = []
TRIALS = int(os.environ.get('TRIALS', 100))
for arch in sys.argv[3:]:
    data = open(arch, 'rb').read()
    for row in rates:
        pdec = row[os.environ.get('DEC', 'zx') + '_frame_ok']
        if pdec < 0.05:
            continue
        T = row['T']; ktot, B, kb = layout(len(data), T)
        cache = {}
        for r in (0.1, 0.3, 0.5):
            rng = np.random.default_rng(12345)
            need, overh = [], []
            for t in range(TRIALS):
                start = int(rng.integers(0, 4 * ktot)); rc = Receiver(); n = start; fed = 0
                while True:
                    if rng.random() >= r and rng.random() < pdec:
                        if n not in cache: cache[n] = fountain_packet(data, T, n % B, n // B)
                        fed += 1
                        if rc.feed(cache[n]): break
                    n += 1
                    if n - start > 200 * ktot + 1000: raise SystemExit('no completion')
                need.append(n - start + 1); overh.append(fed / ktot)
            need = np.array(need)
            d = dict(decoder=os.environ.get('DEC', 'zx'), archive=os.path.basename(arch), bytes=len(data), version=row['version'], ecl=row['ecl'],
                     profile=row['profile'], p_decode=pdec, loss=r, T=T, ktot=ktot, blocks=B,
                     frames_median=float(np.median(need)), frames_p95=float(np.percentile(need, 95)),
                     frames_ideal=ktot, recv_overhead_median=float(np.median(overh)),
                     recv_overhead_p95=float(np.percentile(overh, 95)))
            for fps in (5, 10):
                d[f'Bps_median_{fps}fps'] = len(data) / (d['frames_median'] / fps)
                d[f'Bps_p95_{fps}fps'] = len(data) / (d['frames_p95'] / fps)
            # completion probability within a budget of 1.2x the expected frame count
            budget = 1.2 * ktot / ((1 - r) * pdec)
            d['P_complete_within_1.2x_expected'] = float(np.mean(need <= budget))
            out.append(d)
            print(json.dumps(d), flush=True)
    json.dump(out, open(sys.argv[2], 'w'), indent=1)
