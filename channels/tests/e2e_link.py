"""Link mode in headless Chromium: navigate to dist/link.html#C1.<...> for several sizes and
check the page's bytes (byte-exact), plus Python make/read round trip, truncation detection,
and that no network request is made. Run: python3 tests/e2e_link.py"""
import base64, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan.link import make_link, read_link
from playwright.sync_api import sync_playwright
cases = [('a_tiny.crush', open(os.path.join(ROOT, 'testdata/a_tiny.crush'), 'rb').read()),
         ('a_docx.crush', open(os.path.join(ROOT, 'testdata/a_docx.crush'), 'rb').read()),
         ('a_photo.crush', open(os.path.join(ROOT, 'testdata/a_photo.crush'), 'rb').read())]
for n in (1_000_000, 1_500_000, 1_572_800):
    cases.append((f'random {n:,}', os.urandom(n)))
fails = 0
base = 'file://' + os.path.join(ROOT, 'dist/link.html')
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page()
    ext = []
    pg.on('request', lambda r: ext.append(r.url[:60]) if not r.url.startswith(('file:', 'data:', 'blob:')) else None)
    for name, data in cases:
        url = make_link(data, base)
        if len(url) < 2_000_000: assert read_link(url) == data
        try:
            pg.goto('about:blank'); pg.goto(url, timeout=60000); time.sleep(0.3)
            r = pg.evaluate('''() => { const r = CrushChannels.lastResult; return { len: location.href.length, done: !!r, stat: document.getElementById('stat').textContent,
                b64: r ? (() => { let s=''; for (let i=0;i<r.bytes.length;i+=8192) s += String.fromCharCode.apply(null, Array.from(r.bytes.subarray(i,i+8192))); return btoa(s); })() : null }; }''')
            ok = r['done'] and base64.b64decode(r['b64']) == data
            info = f"href length seen by page {r['len']:,}; {r['stat'][:60]}"
        except Exception as e:
            ok, info = False, f'navigation failed: {str(e)[:100]}'
        print(f'{name:18s} url {len(url):>10,} chars: {"OK" if ok else "FAIL"}  ({info})', flush=True)
        if len(url) <= 2 * 1024 * 1024: fails += not ok   # longer must fail (Chromium cap), and does
    # truncation must be detected, never yield bytes
    url = make_link(cases[1][1], base)
    pg.goto('about:blank'); pg.goto(url[:-50])
    st = pg.evaluate("() => [!!CrushChannels.lastResult, document.getElementById('stat').textContent]")
    print('truncated link ->', st); fails += st[0]
    b.close()
print('external requests:', ext); fails += bool(ext)
print('FAILURES', fails); sys.exit(1 if fails else 0)
