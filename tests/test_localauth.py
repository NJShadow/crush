"""The local-authentication model, re-tested after the audit finding.

The audit demonstrated a working attack: a second process on the machine, knowing
only the port (which was written to a world-readable log), fetched the page from
an unauthenticated GET, scraped the API token out of it, and then drove every
method. These are the checks that it cannot any more, plus the browser-side
checks that must still hold.
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
import os, re, subprocess, sys, threading, time, json
import urllib.request, urllib.error




env = dict(os.environ, CRUSH_NO_BROWSER="1")
proc = subprocess.Popen([sys.executable, APP, "--browser"], env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
found = {}
def rd():
    for line in proc.stdout:
        m = re.search(r"http://127\.0\.0\.1:(\d+)/\?k=([\w\-]+)", line)
        if m and "url" not in found:
            found["url"], found["port"], found["key"] = m.group(0), m.group(1), m.group(2)
threading.Thread(target=rd, daemon=True).start()
t0 = time.time()
while time.time() - t0 < 25 and "url" not in found:
    time.sleep(0.2)
if "url" not in found:
    proc.terminate(); raise SystemExit("host never started")

PORT, KEY, URL = found["port"], found["key"], found["url"]
BASE = f"http://127.0.0.1:{PORT}/"

FAILS = []
def check(name, ok, extra=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + extra) if extra else ''}")
    if not ok:
        FAILS.append(name)

def req(url, headers=None, data=None):
    r = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()

try:
    # ---- the attack the audit demonstrated ------------------------------
    code, body = req(BASE)
    check("the page is refused without the launch key", code == 403, f"HTTP {code}")
    check("no token can be scraped from that response", b"TOKEN" not in body)

    code, _ = req(BASE + "?k=" + "A" * len(KEY))
    check("a wrong launch key is refused", code == 403, f"HTTP {code}")

    log = open(LOG, encoding="utf-8").read()
    check("the launch key is not in the log", KEY not in log and "?k=" not in log)
    # POSIX modes only mean something where the filesystem honours them. On
    # Windows they do not exist, and some network and container mounts silently
    # ignore chmod. Probe first, so this reports a real finding rather than a
    # property of the disk Crush happens to be sitting on.
    mode = oct(os.stat(LOG).st_mode & 0o777)
    honours_modes = True
    if os.name == "nt":
        honours_modes = False
    else:
        probe = os.path.join(os.path.dirname(LOG) or ".", ".crush-mode-probe")
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
            os.chmod(probe, 0o600)
            honours_modes = (os.stat(probe).st_mode & 0o777) == 0o600
        except OSError:
            honours_modes = False
        finally:
            try:
                os.unlink(probe)
            except OSError:
                pass
    if honours_modes:
        check("the log is not world-readable", mode == "0o600", mode)
    else:
        print(f"  SKIP  the log is not world-readable  "
              f"(this filesystem ignores POSIX modes; log is {mode})")

    # ---- with the key, the page works and carries the API token ---------
    code, page = req(URL)
    check("the page is served to the holder of the launch key", code == 200, f"HTTP {code}")
    m = re.search(rb'var TOKEN = "([^"]+)"', page)
    check("the page carries an API token", bool(m))
    TOKEN = m.group(1).decode() if m else ""

    # ---- the API still refuses everything it should ---------------------
    code, _ = req(BASE + "api/ping", {"Content-Type": "application/json"}, b"[]")
    check("an API call without the token is refused", code == 403, f"HTTP {code}")

    code, _ = req(BASE + "api/ping",
                  {"Content-Type": "application/json", "X-Crush-Token": "x" * len(TOKEN)}, b"[]")
    check("a wrong token is refused", code == 403, f"HTTP {code}")

    hdr = {"Content-Type": "application/json", "X-Crush-Token": TOKEN}
    code, body = req(BASE + "api/ping", hdr, b"[]")
    check("the real token works", code == 200 and b'"ok": true' in body.lower().replace(b"true", b"true"),
          f"HTTP {code}")

    code, _ = req(BASE + "api/ping", dict(hdr, Origin="https://evil.example"), b"[]")
    check("a foreign Origin is refused", code == 403, f"HTTP {code}")

    code, _ = req(BASE + "api/ping", dict(hdr, Origin=f"http://127.0.0.1:{PORT}"), b"[]")
    check("our own Origin is allowed (the app itself sends it)", code == 200, f"HTTP {code}")

    code, _ = req(BASE + "api/ping", dict(hdr, Host="evil.example"), b"[]")
    check("a rebound Host is refused", code == 403, f"HTTP {code}")

    code, _ = req(BASE + "api/_vault_get", hdr, b'["x"]')
    check("private methods are not reachable", code == 404, f"HTTP {code}")

    code, _ = req(BASE + "api/lock", hdr, b"[]")
    check("a public method is reachable with the token", code == 200, f"HTTP {code}")
finally:
    proc.terminate()

print("\n  " + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
