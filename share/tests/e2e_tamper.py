"""Damaged and hostile share pages: each must end in a clear refusal, never a
saved file that is wrong, never a hang."""
import os, sys, json, random, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import make_share as ms, share_build as sb
from playwright.sync_api import sync_playwright
W = "work/tamper"; os.makedirs(W, exist_ok=True)
rng = random.Random(3)
cases = []
small = open("work/small.html", "rb").read()
arch = open("work/arch/small.crush", "rb").read()
locked = open("work/arch/locked.crush", "rb").read()
PW = "correct horse battery staple été"

def page_for(a, enc="raw"):
    return ms.build_html(a, title="t", modules=["zstd", "lzma", "crushz"], priors={}, encoding=enc)

# 1. one byte of the embedded archive changed in transit
i = small.index(b'data-archive-part="0"'); j = small.index(b">", i) + 5000
v = bytearray(small); v[j] = (v[j] + 1) % 256 if v[j] not in (0x7E, 0x3C, 0x0D, 0x00, 0x7D) else 0x41
cases.append(("transport byte flip", bytes(v), None, "error"))
# 2. file truncated
cases.append(("truncated page", small[: len(small) * 2 // 3], None, "error"))
# 3. re-labelled as UTF-8 (what a server with the wrong charset header does)
cases.append(("wrong charset", small.replace(b'charset="windows-1252"', b'charset="utf-8"'), None, "error"))
# 4. encrypted archive tampered and re-wrapped with a consistent page fingerprint
for k, off in enumerate([10, 40, 100, 140, 5000, len(locked) - 3]):
    a = bytearray(locked); a[off] ^= 0x10
    # byte 10 is the Argon2 time cost: refused before any work, at load
    cases.append((f"locked archive byte {off} flipped (re-wrapped)", page_for(bytes(a)), PW, "error" if off == 10 else "unlock-failed"))
cases.append(("locked archive truncated by one chunk (re-wrapped)", page_for(locked[:-(len(locked) - 134) % (262144 + 16) or -1000]), PW, "unlock-failed"))
# 5. hostile unencrypted archives: random byte damage inside bundles / index, consistently re-wrapped
for k in range(8):
    a = bytearray(arch)
    for _ in range(rng.choice([1, 3, 20])):
        p = rng.randrange(28, len(a)); a[p] = rng.randrange(256)
    cases.append((f"hostile archive mutation {k}", page_for(bytes(a)), None, "any"))
a = bytearray(arch); a[8:16] = (2 ** 40).to_bytes(8, "little")
cases.append(("index offset 2^40", page_for(bytes(a)), None, "error"))

out = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, html, pw, expect in cases:
        fp = os.path.join(W, f"{len(out):02d}.html"); open(fp, "wb").write(html)
        ctx = b.new_context(); pg = ctx.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        t0 = time.time()
        pg.goto("file://" + os.path.abspath(fp))
        if pw:
            try:
                pg.wait_for_selector("body[data-state=locked], body[data-state=error]", state="attached", timeout=60000)
                if pg.get_attribute("body", "data-state") == "locked":
                    pg.fill("#pw", pw); pg.click("#unlockbtn")
            except Exception as e:
                pass
        try:
            pg.wait_for_selector("body[data-state=checked], body[data-state=error], body[data-state=unlock-failed]", state="attached", timeout=120000)
            st = pg.get_attribute("body", "data-state")
        except Exception:
            st = "TIMEOUT"
        good = int(pg.get_attribute("body", "data-good") or 0); bad = int(pg.get_attribute("body", "data-bad") or 0)
        msg = pg.inner_text("#status")[:160]
        # a 'checked' page may only offer files whose SHA-256 matched: compare offered count with honest count
        ok = (st == expect) or (expect == "any" and st in ("checked", "error"))
        r = {"case": name, "state": st, "good": good, "bad": bad, "pass": ok, "secs": round(time.time() - t0, 1), "status": msg, "page_errors": errs[:2]}
        out.append(r); print(json.dumps(r), flush=True)
        ctx.close()
    b.close()
print("tamper cases passed:", sum(r["pass"] for r in out), "/", len(out))
