"""The browser sender page renders codes that decode (zbar) to exactly the packets
the Python reference encoder produces, for several versions/ECC levels.
Run: python3 tests/e2e_stream_send.py"""
import base64, io, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'py'))
from crushchan.core import fountain_packet, layout
from crushchan.qr import symbol_size, zbar_decode
from PIL import Image
from playwright.sync_api import sync_playwright

data = open(os.path.join(ROOT, 'testdata/a_docx.crush'), 'rb').read()
fails = 0
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.goto('file://' + os.path.join(ROOT, 'dist/stream-send.html'))
    pg.evaluate('b64 => { const s = atob(b64); const u = new Uint8Array(s.length); for (let i=0;i<s.length;i++) u[i]=s.charCodeAt(i); window._bytes = u; }',
                base64.b64encode(data).decode())
    for V, e in [(10, 'l'), (15, 'm'), (20, 'l'), (25, 'l'), (30, 'm'), (40, 'l')]:
        T = symbol_size(V, e)
        Tjs = pg.evaluate(f'CrushChannels.qr.symbolSize({V}, "{e}")')
        ktot, B, kb = layout(len(data), T)
        for n in [0, 1, ktot - 1, ktot, ktot + 7, 5000]:
            url = pg.evaluate(f'''() => {{ const s = new CrushChannels.stream.Sender(document.createElement('canvas'), window._bytes, {{version:{V}, ecl:"{e}"}});
                s.n = {n}; s.frame(); const c = document.createElement('canvas'); const k = 4; c.width = s.canvas.width*k; c.height = s.canvas.height*k;
                const x = c.getContext('2d'); x.imageSmoothingEnabled = false; x.drawImage(s.canvas, 0, 0, c.width, c.height); return c.toDataURL(); }}''')
            img = Image.open(io.BytesIO(base64.b64decode(url.split(',')[1]))).convert('L')
            got = zbar_decode(img)
            exp = fountain_packet(data, T, n % B, n // B)
            ok = T == Tjs and got == [exp]
            fails += not ok
            print(f'V{V}-{e.upper()} T={T} (js {Tjs}) frame {n}: {"OK" if ok else "FAIL"}  size {img.size[0]//4} modules+quiet')
    b.close()
print('FAILURES', fails)
sys.exit(1 if fails else 0)
