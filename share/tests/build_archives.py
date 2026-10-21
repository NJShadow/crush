"""Build test archives with this folder's copy of the engine, from real data.

    python3 tests/build_archives.py OUTDIR [set ...]

Each set writes OUTDIR/<set>.crush and OUTDIR/<set>.json (what went in, with
SHA-256 of every source file, so the browser's output can be compared)."""
import hashlib, json, os, shutil, sys, time, glob
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "engine"))
import crush_engine as ce

B = "/home/claude/bench/"
C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "corpus", "")
C2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "corpus2", "")
SETS = {
    # name: (inputs, tier, extra kwargs)
    "small":   ([C2 + "smallfiles"], "balanced", {}),
    "gen":     ([C2 + "gen"], "balanced", {}),
    "mixed":   ([C + "office_report.docx", C + "photo_q75.jpg", C + "screenshot_ui.png",
                 C + "code_c.h", B + "compat/sheet.xlsx", B + "compat/sec.odt",
                 B + "compat/readme.docx", B + "compat/lo_readme.pdf",
                 B + "pdf/001_qbk-test.pdf", B + "pdf/002_qcr-test.pdf",
                 B + "gz/0252_changelog.Debian.gz", B + "zip/005_patch_ng-1.18.1-py3-none-any.whl",
                 B + "png/001_libreoffice-oasis-drawing-template.png", B + "jpg/china.jpg"],
                "crush", {}),
    "struct":  ([C2 + "struct/telemetry.csv", C + "data_floats.bin", C + "data_timeseries.bin",
                 C + "image_raw16.bin", C + "audio_music.wav"], "crush", {}),
    "locked":  ([C2 + "smallfiles"], "balanced", {"password": "correct horse battery staple été",
                                                  "kdf_profile": "fast"}),
    "locked_bal": ([C + "photo_q75.jpg", C + "code_c.h"], "balanced",
                   {"password": "Søme passphrase", "kdf_profile": "balanced"}),
    "dups":    ([C2 + "revisions", C2 + "smallfiles"], "quick", {}),
    "obsess":  ([C + "text_english.txt", C + "code_python.py"], "obsession", {}),
    "pf":      ([B + "gz/" + f for f in sorted(os.listdir(B + "gz"), key=lambda f: -os.path.getsize(B + "gz/" + f))[:6]]
                + [C + "photo_lossless.png", B + "compat/py_src.tar.gz"], "balanced", {}),
    "singp":   ([C + "data_xml.xml", C + "office_report.docx", B + "compat/lo_readme.pdf", B + "compat/readme.docx"],
                "singularity", {"portable": True}),
    "sing":    ([C2 + "smallfiles"], "singularity", {}),
}

def build(out, name):
    paths, tier, kw = SETS[name]
    arch = os.path.join(out, name + ".crush")
    t0 = time.time()
    r = ce.compress(paths, arch, tier=tier, **kw)
    files = ce.collect(paths)
    exp = {rel: {"src": fp, "sha": hashlib.sha256(open(fp, "rb").read()).hexdigest(),
                 "size": os.path.getsize(fp)} for fp, rel in files}
    idx = ce.read_index(arch, kw.get("password"))
    info = {"name": name, "tier": tier, "in": r["in"], "out": r["out"], "seconds": round(time.time() - t0, 1),
            "files": exp, "password": kw.get("password"),
            "codecs": sorted({b["codec"] for b in idx["bundles"]}),
            "transforms": sorted({s["n"] for f in idx["files"] for s in f["t"]})}
    json.dump(info, open(os.path.join(out, name + ".json"), "w"), indent=1)
    print(f"{name}: {r['in']:,} -> {r['out']:,} in {info['seconds']}s codecs={info['codecs']} transforms={info['transforms']}", flush=True)

if __name__ == "__main__":
    out = sys.argv[1]; os.makedirs(out, exist_ok=True)
    for n in (sys.argv[2:] or SETS):
        build(out, n)
