"""Screen-to-camera fountain stream: simulation measurement.

For each (QR version, ECC level, capture profile):
  1. encode the archive into the endless fountain schedule,
  2. render NF frames as QR codes and pass each through the camera model
     (py/crushchan/camsim.py: perspective, blur, motion blur, noise, contrast, JPEG),
  3. decode every captured frame with zbar AND with the vendored jsQR (node),
  4. Monte-Carlo: drop a random fraction r of frames (r = 0.1 .. 0.5) on top of the
     real decode outcomes, feed survivors in order to the fountain Receiver and
     record how many displayed frames it took to complete (SHA-256 verified).

Usage: python3 tests/sim_stream.py <archive> <out.json> [--versions 10,15,20,25,30]
        [--ecl l,m] [--profiles good,typical,poor] [--trials 200]
"""
import argparse, json, os, struct, subprocess, sys, tempfile, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan.core import fountain_packet, layout, Receiver, b45decode
from crushchan.qr import symbol_size, qr_matrix, render, zbar_decode
from crushchan.camsim import capture
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument('archive'); ap.add_argument('out')
ap.add_argument('--versions', default='10,15,20,25,30')
ap.add_argument('--ecl', default='l,m')
ap.add_argument('--profiles', default='good,typical,poor')
ap.add_argument('--trials', type=int, default=200)
ap.add_argument('--losses', default='0.1,0.3,0.5')
ap.add_argument('--maxframes', type=int, default=0)
ap.add_argument('--rate-only', action='store_true', help='measure per-frame decode rate only (no Monte-Carlo)')
a = ap.parse_args()
data = open(a.archive, 'rb').read()
results = []
for V in map(int, a.versions.split(',')):
    for ecl in a.ecl.split(','):
        T = symbol_size(V, ecl)
        ktot, B, kb = layout(len(data), T)
        NF = a.maxframes or int(ktot / (1 - 0.5) * 2.2 + 40)
        pk = [fountain_packet(data, T, n % B, n // B) for n in range(NF)]
        mats = [qr_matrix(p, V, ecl) for p in pk]
        for prof in a.profiles.split(','):
            rng = np.random.default_rng(V * 1000 + len(prof) + (ecl == 'm'))
            t0 = time.time()
            frames = []
            for m in mats:
                img = render(m, module_px=4)
                frames.append(capture(img, rng, prof))
            t_cap = time.time() - t0
            # zbar
            t0 = time.time()
            zb = []
            for f in frames:
                got = zbar_decode(Image.fromarray(f))
                zb.append(got[0] if got else None)
            t_zbar = (time.time() - t0) / len(frames)
            # jsQR via node, through the page's own scan code (js/src/qrstream.js), two variants
            jsres = {}
            with tempfile.TemporaryDirectory() as td:
                raw = os.path.join(td, 'f.raw'); H, W = frames[0].shape
                with open(raw, 'wb') as fh:
                    fh.write(struct.pack('<III', len(frames), W, H))
                    for f in frames: fh.write(f.tobytes())
                for mode in ('b2', 'raw', 'zx'):
                    outj = os.path.join(td, 'o.json')
                    subprocess.run(['node', os.path.join(ROOT, 'js/tools/jsqr_batch.js'), raw, outj, mode], check=True)
                    jr = json.load(open(outj))
                    jsres[mode] = ([b45decode(fr['codes'][0]) if fr['codes'] else None for fr in jr['frames']], jr['mean_ms'])
            js, jr = jsres['b2'][0], {'mean_ms': jsres['b2'][1]}
            ok_jraw = [j == p for j, p in zip(jsres['raw'][0], pk)]
            ok_z = [z == p for z, p in zip(zb, pk)]
            ok_j = [j == p for j, p in zip(js, pk)]
            wrong_z = sum(1 for z, p in zip(zb, pk) if z is not None and z != p)
            wrong_j = sum(1 for j, p in zip(js, pk) if j is not None and j != p)
            row = dict(version=V, ecl=ecl, profile=prof, T=T, packet_bytes=T + 22, ktot=ktot, blocks=B,
                       frames=len(frames), zbar_frame_ok=sum(ok_z) / len(ok_z), jsqr_frame_ok=sum(ok_j) / len(ok_j), jsqr_raw_frame_ok=sum(ok_jraw) / len(ok_jraw), jsqr_raw_ms=jsres['raw'][1], zx_frame_ok=sum(z == p for z, p in zip(jsres['zx'][0], pk)) / len(pk), zx_ms=jsres['zx'][1], zx_wrong=sum(1 for z, p in zip(jsres['zx'][0], pk) if z is not None and z != p),
                       zbar_wrong=wrong_z, jsqr_wrong=wrong_j, zbar_ms=t_zbar * 1000, jsqr_ms=jr['mean_ms'],
                       capture_ms=t_cap / len(frames) * 1000, mc={})
            # Monte-Carlo frame loss over the real decode outcomes (jsQR = what the browser receiver uses)
            for dec_name, okv, got in ((() if a.rate_only else (('jsqr', ok_j, js), ('zbar', ok_z, zb)))):
                for r in map(float, a.losses.split(',')):
                    mrng = np.random.default_rng(int(r * 1000) + V)
                    need = []
                    for t in range(a.trials):
                        keep = mrng.random(len(frames)) >= r
                        start = int(mrng.integers(0, max(1, len(frames) // 4)))  # receiver joins mid-stream
                        rc = Receiver(); done_at = None; fed = 0
                        for n in range(start, len(frames)):
                            if keep[n] and got[n] is not None:
                                fed += 1
                                if rc.feed(got[n]):
                                    done_at = n - start + 1; break
                        need.append((done_at, fed, rc.stats['useful']))
                    comp = [x for x in need if x[0] is not None]
                    nd = sorted(x[0] for x in comp)
                    row['mc'][f'{dec_name}_loss{r}'] = dict(
                        completion=len(comp) / len(need),
                        frames_median=float(np.median(nd)) if nd else None,
                        frames_p95=float(np.percentile(nd, 95)) if nd else None,
                        decoded_packets_median=float(np.median([x[1] for x in comp])) if comp else None,
                        budget_frames=len(frames) - len(frames) // 8)
            results.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != 'mc'}), flush=True)
            print('   ', {k: (v['completion'], v['frames_median'], v['frames_p95']) for k, v in row['mc'].items()}, flush=True)
            json.dump(dict(archive=os.path.basename(a.archive), bytes=len(data), results=results), open(a.out, 'w'), indent=1)
