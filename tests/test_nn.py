#!/usr/bin/env python3
"""
Crush - tests for crushnn, the learned code model (round 11).

    python tests/test_nn.py

Self-contained; writes only to a temporary folder. Covers:

  * priors/code.cnn is exactly the pinned model;
  * a known-answer test: a fixed input must encode to a pinned SHA-256 on every
    machine (the same check run under Wine for the Windows build);
  * round trip byte-exact, through the codec and through a whole archive;
  * hostile streams (oversized length, wrong magic) are refused before the
    helper runs; a model that is not the pinned one is refused.
"""
import hashlib, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "engine"))
import crush_engine as ce          # noqa: E402

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok    " if cond else "  FAIL  ") + name)
    if cond: PASS += 1
    else:    FAIL += 1

def kat_input():
    lines = []
    for i in range(60):
        lines.append(f"def handler_{i}(request, retries={i % 5}):\n"
                     f"    \"\"\"Process request number {i}.\"\"\"\n"
                     f"    for attempt in range(retries):\n"
                     f"        if request.get('id') == {i * 7}:\n"
                     f"            return attempt\n"
                     f"    return None\n\n")
    return "".join(lines).encode()

KAT_SHA = "e0f31e144c79ff65e1422f0368b5d618e82398ce03678ba921699f8e8f468960"

def main():
    model = ce._prior("code", ".cnn")
    if not ce.HAVE.get("crushnn") or not model:
        print("crushnn or priors/code.cnn missing - skipped"); return 0
    print("Model and helper")
    check("priors/code.cnn matches the pinned SHA-256", ce._nn_model_ok(model))
    data = kat_input()
    blob = ce.c_crushnn(data, model)
    check("encodes", blob is not None and blob[:4] == b"CNM1")
    h = hashlib.sha256(blob).hexdigest()
    check(f"known-answer test ({len(data)} -> {len(blob)} bytes, sha {h[:16]})", h == KAT_SHA)
    check("decodes byte-exact", ce.d_crushnn(blob, model) == data)

    print("Hostile input")
    big = blob[:4] + (ce.NN_MAX_BUNDLE + 1).to_bytes(8, "little") + blob[12:]
    for name, b in (("oversized length refused", big),
                    ("wrong magic refused", b"CCM1" + blob[4:]),
                    ("truncated header refused", blob[:7])):
        try:
            ce.d_crushnn(b, model); ok = False
        except ValueError:
            ok = True
        check(name, ok)
    td = tempfile.mkdtemp()
    try:
        fake = os.path.join(td, "code.cnn")
        mb = bytearray(open(model, "rb").read()); mb[1000] ^= 1
        open(fake, "wb").write(mb)
        try:
            ce.d_crushnn(blob, fake); ok = False
        except RuntimeError:
            ok = True
        check("a model that is not the pinned file is refused", ok)
        check("and is never used to encode", ce.c_crushnn(data, fake) is None)

        print("Archive")
        src = os.path.join(td, "src"); os.makedirs(src)
        open(os.path.join(src, "a.py"), "wb").write(data)
        open(os.path.join(src, "b.py"), "wb").write(open(os.path.join(HERE, "test_nn.py"), "rb").read())
        out = os.path.join(td, "t.crush")
        ce.compress([src], out, tier="singularity")
        codecs = [b["codec"] for b in ce.read_index(out)["bundles"]]
        check(f"singularity picks crushnn_m for code (codecs: {codecs})", "crushnn_m" in codecs)
        dst = os.path.join(td, "x"); os.makedirs(dst)
        ce.extract(out, dst)
        same = all(open(os.path.join(src, f), "rb").read() ==
                   open(os.path.join(dst, "src", f), "rb").read() for f in ("a.py", "b.py"))
        check("archive extracts byte-exact", same)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
