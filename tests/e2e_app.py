"""End-to-end test of the Crush interface against a real running host.

Covers the three things that were reported broken or missing:
  1. the page reaches the engine at all (the "ping is not a function" failure)
  2. dropping a file ingests it and does NOT open a file dialog
  3. individual files can be extracted out of a .crush archive
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
import asyncio, os, re, shutil, subprocess, sys, time, json
from pathlib import Path
from playwright.async_api import async_playwright

# Use a pinned browser if one is present, else whatever Playwright installed.
_PINNED = os.environ.get('CRUSH_CHROMIUM', '/opt/pw-browsers/chromium')
_LAUNCH = {'executable_path': _PINNED} if os.path.exists(_PINNED) else {}


import tempfile
WORK = Path(tempfile.gettempdir()) / "crush-e2e"
SRC = WORK / "src"; OUT = WORK / "out"


def build_corpus():
    shutil.rmtree(WORK, ignore_errors=True)
    (SRC / "sub").mkdir(parents=True)
    (SRC / "notes.txt").write_text("crush the bytes\n" * 4000)
    (SRC / "sub" / "table.csv").write_text("i,sq\n" + "".join(f"{i},{i*i}\n" for i in range(3000)))
    (SRC / "sub" / "copy.txt").write_text("crush the bytes\n" * 4000)   # duplicate
    (SRC / "data.json").write_text("[" + ",".join('{"i":%d}' % i for i in range(3000)) + "]")
    OUT.mkdir(parents=True)


def start_host():
    env = dict(os.environ, CRUSH_NO_BROWSER="1")
    p = subprocess.Popen([sys.executable, APP, "--browser"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    t0 = time.time()
    while time.time() - t0 < 20:
        line = p.stdout.readline()
        if not line:
            break
        m = re.search(r"http://127\.0\.0\.1:\d+/\?k=[\w\-]+", line)
        if m:
            url = m.group(0); break
    assert url, "host never printed its URL"
    return p, url


FAILS = []
def check(name, ok, extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + extra) if extra else ''}")
    if not ok:
        FAILS.append(name)


async def main():
    build_corpus()
    proc, url = start_host()
    print(f"host at {url}\n")
    try:
        async with async_playwright() as pw:
            br = await pw.chromium.launch(**_LAUNCH)
            pg = await br.new_page(viewport={"width": 1180, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

            # ---- 1. boot -------------------------------------------------
            await pg.goto(url)
            await pg.wait_for_function("document.getElementById('boot').style.display==='none'",
                                       timeout=15000)
            tiers = await pg.eval_on_selector_all("[data-tier]", "e=>e.length")
            check("interface reaches the engine", tiers == 5, f"{tiers} tiers")
            check("no JavaScript errors on boot", not errs, str(errs[:2]))

            # ---- 2. drop -------------------------------------------------
            # A dialog would appear as an api call to pick_files; watch for it.
            await pg.evaluate("""() => {
              window.__dialogs = 0;
              const orig = window.crushApi.pick_files;
              window.crushApi.pick_files = (...a) => { window.__dialogs++; return orig(...a); };
              const of = window.crushApi.pick_folder;
              window.crushApi.pick_folder = (...a) => { window.__dialogs++; return of(...a); };
            }""")
            body_a = (SRC / "notes.txt").read_text()
            body_b = (SRC / "sub" / "table.csv").read_text()
            await pg.evaluate("""async ([a, b]) => {
              const dt = new DataTransfer();
              dt.items.add(new File([a], "notes.txt",  {type:"text/plain"}));
              dt.items.add(new File([b], "table.csv", {type:"text/csv"}));
              const dz = document.getElementById("dz");
              dz.dispatchEvent(new DragEvent("dragenter", {dataTransfer: dt, bubbles:true}));
              dz.dispatchEvent(new DragEvent("drop",      {dataTransfer: dt, bubbles:true}));
            }""", [body_a, body_b])
            await pg.wait_for_function(
                "document.getElementById('stage-ready').style.display!=='none'", timeout=20000)
            qn = await pg.eval_on_selector_all("#qlist .item", "e=>e.length")
            dialogs = await pg.evaluate("window.__dialogs")
            check("drop ingests the files", qn == 2, f"{qn} queued")
            check("drop opens NO file dialog", dialogs == 0, f"{dialogs} dialog(s)")
            meta = await pg.eval_on_selector("#qmeta", "e=>e.textContent")
            check("dropped bytes actually arrived", "KB" in meta or "MB" in meta, meta.strip())

            # ---- 3. compress ---------------------------------------------
            arc = str(OUT / "e2e.crush")
            await pg.evaluate("""p => {
              window.crushApi.suggest_output = async () => p;
              window.crushApi.choose_output  = async () => p;
              window.crushApi.choose_dest    = async () => window.__dest;
            }""", arc)
            await pg.click("[data-tier='balanced']")
            await pg.click("#b-go")
            await pg.wait_for_function(
                "document.getElementById('stage-done').style.display!=='none'", timeout=180000)
            pct = await pg.eval_on_selector("#r-pct", "e=>e.textContent")
            check("compression finishes", os.path.exists(arc), f"{pct} · {os.path.getsize(arc):,} B")

            # ---- 3b. share: web page, paper, sound -----------------------
            for kind, btn, ext in (("page", "#b-sh-page", ".html"),
                                   ("paper", "#b-sh-paper", ".paper.pdf"),
                                   ("audio", "#b-sh-audio", ".wav")):
                await pg.click(btn)
                await pg.wait_for_function(
                    "(()=>{const t=document.getElementById('r-sharemsg').textContent;"
                    "return /saved next to|did not work|needs|suits|Error|error/.test(t)})()",
                    timeout=600000)
                msg = await pg.eval_on_selector("#r-sharemsg", "e=>e.textContent")
                outp = str(OUT / ("e2e" + ext))
                check(f"share as {kind}", os.path.exists(outp) and "saved next to" in msg, msg.strip()[:90])
            # the page must actually open and verify every file
            b2 = await pw.chromium.launch(**_LAUNCH)
            sp = await b2.new_page()
            await sp.goto("file://" + str(OUT / "e2e.html"))
            await sp.wait_for_selector("body[data-state=checked], body[data-state=error]",
                                       state="attached", timeout=120000)
            st = await sp.inner_text("#status")
            check("share page opens and checks every file", "exactly the files" in st, st[:80])
            await b2.close()
            # the paper backup restores byte-exact from its own PDF, and the sound decodes
            rp = subprocess.run([sys.executable, "-m", "crushchan.paper", "restore",
                                 str(OUT / "e2e.paper.out"), "--pdf", str(OUT / "e2e.paper.pdf")],
                                cwd=str(ROOT / "channels" / "py"), capture_output=True, text=True)
            ok = os.path.exists(OUT / "e2e.paper.out") and \
                 open(OUT / "e2e.paper.out", "rb").read() == open(arc, "rb").read()
            check("paper backup restores byte-exact", ok, (rp.stdout or rp.stderr).strip()[-80:])
            ra = subprocess.run([sys.executable, "-m", "crushchan.audio", "decode",
                                 str(OUT / "e2e.wav"), str(OUT / "e2e.wav.out")],
                                cwd=str(ROOT / "channels" / "py"), capture_output=True, text=True)
            ok = os.path.exists(OUT / "e2e.wav.out") and \
                 open(OUT / "e2e.wav.out", "rb").read() == open(arc, "rb").read()
            check("sound decodes byte-exact", ok, (ra.stdout or ra.stderr).strip()[-80:])

            # ---- 4. selective extraction ---------------------------------
            # Use a richer archive built from the whole corpus, including a
            # duplicate file, so the dedup path is exercised through the UI.
            arc2 = str(OUT / "corpus.crush")
            subprocess.run([sys.executable, str(Path(ENGINE) / "crush_engine.py"),
                            "c", arc2, str(SRC), "--tier", "balanced"],
                           check=True, capture_output=True)
            dest = str(OUT / "picked")
            await pg.evaluate("""([a, d]) => {
              window.__dest = d;
              window.crushApi.choose_dest = async () => d;
              window.crushApi.pick_archive = async () => window.crushApi.inspect(a);
            }""", [arc2, dest])
            await pg.click(".navbtn[data-nav='open']")
            await pg.click("#b-pickarch")
            await pg.wait_for_selector("#archlist .ck")
            rows = await pg.eval_on_selector_all("#archlist .ck", "e=>e.length")
            check("archive lists its files with checkboxes", rows == 4, f"{rows} rows")

            disabled = await pg.eval_on_selector("#b-extract-sel", "e=>e.disabled")
            check("'Extract selected' is off until something is picked", disabled is True)

            await pg.fill("#arch-q", "sub/")
            filt = await pg.eval_on_selector_all("#archlist .ck", "e=>e.length")
            check("the filter narrows the list", filt == 2, f"{filt} rows match 'sub/'")

            await pg.check("#archlist .ck[data-p='src/sub/copy.txt']")
            label = await pg.eval_on_selector("#b-extract-sel", "e=>e.textContent.trim()")
            check("the button counts the selection", "1 selected" in label, label)

            await pg.click("#b-extract-sel")
            await pg.wait_for_selector("#b-showdest", timeout=120000)
            got = sorted(str(p.relative_to(dest)) for p in Path(dest).rglob("*") if p.is_file())
            check("only the chosen file was written", got == ["src/sub/copy.txt"], str(got))
            same = (Path(dest) / "src/sub/copy.txt").read_bytes() == (SRC / "sub" / "copy.txt").read_bytes()
            check("the chosen file is byte-exact", same)
            stray = [p for p in Path(dest).rglob(".crush-src-*")]
            check("no scratch files left behind", not stray, str(stray))

            # ---- 5. extract everything still works -----------------------
            dest2 = str(OUT / "all")
            await pg.evaluate("d => { window.crushApi.choose_dest = async () => d; }", dest2)
            await pg.click("#b-backarch")
            await pg.wait_for_selector("#b-extract")
            await pg.click("#b-extract")
            await pg.wait_for_selector("#b-showdest", timeout=120000)
            ok = True
            for p in SRC.rglob("*"):
                if p.is_file():
                    q = Path(dest2) / "src" / p.relative_to(SRC)
                    if not q.exists() or q.read_bytes() != p.read_bytes():
                        ok = False; print("     mismatch:", p)
            check("extract everything is byte-exact", ok)

            # ---- 6. extracting over existing files asks first -------------
            victim = Path(dest2) / "src" / "notes.txt"
            victim.write_text("edited after the backup")
            await pg.click("#b-backarch")
            await pg.wait_for_selector("#b-extract")
            await pg.click("#b-extract")
            await pg.wait_for_selector("#b-x-replace", timeout=120000)
            msg = await pg.eval_on_selector("#archinfo", "e=>e.textContent")
            check("existing files: Crush asks instead of replacing",
                  victim.read_text() == "edited after the backup" and "Nothing has been" in msg,
                  msg.strip()[:80])
            await pg.click("#b-x-replace")
            await pg.wait_for_selector("#b-showdest", timeout=120000)
            check("'Replace them' restores the archived version",
                  victim.read_bytes() == (SRC / "notes.txt").read_bytes())
            check("still no JavaScript errors", not errs, str(errs[:3]))
            await br.close()
    finally:
        proc.terminate()

    print("\n  " + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
