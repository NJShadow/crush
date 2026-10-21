"""Link mode: an archive carried entirely in the URL fragment of the static decoder page.

    python3 -m crushchan.link make archive.crush [--base https://example.org/link.html]
    python3 -m crushchan.link read 'https://.../link.html#C1.....' out.crush

Fragment: '#C1.' + base64url(data || SHA-256(data)), no padding. The fragment is never
sent to the web server that serves link.html (browsers strip it from requests), but it
is visible to anything that sees the whole link (messenger servers, link-preview bots,
browser history). Encrypt the archive (Crush --ask) if that matters.
"""
import argparse, base64, hashlib, sys

PREFIX = "C1."
MAX_FRAGMENT = 2 * 1024 * 1024  # decoder refuses anything longer (Chromium's own URL cap)


def make_link(data, base="link.html"):
    blob = data + hashlib.sha256(data).digest()
    return base.split("#")[0] + "#" + PREFIX + base64.urlsafe_b64encode(blob).decode().rstrip("=")


def read_link(url):
    frag = url.split("#", 1)[1] if "#" in url else url
    if not frag.startswith(PREFIX) or len(frag) > MAX_FRAGMENT:
        raise ValueError("not a Crush link")
    s = frag[len(PREFIX):]
    blob = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if len(blob) < 32 or hashlib.sha256(blob[:-32]).digest() != blob[-32:]:
        raise ValueError("SHA-256 mismatch: the link is damaged or truncated")
    return blob[:-32]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crushchan.link")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make"); m.add_argument("archive"); m.add_argument("--base", default="link.html")
    r = sub.add_parser("read"); r.add_argument("url"); r.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "make":
        url = make_link(open(a.archive, "rb").read(), a.base)
        print(url)
        print(f"{len(url):,} characters", file=sys.stderr)
    else:
        d = read_link(a.url)
        open(a.out, "wb").write(d)
        print(f"{len(d):,} bytes, SHA-256 verified", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
