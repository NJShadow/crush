"""Paper backup robustness: render the PDF, damage the pages like phone photos / scans,
restore with zbar (Python restore path) and with the browser decode path (jsQR via node,
CrushChannels.scan.jsqrMulti - the same code receive.html runs on dropped images).

Scenarios (per page, random seed fixed):
  scan300   : 300 dpi scan, rotation +-2 deg, light noise, JPEG 90
  scan200   : 200 dpi scan, rotation +-2 deg, noise, JPEG 85
  photo12   : 12 MP phone photo: page fills ~85% of the frame, any rotation (0/90/180/270 + -12..12 deg),
              perspective, blur sigma 1.2, noise 6, uneven light, JPEG 80
  photo12cov: photo12 + an opaque blob covering 25-35% of the page
  photo8blur: 8 MP photo, blur sigma 2.0, noise 8
Each scenario can also drop whole pages (--drop-pages).
Usage: python3 tests/sim_paper.py backup.pdf archive out.json [--scenarios ...] [--drop-pages 1]
"""
import argparse, json, os, struct, subprocess, sys, tempfile, time
import cv2, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
import pypdfium2
from PIL import Image
from crushchan.core import Receiver, b45decode
from crushchan.paper import decode_images

ap = argparse.ArgumentParser()
ap.add_argument('pdf'); ap.add_argument('archive'); ap.add_argument('out')
ap.add_argument('--scenarios', default='scan300,scan200,photo12,photo12cov,photo8blur')
ap.add_argument('--drop-pages', type=int, default=0)
ap.add_argument('--no-js', action='store_true')
ap.add_argument('--js-mode', default='zxmulti', help='zxmulti (zxing-cpp, receive.html) or multi (jsQR, receive-lite.html)')
a = ap.parse_args()
JSMODE = a.js_mode
data = open(a.archive, 'rb').read()
doc = pypdfium2.PdfDocument(a.pdf)
npages = len(doc)


def page_img(i, dpi):
    return np.asarray(doc[i].render(scale=dpi / 72, grayscale=True).to_pil().convert('L'))


def damage(pg, rng, sc):
    h, w = pg.shape
    if sc.startswith('scan'):
        ang = rng.uniform(-2, 2)
        M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
        img = cv2.warpAffine(pg.astype(np.float32), M, (w, h), borderValue=235)
        img = img * 0.9 + 15
        img += rng.normal(0, 3 if sc == 'scan300' else 5, img.shape)
        q = 90 if sc == 'scan300' else 85
    else:
        FW, FH = (4032, 3024) if sc.startswith('photo12') else (3264, 2448)
        rot90 = int(rng.integers(0, 4))
        src = np.rot90(pg, rot90).astype(np.float32)
        sh, sw = src.shape
        scale = 0.85 * min(FW / sw, FH / sh)
        cx, cy = FW / 2 + rng.uniform(-0.04, 0.04) * FW, FH / 2 + rng.uniform(-0.04, 0.04) * FH
        ang = np.deg2rad(rng.uniform(-12, 12))
        corners = np.array([[-sw / 2, -sh / 2], [sw / 2, -sh / 2], [sw / 2, sh / 2], [-sw / 2, sh / 2]]) * scale
        R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
        dst = corners @ R.T + [cx, cy]
        dst += rng.uniform(-0.04, 0.04, (4, 2)) * [sw * scale, sh * scale]
        M = cv2.getPerspectiveTransform(np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]]), np.float32(dst))
        if sc.endswith('cov'):
            # opaque blob (hand / coffee) over 25-35% of the page, drawn on the page before the warp
            frac = rng.uniform(0.25, 0.35)
            # ellipse with area = frac * page area, aspect 0.6..1.6
            asp = rng.uniform(0.6, 1.6)
            ea = np.sqrt(frac * sw * sh / np.pi * asp); eb = frac * sw * sh / np.pi / ea
            cx0, cy0 = int(rng.uniform(ea * 0.5, sw - ea * 0.5)), int(rng.uniform(eb * 0.5, sh - eb * 0.5))
            src = src.copy()
            cv2.ellipse(src, (cx0, cy0), (int(ea), int(eb)), 0, 0, 360, 90.0, -1)
        img = cv2.warpPerspective(src, M, (FW, FH), flags=cv2.INTER_AREA, borderValue=60.0)  # dark table
        img = 20 + img * 0.8
        yy, xx = np.mgrid[0:FH, 0:FW].astype(np.float32)
        g = rng.uniform(-1, 1, 2)
        img *= 1 + 0.25 * (g[0] * (xx / FW - 0.5) + g[1] * (yy / FH - 0.5))
        sig = 2.0 if sc == 'photo8blur' else 1.2
        img = cv2.GaussianBlur(img, (0, 0), sig)
        img += rng.normal(0, 8 if sc == 'photo8blur' else 6, img.shape)
        q = 80
    img = np.clip(img, 0, 255).astype(np.uint8)
    ok, enc = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return enc.tobytes()


