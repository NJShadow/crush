"""End-to-end: open share files in headless Chromium, extract every file
(the 'Save all' zip, plus individual saves), compare bytes with the sources.

    python3 tests/e2e_share.py CASE.html[:passphrase] ...   (expects CASE's
    archive manifest at work/arch/<name>.json, named by data-arch on argv)

Each argument is  html=PATH arch=JSON [pw=PASS] [wrongpw=1] [singles=N]
"""
import hashlib, io, json, os, sys, time, zipfile
from playwright.sync_api import sync_playwright

def parse(arg):
    d = {}
    for part in arg.split(","):
        k, v = part.split("=", 1); d[k] = v
    return d

def run(cases, headless=True, timeout_s=600):
    results = []
    with sync_playwright() as p:
        for c in cases:
            # one browser per case: in a long session, Chromium sometimes stopped
            # delivering download events after many saves (see REPORT.md)
            b = p.chromium.launch(headless=headless)
            dev = dict(p.devices[c["device"]]) if c.get("device") else {}
            dev.pop("default_browser_type", None)
            ctx = b.new_context(accept_downloads=True, **dev)
            page = ctx.new_page()
            reqs, errors = [], []
            page.on("request", lambda r: reqs.append(r.url))
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and errors.append(m.text))
            info = json.load(open(c["arch"]))
            t0 = time.time()
            page.goto("file://" + os.path.abspath(c["html"]), timeout=timeout_s * 1000)
            t_load = time.time() - t0
            r = {"case": os.path.basename(c["html"]) + (" @" + c["device"] if c.get("device") else ""), "html_bytes": os.path.getsize(c["html"])}
            if c.get("wrongpw"):
                page.wait_for_selector("body[data-state=locked]", state="attached", timeout=timeout_s * 1000)
                page.fill("#pw", "definitely not it")
                t1 = time.time()
                page.click("#unlockbtn")
                page.wait_for_selector("body[data-state=unlock-failed]", state="attached", timeout=timeout_s * 1000)
                r["wrongpw_seconds"] = round(time.time() - t1, 2)
                r["wrongpw_status"] = page.inner_text("#status")
                r["wrongpw_list_hidden"] = page.is_hidden("#listcard")
            if c.get("pw"):
                page.wait_for_selector("body[data-state=locked], body[data-state=unlock-failed]", state="attached", timeout=timeout_s * 1000)
                page.fill("#pw", c["pw"])
                t1 = time.time()
                page.click("#unlockbtn")
            page.wait_for_selector("body[data-state=checked], body[data-state=error], body[data-state=ready]", state="attached", timeout=timeout_s * 1000)
            if page.get_attribute("body", "data-state") == "ready":       # big archive: checking waits for a click
                r["needed_click"] = True
                page.click("#checkbtn")
                page.wait_for_selector("body[data-state=checked], body[data-state=error]", state="attached", timeout=timeout_s * 1000)
            r["open_to_checked_s"] = round(time.time() - t0, 2)
            r["load_s"] = round(t_load, 2)
            r["state"] = page.get_attribute("body", "data-state")
            r["status"] = page.inner_text("#status")
            r["good"] = int(page.get_attribute("body", "data-good") or 0)
            r["bad"] = int(page.get_attribute("body", "data-bad") or 0)
            want = {k: v["sha"] for k, v in info["files"].items()}
            # Save all
            mism = []
            if r["state"] == "checked" and r["good"]:
                with page.expect_download(timeout=timeout_s * 1000) as dl:
                    page.click("#saveall")
                path = dl.value.path()
                data = open(path, "rb").read()
                r["saveall_name"] = dl.value.suggested_filename
                if len(want) == 1:
                    got = {list(want)[0]: hashlib.sha256(data).hexdigest()}
                else:
                    z = zipfile.ZipFile(io.BytesIO(data))
                    if z.testzip() is not None:
                        mism.append("zip CRC failure")
                    got = {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}
                r["zip_members"] = len(got)
                for k, h in want.items():
                    if got.get(k) != h:
                        mism.append(k)
                # individual saves
                n_single = int(c.get("singles", 5))
                btns = page.query_selector_all("li.file button.save:not([disabled])")
                singles_ok = 0
                for btn in btns[:n_single]:
                    title = btn.get_attribute("title")
                    with page.expect_download(timeout=180000) as dl:
                        btn.click()
                    sd = open(dl.value.path(), "rb").read()
                    name = dl.value.suggested_filename
                    sh = hashlib.sha256(sd).hexdigest()
                    if any(os.path.basename(k) == name and h == sh for k, h in want.items()):
                        singles_ok += 1
                    else:
                        mism.append("single:" + name)
                r["singles_ok"] = f"{singles_ok}/{min(n_single, len(btns))}"
            r["byte_exact"] = f"{len(want) - len([m for m in mism if not m.startswith(('single:', 'zip'))])}/{len(want)}" if r["state"] == "checked" else "0"
            r["mismatches"] = mism[:10]
            r["network_requests"] = [u for u in reqs if not u.startswith("file://")]
            r["page_errors"] = errors[:5]
            results.append(r)
            print(json.dumps(r), flush=True)
            ctx.close()
            b.close()
    return results

if __name__ == "__main__":
    run([parse(a) for a in sys.argv[1:]])
