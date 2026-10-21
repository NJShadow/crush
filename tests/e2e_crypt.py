"""The encryption flow, driven through the real interface in a real browser.

Covers what unit tests cannot: that the panel works, that the passphrase reaches
the engine, that a locked archive shows the lock screen and gives up nothing,
that a wrong passphrase is refused and a right one opens it, that extraction
works through the session, and that the passphrase is not left lying around.
"""

import os, sys
from pathlib import Path

# The project root is the folder above this one, wherever Crush has been put.
# Nothing here assumes a particular machine or operating system.
ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")
ENGINE = str(ROOT / "engine")
LOG = str(ROOT / "crush-debug.log")
sys.path.insert(0, ENGINE)
import asyncio, os, re, shutil, subprocess, sys, time
from pathlib import Path
from playwright.async_api import async_playwright

# Use a pinned browser if one is present, else whatever Playwright installed.
_PINNED = os.environ.get('CRUSH_CHROMIUM', '/opt/pw-browsers/chromium')
_LAUNCH = {'executable_path': _PINNED} if os.path.exists(_PINNED) else {}


import tempfile
W = Path(tempfile.gettempdir()) / "crush-e2e-crypt"
SRC = W / "secrets"; OUT = W / "out"
PW = "brine culvert casserole cask bubble ardent dormer gramme"

def build():
    shutil.rmtree(W, ignore_errors=True)
    (SRC / "inner").mkdir(parents=True); OUT.mkdir(parents=True)
    (SRC / "plans.txt").write_text("MERGER CODENAME BLUEBIRD\n" * 900)
    (SRC / "inner" / "ledger.csv").write_text(
        "id,amount\n" + "".join(f"{i},{i*37}\n" for i in range(4000)))

