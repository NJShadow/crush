import sys, os
from playwright.sync_api import sync_playwright
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    for (w, h, tag, dark) in ((390, 844, "phone", False), (1100, 900, "desktop", False), (390, 844, "phone-dark", True)):
        ctx = b.new_context(viewport={"width": w, "height": h}, device_scale_factor=2 if "phone" in tag else 1,
                            color_scheme="dark" if dark else "light", accept_downloads=True)
        pg = ctx.new_page()
        pg.goto("file://" + os.path.abspath("work/locked.html"))
        pg.wait_for_selector("body[data-state=locked]", state="attached")
        pg.screenshot(path=f"{out}/{tag}-locked.png", full_page=True)
        pg.fill("#pw", "wrong one"); pg.click("#unlockbtn")
        pg.wait_for_selector("body[data-state=unlock-failed]", state="attached")
        pg.screenshot(path=f"{out}/{tag}-wrongpw.png")
        pg.goto("file://" + os.path.abspath("work/mixed.html"))
        pg.wait_for_selector("body[data-state=checked]", state="attached")
        pg.screenshot(path=f"{out}/{tag}-mixed.png", full_page=True)
        ctx.close()
    b.close()
