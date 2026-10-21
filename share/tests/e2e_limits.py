"""Size limits: open big share pages in Chromium, check, save all, compare;
record time and peak RSS of all Chromium processes (sampled every 0.25 s)."""
import json, os, sys, threading, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_share

def chrome_rss():
    tot = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit(): continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
            if b"chrom" not in cmd: continue
            for l in open(f"/proc/{pid}/status"):
                if l.startswith("VmRSS:"): tot += int(l.split()[1]) * 1024
        except Exception: pass
    return tot

for html in sys.argv[1:]:
    peak = [0]; stop = [False]
    def mon():
        while not stop[0]:
            peak[0] = max(peak[0], chrome_rss()); time.sleep(0.25)
    t = threading.Thread(target=mon); t.start()
    try:
        r = e2e_share.run([{"html": html, "arch": html.replace(".html", ".json"), "singles": "1"}], timeout_s=1500)[0]
    except Exception as e:
        r = {"case": html, "error": str(e)[:300]}
    stop[0] = True; t.join()
    r["peak_chromium_rss_mb"] = round(peak[0] / 2**20)
    print("LIMIT", json.dumps(r), flush=True)