def start():
    env = dict(os.environ, CRUSH_NO_BROWSER="1")
    p = subprocess.Popen([sys.executable, APP, "--browser"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    while time.time() - t0 < 25:
        line = p.stdout.readline()
        if not line:
            break
        m = re.search(r"http://127\.0\.0\.1:\d+/\?k=[\w\-]+", line)
        if m:
            return p, m.group(0)
    raise SystemExit("host never printed its launch URL")

FAILS = []
def check(name, ok, extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + extra) if extra else ''}")
    if not ok:
        FAILS.append(name)

async def main():
    build()
    proc, url = start()
    arc = str(OUT / "locked.crush")
    dest = str(OUT / "restored")
    try:
        async with async_playwright() as pw:
            br = await pw.chromium.launch(**_LAUNCH)
            pg = await br.new_page(viewport={"width": 1180, "height": 1000})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            await pg.goto(url)
            await pg.wait_for_function(
                "document.getElementById('boot').style.display==='none'", timeout=20000)

            await pg.evaluate("""p => {
              window.crushApi.pick_files = async () => window.crushApi.add_paths([p]);
            }""", str(SRC))
            await pg.click("#b-files")
            await pg.wait_for_function(
                "document.getElementById('stage-ready').style.display!=='none'", timeout=20000)

            # ---- the encryption panel ------------------------------------
            av = await pg.evaluate("window.crushApi.crypto_available()")
            check("encryption is available", av.get("ok") and av.get("strong_kdf"),
                  str(av.get("kdf")))
            hidden = await pg.eval_on_selector("#crypt-body", "e=>e.style.display")
            check("the panel is closed by default", hidden == "none")

            await pg.click("#sw-crypt")
            await pg.fill("#pw1", "password")
            await pg.wait_for_timeout(500)
            weak = await pg.eval_on_selector("#pwnote", "e=>e.textContent")
            check("the meter calls a terrible passphrase terrible", "very weak" in weak,
                  weak.split("·")[0].strip())

            await pg.click("#b-pwgen")
            await pg.wait_for_timeout(500)
            gen = await pg.eval_on_selector("#pw1", "e=>e.value")
            note = await pg.eval_on_selector("#pwnote", "e=>e.textContent")
            check("Suggest produces a provably strong passphrase",
                  len(gen.split()) == 8 and "72" in note, f"{len(gen.split())} words")

            # mismatch must block, not silently encrypt with the wrong one
            await pg.fill("#pw1", PW)
            await pg.fill("#pw2", PW + "x")
            await pg.click("#b-go")
            await pg.wait_for_timeout(400)
            err = await pg.eval_on_selector("#errbox", "e=>e.textContent")
            check("a mismatched confirmation is refused", "do not match" in err, err.strip())

            # ---- compress with a passphrase ------------------------------
            await pg.fill("#pw2", PW)
            await pg.evaluate("""a => {
              window.crushApi.suggest_output = async () => a;
              window.crushApi.choose_output  = async () => a;
            }""", arc)
            await pg.click("#b-go")
            await pg.wait_for_function(
                "document.getElementById('stage-done').style.display!=='none'", timeout=180000)
            head = open(arc, "rb").read(8)
            check("the archive is written and is encrypted", head == b"CRUSHENC", str(head))
            blob = open(arc, "rb").read()
            check("no plaintext leaks into the archive",
                  b"BLUEBIRD" not in blob and b"ledger" not in blob and b"plans" not in blob)
            left = await pg.eval_on_selector("#pw1", "e=>e.value")
            left2 = await pg.eval_on_selector("#pw2", "e=>e.value")
            check("the page clears the passphrase after use", left == "" and left2 == "")

            # ---- the lock screen -----------------------------------------
            await pg.evaluate("""a => {
              window.crushApi.pick_archive = async () => window.crushApi.archive_info(a);
              window.crushApi.choose_dest  = async () => window.__dest;
              window.__dest = null;
            }""", arc)
            await pg.click(".navbtn[data-nav='open']")
            await pg.click("#b-pickarch")
            await pg.wait_for_selector("#lockpw", timeout=15000)
            meta = await pg.eval_on_selector(".lockmeta", "e=>e.textContent")
            body = await pg.eval_on_selector("#archinfo", "e=>e.textContent")
            check("the lock screen names the real algorithms",
                  "Argon2id" in meta and "XChaCha20-Poly1305" in meta)
            check("the lock screen reveals nothing about the contents",
                  "plans" not in body and "ledger" not in body and "2 files" not in body)

            await pg.fill("#lockpw", "not the passphrase")
            await pg.click("#b-unlock")
            await pg.wait_for_function(
                "document.getElementById('lockerr').textContent.length > 0", timeout=30000)
            lerr = await pg.eval_on_selector("#lockerr", "e=>e.textContent")
            still = await pg.query_selector("#lockpw")
            check("a wrong passphrase is refused and the screen stays", bool(still),
                  lerr.strip())
            cleared = await pg.eval_on_selector("#lockpw", "e=>e.value")
            check("a failed attempt clears the field", cleared == "")

            await pg.fill("#lockpw", PW)
            await pg.click("#b-unlock")
            await pg.wait_for_selector("#archlist .ck", timeout=60000)
            rows = await pg.eval_on_selector_all("#archlist .ck", "e=>e.length")
            check("the right passphrase opens it", rows == 2, f"{rows} files")
            badge = await pg.eval_on_selector_all(".pill.lock", "e=>e.length")
            check("the unlocked archive is marked as encrypted", badge == 1)

            # ---- extract through the session -----------------------------
            await pg.evaluate("d => { window.__dest = d; window.crushApi.choose_dest = async () => d; }", dest)
            await pg.click("#b-extract")
            await pg.wait_for_selector("#b-showdest", timeout=120000)
            ok = True
            for p in SRC.rglob("*"):
                if p.is_file():
                    q = Path(dest) / "secrets" / p.relative_to(SRC)
                    if not q.exists() or q.read_bytes() != p.read_bytes():
                        ok = False
            check("extraction through the unlocked session is byte-exact", ok)

            # ---- leaving the view re-locks -------------------------------
            await pg.click(".navbtn[data-nav='compress']")
            await pg.wait_for_timeout(400)
            sid = await pg.evaluate("STATE.session")
            check("leaving the archive view forgets the passphrase", sid is None)

            log = open(LOG, encoding="utf-8").read()
            check("the passphrase never reaches the log",
                  PW not in log and "brine" not in log)
            check("the launch key never reaches the log", "?k=" not in log)
            check("no JavaScript errors anywhere", not errs, str(errs[:2]))
            await br.close()
    finally:
        proc.terminate()
    print("\n  " + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0

sys.exit(asyncio.run(main()))
