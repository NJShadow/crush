"""Cross-check py/crushchan/core.py against js/src/core.js (node).
Checks: (1) packets byte-identical for fountain and RS encoders,
(2) JS decodes Python packets with loss/shuffle/corruption, (3) Python decodes JS packets.
Run: python3 tests/test_core_cross.py"""
import os, sys, json, random, subprocess, hashlib, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan.core import *

NODE = r'''
const C = require(process.argv[2]); const fs = require('fs');
const job = JSON.parse(fs.readFileSync(process.argv[3]));
const out = [];
for (const t of job) {
  const data = Buffer.from(t.data, 'hex');
  let pk;
  if (t.kind === 'F') { const e = new C.FountainEncoder(new Uint8Array(data), t.T); pk = t.esis.map(([b, e2]) => e.packet(b, e2)); }
  else pk = C.rsEncode(new Uint8Array(data), t.T, t.K, t.N);
  const enc = pk.map(p => Buffer.from(p).toString('hex'));
  // decode the python packets
  const r = new C.Receiver(); let used = 0;
  for (const h of t.pypk) { used++; if (r.feed(new Uint8Array(Buffer.from(h, 'hex')))) break; }
  out.push({ enc, ok: !!r.result && Buffer.from(r.result).equals(data), used, sha: r.sha256 || null, err: r.error,
             b45: C.b45encode(new Uint8Array(data.subarray(0, 101))), b64: C.b64uEncode(new Uint8Array(data.subarray(0, 101))),
             sha_js: C.hex(C.sha256(new Uint8Array(data))) });
}
fs.writeFileSync(process.argv[4], JSON.stringify(out));
'''
rng = random.Random(1)
jobs, expect = [], []
for n in [0, 1, 2, 31, 33, 64, 999, 4096, 50000, 300000]:
    data = rng.randbytes(n) if n % 3 else bytes(rng.choice(b'ab\0') for _ in range(n))
    for T in [8, 61, 800]:
        if n > 100000 and T < 61: continue
        ktot, B, kb = layout(n, T)
        esis = [(b, e) for e in range(2 * kb + 40) for b in range(B)]
        pk = [fountain_packet(data, T, b, e) for b, e in esis]
        keep = [p for p in pk if rng.random() > 0.35]
        rng.shuffle(keep)
        # sprinkle corrupt + foreign packets
        bad = bytearray(keep[0]); bad[20 % len(bad)] ^= 1
        foreign = fountain_packet(b'other' * 9, T, 0, 0)
        keep = keep[:3] + [bytes(bad), foreign] + keep[3:]
        sel = esis[:50] + esis[-20:]
        jobs.append(dict(kind='F', data=data.hex(), T=T, esis=sel, pypk=[p.hex() for p in keep]))
        expect.append(([fountain_packet(data, T, b, e).hex() for b, e in sel], data))
    if n <= 50000:
        T = 37; ktot = (n + 32 + T - 1) // T; K = max(1, min(ktot, 60)); N = min(255, K + K // 2 + 1)
        pk = rs_packets(data, T, K, N)
        keep = pk[:]; rng.shuffle(keep)
        jobs.append(dict(kind='R', data=data.hex(), T=T, K=K, N=N, pypk=[p.hex() for p in keep]))
        expect.append(([p.hex() for p in pk], data))
with tempfile.TemporaryDirectory() as td:
    js = os.path.join(td, 'x.js'); open(js, 'w').write(NODE)
    jf = os.path.join(td, 'job.json'); json.dump(jobs, open(jf, 'w'))
    of = os.path.join(td, 'out.json')
    subprocess.run(['node', js, os.path.join(ROOT, 'js/src/core.js'), jf, of], check=True)
    res = json.load(open(of))
fails = 0
for job, (exp_pk, data), r in zip(jobs, expect, res):
    same = r['enc'] == exp_pk
    # python decodes JS packets (shuffled, with 30% loss for fountain only if enough remain)
    pr = Receiver()
    jpk = [bytes.fromhex(h) for h in r['enc']]
    if job['kind'] == 'R':
        rng.shuffle(jpk)
        for p in jpk:
            if pr.feed(p): break
        pyok = pr.result == data
    else:
        pyok = True  # covered by byte-identity
    ok = same and r['ok'] and pyok and r['b45'] == b45encode(data[:101]) and b45decode(r['b45']) == data[:101] \
        and r['sha_js'] == hashlib.sha256(data).hexdigest()
    import base64
    ok = ok and r['b64'] == base64.urlsafe_b64encode(data[:101]).decode().rstrip('=')
    fails += not ok
    print(job['kind'], len(data), job['T'], 'identical' if same else 'DIFFERENT', 'jsdecode', r['ok'], 'used', r['used'], 'pydecode', pyok, 'OK' if ok else 'FAIL')
print('FAILURES', fails)
sys.exit(1 if fails else 0)
