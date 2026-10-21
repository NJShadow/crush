"""dist/audio.html in headless Chromium:
 (1) microphone path: Chromium's fake audio capture plays a WAV that went through the
     simulated room channel (tests/sim_audio.py channel) and was saved at 44.1 kHz;
 (2) recording path: the same kind of WAV given to the file input (decodeAudioData).
Byte-exact comparison of the page's result. Also asserts no network request is made.
Usage: python3 tests/e2e_audio.py archive [--cond combo --snr 10 --mode echo]"""
import argparse, base64, hashlib, json, os, sys, tempfile, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan import audio as A
from playwright.sync_api import sync_playwright
ap = argparse.ArgumentParser(); ap.add_argument('archive'); ap.add_argument('--cond', default='combo'); ap.add_argument('--snr', type=float, default=10)
ap.add_argument('--mode', default='echo'); ap.add_argument('--path', default='mic,file'); ap.add_argument('--timeout', type=float, default=400)
opt = ap.parse_args()
sys.argv = [sys.argv[0], opt.archive, '/dev/null', '--conds', opt.cond]
src = open(os.path.join(HERE, 'sim_audio.py')).read().split('ktot, B, kb = layout')[0]
exec(compile(src, 'sim_audio', 'exec'))
a = opt
data = open(a.archive, 'rb').read()
x, nf = A.encode(data, 0.5, a.mode)
y = channel(np.asarray(x, dtype=np.float64), a.cond, a.snr, np.random.default_rng(1))
from scipy.signal import resample_poly
y44 = resample_poly(y, 147, 160)
y44 = y44 / np.max(np.abs(y44)) * 0.7
td = tempfile.mkdtemp(dir=os.environ.get('TMPDIR'))
wavp = os.path.join(td, 'rec.wav'); A.write_wav(wavp, y44, 44100)
print(f'{len(data)} B, {nf} frames, {len(x)/A.FS:.1f} s, channel {a.cond} SNR {a.snr} dB, WAV at 44.1 kHz', flush=True)
ok_all = True
import subprocess
r = subprocess.run([sys.executable, '-m', 'crushchan.audio', 'decode', wavp, wavp + '.out'], cwd=os.path.join(ROOT, 'py'), capture_output=True, text=True)
okpy = os.path.exists(wavp + '.out') and open(wavp + '.out', 'rb').read() == data
ok_all &= okpy
print(json.dumps(dict(path='python CLI', ok=okpy, out=r.stdout.strip().replace(chr(10), ' | '))), flush=True)
for path in a.path.split(','):
    with sync_playwright() as p:
        args = ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=no-user-gesture-required']
        if path == 'mic': args.append(f'--use-file-for-fake-audio-capture={wavp}')
        b = p.chromium.launch(args=args); pg = b.new_page()
        ext = []
        pg.on('request', lambda r: ext.append(r.url) if not r.url.startswith(('file:', 'data:', 'blob:')) else None)
        pg.goto('file://' + os.path.join(ROOT, 'dist/audio.html'))
        t0 = time.time()
        if path == 'mic': pg.click('#mic')
        else: pg.set_input_files('#rec', wavp)
        res = None
        while time.time() - t0 < a.timeout:
            time.sleep(2)
            res = pg.evaluate('''() => { const r = CrushChannels.lastResult; return { done: !!r, sha: r ? r.sha256 : null,
                b64: r ? (() => { let s=''; for (let i=0;i<r.bytes.length;i+=8192) s += String.fromCharCode.apply(null, Array.from(r.bytes.subarray(i,i+8192))); return btoa(s); })() : null,
                stat: document.getElementById('stat').textContent }; }''')
            if res['done']: break
        el = time.time() - t0; b.close()
    ok = bool(res and res['done'] and base64.b64decode(res['b64']) == data and not ext)
    ok_all &= ok
    print(json.dumps(dict(path=path, ok=ok, wall_s=round(el, 1), stat=res['stat'], external_requests=ext)), flush=True)
sys.exit(0 if ok_all else 1)
