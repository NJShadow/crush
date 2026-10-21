#!/usr/bin/env python3
"""Build single-file receivers/senders into dist/.

Every /*INLINE:path*/ marker in js/pages/*.html is replaced by the file at
js/<path>. jsQR is minified with terser (licence header kept) if
vendor/jsQR.min.js is missing. Output pages make no external requests.
Usage: python3 build.py
"""
import os, re, subprocess, sys, hashlib
ROOT = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(ROOT, 'js')
DIST = os.path.join(ROOT, 'dist')
os.makedirs(DIST, exist_ok=True)

minq = os.path.join(JS, 'vendor/jsQR.min.js')
if not os.path.exists(minq):
    terser = os.path.join(ROOT, 'vendor_src/tsbuild/node_modules/.bin/terser')
    body = subprocess.run([terser, os.path.join(JS, 'vendor/jsQR.js'), '-c', '-m'], check=True, capture_output=True).stdout.decode()
    hdr = '/* jsQR 1.4.0 (commit 8e6a036) - Copyright Cosmo Wolfe - Apache License 2.0 - https://github.com/cozmo/jsQR - minified, unmodified */\n'
    open(minq, 'w').write(hdr + body)

pat = re.compile(r'/\*INLINE:([^*]+)\*/')
for name in sorted(os.listdir(os.path.join(JS, 'pages'))):
    if not name.endswith('.html'):
        continue
    src = open(os.path.join(JS, 'pages', name)).read()

    def rep(m):
        t = open(os.path.join(JS, m.group(1))).read()
        if '</script' in t.lower():
            sys.exit(f'{m.group(1)} contains </script')
        return t
    out = pat.sub(rep, src)
    # sanity: no external fetches
    for bad in ('http://', 'https://'):
        for mm in re.finditer(re.escape(bad) + r'[^\s"\'<)]*', out):
            u = mm.group(0)
            ctx = out[max(0, mm.start() - 80):mm.start()]
            if re.search(r'(src|href)\s*=\s*["\']?$', ctx) or 'fetch(' in ctx[-20:] or 'import' in ctx[-12:]:
                sys.exit(f'{name}: external reference {u}')
    variants = [(name, out)]
    if 'zxing' in src:   # lite variant: jsQR only (no 1.3 MB WASM)
        lite = pat.sub(rep, '\n'.join(l for l in src.split('\n') if 'INLINE:vendor/zxing' not in l))
        variants.append((name.replace('.html', '-lite.html'), lite))
    for nm, o in variants:
        open(os.path.join(DIST, nm), 'w').write(o)
        print(f'{nm:28s} {len(o.encode()):>9,d} bytes  sha256 {hashlib.sha256(o.encode()).hexdigest()[:16]}')

# Embeddable bundle for host pages (e.g. Crush's self-extracting share .html):
# dist/crushchan.js = core + qrstream + audio + ui; vendors are separate files so a host
# can pick: qrcodegen (sender), jsQR (light QR decoder), zxing (strong QR decoder, 1.3 MB).
bundle = '\n'.join(open(os.path.join(JS, 'src', f)).read() for f in ('core.js', 'qrstream.js', 'audio.js', 'ui.js'))
open(os.path.join(DIST, 'crushchan.js'), 'w').write(bundle)
import shutil
os.makedirs(os.path.join(DIST, 'vendor'), exist_ok=True)
for f in ('qrcodegen.js', 'jsQR.min.js', 'zxing-reader.iife.js', 'zxing_reader.wasm.b64.js', 'jsQR.LICENSE', 'zxing-wasm.LICENSE', 'VERSIONS.txt'):
    shutil.copy(os.path.join(JS, 'vendor', f), os.path.join(DIST, 'vendor', f))
print(f'crushchan.js                 {len(bundle.encode()):>9,d} bytes (+ dist/vendor/)')
