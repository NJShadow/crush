#!/usr/bin/env python3
"""
Crush - desktop application host.

Two ways to show the interface, so a failure in one is not a dead app:

  1. NATIVE   a pywebview window (WebView2 on Windows). Preferred.
  2. BROWSER  your default browser.
              Used automatically if pywebview is missing or fails to start.

A loopback HTTP server runs in BOTH modes and the native window simply loads
its URL, so there is exactly one path from the interface to the engine. The
earlier build used pywebview's injected JavaScript bridge in native mode; that
bridge appears asynchronously, and calling it a moment too early produced
"api(...).ping is not a function" in a window that then never recovered.

Everything is logged to crush-debug.log next to this file, because pythonw.exe
discards stdout and stderr and an app that dies silently is impossible to fix.
"""
from __future__ import annotations

import os, sys, json, threading, traceback, socket, time, subprocess, webbrowser
import secrets, shutil, tempfile, urllib.parse, hmac
from pathlib import Path

# --------------------------------------------------------------------------
#  Paths - frozen (PyInstaller) and source layouts differ
# --------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    EXE_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent
    EXE_DIR = APP_DIR
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "engine"))
os.environ.setdefault("CRUSH_TOOLS", str(EXE_DIR / "tools"))
os.environ.setdefault("CRUSH_PRIORS", str(EXE_DIR / "priors"))

LOG_PATH = EXE_DIR / "crush-debug.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def log_exc(what: str) -> None:
    log(f"ERROR in {what}:\n{traceback.format_exc()}")


