"""End-to-end: dist/receive.html in headless Chromium, fed by Chromium's fake camera
playing an MJPEG made of simulated screen-to-camera captures of the fountain stream.

Each displayed code is held for HOLD camera frames (30 fps camera => 30/HOLD codes/s);
a fraction LOSS of displayed codes is replaced by a blank/garbled frame (missed codes).
The page must finish on its own and its bytes must equal the archive.

Usage: python3 tests/e2e_camera.py archive [--version 20 --ecl l --profile typical --loss 0.3 --hold 3]
"""
import argparse, base64, hashlib, json, os, sys, tempfile, time
import cv2, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan.core import fountain_packet, layout
from crushchan.qr import symbol_size, qr_matrix, render
from crushchan.camsim import capture
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument('archive'); ap.add_argument('--version', type=int, default=20); ap.add_argument('--ecl', default='l')
ap.add_argument('--profile', default='typical'); ap.add_argument('--loss', type=float, default=0.3)
ap.add_argument('--hold', type=int, default=3); ap.add_argument('--codes', type=int, default=0)
ap.add_argument('--timeout', type=float, default=240)
ap.add_argument('--page', default='receive.html')
a = ap.parse_args()
data = open(a.archive, 'rb').read()
T = symbol_size(a.version, a.ecl); ktot, B, kb = layout(len(data), T)
ncodes = a.codes or int(ktot / (1 - a.loss) * 1.6 + 10)
rng = np.random.default_rng(7)
td = tempfile.mkdtemp(dir=os.environ.get('TMPDIR'))
mj = os.path.join(td, 'cam.mjpeg')
start = int(rng.integers(0, 3 * ktot))   # stream already running when the camera opens
dropped = 0
with open(mj, 'wb') as fh:
    for i in range(ncodes):
        n = start + i
        if rng.random() < a.loss:
            dropped += 1
            # missed code: camera saw a transition / looked away -> blend of two codes
            m1 = render(qr_matrix(fountain_packet(data, T, n % B, n // B), a.version, a.ecl))
            m2 = render(qr_matrix(fountain_packet(data, T, (n + 1) % B, (n + 1) // B), a.version, a.ecl))
            from PIL import Image
            img = Image.blend(m1, m2, 0.5)
        else:
            img = render(qr_matrix(fountain_packet(data, T, n % B, n // B), a.version, a.ecl))
        for h in range(a.hold):
            f = capture(img, rng, a.profile)
            ok, enc = cv2.imencode('.jpg', f, [cv2.IMWRITE_JPEG_QUALITY, 90])
            fh.write(enc.tobytes())
print(f'archive {len(data)} B, V{a.version}-{a.ecl.upper()} T={T} ktot={ktot}, {ncodes} codes x {a.hold} frames, {dropped} garbled, mjpeg {os.path.getsize(mj)//1024} KiB', flush=True)
with sync_playwright() as p:
    b = p.chromium.launch(args=['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
                                f'--use-file-for-fake-video-capture={mj}'])
    ctx = b.new_context(); pg = ctx.new_page()
    ext = []
    pg.on('request', lambda r: ext.append(r.url[:60]) if not r.url.startswith(('file:', 'data:', 'blob:')) else None)
    pg.goto('file://' + os.path.join(ROOT, 'dist', a.page))
    t0 = time.time()
    pg.click('#cam')
    res = None
    while time.time() - t0 < a.timeout:
        time.sleep(1)
        res = pg.evaluate('''() => { const r = CrushChannels.lastResult; const rx = CrushChannels.receiver;
            return { done: !!r, sha: r ? r.sha256 : null, b64: r ? (() => { let s=''; for (let i=0;i<r.bytes.length;i+=8192) s += String.fromCharCode.apply(null, Array.from(r.bytes.subarray(i,i+8192))); return btoa(s); })() : null,
                     stat: document.getElementById('stat').textContent, extra: (document.querySelector('.cc-extra')||{}).textContent || '' }; }''')
        if res['done']:
            break
    el = time.time() - t0
    b.close()
ok = bool(res and res['done'] and base64.b64decode(res['b64']) == data and res['sha'] == hashlib.sha256(data).hexdigest() and not ext)
print(json.dumps(dict(page=a.page, ok=ok, wall_s=round(el, 1), stat=res['stat'], extra=res['extra'], external_requests=ext)))
sys.exit(0 if ok else 1)
