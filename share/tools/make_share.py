#!/usr/bin/env python3
"""make_share - wrap a .crush archive into one self-opening .html file.

    python3 tools/make_share.py ARCHIVE.crush [-o OUT.html] [--title T]
                                [--encoding raw|base64] [--allow-partial]
    (an encrypted archive: CRUSH_PASSWORD in the environment lets make_share
     look inside to embed only the decoders it needs and to check that every
     file can be opened in a browser; without it, all decoders are embedded)

The page carries: the archive bytes, the WASM decoders this archive needs
(and only those), any shared prior a bundle names, the Monocypher crypto
module if the archive is encrypted, and the reader. It makes no network
requests (enforced by a Content-Security-Policy in the page itself).
"""
from __future__ import annotations
import html, json, os, re, struct, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import share_build as sb

ENGINE = os.path.normpath(os.path.join(sb.ROOT, "..", "engine"))
PRIORS = os.path.normpath(os.path.join(sb.ROOT, "..", "priors"))
CRYPTO_WASM = os.path.join(sb.ROOT, "crypto", "crush_crypto.wasm")


class ShareError(Exception):
    pass


def _engine():
    if ENGINE not in sys.path:
        sys.path.insert(0, ENGINE)
    import crush_engine
    return crush_engine


def inspect(archive_path: str, password=None):
    """What the archive needs: codecs, transforms, priors, index codec.
    Returns None when the archive is encrypted and no passphrase was given."""
    ce = _engine()
    data = open(archive_path, "rb").read(32)
    if data[:8] == b"CRUSHENC":
        import crush_crypt
        h = crush_crypt.header_info(archive_path)
        if not h["supported"] or h["suite"] != crush_crypt.SUITE_ARGON2ID_XCHACHA:
            raise ShareError("this archive's encryption (suite %d) cannot be opened in a browser; "
                             "only Argon2id archives can" % h["suite"])
        if password is None:
            return None
        f = ce._open_archive(archive_path, password)
        try:
            head = f.read(28)
        finally:
            f.close()
    else:
        if data[:6] != ce.MAGIC:
            raise ShareError("not a Crush archive")
        head = data[:28]
    icodec = struct.unpack_from("<I", head, 24)[0]
    idx = ce.read_index(archive_path, password)
    codecs = sorted({b["codec"] for b in idx["bundles"]})
    transforms = sorted({s["n"] for f in idx["files"] for s in f["t"]})
    priors = sorted({b["prior"] + (".prime" if b["codec"] == "crushcm_p" else ".zdict")
                     for b in idx["bundles"] if b.get("prior") and b["codec"] in sb.CODEC_MODULE})
    echo_zlib = any(b.get("echo") and not b["echo"].get("z") for b in idx["bundles"])
    echo_zstd = any(b.get("echo") and b["echo"].get("z") for b in idx["bundles"])
    contentexact = [f["p"] for f in idx["files"] if not f.get("ex", True)]
    blocked = {}
    bcodec = {b["id"]: b["codec"] for b in idx["bundles"]}
    for f in idx["files"]:
        bad = [s["n"] for s in f["t"] if s["n"] not in sb.PORTABLE_TRANSFORMS]
        c = bcodec.get(f.get("b"))
        if c is not None and c != "store" and c not in sb.CODEC_MODULE:
            bad.append(c)                 # e.g. crushnn_m: no browser decoder
        if bad or not f.get("ex", True):
            blocked[f["p"]] = bad or ["content-exact"]
    return {"icodec": icodec, "codecs": codecs, "transforms": transforms, "priors": priors,
            "echo_zlib": echo_zlib, "echo_zstd": echo_zstd, "blocked": blocked,
            "contentexact": contentexact, "files": len(idx["files"]),
            "total_in": idx.get("total_in")}


