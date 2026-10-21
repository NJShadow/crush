"""Fountain QR stream as a video/GIF/PNG sequence (for playing on any screen without the sender page).

    python3 -m crushchan.stream make archive.crush out.mp4|out.gif|outdir/ [--version 20 --ecl l --rate 8 --factor 2.0]
    python3 -m crushchan.stream decode in.mp4|in.gif|dir out.crush       (zbar; for testing)

The sequence holds ceil(factor * K) packets (source first, then repair), so a viewer who
misses up to ~1-1/factor of the codes still completes; loop the video for more.
"""
import argparse, glob, math, os, subprocess, sys, tempfile
from .core import fountain_packet, layout, Receiver
from .qr import symbol_size, qr_matrix, render, zbar_decode


def frames(data, version=20, ecl="l", factor=2.0, module_px=6):
    T = symbol_size(version, ecl)
    ktot, B, kb = layout(len(data), T)
    n = math.ceil(ktot * factor) + 2
    for i in range(n):
        yield render(qr_matrix(fountain_packet(data, T, i % B, i // B), version, ecl), module_px)


def make(data, out, version=20, ecl="l", rate=8, factor=2.0):
    imgs = list(frames(data, version, ecl, factor))
    if out.endswith(".gif"):
        imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=int(1000 / rate), loop=0, optimize=False)
    elif out.endswith((".mp4", ".mkv", ".webm")):
        with tempfile.TemporaryDirectory() as td:
            for i, im in enumerate(imgs):
                im.convert("RGB").save(os.path.join(td, f"f{i:06d}.png"))
            # each code held for a whole number of 30 fps frames; lossless-ish x264
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(rate), "-i", os.path.join(td, "f%06d.png"),
                            "-r", "30", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2:color=white", out], check=True)
    else:
        os.makedirs(out, exist_ok=True)
        for i, im in enumerate(imgs):
            im.save(os.path.join(out, f"code{i:06d}.png"))
    return len(imgs)


def decode(src):
    from PIL import Image
    rx = Receiver()
    with tempfile.TemporaryDirectory() as td:
        if os.path.isdir(src):
            files = sorted(glob.glob(os.path.join(src, "*.png")))
        elif src.endswith(".gif"):
            im = Image.open(src); files = []
            for i in range(getattr(im, "n_frames", 1)):
                im.seek(i); f = os.path.join(td, f"{i:06d}.png"); im.convert("L").save(f); files.append(f)
        else:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-i", src, os.path.join(td, "%06d.png")], check=True)
            files = sorted(glob.glob(os.path.join(td, "*.png")))
        for f in files:
            for pk in zbar_decode(Image.open(f).convert("L")):
                rx.feed(pk)
            if rx.result is not None:
                break
    return rx


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crushchan.stream")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make"); m.add_argument("archive"); m.add_argument("out")
    m.add_argument("--version", type=int, default=20); m.add_argument("--ecl", default="l")
    m.add_argument("--rate", type=float, default=8); m.add_argument("--factor", type=float, default=2.0)
    d = sub.add_parser("decode"); d.add_argument("src"); d.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "make":
        n = make(open(a.archive, "rb").read(), a.out, a.version, a.ecl, a.rate, a.factor)
        print(f"{n} codes at {a.rate}/s -> {a.out} ({os.path.getsize(a.out) if os.path.isfile(a.out) else 0:,} bytes)")
    else:
        rx = decode(a.src)
        if rx.result is None:
            print(f"NOT DECODED ({rx.progress() * 100:.0f}%)"); return 2
        open(a.out, "wb").write(rx.result)
        print(f"decoded {len(rx.result):,} bytes, SHA-256 verified; {rx.stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
