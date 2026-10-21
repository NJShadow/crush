"""Paper backup: printable PDF of QR codes with Reed-Solomon erasure coding.

    python3 -m crushchan.paper make  archive.crush backup.pdf [--paper a4|letter] [--version 20] [--ecl m]
                                     [--cols 3] [--rows 4] [--parity 0.25]
    python3 -m crushchan.paper restore out.crush page1.jpg page2.png ... [--pdf backup.pdf]

Every code is one packet (see core.py, kind 0x12). Each RS block has K source shards and
N-K parity shards; ANY K of the N codes of a block restore it. Shards are laid out
interleaved across blocks, so losing a page costs every block about the same number.
"""
import argparse, datetime, hashlib, math, os, sys
from .core import rs_packets, Receiver, layout, KIND_RS, OVERHEAD, parse
from .qr import qr_matrix, symbol_size, zbar_decode

PAPER = {"a4": (595.2756, 841.8898), "letter": (612.0, 792.0)}
MM = 72 / 25.4


def plan(nbytes, version, ecl, per_page, parity):
    T = symbol_size(version, ecl)
    ktot = (nbytes + 32 + T - 1) // T
    # RS over GF(256): N <= 255 per block. Choose K per block so N = K + parity fits.
    par_min = max(1, per_page)  # at least one full page of parity...
    Kmax = 255
    while Kmax > 1 and Kmax + max(math.ceil(Kmax * parity), min(par_min, Kmax)) > 255:
        Kmax -= 1
    B = (ktot + Kmax - 1) // Kmax
    K = (ktot + B - 1) // B
    # parity per block: fraction, but enough that losing any one page is survivable
    lost_per_block_one_page = math.ceil(per_page / B)
    P = max(math.ceil(K * parity), lost_per_block_one_page)
    N = min(255, K + P)
    # the last page is paid for anyway: fill it with extra parity
    pages = math.ceil(B * N / per_page)
    N = min(255, max(N, (pages * per_page) // B))
    return T, K, N, B, ktot


def make(data, out_pdf, paper="a4", version=20, ecl="m", cols=3, rows=4, parity=0.25, title=None):
    from reportlab.pdfgen import canvas as rl
    per_page = cols * rows
    T, K, N, B, ktot = plan(len(data), version, ecl, per_page, parity)
    pk = rs_packets(data, T, K, N)          # block-major
    order = [pk[b * N + i] for i in range(N) for b in range(B)]  # interleave blocks
    pages = math.ceil(len(order) / per_page)
    W, H = PAPER[paper]
    sha = hashlib.sha256(data).hexdigest()
    c = rl.Canvas(out_pdf, pagesize=(W, H))
    c.setTitle(title or "Crush paper backup")
    c.setAuthor("Crush")
    margin = 12 * MM
    head_h = 34 * MM
    gw = (W - 2 * margin) / cols
    gh = (H - 2 * margin - head_h) / rows
    nmod = 4 * version + 17 + 8  # with 4-module quiet zone
    lab_h = 4 * MM
    side = min(gw, gh - lab_h) * 0.98
    mod = side / nmod
    date = datetime.date.today().isoformat()
    for pg in range(pages):
        # header
        y = H - margin
        c.setFont("Helvetica-Bold", 13)
        c.drawString(margin, y - 12, title or "Crush paper backup")
        c.setFont("Helvetica", 8.5)
        c.drawRightString(W - margin, y - 12, f"page {pg + 1} of {pages}   {date}")
        lines = [
            f"Archive: {len(data):,} bytes   SHA-256: {sha}",
            (f"{len(order)} codes. Any {K} of the {N} codes restore it ({N - K} may be lost or unreadable)." if B == 1 else
             f"{len(order)} codes in {B} groups of {N}: any {K} codes of each group restore it ({N - K} per group may be lost)."),
            "Restore: open the Crush QR receiver page (receive.html) and scan the pages with a camera, or give it photos/scans;",
            "or run: python3 -m crushchan.paper restore out.crush <photos or scans>. Codes can be read in any order.",
            f"Each code: QR version {version}, error correction {ecl.upper()}, {T} data bytes, Base45 text. Do not fold through codes.",
        ]
        for i, ln in enumerate(lines):
            c.drawString(margin, y - 26 - i * 10.5, ln)
        c.setLineWidth(0.4)
        c.line(margin, H - margin - head_h + 4, W - margin, H - margin - head_h + 4)
        for k in range(per_page):
            idx = pg * per_page + k
            if idx >= len(order):
                break
            r, col = divmod(k, cols)
            x0 = margin + col * gw + (gw - side) / 2
            y0 = H - margin - head_h - (r + 1) * gh + lab_h + (gh - lab_h - side) / 2
            mat = qr_matrix(order[idx], version, ecl)
            n = mat.shape[0]
            c.setFillColorRGB(0, 0, 0)
            for yy in range(n):
                xx = 0
                while xx < n:
                    if mat[yy, xx]:
                        x1 = xx
                        while x1 < n and mat[yy, x1]:
                            x1 += 1
                        c.rect(x0 + (4 + xx) * mod, y0 + side - (4 + yy + 1) * mod, (x1 - xx) * mod, mod * 1.0, stroke=0, fill=1)
                        xx = x1
                    else:
                        xx += 1
            d = parse(order[idx])
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(x0 + side / 2, y0 - lab_h + 3, f"code {idx + 1}/{len(order)}   group {d['block'] + 1}  #{d['esi'] + 1}")
        c.showPage()
    c.save()
    return dict(T=T, K=K, N=N, B=B, codes=len(order), pages=pages, per_page=per_page,
                bytes_per_page=len(data) / pages, module_mm=mod / MM, sha256=sha)


def decode_images(paths, receiver=None, log=print, stop_early=True):
    """zbar every image (also tries rotations/upscale/threshold when zbar finds few codes)."""
    from PIL import Image, ImageOps, ImageFilter
    rx = receiver or Receiver()
    seen = set()
    for p in paths:
        img = Image.open(p)
        img = ImageOps.exif_transpose(img).convert("L")
        found = set()
        variants = [lambda im: im,
                    lambda im: im.filter(ImageFilter.GaussianBlur(1)),
                    lambda im: ImageOps.autocontrast(im, cutoff=1),
                    lambda im: im.resize((im.width * 3 // 2, im.height * 3 // 2)),
                    lambda im: im.resize((im.width // 2, im.height // 2))]
        for v in variants:
            for pkt in zbar_decode(v(img)):
                found.add(pkt)
        new = [f for f in found if f not in seen]
        seen |= found
        for pkt in new:
            rx.feed(pkt)
        log(f"{os.path.basename(p)}: {len(found)} codes ({len(new)} new); progress {rx.progress() * 100:.0f}%")
        if rx.result is not None and stop_early:
            break
    return rx


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crushchan.paper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("archive"); m.add_argument("pdf")
    m.add_argument("--paper", default="a4", choices=list(PAPER))
    m.add_argument("--version", type=int, default=20); m.add_argument("--ecl", default="m")
    m.add_argument("--cols", type=int, default=3); m.add_argument("--rows", type=int, default=4)
    m.add_argument("--parity", type=float, default=0.25)
    r = sub.add_parser("restore")
    r.add_argument("out"); r.add_argument("images", nargs="*")
    r.add_argument("--pdf", help="read the codes straight from a backup PDF (rendered at 200 dpi)")
    a = ap.parse_args(argv)
    if a.cmd == "make":
        data = open(a.archive, "rb").read()
        info = make(data, a.pdf, a.paper, a.version, a.ecl, a.cols, a.rows, a.parity)
        print(info)
    else:
        imgs = list(a.images)
        if a.pdf:
            import tempfile, pypdfium2
            td = tempfile.mkdtemp()
            doc = pypdfium2.PdfDocument(a.pdf)
            for i in range(len(doc)):
                f = os.path.join(td, f"p{i}.png")
                doc[i].render(scale=200 / 72).to_pil().save(f)
                imgs.append(f)
        rx = decode_images(imgs)
        if rx.result is None:
            print(f"NOT RESTORED: have {rx.progress() * 100:.0f}% of what is needed. Scan more codes.")
            return 2
        open(a.out, "wb").write(rx.result)
        print(f"restored {len(rx.result):,} bytes, SHA-256 verified: {hashlib.sha256(rx.result).hexdigest()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