try:
    # 0600 from creation. The log names paths the user is working on, and a
    # world-readable file in the Crush folder is one more thing to get wrong.
    _fd = os.open(str(LOG_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(_fd)
    try:
        os.chmod(str(LOG_PATH), 0o600)
    except OSError:
        pass
except Exception:
    pass
log(f"Crush starting · python {sys.version.split()[0]} · frozen={getattr(sys,'frozen',False)}")
log(f"APP_DIR={APP_DIR}")
log(f"EXE_DIR={EXE_DIR}")

# --------------------------------------------------------------------------
#  Engine
# --------------------------------------------------------------------------
MISSING = []
try:
    import crush_engine as ce
    log("engine loaded")
except Exception:
    log_exc("importing the engine")
    ce = None
    for mod in ("numpy", "zstandard", "brotli", "pyppmd"):
        try:
            __import__(mod)
        except Exception:
            MISSING.append(mod)
    log(f"missing packages: {MISSING or 'none obvious'}")

try:
    import webview
    log(f"pywebview {getattr(webview, '__version__', '?')} available")
except Exception:
    webview = None
    log("pywebview not available - browser mode will be used")


TRANSFORM_LABELS = {
    "oracle":         "Program synthesis",
    "prism":          "Structural shred",
    "perc_image":     "Perceptual photo",
    "perc_audio":     "Perceptual audio",
    "deflate_unwrap": "Deflate unwrap",
    "zip_recompose":  "Container recompose",
    "png_unwrap":     "PNG stream unwrap",
    "pdf_unwrap":     "PDF stream unwrap",
    "gzip_unwrap":    "Gzip unwrap",
    "jpegxl":         "JPEG XL re-code",
    "jpeg_pack":      "JPEG recompression",
    "jpeg_embedded":  "Embedded JPEG recompression",
    "flac":           "FLAC",
    "audio_predict":  "Audio prediction",
    "transpose":      "Byte-plane transpose",
    "floatarray":     "Numeric array codec",
    "genome":         "Sequencing streams",
    "loom":           "Structure discovery",
}

PLAN_TEXT = {
    "zipfamily": "Unwrap the container, re-compress the real contents",
    "png":       "Unwrap the deflate stream to raw scanlines",
    "jpeg":      "Lossless recompression of the same coefficients (built in)",
    "wav":       "Linear-prediction transform, then entropy code the residuals",
    "exe_pe":    "x86 branch-address filter, then LZMA2",
    "exe_elf":   "x86 branch-address filter, then LZMA2",
    "text":      "Program search, structural shredding, then a shared-prior codec",
    "code":      "Structural shredding, solid bundle, shared-prior context mixing",
    "numeric":   "Program search, then byte-plane transposition",
    "generic":   "Program search, structure probe, then the codec tournament",
    "media":     "Already compressed - stored, never inflated",
    "archive":   "Already compressed - stored, never inflated",
    "gzip":      "Unwrap the gzip layer, re-compress the contents",
    "db":        "Solid bundle with record-aware filters",
    "pdf":       "Unwrap embedded streams where reproducible",
    "font":      "Solid bundle with the codec tournament",
    "image":     "Delta filter, then the codec tournament",
    "genomic":   "Split into identifiers, bases and qualities, each modelled separately",
}


# ==========================================================================
#  The API the interface calls
# ==========================================================================
class Api:
    def __init__(self):
        self.window = None
        self.queue = []
        self.last_result = None
        self._emit_lock = threading.Lock()
        self._events = []
        self.mode = "native"
        self.native_drop = False     # set once pywebview agrees to hand us paths
        # Passphrases live here and nowhere else: never on disk, never in the
        # log, and handed to the interface only as an opaque session id so the
        # passphrase itself crosses the boundary exactly once per archive.
        self._vault = {}             # session id -> {"pw", "path", "seen"}
        self._vault_lock = threading.Lock()
        self.drop_root = Path(tempfile.gettempdir()) / f"crush-drop-{os.getpid()}"

    # ------------------------------------------------------------ events
    def _emit(self, event, payload):
        """Push an event to the interface.

        Both modes now poll for these over the local HTTP server. Pushing them
        by evaluating JavaScript from a worker thread was the other half of the
        native-mode fragility: it depends on the pywebview bridge being alive in
        exactly the way that kept failing, and it fails silently when it is not.
        Buffering costs one small request every 300 ms and cannot deadlock."""
        with self._emit_lock:
            self._events.append({"e": event, "p": payload})
            if len(self._events) > 4000:
                del self._events[:-2000]

    def poll_events(self):
        with self._emit_lock:
            out, self._events = self._events, []
        return out

    def ping(self):
        return {"ok": True, "engine": ce is not None, "missing": MISSING,
                "mode": self.mode, "native_drop": bool(self.native_drop),
                "log": str(LOG_PATH)}

    # ------------------------------------------------------ capabilities
    def capabilities(self):
        if ce is None:
            return [{"id": "err", "name": "Engine not loaded", "ok": False,
                     "detail": "Missing packages: " + (", ".join(MISSING) or "see crush-debug.log"),
                     "note": "run: pip install numpy zstandard brotli pyppmd"}]
        n_pri = len([p for p in ("text", "code", "web", "data")
                     if ce._prior(p, ".zdict") or ce._prior(p, ".prime")])
        return [
            {"id": "core", "name": "Core engine", "ok": True, "note": "Always available",
             "detail": "Zstandard, LZMA2, Brotli, bzip2, deflate unwrapping, audio prediction, "
                       "byte-plane transposition"},
            {"id": "ppmd", "name": "PPMd", "ok": ce.pyppmd is not None,
             "detail": "Order-16 prediction by partial matching, strong on prose and code",
             "note": "Ready" if ce.pyppmd is not None else "pip install pyppmd"},
            {"id": "crushcm", "name": "Crush CM", "ok": bool(ce.HAVE.get("crushcm")),
             "detail": "Context mixing with a two-layer neural mixer. Smallest results, slowest",
             "note": "Ready" if ce.HAVE.get("crushcm") else "engine/crushcm.exe is missing"},
            {"id": "zpin", "name": "Pinned deflate", "ok": ce.zpin.available(),
             "detail": "Crush's own copy of zlib 1.3.1, checked against reference output at "
                       "startup. Rebuilds the original bytes of zip, docx, PNG, PDF and gzip "
                       "files exactly on any machine, whatever zlib Python itself ships",
             "note": ("Ready - " + ce.zpin.status()) if ce.zpin.available()
                     else "container unwrapping off: " + ce.zpin.status()},
            {"id": "reflate", "name": "Deflate reconstruction", "ok": ce.zpin.pf_available(),
             "detail": "preflate, for zip, PDF, PNG and gzip streams that zlib did not write "
                       "(GNU gzip, Chrome, Microsoft Office, 7-Zip...). Stores a small "
                       "correction record instead of leaving the stream compressed",
             "note": "Ready" if ce.zpin.pf_available()
                     else f"engine/{os.path.basename(ce.zpin.PF_PATH)} is missing"},
            {"id": "jpeg", "name": "JPEG recompression", "ok": ce._jpeg.available(),
             "detail": "Re-codes a JPEG's own coefficients with a far stronger model and rebuilds "
                       "the original file bit for bit: Brunsli at the balanced tier, Crush's own "
                       "context-mixing model (jcm) at Crush tier and above. Nothing to install",
             "note": "Ready" if ce._jpeg.available()
                     else f"engine/{ce._jpeg.NAME} is missing"},
            {"id": "nn", "name": "Learned code model",
             "ok": bool(ce.HAVE.get("crushnn")) and bool(ce._prior("code", ".cnn")),
             "detail": "A small neural network trained on source code, run with integer-only "
                       "arithmetic so every computer decodes it identically. Singularity tier, "
                       "source-code bundles up to 256 KB. About 18% smaller than the shared prior "
                       "on held-out Python; slow (about 6 KB/s)",
             "note": "Ready" if (ce.HAVE.get("crushnn") and ce._prior("code", ".cnn"))
                     else "engine/crushnn or priors/code.cnn is missing"},
            {"id": "oracle", "name": "Oracle - program synthesis", "ok": True,
             "detail": "Searches for the generator behind a file: counters, timestamps, linear "
                       "recurrences, congruential streams, repeated structure",
             "note": "Always available"},
            {"id": "prism", "name": "Prism - structural shredding", "ok": True,
             "detail": "Shreds JSON, CSV, logs and markup into per-field columns, then fits each "
                       "column's law where one exists",
             "note": "Always available"},
            {"id": "echo", "name": "Echo - chunk deduplication", "ok": True,
             "detail": "Content-defined chunking across the whole archive, past any codec's window",
             "note": "Engages above 24 MB"},
            {"id": "atlas", "name": "Atlas - shared priors", "ok": n_pri > 0,
             "detail": "Dictionaries and pretrained context models that ship with Crush, so the "
                       "archive carries only what the decoder does not already know",
             "note": f"{n_pri} prior sets loaded" if n_pri else "priors/ folder missing"},
            {"id": "vector", "name": "Vector - numeric array codec", "ok": ce.crush_vector is not None,
             "detail": "Float and integer arrays split into their real channels, then coded per "
                       "channel: XOR against the previous value, delta, byte planes, or the exact "
                       "decimal a float was printed from",
             "note": "Ready" if ce.crush_vector is not None else "crush_vector.py is missing"},
            {"id": "genome", "name": "Helix - sequencing data", "ok": ce._genome is not None,
             "detail": "FASTQ, FASTA, SAM and VCF split into identifiers, bases and quality "
                       "strings - three streams with nothing statistically in common",
             "note": "Ready" if ce._genome is not None else "crush_genome.py is missing"},
            {"id": "loom", "name": "Loom - structure discovery", "ok": ce.crush_loom is not None,
             "detail": "Infers the record layout of binary formats nobody wrote a parser for, "
                       "then codes each field on its own terms",
             "note": "Crush tier and above" if ce.crush_loom is not None else "crush_loom.py is missing"},
            {"id": "crypt", "name": "Encryption",
             "ok": ce.crush_crypt is not None,
             "detail": ("Argon2id key stretching, then XChaCha20-Poly1305 in STREAM mode. "
                        "The whole archive including its index is encrypted, so a locked "
                        "archive cannot be listed without the passphrase"
                        if ce.crush_crypt is not None and ce.crush_crypt.HAVE_ARGON2 else
                        "scrypt key stretching, then XChaCha20-Poly1305. Argon2id is stronger "
                        "and needs argon2-cffi"),
             "note": ("Ready" if ce.crush_crypt is not None and ce.crush_crypt.HAVE_ARGON2
                      else ("pip install argon2-cffi" if ce.crush_crypt is not None
                            else "pip install cryptography argon2-cffi"))},
            {"id": "perc", "name": "Perceptual tier",
             "ok": bool(ce.HAVE.get("cjxl") and ce.HAVE.get("djxl")) or bool(ce.HAVE.get("ffmpeg")),
             "detail": "Opt-in lossy transcoding for photos and audio, with PSNR and SSIM measured "
                       "per file against the original",
             "note": ("photos: " + ("yes" if ce.HAVE.get("cjxl") else "needs cjxl") +
                      " · audio: " + ("yes" if ce.HAVE.get("ffmpeg") else "needs ffmpeg"))},
        ]

    def tiers(self):
        if ce is None:
            return []
        return [{"id": k, **v} for k, v in ce.TIERS.items()]

    # ------------------------------------------------------- file input
    def pick_files(self):
        r = self._dialog("open")
        return self._stage(r or [])

    def pick_folder(self):
        r = self._dialog("folder")
        return self._stage(r or [])

    def pick_archive(self):
        r = self._dialog("open", types=("Crush archive (*.crush)", "All files (*.*)"))
        if not r:
            return None
        return self.inspect(r[0])

    def add_paths(self, paths):
        return self._stage(paths or [])

    # --------------------------------------------------------------- drop
    # A drop must ingest what was dropped. It must never open a file dialog -
    # that is the one thing the person did *not* ask for by dropping.
    #
    # Getting there takes two routes, because a web view is not allowed to see
    # a local path:
    #   1. NATIVE  - pywebview hands the real paths to Python (see attach_drop
    #                below). Nothing is copied; the files are read where they
    #                already are. This is the good path.
    #   2. FALLBACK- the page uploads the dropped bytes to this local server,
    #                preserving folder structure, and the copy is queued. Used
    #                in browser mode and on any pywebview too old to help.

    def drop_paths(self, paths):
        """Route 1: real paths, straight from the window manager."""
        paths = [p for p in (paths or []) if p]
        log(f"drop: {len(paths)} native path(s)")
        s = self._stage(paths)
        self._emit("drop_ingested", {"n": len(paths)})
        self._emit("summary", s)
        return s

    def begin_drop(self):
        """Route 2, step 1: open a staging folder for an upload."""
        did = secrets.token_hex(8)
        (self.drop_root / did).mkdir(parents=True, exist_ok=True)
        return {"id": did}

    def finish_drop(self, did):
        """Route 2, step 3: queue whatever landed in the staging folder."""
        d = self.drop_root / str(did)
        if not d.is_dir():
            return self.summary(quick=True)
        kids = sorted(d.iterdir())
        log(f"drop: {len(kids)} uploaded item(s) staged in {d}")
        s = self._stage([str(k) for k in kids])
        self._emit("summary", s)
        return s

    def _drop_dest(self, did, rel):
        """Resolve one uploaded file's path, refusing anything that climbs out
        of the staging folder. The relative path arrives from the page."""
        root = (self.drop_root / str(did)).resolve()
        if not str(root).startswith(str(self.drop_root.resolve())):
            raise ValueError("bad drop id")
        rel = (rel or "dropped").replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
        if not parts:
            parts = ["dropped"]
        out = root.joinpath(*parts).resolve()
        if not str(out).startswith(str(root) + os.sep):
            raise ValueError("upload escapes its staging folder")
        out.parent.mkdir(parents=True, exist_ok=True)
        return out

    def cleanup_drops(self):
        try:
            if self.drop_root.exists():
                shutil.rmtree(self.drop_root, ignore_errors=True)
        except Exception:
            pass

    def _dialog(self, kind, types=None, save_name=None):
        """Native dialog via pywebview, else tkinter, else nothing."""
        try:
            if self.window is not None and webview is not None:
                if kind == "open":
                    kw = {"allow_multiple": True}
                    if types:
                        kw["file_types"] = types
                    return self.window.create_file_dialog(webview.OPEN_DIALOG, **kw)
                if kind == "folder":
                    r = self.window.create_file_dialog(webview.FOLDER_DIALOG)
                    return list(r) if r else []
                if kind == "save":
                    r = self.window.create_file_dialog(
                        webview.SAVE_DIALOG, save_filename=save_name or "Archive.crush")
                    if isinstance(r, (list, tuple)):
                        r = r[0] if r else None
                    return [r] if r else []
        except Exception:
            log_exc("native file dialog")
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            try:
                if kind == "open":
                    r = list(filedialog.askopenfilenames())
                elif kind == "folder":
                    d = filedialog.askdirectory(); r = [d] if d else []
                else:
                    f = filedialog.asksaveasfilename(defaultextension=".crush",
                                                     initialfile=save_name or "Archive.crush")
                    r = [f] if f else []
            finally:
                root.destroy()
            return r
        except Exception:
            log_exc("tkinter file dialog")
        return []

    def _stage(self, paths):
        for p in paths:
            try:
                p = os.path.abspath(p)
            except Exception:
                continue
            if not os.path.exists(p) or any(q["path"] == p for q in self.queue):
                continue
            if os.path.isdir(p):
                self.queue.append({"path": p, "name": os.path.basename(p) or p,
                                   "dir": True, "files": 0, "size": 0, "scanning": True})
            else:
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                self.queue.append({"path": p, "name": os.path.basename(p),
                                   "dir": False, "files": 1, "size": sz, "scanning": False})
        # Scanning a folder can touch hundreds of thousands of files. Doing that
        # on the thread that serves the interface is what makes a window stop
        # responding, so it happens in the background and the result arrives as
        # an event.
        threading.Thread(target=self._scan_worker, daemon=True).start()
        return self.summary(quick=True)

    def clear(self):
        self.queue = []
        return self.summary(quick=True)

    def remove(self, path):
        self.queue = [q for q in self.queue if q["path"] != path]
        threading.Thread(target=self._scan_worker, daemon=True).start()
        return self.summary(quick=True)

    def _scan_worker(self):
        try:
            s = self.summary(quick=False)
            self._emit("summary", s)
        except Exception:
            log_exc("folder scan")
            self._emit("error", {"message": "Could not scan that folder:\n"
                                            + traceback.format_exc()})

    def _files_under(self, q, cap=200000):
        if not q["dir"]:
            return [q["path"]]
        out = []
        for dp, _, fs in os.walk(q["path"]):
            for f in fs:
                fp = os.path.join(dp, f)
                if os.path.isfile(fp) and not os.path.islink(fp):
                    out.append(fp)
                    if len(out) >= cap:
                        return out
        return out

    def summary(self, quick=False):
        """quick=True returns immediately with what is already known."""
        if quick:
            return {"queue": self.queue, "scanning": any(q.get("scanning") for q in self.queue),
                    "total_size": sum(q["size"] for q in self.queue),
                    "total_files": sum(q["files"] for q in self.queue),
                    "breakdown": []}
        breakdown = {}
        for q in self.queue:
            files = self._files_under(q)
            n = sz_total = 0
            for fp in files:
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                n += 1; sz_total += sz
                cls = "generic"
                if ce is not None:
                    try:
                        with open(fp, "rb") as f:
                            head = f.read(4096)
                        cls = ce.sniff(fp, head)
                    except Exception:
                        cls = "generic"
                b = breakdown.setdefault(cls, {"n": 0, "size": 0})
                b["n"] += 1; b["size"] += sz
            q["files"], q["size"], q["scanning"] = n, sz_total, False
        rows = [{"cls": k, "label": (ce.CLASS_LABELS.get(k, k) if ce else k),
                 "n": v["n"], "size": v["size"], "plan": PLAN_TEXT.get(k, "Codec tournament")}
                for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1]["size"])]
        return {"queue": self.queue, "scanning": False,
                "total_size": sum(q["size"] for q in self.queue),
                "total_files": sum(q["files"] for q in self.queue),
                "breakdown": rows}

    # ------------------------------------------------------------ actions
    def suggest_output(self):
        if not self.queue:
            return ""
        first = self.queue[0]
        base = first["name"] if first["dir"] else os.path.splitext(first["name"])[0]
        if len(self.queue) > 1:
            base = "Archive"
        return os.path.join(os.path.dirname(first["path"]), base + ".crush")

    def choose_output(self, default_name):
        r = self._dialog("save", save_name=default_name or "Archive.crush")
        return (r[0] if r else "") or ""

    def choose_dest(self):
        r = self._dialog("folder")
        return (r[0] if r else "") or ""

    def compress(self, out_path, tier, harder, perceptual=None,
                 password=None, kdf=None):
        if ce is None:
            return {"error": "The compression engine did not load. See crush-debug.log."}
        if not self.queue:
            return {"error": "Nothing to compress."}
        if not out_path:
            return {"error": "No output file chosen."}
        if password is not None:
            av = self.crypto_available()
            if not av["ok"]:
                return {"error": "Encryption is unavailable: " + av["reason"] +
                                 ".\nRun:  pip install cryptography argon2-cffi"}
            if not str(password):
                return {"error": "The passphrase is empty."}
        threading.Thread(target=self._compress_worker,
                         args=(out_path, tier, bool(harder), perceptual or None,
                               password, kdf),
                         daemon=True).start()
        return {"started": True}

    def _compress_worker(self, out_path, tier, harder, perceptual,
                         password=None, kdf=None):
        try:
            # The passphrase is deliberately absent from this line.
            log(f"compress -> {out_path} tier={tier} harder={harder} "
                f"perceptual={perceptual} encrypted={password is not None}")
            inputs = [q["path"] for q in self.queue]
            r = ce.compress(inputs, out_path, tier,
                            lambda s: self._emit("progress", s),
                            harder=harder, perceptual=perceptual,
                            password=password, kdf_profile=kdf)
            r["out_path"] = out_path
            r["report"] = self._report(out_path, password)
            r["share"] = self._share_info(out_path, password)
            st = r.get("stats", {})
            # what was left out or changed must be said, not just recorded
            r["warnings"] = {"skipped_links": st.get("skipped_links", 0),
                             "changed": (st.get("changed_while_reading") or [])[:20]}
            if password is not None:
                # kept (in the vault, 30 min) only so "Share" can build a page
                r["share_sid"] = self._vault_put(out_path, password)
            self.last_result = r
            log(f"compressed {r['in']:,} -> {r['out']:,} in {r['seconds']:.1f}s")
            self._emit("done", r)
        except Exception:
            log_exc("compression")
            self._emit("error", {"message": traceback.format_exc()})

    def _report(self, out_path, password=None):
        idx = ce.read_index(out_path, password)
        per, highlights = {}, []
        for f in idx["files"]:
            for st in f["t"]:
                d = None
                if st["n"] == "oracle":       d = ce.Oracle.describe(st["a"])
                elif st["n"] == "prism":      d = ce.Prism.describe(st["a"])
                elif st["n"] == "perc_image": d = ce.PerceptualImage.describe(st["a"])
                elif st["n"] == "perc_audio": d = ce.PerceptualAudio.describe(st["a"])
                if d:
                    kind = ("oracle" if st["n"] == "oracle" else
                            "prism" if st["n"] == "prism" else "perceptual")
                    highlights.append({"kind": kind, "file": f["p"], "size": f["s"], "detail": d})
        blank = lambda k: {"label": ce.CLASS_LABELS.get(k, k), "n": 0, "in": 0, "out": 0,
                           "transforms": {}, "codec": "", "exact": True}
        for f in idx["files"]:
            c = per.setdefault(f["cls"], blank(f["cls"]))
            c["n"] += 1; c["in"] += f["s"]
            for st in f["t"]:
                c["transforms"][st["n"]] = c["transforms"].get(st["n"], 0) + 1
            if not f.get("ex", True):
                c["exact"] = False
        for b in idx["bundles"]:
            c = per.setdefault(b["cls"], blank(b["cls"]))
            c["out"] += b["clen"]
            c["codec"] = ce.CODECS[b["codec"]][2]
        rows = []
        for k, v in sorted(per.items(), key=lambda kv: -kv[1]["in"]):
            if not v["in"]:
                continue
            v["cls"] = k
            v["pct"] = 100.0 * v["out"] / v["in"]
            v["transform_names"] = [TRANSFORM_LABELS.get(t, t) for t in v["transforms"]]
            rows.append(v)
        return {"rows": rows,
                "dedup_files": idx["stats"].get("dedup_files", 0),
                "dedup_bytes": idx["stats"].get("dedup_bytes", 0),
                "recomposed": idx["stats"].get("recomposed", 0),
                "echo_saved": idx["stats"].get("echo_saved", 0),
                "priors": sorted({b["prior"] for b in idx["bundles"] if b.get("prior")}),
                "highlights": sorted(highlights, key=lambda h: -h["size"])[:12]}

    # ------------------------------------------------------------ share
    # Round 11: an archive can leave Crush as a self-opening web page, a paper
    # backup of QR codes, or sound. Each one carries the archive bytes
    # unchanged and checks its SHA-256 before releasing anything.
    PAPER_MAX = 400 * 1024        # ~70 A4 pages
    AUDIO_MAX = 32 * 1024         # ~4 minutes at 132 B/s

    def _share_info(self, out_path, password=None):
        try:
            ms = _make_share_mod()
            req = ms.inspect(out_path, password)
            return {"page_ok": not (req and req["blocked"]),
                    "blocked": len(req["blocked"]) if req else 0,
                    "size": os.path.getsize(out_path)}
        except Exception as e:
            return {"page_ok": False, "error": str(e), "size": os.path.getsize(out_path)}

    def share(self, kind, out_path, session=None):
        """kind: 'page' | 'paper' | 'audio'. Writes next to the archive."""
        if ce is None:
            return {"error": "The compression engine did not load."}
        if not out_path or not os.path.exists(out_path):
            return {"error": "The archive is no longer there."}
        base = os.path.splitext(out_path)[0]
        try:
            if kind == "page":
                password = self._vault_get(session, out_path) if session else None
                ms = _make_share_mod()
                req = ms.inspect(out_path, password)
                src, tmp = out_path, None
                if req and req["blocked"]:
                    # Rebuild with only browser-decodable layers (portable=True),
                    # from the ARCHIVE's own contents - never from the original
                    # files, which may have changed since, and never from
                    # whatever happens to be queued now. The contents are
                    # restored (and verified) into a scratch folder beside the
                    # archive, re-packed, and the scratch folder is removed.
                    tier = ce.read_index(out_path, password).get("tier", "crush")
                    outdir = os.path.dirname(os.path.abspath(out_path))
                    scratch = tempfile.mkdtemp(prefix=".crush-share-", dir=outdir)
                    try:
                        x = ce.extract(out_path, scratch, password=password)
                        if x["failed"]:
                            return {"error": f"{len(x['failed'])} file(s) in this archive did not "
                                             "verify, so no page was made from it."}
                        tops = sorted(os.path.join(scratch, n) for n in os.listdir(scratch))
                        fd, tmp = tempfile.mkstemp(prefix=".crush-share-", suffix=".crush", dir=outdir)
                        os.close(fd); os.unlink(tmp)
                        ce.compress(tops, tmp, tier,
                                    lambda st: self._emit("progress", st),
                                    password=password, portable=True)
                    finally:
                        shutil.rmtree(scratch, ignore_errors=True)
                    src = tmp
                try:
                    page = ms.make_share(src, ms.free_name(base + ".html"), password=password)
                finally:
                    if tmp and os.path.exists(tmp):
                        os.unlink(tmp)
                return {"path": page, "bytes": os.path.getsize(page), "repacked": tmp is not None}
            data = open(out_path, "rb").read()
            chan = _channels_mod()
            if kind == "paper":
                if len(data) > self.PAPER_MAX:
                    return {"error": f"A paper backup suits archives up to {self.PAPER_MAX // 1024} KB "
                                     f"(about 70 pages); this one is {len(data) // 1024:,} KB."}
                from crushchan import paper
                path = _make_share_mod().free_name(base + ".paper.pdf")
                info = paper.make(data, path, title=os.path.basename(out_path))
                return {"path": path, "bytes": os.path.getsize(path), "pages": info["pages"],
                        "any_of": f"any {info['K']} of {info['N']} codes"}
            if kind == "audio":
                if len(data) > self.AUDIO_MAX:
                    return {"error": f"Sound suits archives up to {self.AUDIO_MAX // 1024} KB "
                                     f"(about 4 minutes); this one is {len(data) // 1024:,} KB."}
                from crushchan import audio
                path = _make_share_mod().free_name(base + ".wav")
                x, nf = audio.encode(data, 0.5, "echo")
                audio.write_wav(path, x)
                return {"path": path, "bytes": os.path.getsize(path),
                        "seconds": round(len(x) / audio.FS, 1)}
            return {"error": "unknown share kind"}
        except ImportError as e:
            return {"error": f"This needs an extra package ({e.name}). Run:  "
                             "python -m pip install segno reportlab pillow"}
        except Exception as e:
            log_exc("share " + kind)
            return {"error": str(e)}

    # ------------------------------------------------------------ vault
    VAULT_TTL = 30 * 60           # a forgotten window should not stay unlocked

    def _vault_put(self, path, password):
        sid = secrets.token_urlsafe(18)
        with self._vault_lock:
            self._vault_sweep()
            self._vault[sid] = {"pw": password, "path": os.path.abspath(path),
                                "seen": time.time()}
        return sid

    def _vault_get(self, sid, path=None):
        with self._vault_lock:
            self._vault_sweep()
            v = self._vault.get(sid or "")
            if not v:
                return None
            # A session unlocks one archive, not every archive.
            if path is not None and os.path.abspath(path) != v["path"]:
                return None
            v["seen"] = time.time()
            return v["pw"]

    def _vault_sweep(self):
        now = time.time()
        for k in [k for k, v in self._vault.items() if now - v["seen"] > self.VAULT_TTL]:
            self._vault.pop(k, None)

    def lock(self, sid=None):
        """Forget a passphrase. Called when the archive view is left."""
        with self._vault_lock:
            if sid:
                self._vault.pop(sid, None)
            else:
                self._vault.clear()
        return {"ok": True}

    # -------------------------------------------------------- passphrases
    def password_strength(self, pw):
        """Advice only - a passphrase is never rejected. Nothing is logged."""
        if ce is None or ce.crush_crypt is None:
            return {"bits": 0, "label": "", "level": 0, "notes": []}
        return ce.crush_crypt.password_strength(pw or "")

    def suggest_passphrase(self):
        if ce is None or ce.crush_crypt is None:
            return ""
        return ce.crush_crypt.suggest_passphrase()

    def crypto_available(self):
        cc = getattr(ce, "crush_crypt", None) if ce else None
        if cc is None:
            return {"ok": False, "reason": "the 'cryptography' package is not installed",
                    "kdf": None}
        return {"ok": True, "kdf": "argon2id" if cc.HAVE_ARGON2 else "scrypt",
                "strong_kdf": bool(cc.HAVE_ARGON2)}

    def unlock(self, path, password):
        """Try a passphrase against an archive. On success the passphrase is
        kept here and the interface gets only a session id."""
        if ce is None:
            return {"error": "engine not loaded"}
        try:
            info = self.inspect(path, password=password)
        except Exception as e:
            return {"error": str(e)}
        if info.get("error"):
            return info
        info["session"] = self._vault_put(path, password)
        return info

    def archive_info(self, path):
        """What is knowable about a file without any passphrase."""
        if ce is None:
            return {"error": "engine not loaded"}
        if not ce.is_encrypted(path):
            return {"locked": False}
        h = ce.crush_crypt.header_info(path)
        return {"locked": True, "path": path, "size": h["size"],
                "kdf": h["kdf"], "memory_mb": (h["m"] // 1024) if h["kdf"] == "argon2id" else 0,
                "supported": h["supported"], "version": h["version"]}

    def inspect(self, path, password=None, session=None):
        if ce is None:
            return {"error": "engine not loaded"}
        pw = password if password is not None else self._vault_get(session, path)
        if ce.is_encrypted(path) and pw is None:
            return dict(self.archive_info(path), error="")
        try:
            idx = ce.read_index(path, pw)
        except Exception as e:
            if ce.crush_crypt is not None and isinstance(e, ce.crush_crypt.WrongPassword):
                return {"locked": True, "bad_password": True,
                        "error": "That passphrase did not open this archive."}
            return {"error": str(e)}
        return {"path": path, "size": os.path.getsize(path),
                "encrypted": ce.is_encrypted(path), "locked": False,
                "total_in": sum(f["s"] for f in idx["files"]),
                "files": [{"p": f["p"], "s": f["s"], "cls": f["cls"],
                           "label": ce.CLASS_LABELS.get(f["cls"], f["cls"]),
                           "exact": f.get("ex", True)} for f in idx["files"]],
                "tier": idx.get("tier"), "harder": idx.get("harder", False)}

    def extract(self, archive, dest, select=None, session=None, overwrite=False):
        """select=None restores the whole archive; otherwise it is the list of
        stored paths to restore."""
        if ce is None:
            return {"error": "engine not loaded"}
        pw = self._vault_get(session, archive)
        if ce.is_encrypted(archive) and pw is None:
            return {"error": "This archive is locked. Unlock it first."}
        threading.Thread(target=self._extract_worker,
                         args=(archive, dest, select, pw, bool(overwrite)), daemon=True).start()
        return {"started": True}

    def _extract_worker(self, archive, dest, select=None, password=None, overwrite=False):
        try:
            r = ce.extract(archive, dest, progress=lambda s: self._emit("progress", s),
                           select=select, password=password, overwrite=overwrite)
            r["dest"] = dest
            log(f"extracted {r['written']} file(s) to {dest}"
                + (f" (selection of {r['total']})" if r.get("partial") else ""))
            self._emit("extracted", r)
        except FileExistsError as e:
            # nothing was written; the interface can offer "replace them"
            self._emit("error", {"message": str(e), "exists": True})
        except Exception as e:
            log_exc("extraction")
            self._emit("error", {"message": f"Extraction failed: {type(e).__name__}: {e}\n"
                                            f"Details were written to {LOG_PATH}"})

    def reveal(self, path):
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
        except Exception:
            log_exc("reveal")
        return True


# ==========================================================================
#  Browser fallback: a tiny local server exposing the same API
# ==========================================================================
def _make_share_mod():
    d = str(APP_DIR / "share" / "tools")
    if d not in sys.path:
        sys.path.insert(0, d)
    import make_share
    return make_share


def _channels_mod():
    d = str(APP_DIR / "channels" / "py")
    if d not in sys.path:
        sys.path.insert(0, d)
    import crushchan
    return crushchan


def read_ui() -> str:
    return (APP_DIR / "ui" / "index.html").read_text(encoding="utf-8")

SHIM = """
<script>
/* ------------------------------------------------------------------------
   Crush host bridge.

   The interface talks to Python over this local server in BOTH modes - the
   native window loads the same http://127.0.0.1 URL as the browser does.
   That is deliberate. The previous build used pywebview's JavaScript bridge
   in native mode, which is injected asynchronously: window.pywebview.api
   exists as an empty object for a while before its methods appear, and a call
   made in that window fails with "api(...).ping is not a function". Defining
   the API synchronously, right here in the page, makes that whole class of
   failure impossible.
   ------------------------------------------------------------------------ */
(function(){
  var TOKEN = "__TOKEN__";
  async function call(name, args){
    const r = await fetch('/api/' + name, {method:'POST',
      headers:{'Content-Type':'application/json','X-Crush-Token':TOKEN},
      body: JSON.stringify(args||[])});
    if(!r.ok) throw new Error(await r.text());
    return await r.json();
  }
  const names = ['ping','capabilities','tiers','pick_files','pick_folder','pick_archive',
                 'add_paths','drop_paths','begin_drop','finish_drop','clear','remove',
                 'summary','suggest_output','choose_output','choose_dest','compress',
                 'inspect','extract','reveal','crypto_available','password_strength',
                 'suggest_passphrase','archive_info','unlock','lock','share'];
  const api = {};
  names.forEach(n => api[n] = (...a) => call(n, a));

  /* Upload one dropped file's bytes into a staging folder on this machine,
     keeping its path within the dropped folder. */
  api.upload = async function(dropId, rel, file){
    const r = await fetch('/upload?id=' + encodeURIComponent(dropId) +
                          '&rel=' + encodeURIComponent(rel) +
                          '&m=' + encodeURIComponent(file.lastModified || ''),
      {method:'POST', headers:{'X-Crush-Token':TOKEN,
                               'Content-Type':'application/octet-stream'}, body:file});
    if(!r.ok) throw new Error(await r.text());
    return await r.json();
  };

  window.crushApi = api;
  window.CRUSH_TOKEN = TOKEN;
  setInterval(async () => {
    try {
      const evs = await call('poll_events', []);
      (evs||[]).forEach(x => window.crushEvent && window.crushEvent(x.e, x.p));
    } catch(e){}
  }, 300);
  window.dispatchEvent(new Event('crushready'));
})();
</script>
"""


def build_page(token: str) -> str:
    shim = SHIM.replace("__TOKEN__", token)
    ui = read_ui()
    return ui.replace("</body>", shim + "</body>") if "</body>" in ui else ui + shim


class Server:
    """A loopback HTTP server that exposes the Api and accepts dropped bytes.

    Bound to 127.0.0.1 on an ephemeral port, and every request must carry a
    token generated fresh for this run, so nothing else on the machine can
    drive it.
    """

    def __init__(self, api: "Api"):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.api = api
        self.token = secrets.token_urlsafe(24)
        # The page carries the API token, so handing the page to anyone who
        # asks hands them the API - and an audit demonstrated exactly that: a
        # second process that knew only the port scraped the token from an
        # unauthenticated GET and then drove every method. The page is now
        # released only to a request carrying this launch key, which lives in
        # the URL the window (or the browser) is opened with, is never written
        # to the log, and is never printed anywhere Crush controls.
        self.launch_key = secrets.token_urlsafe(24)
        page = build_page(self.token).encode("utf-8")
        token = self.token
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            OK_ORIGINS = frozenset()

            def log_message(self, *a):
                pass

            def _authed(self):
                """Three checks, not one.

                The token stops any other local process driving this server. The
                Origin check stops a web page the user happens to have open from
                POSTing here - a custom header normally forces a CORS preflight
                the browser would block, but refusing an Origin outright is one
                line and does not depend on that. The Host check stops DNS
                rebinding, where an attacker's domain resolves to 127.0.0.1 and
                the browser then treats this server as same-origin."""
                origin = self.headers.get("Origin")
                if origin is not None and origin not in H.OK_ORIGINS:
                    self.send_error(403, "cross-origin requests are refused")
                    return False
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
                    self.send_error(403, "bad host")
                    return False
                tok = self.headers.get("X-Crush-Token") or ""
                if len(tok) == len(token) and hmac.compare_digest(tok, token):
                    return True
                self.send_error(403, "bad token")
                return False

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                if path not in ("/", "/index.html"):
                    self.send_error(404, "not found"); return
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
                    self.send_error(403, "bad host"); return
                origin = self.headers.get("Origin")
                if origin is not None and origin not in H.OK_ORIGINS:
                    self.send_error(403, "cross-origin requests are refused"); return
                # Reusable for the life of this process, because a browser tab
                # gets refreshed and reopened and that must keep working. Secret,
                # because that is what does the actual work.
                given = (urllib.parse.parse_qs(query).get("k") or [""])[0]
                if not (len(given) == len(outer.launch_key)
                        and hmac.compare_digest(given, outer.launch_key)):
                    self.send_error(403, "not this Crush window")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def do_POST(self):
                if not self._authed():
                    return
                path, _, query = self.path.partition("?")
                n = int(self.headers.get("Content-Length") or 0)

                if path == "/upload":
                    q = urllib.parse.parse_qs(query)
                    did = (q.get("id") or [""])[0]
                    rel = (q.get("rel") or [""])[0]
                    try:
                        dest = outer.api._drop_dest(did, rel)
                    except Exception as e:
                        self.send_error(400, str(e)); return
                    left = n
                    # "xb": two uploads never land on one staged name
                    try:
                        g = open(dest, "xb")
                    except FileExistsError:
                        self.send_error(409, "two dropped files have the same name"); return
                    with g:
                        while left > 0:
                            chunk = self.rfile.read(min(left, 1 << 20))
                            if not chunk:
                                break
                            g.write(chunk); left -= len(chunk)
                    if left:
                        # the page stopped sending: a short copy must not be queued
                        try:
                            os.unlink(dest)
                        except OSError:
                            pass
                        self.send_error(400, "upload incomplete"); return
                    try:
                        # keep the file's own modification time (ms since 1970)
                        ms = float((q.get("m") or [""])[0] or "nan")
                        if ms == ms and ms > 0:
                            os.utime(dest, (ms / 1000.0, ms / 1000.0))
                    except (ValueError, OSError):
                        pass
                    self._json({"ok": True, "bytes": n - left})
                    return

                name = path.rsplit("/", 1)[-1]
                try:
                    args = json.loads(self.rfile.read(n) or b"[]")
                except Exception:
                    args = []
                fn = getattr(outer.api, name, None)
                if not callable(fn) or name.startswith("_"):
                    self.send_error(404, "no such method"); return
                try:
                    out = fn(*args)
                except Exception:
                    # The traceback may quote arguments, and one of those
                    # arguments can be a passphrase. Methods that take one get a
                    # scrubbed message instead of the full trace.
                    if name in ("compress", "unlock", "inspect", "password_strength"):
                        log(f"ERROR in api.{name}: {type(sys.exc_info()[1]).__name__}")
                        body = b"the operation failed - see crush-debug.log"
                    else:
                        log_exc(f"api.{name}")
                        body = traceback.format_exc().encode()
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body); return
                self._json(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        _port = self.httpd.server_address[1]
        # A browser sends Origin on same-origin POSTs too, so "refuse any Origin"
        # refuses the app itself. What must be refused is an origin that is not
        # ours - which is what a page on some other site would send.
        H.OK_ORIGINS = frozenset(f"http://{h}:{_port}" for h in ("127.0.0.1", "localhost")) \
                       | frozenset(f"http://[{h}]:{_port}" for h in ("::1",))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?k={self.launch_key}"
        self.safe_url = f"http://127.0.0.1:{self.port}/"

    def serve_in_background(self):
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        log(f"host server on {self.safe_url}")     # never log the launch key
        return t

    def serve_forever(self):
        log(f"host server on {self.safe_url}")
        self.httpd.serve_forever()


# --------------------------------------------------------------------------
#  Native-window drag and drop
# --------------------------------------------------------------------------
def attach_drop(win, api: "Api") -> bool:
    """Ask pywebview for the real paths of files dropped on the window.

    A web view is not allowed to tell JavaScript where a dropped file lives on
    disk. pywebview's WebView2 backend can, because the drop also reaches the
    native side - but only if a Python handler is attached to the element, so
    it has to be wired up from here rather than from the page. Older pywebview
    builds have no such API; the page then falls back to uploading the bytes.
    """
    try:
        el = win.dom.get_element("#dz")
        if el is None:
            log("drop: #dz not found in the DOM")
            return False

        def on_drop(e):
            try:
                items = (e.get("dataTransfer") or {}).get("files") or []
                paths = [i.get("pywebviewFullPath") for i in items if i.get("pywebviewFullPath")]
                if paths:
                    api.drop_paths(paths)
                else:
                    log("drop: native handler fired with no paths - page will upload instead")
                    api._emit("drop_native_empty", {})
            except Exception:
                log_exc("native drop handler")

        el.events.drop += on_drop
        log("drop: native path capture attached")
        return True
    except Exception as e:
        log(f"drop: native path capture unavailable ({e}) - the page will upload instead")
        return False


def selftest() -> int:
    """Exercise the engine with no interface at all, so a GUI problem can be
    told apart from an engine problem."""
    import tempfile, shutil
    print("Crush self-test")
    print(f"  python        {sys.version.split()[0]}")
    print(f"  engine        {('Crush ' + ce.CRUSH_VERSION + ' loaded') if ce else 'FAILED - see crush-debug.log'}")
    if ce is None:
        print(f"  missing       {', '.join(MISSING) or 'unknown'}")
        return 1
    for c in Api().capabilities():
        print(f"  {'ok ' if c['ok'] else '-- '} {c['name']:<28} {c['note']}")
    td = tempfile.mkdtemp(prefix="crush_selftest_")
    try:
        src = os.path.join(td, "src"); os.makedirs(src)
        open(os.path.join(src, "a.txt"), "w").write("hello crush\n" * 5000)
        open(os.path.join(src, "b.csv"), "w").write(
            "id,v\n" + "".join(f"{i},{i*3}\n" for i in range(5000)))
        with open(os.path.join(src, "c.bin"), "wb") as f:
            f.write(os.urandom(200000))
        arc = os.path.join(td, "t.crush"); dst = os.path.join(td, "out")
        r = ce.compress([src], arc, "balanced")
        x = ce.extract(arc, dst)
        ok = True
        for root, _, fs in os.walk(src):
            for f in fs:
                p = os.path.join(root, f)
                q = os.path.join(dst, os.path.relpath(p, os.path.dirname(src)))
                if not os.path.exists(q) or open(p, "rb").read() != open(q, "rb").read():
                    ok = False; print(f"  MISMATCH {f}")
        print(f"  compress      {r['in']:,} -> {r['out']:,} bytes ({100*r['ratio']:.1f}%)")
        print(f"  extract       {x['written']} files, {x['verified']} verified")
        print(f"  roundtrip     {'BYTE-EXACT' if ok else 'FAILED'}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(td, ignore_errors=True)


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    api = Api()
    force_browser = "--browser" in sys.argv or os.environ.get("CRUSH_BROWSER") == "1"

    # The host server runs in every mode. The native window is a frame around
    # the same page the browser would get, which means one code path to the
    # engine instead of two, and no asynchronously-injected bridge to race.
    try:
        srv = Server(api)
    except Exception:
        log_exc("starting the host server")
        print("Crush could not open its local server. See", LOG_PATH)
        return 1

    if webview is not None and not force_browser and sys.platform.startswith("win"):
        # pywebview's Windows backend runs through pythonnet/clr, which does not
        # always have a working wheel for the newest Python. Half-working means a
        # window that opens and never renders, so check before opening one.
        try:
            import clr_loader, pythonnet          # noqa: F401
            log("pythonnet/clr present")
        except Exception as e:
            log(f"pythonnet unavailable ({e}) - using browser mode")
            force_browser = True

    try:
        if webview is not None and not force_browser:
            srv.serve_in_background()
            try:
                api.mode = "native"
                win = webview.create_window(
                    "Crush", url=srv.url, width=1180, height=820,
                    min_size=(940, 660), background_color="#0E1014")
                api.window = win

                state = {"loaded": False, "fell_back": False}

                def _on_loaded():
                    state["loaded"] = True
                    log("window reported loaded")
                    api.native_drop = attach_drop(win, api)

                try:
                    win.events.loaded += _on_loaded
                except Exception:
                    state["loaded"] = True       # cannot observe it; assume fine

                # The failure being guarded against is not an exception - it is a
                # window that opens and never comes alive. If nothing has loaded
                # in 25 seconds, this machine is not going to run the native path;
                # tear it down and use the browser rather than leave a frozen
                # window behind.
                def _watchdog():
                    for _ in range(250):
                        time.sleep(0.1)
                        if state["loaded"]:
                            return
                    log("window never finished loading - falling back to browser mode")
                    state["fell_back"] = True
                    try:
                        win.destroy()
                    except Exception:
                        log_exc("destroying the stalled window")

                threading.Thread(target=_watchdog, daemon=True).start()
                log("starting native window")
                webview.start(debug=False)
                log("native window closed")
                if not state["fell_back"]:
                    return 0
                api.window = None
                api.native_drop = False
                print("The native window did not load; opening Crush in your browser instead.")
            except Exception:
                log_exc("native window")
                print("The native window failed to start; falling back to your browser.")

        api.mode = "browser"
        api.window = None
        if os.environ.get("CRUSH_NO_BROWSER") != "1":
            try:
                webbrowser.open(srv.url)
            except Exception:
                log_exc("opening browser")
        # Printed either way. This address carries the launch key, and it is the
        # only way back in if the tab is closed - it is deliberately kept out of
        # crush-debug.log, because anyone holding it can drive this copy of Crush.
        print(f"\n  Crush is running at {srv.url}\n"
              f"  Leave this window open while you use it, and keep that address\n"
              f"  to yourself - it is what lets the page reach the engine.\n",
              flush=True)
        srv.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        log_exc("running Crush")
        print("Crush could not start. See", LOG_PATH)
        return 1
    finally:
        api.cleanup_drops()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log_exc("main")
        sys.exit(1)
