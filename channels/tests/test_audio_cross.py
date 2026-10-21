"""Audio modem: Python WAV -> JS demodulator (node) and JS modulator -> Python demodulator,
all three modes, clean channel; byte-exact comparison. Run: python3 tests/test_audio_cross.py"""
import os, sys, subprocess, tempfile, hashlib
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan import audio as A
NODE = r'''
const fs = require('fs'); require(process.argv[2] + '/core.js'); require(process.argv[2] + '/audio.js');
const CC = globalThis.CrushChannels, cmd = process.argv[3];
function readWav(p) { const b = fs.readFileSync(p); const n = (b.length - 44) / 2; const x = new Float32Array(n); for (let i = 0; i < n; i++) x[i] = b.readInt16LE(44 + 2 * i) / 32768; return x; }
if (cmd === 'dec') {
  const x = readWav(process.argv[4]); const rx = new CC.audio.Rx();
  for (let o = 0; o < x.length && !rx.rx.result; o += 4096) rx.push(x.subarray(o, o + 4096));
  rx.push(new Float32Array(rx.maxF + rx.B));
  if (rx.rx.result) fs.writeFileSync(process.argv[5], rx.rx.result);
  console.log(JSON.stringify(rx.stats));
} else {
  const data = new Uint8Array(fs.readFileSync(process.argv[4]));
  fs.writeFileSync(process.argv[5], CC.audio.wav(CC.audio.modulate(data, { mode: process.argv[6], repair: 0.3 })));
}
'''
data = open(os.path.join(ROOT, 'testdata/a_docx.crush'), 'rb').read()
fails = 0
with tempfile.TemporaryDirectory() as td:
    js = os.path.join(td, 'n.js'); open(js, 'w').write(NODE)
    arc = os.path.join(td, 'a.crush'); open(arc, 'wb').write(data)
    for mode in A.MODES:
        w1 = os.path.join(td, f'py_{mode}.wav')
        x, nf = A.encode(data, 0.3, mode); A.write_wav(w1, x)
        out = os.path.join(td, 'o1')
        r = subprocess.run(['node', js, os.path.join(ROOT, 'js/src'), 'dec', w1, out], capture_output=True, text=True)
        ok1 = os.path.exists(out) and open(out, 'rb').read() == data
        w2 = os.path.join(td, f'js_{mode}.wav')
        subprocess.run(['node', js, os.path.join(ROOT, 'js/src'), 'enc', arc, w2, mode], check=True)
        y, fs = A.read_wav(w2); st = {}
        rx = A.demodulate(A.to_48k(y, fs), None, stats=st)
        ok2 = rx.result == data
        print(f'{mode:7s} py->js {"OK" if ok1 else "FAIL"} {r.stdout.strip()} | js->py {"OK" if ok2 else "FAIL"} {st}  ({len(x)/A.FS:.1f} s for {len(data)} B)')
        fails += (not ok1) + (not ok2)
print('FAILURES', fails); sys.exit(1 if fails else 0)