def js_decode(jpgs, td):
    """browser path: scale to <= 2600 px long side (as receive.html does), jsqrMulti."""
    out = []
    for i, j in enumerate(jpgs):
        im = cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_GRAYSCALE)
        s = min(1.0, 2600 / max(im.shape))
        im = cv2.resize(im, (round(im.shape[1] * s), round(im.shape[0] * s)), interpolation=cv2.INTER_AREA)
        raw = os.path.join(td, f'j{i}.raw')
        with open(raw, 'wb') as fh:
            fh.write(struct.pack('<III', 1, im.shape[1], im.shape[0])); fh.write(im.tobytes())
        oj = os.path.join(td, f'j{i}.json')
        subprocess.run(['node', os.path.join(ROOT, 'js/tools/jsqr_batch.js'), raw, oj, JSMODE], check=True)
        fr = json.load(open(oj))['frames'][0]
        out.append(([b45decode(s) for s in fr['codes']], fr['ms']))
    return out


results = []
for sc in a.scenarios.split(','):
    rng = np.random.default_rng(len(sc) * 7 + a.drop_pages)
    dpi = 300 if sc in ('scan300',) else 200 if sc == 'scan200' else 300
    drop = set(rng.choice(npages, size=min(a.drop_pages, npages), replace=False).tolist()) if a.drop_pages else set()
    with tempfile.TemporaryDirectory() as td:
        jpgs, paths = [], []
        for i in range(npages):
            if i in drop:
                continue
            j = damage(page_img(i, dpi), rng, sc)
            p = os.path.join(td, f'page{i}.jpg'); open(p, 'wb').write(j)
            jpgs.append(j); paths.append(p)
        if sc == 'photo12cov':
            os.makedirs(os.path.join(ROOT, 'results/paper'), exist_ok=True)
            open(os.path.join(ROOT, 'results/paper/sample_photo12cov.jpg'), 'wb').write(jpgs[0])
        t0 = time.time()
        logs = []
        rx = decode_images(paths, log=logs.append, stop_early=False)
        tz = time.time() - t0
        zcount = [int(l.split(': ')[1].split(' ')[0]) for l in logs]
        row = dict(scenario=sc, pages=npages, dropped_pages=sorted(drop), zbar_codes_per_page=zcount,
                   zbar_restored=rx.result == data, zbar_s=round(tz, 1), K=rx.p['kb'] if rx.p else None,
                   N=rx.p.get('N') if rx.p else None)
        if not a.no_js:
            t0 = time.time()
            jr = js_decode(jpgs, td)
            rj = Receiver()
            for codes, ms in jr:
                for c in codes:
                    if c: rj.feed(c)
            row.update(js_mode=JSMODE, jsqr_codes_per_page=[len([c for c in codes if c]) for codes, ms in jr],
                       jsqr_restored=rj.result == data, jsqr_ms_per_page=round(float(np.mean([ms for _, ms in jr])), 0))
        results.append(row)
        print(json.dumps(row), flush=True)
        json.dump(results, open(a.out, 'w'), indent=1)
