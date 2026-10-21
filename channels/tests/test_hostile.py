"""Hostile input: mutated/forged packets must never crash, hang or allocate in proportion to a
claimed size, in Python and in JS (node). Forged CRC-valid packets with absurd headers included.
Run: python3 tests/test_hostile.py"""
import os, sys, random, struct, zlib, subprocess, json, time, resource, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan.core import *
rng = random.Random(7)
data = open(os.path.join(ROOT, 'testdata/a_docx.crush'), 'rb').read()
good = [fountain_packet(data, 200, 0, e) for e in range(20)] + rs_packets(data, 200, 40, 60)[:20]

def forge(kind, L, T, f12, f14):
    h = struct.pack('>BBIIH', MAGIC, kind, 0x1234, L, T) + f12 + f14 + bytes(rng.getrandbits(8) for _ in range(T))
    return h + struct.pack('>I', zlib.crc32(h))
pk = []
for _ in range(3000):
    p = bytearray(rng.choice(good)); op = rng.random()
    if op < 0.4:
        for _ in range(rng.randint(1, 4)): p[rng.randrange(len(p))] ^= 1 << rng.randrange(8)
    elif op < 0.6: p = p[:rng.randrange(len(p))]
    else: p += bytes(rng.randrange(256) for _ in range(rng.randint(1, 30)))
    pk.append(bytes(p))
for L in (0, 1, MAX_L, MAX_L + 1, 0xFFFFFFFF):
    for T in (8, 9, 200, 8192, 8193, 0, 65535):
        if T > 9000: continue
        pk.append(forge(KIND_FOUNTAIN, L, T, struct.pack('>H', 0xFFFF), struct.pack('>I', 0xFFFFFFFF)))
        pk.append(forge(KIND_FOUNTAIN, L, T, struct.pack('>H', 0), struct.pack('>I', 0xFFFFFFFF)))
        pk.append(forge(KIND_RS, L, T, bytes([255, 255]), struct.pack('>HH', 0, 254)))
        pk.append(forge(KIND_RS, L, T, bytes([0, 0]), struct.pack('>HH', 0, 0)))
t0 = time.time()
accepted = 0
for p in pk:
    r = Receiver(); r.feed(p)                     # fresh receiver: the forged header is the session
    accepted += r.p is not None
    assert r.result is None
r = Receiver()
for p in pk: r.feed(p)
mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
print(f'python: {len(pk)} hostile packets, {accepted} parsed as a session start, no result, {time.time()-t0:.1f}s, peak RSS {mem} MB')
ok = mem < 400
with tempfile.TemporaryDirectory() as td:
    f = os.path.join(td, 'p.json'); json.dump([p.hex() for p in pk], open(f, 'w'))
    js = r'''const C=require(process.argv[2]);const pk=JSON.parse(require('fs').readFileSync(process.argv[3]));let n=0;
    for(const h of pk){const r=new C.Receiver();r.feed(new Uint8Array(Buffer.from(h,'hex')));if(r.result)throw 'result';}
    const r=new C.Receiver();for(const h of pk)r.feed(new Uint8Array(Buffer.from(h,'hex')));
    for(let i=0;i<20000;i++){const s=Array.from({length:Math.random()*60|0},()=>String.fromCharCode(Math.random()*128|0)).join('');C.b45decode(s);C.b64uDecode(s);}
    console.log('node: ok, heap MB', (process.memoryUsage().rss/1048576)|0);'''
    jf = os.path.join(td, 'x.js'); open(jf, 'w').write(js)
    out = subprocess.run(['node', jf, os.path.join(ROOT, 'js/src/core.js'), f], capture_output=True, text=True)
    print(out.stdout.strip(), out.stderr.strip()[:300]); ok &= out.returncode == 0
print('PASS' if ok else 'FAIL'); sys.exit(0 if ok else 1)
