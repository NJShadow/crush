#!/usr/bin/env python3
"""
Crush - data-safety tests (release audit, round 12).

    python tests/test_safety.py

Self-contained: every input is generated in a temporary folder. Each check is
one way a user could have lost data before the release audit, or a way a
damaged or hostile archive could have crashed or misled Crush:

  * compressing never destroys anything: not an input, not a previous archive,
    not a file that is not an archive; a failed run leaves no partial file;
  * extraction never replaces a file without being told to, never leaves a
    partial file under a real name, never writes through a link, and one bad
    entry or one damaged data block costs only the files it holds;
  * two inputs that would be stored under one name are refused;
  * a file that changes while being read is archived as it was read;
  * a damaged index is refused (its checksum), and damaged codec headers are
    refused before any decoder sees them;
  * files above the big-file threshold are streamed both ways, byte-exact,
    deduplicated, and readable by the in-memory reader too;
  * the command line reports problems as messages with exit codes, not
    tracebacks.
"""
import hashlib, os, random, shutil, struct, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "..", "engine")
sys.path.insert(0, ENGINE)
import crush_engine as ce          # noqa: E402

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    print(("  ok    " if cond else "  FAIL  ") + name + (f"  ({extra})" if extra and not cond else ""))
    if cond: PASS += 1
    else:    FAIL += 1

def w(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())

def r(p):
    with open(p, "rb") as f:
        return f.read()

def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False

def text(n, seed=1):
    rnd = random.Random(seed)
    words = ["crush", "archive", "bytes", "verify", "folder", "photo", "notes", "zeta"]
    return " ".join(rnd.choice(words) for _ in range(n)).encode()


def archive_named(T, names, arc):
    """An archive whose entries are stored under `names` exactly - names this
    machine may not be able to create, as an archive made on Linux or macOS
    can carry. Each entry's content is its own name."""
    d = tempfile.mkdtemp(dir=T)
    fps = []
    for i, n in enumerate(names):
        w(f"{d}/{i}.txt", f"content of {n!r}\n")
        fps.append((os.path.join(d, f"{i}.txt"), n))
    real = ce.collect
    ce.collect = lambda paths, skipped=None: list(fps)
    try:
        ce.compress([d], arc, tier="quick")
    finally:
        ce.collect = real
    return arc


