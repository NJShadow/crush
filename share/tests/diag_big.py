"""Watch a big share page open: state + status every 10 s, crashes, errors."""
import sys, os, time
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.on("crash", lambda: print("RENDERER CRASHED", flush=True))
    pg.on("console", lambda m: print("console", m.type, m.text[:200], flush=True))
    pg.on("pageerror", lambda e: print("pageerror", str(e)[:200], flush=True))
    t0 = time.time()
    try:
        pg.goto("file://" + os.path.abspath(sys.argv[1]), timeout=600000)
    except Exception as e:
        print("goto", e)
    print("loaded", round(time.time() - t0, 1), flush=True)
    for i in range(40):
        try:
            st = pg.get_attribute("body", "data-state")
            print(round(time.time() - t0), st, pg.inner_text("#status")[:100], flush=True)
            if st in ("checked", "error"): break
        except Exception as e:
            print("ERR", str(e)[:150], flush=True); break
        time.sleep(10)
    b.close()
