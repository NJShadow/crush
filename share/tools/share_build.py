"""Pieces shared by make_share and the tests: locating the WASM decoders,
turning crush_codecs.js (an ES module) into a classic script, and the
binary-in-HTML encoding."""
from __future__ import annotations
import hashlib, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
# The decoders, as vendored into this folder by tools/vendor_codecs.sh (a
# pinned snapshot of the wasm teammate's build, with sha256s in CODECS.lock).
CODECS_DIR = os.environ.get("CRUSH_CODECS_DIR") or os.path.join(ROOT, "codecs")

# Which WASM module serves which need.
CODEC_MODULE = {
    "store": None, "zstd12": "zstd", "zstd19": "zstd", "zstd22": "zstd",
    "zstd19d": "zstd", "zstd22d": "zstd", "lzma": "lzma", "lzma_x86": "lzma",
    "lzma_d4": "lzma", "lzma_text": "lzma", "ppmd": "lzma", "brotli": "brotli",
    "bzip2": "bzip2", "crushcm": "crushcm", "crushcm_p": "crushcm",
}
TRANSFORM_MODULES = {
    "deflate_unwrap": ["crushz", "crushpf"], "pdf_unwrap": ["crushz", "crushpf"],
    "gzip_unwrap": ["crushz", "crushpf"], "png_unwrap": ["crushz", "crushpf"],
    "jpeg_pack": ["crushjpg"], "jpeg_embedded": ["crushjpg"],
}
PORTABLE_TRANSFORMS = {"oracle", "prism", "transpose", "audio_predict", "floatarray",
                       "genome", "loom", "deflate_unwrap", "pdf_unwrap", "gzip_unwrap",
                       "png_unwrap", "jpeg_pack", "jpeg_embedded"}
ALL_MODULES = ["zstd", "crushz", "lzma", "brotli", "bzip2", "crushcm", "crushpf", "crushjpg"]
JS_FILES = ["bytes.js", "sha256.js", "crypt.js", "transforms.js", "container.js",
            "codecs.js", "zipwriter.js", "app.js"]


def wasm_path(name: str) -> str:
    return os.path.join(CODECS_DIR, name + ".wasm")


def codecs_js_path() -> str:
    return os.path.join(CODECS_DIR, "crush_codecs.js")


def classic_codecs_script() -> str:
    """crush_codecs.js is an ES module; the share page needs a classic script
    (an inline module cannot be relied on from file:// everywhere, and the
    rest of the page is classic). The rewrite is mechanical and checked: if
    the upstream file changes shape, this raises instead of guessing."""
    s = open(codecs_js_path(), encoding="utf-8").read()
    if any(ord(c) > 127 for c in s):
        raise RuntimeError("crush_codecs.js contains non-ASCII text; the page is windows-1252")
    def sub(pat, rep, count=1):
        nonlocal s
        s2, n = re.subn(pat, rep, s, count=count)
        if n == 0:
            raise RuntimeError(f"crush_codecs.js changed shape: {pat!r} not found")
        s = s2
    sub(r"const EMBEDDED = null;", "const EMBEDDED = globalThis.__CRUSH_WASM__ || null;")
    sub(r"return b64bytes\(EMBEDDED\[name\]\);",
        'return typeof EMBEDDED[name] === "string" ? b64bytes(EMBEDDED[name]) : EMBEDDED[name];')
    s = s.replace("import.meta.url", '(globalThis.location ? location.href : "file:///")')
    names = re.findall(r"^export (?:async function|function|const|class|let) (\w+)", s, re.M)
    s = re.sub(r"^export (async function|function|const|class|let) ", r"\1 ", s, flags=re.M)
    if re.search(r"^\s*(export|import)\s", s, re.M):
        raise RuntimeError("crush_codecs.js has module syntax this rewrite does not handle")
    return ("(function () {\n" + s + "\nglobalThis.CC = {" + ", ".join(names) + "};\n})();\n")


# ---------------------------------------------------------------------------
#  Binary inside HTML.
#
#  "raw" (default): the page is declared windows-1252, a single-byte encoding
#  in which every byte 0x00-0xFF decodes to exactly one character and back
#  (WHATWG's table maps the five undefined bytes to C1 controls, so it is a
#  bijection). Inside <script type="application/x-crush"> the only bytes the
#  HTML parser alters are NUL (to U+FFFD) and CR (newline normalisation), and
#  the only byte that could end the element is '<'. Those three and the escape
#  byte itself are escaped as two bytes, so random data grows by 4/256, about
#  1.6%. Base64 grows it by 33%.
#
#  "base64": plain base64, for channels that might re-encode text.
# ---------------------------------------------------------------------------
ESC = 0x7E                        # '~'
_ESCAPES = {0x00: ord("0"), 0x0D: ord("r"), 0x3C: ord("l"), ESC: ESC}
_TABLE = None


def _table():
    global _TABLE
    if _TABLE is None:
        t = [bytes([b]) for b in range(256)]
        for b, e in _ESCAPES.items():
            t[b] = bytes([ESC, e])
        _TABLE = t
    return _TABLE


def encode_raw(data: bytes) -> bytes:
    # bytes.translate cannot expand, so split on the four special bytes
    t = _table()
    if not any(b in data for b in (b"\x00", b"\r", b"<", b"~")):
        return data
    out = bytearray()
    out_append = out.extend
    start = 0
    special = re.compile(rb"[\x00\r<~]")
    for m in special.finditer(data):
        out_append(data[start:m.start()])
        out_append(t[data[m.start()]])
        start = m.end()
    out_append(data[start:])
    return bytes(out)


def decode_raw(enc: bytes) -> bytes:          # for tests
    inv = {v: k for k, v in _ESCAPES.items()}
    out, i = bytearray(), 0
    while i < len(enc):
        b = enc[i]
        if b == ESC:
            out.append(inv[enc[i + 1]]); i += 2
        else:
            out.append(b); i += 1
    return bytes(out)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