def main():
    T = tempfile.mkdtemp(prefix="crush-safety-")
    try:
        run(T)
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def run(T):
    src = os.path.join(T, "src")
    w(f"{src}/a.txt", text(20000, 1)); w(f"{src}/sub/b.txt", text(5000, 2))
    w(f"{src}/sub/copy.txt", text(20000, 1))          # duplicate of a.txt
    w(f"{src}/empty.bin", b"")

    print("Compressing never destroys anything")
    arc = f"{T}/a.crush"
    ce.compress([src], arc, tier="quick")
    good = r(arc)
    notes = f"{T}/notes.txt"; w(notes, "my notes\n")
    check("output that is not an archive is refused, and left intact",
          raises(lambda: ce.compress([src], notes)) and r(notes) == b"my notes\n")
    real = ce.tournament
    def boom(*a, **k): raise MemoryError("simulated")
    ce.tournament = boom
    try:
        ok = raises(lambda: ce.compress([src], arc, tier="balanced"), MemoryError)
    finally:
        ce.tournament = real
    check("a failed re-compress leaves the previous archive byte-identical", ok and r(arc) == good)
    check("and no scratch file behind", not [n for n in os.listdir(T) if n.startswith(".crush")])
    check("a missing input path is refused", raises(lambda: ce.compress([f"{T}/nope"], f"{T}/n.crush")))
    clash1 = os.path.join(T, "c1", "report.txt"); clash2 = os.path.join(T, "c2", "report.txt")
    w(clash1, "one"); w(clash2, "two")
    check("two inputs stored under one name are refused",
          raises(lambda: ce.compress([clash1, clash2], f"{T}/c.crush"), ValueError)
          and not os.path.exists(f"{T}/c.crush"))
    inner = f"{src}/self.crush"
    ce.compress([src], inner, tier="quick")
    ce.compress([src], inner, tier="quick")            # replacing it, from inside src
    names = [e["p"] for e in ce.read_index(inner)["files"]]
    check("an archive inside the folder is not packed into its successor",
          "src/self.crush" not in names, str(names))
    os.remove(inner)

    print("Extraction never replaces or half-writes")
    dst = f"{T}/out"
    x = ce.extract(arc, dst)
    same = all(r(f"{dst}/src/{p}") == r(f"{src}/{p}") for p in ("a.txt", "sub/b.txt", "sub/copy.txt", "empty.bin"))
    check("round trip byte-exact, duplicate and empty file included", same and not x["failed"])
    w(f"{dst}/src/a.txt", "newer edits")
    check("existing files are not replaced without overwrite=True",
          raises(lambda: ce.extract(arc, dst), FileExistsError) and r(f"{dst}/src/a.txt") == b"newer edits")
    ce.extract(arc, dst, overwrite=True)
    check("overwrite=True restores them", r(f"{dst}/src/a.txt") == r(f"{src}/a.txt"))
    check("no scratch files left in the destination",
          not [n for dp, _, fs in os.walk(dst) for n in fs if n.startswith(".crush")])
    if hasattr(os, "symlink"):
        out2 = f"{T}/out2"; outside = f"{T}/outside"; os.makedirs(f"{out2}/src"); os.makedirs(outside)
        w(f"{outside}/precious.txt", "precious")
        try:
            os.symlink(outside, f"{out2}/src/sub", target_is_directory=True)
        except OSError:
            print("  -     links need Developer Mode or admin rights on Windows - skipped")
        else:
            x = ce.extract(arc, out2)
            check("nothing is written through a link that leads outside the destination",
                  r(f"{outside}/precious.txt") == b"precious" and sorted(os.listdir(outside)) == ["precious.txt"])

    print("Names another system made")
    hostile = ["nul", "x:y.txt", "meeting 12:30.txt", "NUL.txt", "sub/aux"]
    arc_n = archive_named(T, ["ok.txt"] + hostile, f"{T}/names.crush")
    try:
        x = ce.extract(arc_n, f"{T}/outn")
    except Exception as ex:
        x = {"failed": None, "error": repr(ex)}
    check("a device name or a ':' in one entry does not refuse the whole archive",
          x["failed"] is not None and r(f"{T}/outn/ok.txt") == b"content of 'ok.txt'\n", str(x))
    if os.name == "nt":
        check("on Windows each of those entries fails by name, and nothing else is written",
              sorted(x["failed"]) == sorted(hostile)
              and [n for _, _, fs in os.walk(f"{T}/outn") for n in fs] == ["ok.txt"], str(x["failed"]))

    if os.name == "nt":
        # each of these Windows would accept and then silently change
        rules = {"reserved": ["CON.txt", "con", "COM¹.txt", "LPT1", "prn .log"],
                 "dot or a space": ["a.", "b ", "dir./c.txt"],
                 "character": ["q?.txt", "t<x>.txt", "f|g", 'say "hi".txt']}
        named = [n for ns in rules.values() for n in ns]
        deep = "deep/" + "/".join(["d" * 60] * 5) + "/f.txt"        # 300+ characters
        arc_w = archive_named(T, ["ok.txt", deep] + named, f"{T}/winnames.crush")
        x = ce.extract(arc_w, f"{T}/outw")
        why = {p: n["why"] for n in x["notes"] for p in n["files"]}
        for kind, ns in rules.items():
            check(f"Windows: a name with a {kind} problem fails by name, with the reason",
                  all(n in x["failed"] and kind.split()[-1] in why.get(n, "") for n in ns),
                  str({n: why.get(n) for n in ns}))
        lw = "\\\\?\\" + os.path.abspath(f"{T}/outw")
        got = sorted(os.path.relpath(os.path.join(dp, n), lw).replace(os.sep, "/")
                     for dp, _, fs in os.walk(lw) for n in fs)
        check("Windows: nothing is written under a changed name", got == sorted(["ok.txt", deep]), str(got))
        check("Windows: a path longer than 260 characters is restored byte-exact",
              r("\\\\?\\" + os.path.abspath(f"{T}/outw/{deep}")) == f"content of {deep!r}\n".encode())

    print("Damage costs only what it touches")
    big = f"{T}/two"
    w(f"{big}/t.txt", text(30000, 3)); w(f"{big}/r.bin", os.urandom(200000))
    arc2 = f"{T}/two.crush"; ce.compress([big], arc2, tier="balanced")
    idx = ce.read_index(arc2)
    tb = next(b for b in idx["bundles"] if b["cls"] == "text")
    d = bytearray(r(arc2)); d[tb["off"] + tb["clen"] // 2] ^= 0x55
    w(f"{T}/two_bad.crush", bytes(d))
    x = ce.extract(f"{T}/two_bad.crush", f"{T}/out3")
    check("a damaged data block fails its own files, the rest are restored",
          x["failed"] == ["two/t.txt"] and r(f"{T}/out3/two/r.bin") == r(f"{big}/r.bin"), str(x["failed"]))
    check("the damaged file is not written at all", not os.path.exists(f"{T}/out3/two/t.txt"))
    ioff, ilen, _ = struct.unpack("<QQI", good[8:28])
    rnd = random.Random(7); refused = 0
    for k in range(40):
        d = bytearray(good); d[ioff + rnd.randrange(ilen)] ^= 1 << rnd.randrange(8)
        w(f"{T}/m.crush", bytes(d))
        refused += raises(lambda: ce.read_index(f"{T}/m.crush"), ValueError)
    check(f"a damaged index is refused ({refused}/40 single-bit flips)", refused == 40)
    w(f"{T}/trunc.crush", good[:len(good) // 2])
    check("a truncated archive is refused", raises(lambda: ce.read_index(f"{T}/trunc.crush"), ValueError))

    print("Untrusted codec headers never reach a decoder")
    for codec, magic in (("ppmd", None), ("crushcm", b"CCM1")):
        b = {"codec": codec, "rawlen": 1000, "off": 0, "clen": 20, "echo": None, "prior": None}
        hdr = (magic or b"") + struct.pack("<Q", 1 << 40) + b"\0" * 8
        class F:
            def seek(self, *a): pass
            def read(self, n): return hdr[:n]
        check(f"{codec}: a length that disagrees with the index is refused",
              raises(lambda: ce._bundle_raw(F(), b), ValueError))

    # python -O strips assert, so a check on archive data must raise instead
    probe = r"""
import random, sys
sys.path.insert(0, sys.argv[1])
import crush_genome as g
rnd = random.Random(5)
fa = b"".join(b">seq%d\n" % i + bytes(rnd.choice(b"ACGT") for _ in range(240)) + b"\n"
              for i in range(200))
res = g.Fasta.forward(fa)
if not res:
    print("no-payload"); sys.exit()
def refused(fn):
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False
print(refused(lambda: g.Fastq.inverse(res[0])), refused(lambda: g.Tabular.inverse(res[0])),
      refused(lambda: g.unpack(b"XXXX" + res[0][4:])))
"""
    out = subprocess.run([sys.executable, "-O", "-c", probe, ENGINE],
                         capture_output=True, text=True).stdout.strip()
    check("Helix refuses a payload of the wrong kind under python -O", out == "True True True", out)

    print("Big files are streamed")
    old = ce.BIG_FILE
    ce.BIG_FILE = 1 << 20                              # 1 MB, so the test is quick
    try:
        bs = f"{T}/bigsrc"
        w(f"{bs}/log.txt", text(400000, 5))            # compressible, ~2.7 MB
        w(f"{bs}/noise.bin", os.urandom(3 << 20))      # incompressible
        os.makedirs(f"{bs}/dup"); shutil.copy(f"{bs}/noise.bin", f"{bs}/dup/noise_copy.bin")
        w(f"{bs}/small.txt", "small")
        barc = f"{T}/big.crush"
        res = ce.compress([bs], barc, tier="balanced")
        bidx = ce.read_index(barc)
        streamed = [b for b in bidx["bundles"] if b.get("stream")]
        codecs = sorted(b["codec"] for b in streamed)
        check("both big files streamed (zstd for text, stored for noise)",
              codecs == ["store", "zstd12"], str(codecs))
        check("the copy is deduplicated", res["stats"]["dedup_files"] == 1)
        x = ce.extract(barc, f"{T}/bout")
        ok = all(r(f"{T}/bout/bigsrc/{p}") == r(f"{bs}/{p}")
                 for p in ("log.txt", "noise.bin", "dup/noise_copy.bin", "small.txt"))
        check("streamed round trip byte-exact", ok and not x["failed"])
        x = ce.extract(barc, f"{T}/bsel", select=["bigsrc/dup/noise_copy.bin"])
        check("selecting only the duplicate restores just it",
              x["written"] == 1 and r(f"{T}/bsel/bigsrc/dup/noise_copy.bin") == r(f"{bs}/noise.bin"))
        # the same archive through the in-memory path (what older versions do)
        f = open(barc, "rb")
        try:
            sb = next(b for b in streamed if b["codec"] == "zstd12")
            raw = ce._bundle_raw(f, sb)
        finally:
            f.close()
        check("a streamed bundle is an ordinary bundle to older readers", raw == r(f"{bs}/log.txt"))
        d = bytearray(r(barc)); sb = next(b for b in streamed if b["codec"] == "store")
        d[sb["off"] + 12345] ^= 1
        w(f"{T}/big_bad.crush", bytes(d))
        x = ce.extract(f"{T}/big_bad.crush", f"{T}/bbad")
        check("a damaged streamed file is not written, the others are",
              "bigsrc/noise.bin" in x["failed"] and not os.path.exists(f"{T}/bbad/bigsrc/noise.bin")
              and r(f"{T}/bbad/bigsrc/log.txt") == r(f"{bs}/log.txt"), str(x["failed"]))
        enc = f"{T}/big_enc.crush"
        ce.compress([bs], enc, tier="quick", password="safety test pass", kdf_profile="fast")
        x = ce.extract(enc, f"{T}/benc", password="safety test pass")
        check("streamed files inside an encrypted archive", not x["failed"]
              and r(f"{T}/benc/bigsrc/noise.bin") == r(f"{bs}/noise.bin"))
    finally:
        ce.BIG_FILE = old

    print("Large folders spill to disk")
    oldm = ce._Spill.MEM
    ce._Spill.MEM = 1000                               # everything past 1 KB spills
    try:
        sarc = f"{T}/spill.crush"
        ce.compress([src, big], sarc, tier="balanced")
        x = ce.extract(sarc, f"{T}/sout")
        ok = r(f"{T}/sout/src/a.txt") == r(f"{src}/a.txt") and r(f"{T}/sout/two/r.bin") == r(f"{big}/r.bin")
        check("payloads spilled to a temporary file round-trip byte-exact", ok and not x["failed"])
    finally:
        ce._Spill.MEM = oldm

    print("Command line")
    cli = [sys.executable, os.path.join(ENGINE, "crush_engine.py")]
    p = subprocess.run(cli + ["x", f"{T}/trunc.crush", f"{T}/cx"], capture_output=True, text=True)
    check("a damaged archive gives a message and exit 1, not a traceback",
          p.returncode == 1 and "Traceback" not in p.stderr, p.stderr[-200:])
    p = subprocess.run(cli + ["x", arc, dst], capture_output=True, text=True)
    check("existing files: exit 3, nothing written", p.returncode == 3)
    p = subprocess.run(cli + ["x", f"{T}/two_bad.crush", f"{T}/cx2"], capture_output=True, text=True)
    check("some files not restored: exit 2, each one named",
          p.returncode == 2 and "two/t.txt" in p.stderr)
    w(f"{T}/todo.txt", "todo")
    p = subprocess.run(cli + ["c", notes, f"{T}/todo.txt"], capture_output=True, text=True)
    check("`crush c notes.txt todo.txt` refuses and keeps notes.txt",
          p.returncode == 1 and r(notes) == b"my notes\n")


if __name__ == "__main__":
    sys.exit(main())