def modules_for(req) -> list:
    if req is None:
        return list(sb.ALL_MODULES)
    mods = set()
    mods.add("zstd" if req["icodec"] == 1 else "crushz")
    if req["echo_zstd"]:
        mods.add("zstd")
    if req["echo_zlib"]:
        mods.add("crushz")
    for c in req["codecs"]:
        m = sb.CODEC_MODULE.get(c)
        if m:
            mods.add(m)
    for t in req["transforms"]:
        mods.update(sb.TRANSFORM_MODULES.get(t, []))
    return [m for m in sb.ALL_MODULES if m in mods]


def _blob_element(attrs: str, data: bytes, encoding: str, deflate: bool = False) -> bytes:
    """deflate=True stores the bytes raw-deflated (the page inflates them with
    the browser's own DecompressionStream). Used for the decoders and priors,
    which shrink 2.3-2.9x; never for the archive, which is already compressed."""
    if deflate:
        import zlib
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        z = co.compress(data) + co.flush()
        attrs += f' data-z="deflate-raw" data-rawlen="{len(data)}" data-sha256="{sb.sha256(data)}"'
        data = z
    if encoding == "raw":
        body = sb.encode_raw(data)
        enc = "raw"
    else:
        import base64
        body = base64.b64encode(data)
        enc = "b64"
    return (f'<script type="application/x-crush" {attrs} data-enc="{enc}" data-len="{len(data)}">'
            .encode("ascii") + body + b"</script>\n")


def _ascii_html(s: str) -> str:
    return html.escape(s).encode("ascii", "xmlcharrefreplace").decode("ascii")


def build_html(archive: bytes, *, title: str, modules: list, priors: dict, encoding: str = "raw",
               zipname: str = "crush-files", autocheck: int = 200 << 20,
               compact_code: bool = True) -> bytes:
    if encoding not in ("raw", "base64"):
        raise ShareError("encoding must be raw or base64")
    encrypted = archive[:8] == b"CRUSHENC"
    meta = {"v": 1, "sha256": sb.sha256(archive), "size": len(archive), "encrypted": encrypted,
            "parts": max(1, -(-len(archive) // (4 << 20))),
            "modules": modules, "zipname": zipname, "autocheck": autocheck,
            "made": time.strftime("%Y-%m-%d")}
    meta_js = json.dumps(meta, separators=(",", ":")).replace("<", "\\u003c")
    parts = [b'<script type="application/json" id="crush-meta">' + meta_js.encode("ascii") + b"</script>\n"]
    if encoding == "raw":
        parts.append(_blob_element('id="crush-canary"', bytes(range(256)), encoding))
    # The archive is split across elements of PART bytes: Chromium appends one
    # long text node's chunks in a way that grows quadratically (measured: a
    # 500 MB archive in one element took 202 s just to parse).
    PART = 4 << 20
    for k, o in enumerate(range(0, max(len(archive), 1), PART)):
        parts.append(_blob_element(f'data-archive-part="{k}"', archive[o:o + PART], encoding))
    for m in modules:
        parts.append(_blob_element(f'data-wasm="{m}"', open(sb.wasm_path(m), "rb").read(), encoding, True))
    for name, blob in priors.items():
        if not re.fullmatch(r"[a-z]+\.(zdict|prime)", name):
            raise ShareError("bad prior name " + name)
        parts.append(_blob_element(f'data-prior="{name}"', blob, encoding, True))
    if encrypted:
        parts.append(_blob_element('data-crypto="1"', open(CRYPTO_WASM, "rb").read(), encoding, True))
    js = ["globalThis.__CRUSH_WASM__ = {};   // filled from the page by app.js, read lazily by the codecs"]
    if modules:
        js.append(sb.classic_codecs_script())
    for f in sb.JS_FILES:
        js.append(open(os.path.join(sb.SRC, f), encoding="utf-8").read())
    code = "\n".join(js)
    if any(ord(c) > 127 for c in code):
        raise ShareError("the page's JavaScript must be ASCII (the page is windows-1252)")
    if re.search(r"</script", code, re.I):
        raise ShareError("the page's JavaScript contains '</script'")
    if compact_code:
        # The reader (~120 KB of commented JavaScript) travels raw-deflated and a
        # 1 KB bootstrap inflates it into a <script> element. Its source is in
        # the Crush folder (share/src); the page records its SHA-256.
        parts.append(_blob_element('id="crush-code"', code.encode("ascii"), encoding, True))
        parts.append(b"<script>\n" + open(os.path.join(sb.SRC, "boot.js"), "rb").read() + b"\n</script>\n")
    else:
        parts.append(b"<script>\n" + code.encode("ascii") + b"\n</script>\n")
    page = open(os.path.join(sb.SRC, "page.html"), encoding="utf-8").read()
    if any(ord(c) > 127 for c in page):
        raise ShareError("page.html must be ASCII")
    head, tail = page.split("{{SCRIPTS}}")
    t = _ascii_html(title)
    def fill(s):
        return (s.replace("{{TITLE}}", t).replace("{{HEADING}}", t)
                 .replace("{{SIZE}}", _ascii_html(_size(len(archive)))))
    return fill(head).encode("ascii") + b"".join(parts) + fill(tail).encode("ascii")


def _size(n):
    for u in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "bytes" else f"{n:.1f} {u}"
        n /= 1024


def free_name(path: str) -> str:
    """path, or 'name (2).ext', 'name (3).ext', ... - the first that does not exist."""
    if not os.path.lexists(path):
        return path
    root, ext = os.path.splitext(path)
    for n in range(2, 10000):
        cand = f"{root} ({n}){ext}"
        if not os.path.lexists(cand):
            return cand
    raise FileExistsError(path)


def make_share(archive_path: str, html_path: str = None, *, title: str = None,
               encoding: str = "raw", password=None, allow_partial: bool = False) -> str:
    """Wrap archive_path into a self-opening HTML file; returns its path.

    Refuses (ShareError) when some file in the archive could not be opened in
    a browser - jpegxl/flac/zip_recompose/perceptual transforms - unless
    allow_partial, in which case the page opens everything else and names the
    rest. An encrypted archive with no passphrase cannot be inspected: every
    decoder is embedded and the check is skipped."""
    archive = open(archive_path, "rb").read()
    req = inspect(archive_path, password)
    if req and req["blocked"] and not allow_partial:
        names = ", ".join(list(req["blocked"])[:5])
        raise ShareError(f"{len(req['blocked'])} file(s) use transforms or codecs a browser cannot rebuild "
                         f"({names}...). Re-compress with portable=True (--portable), or pass allow_partial.")
    priors = {}
    if req:
        for p in req["priors"]:
            fp = os.path.join(PRIORS, p)
            if not os.path.exists(fp):
                raise ShareError("the archive needs prior " + p + ", which is missing")
            priors[p] = open(fp, "rb").read()
    base = os.path.basename(archive_path)
    stem = base[:-6] if base.endswith(".crush") else base
    title = title or stem
    if not html_path:
        # never replace an existing file the caller did not name: compressing
        # index.html to index.crush must not make the page overwrite index.html
        html_path = free_name(os.path.splitext(archive_path)[0] + ".html")
    page = build_html(archive, title=title, modules=modules_for(req), priors=priors,
                      encoding=encoding, zipname=re.sub(r"[^\w.\- ]+", "_", stem) or "crush-files")
    fd, tmp = tempfile.mkstemp(prefix=".crush-share-", suffix=".part",
                               dir=os.path.dirname(os.path.abspath(html_path)) or ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(page)
        os.replace(tmp, html_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return html_path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive"); ap.add_argument("-o", "--out")
    ap.add_argument("--title"); ap.add_argument("--encoding", default="raw", choices=("raw", "base64"))
    ap.add_argument("--allow-partial", action="store_true")
    a = ap.parse_args()
    out = make_share(a.archive, a.out, title=a.title, encoding=a.encoding,
                     password=os.environ.get("CRUSH_PASSWORD"), allow_partial=a.allow_partial)
    n, m = os.path.getsize(a.archive), os.path.getsize(out)
    print(f"{out}: {m:,} bytes ({m - n:+,} over the {n:,}-byte archive)")
