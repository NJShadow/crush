"""Round-trip and determinism checks for the recurrent-mixer build.

A model change that shrinks the output is worthless if the decoder cannot
reproduce the encoder's probabilities exactly. Three checks:
  1. compress -> decompress -> compare, on every file
  2. compressing the same input twice gives identical bytes
  3. a -O0 build and a -O2 build produce identical bytes (no FP drift)
"""
import os, subprocess, sys, time, hashlib
from pathlib import Path

D = Path("/home/claude/crush/lab/v4/mixer")
O2, O0 = D / "tmp/lstm_o2", D / "tmp/lstm_o0"
REF = D / "crushcm_ref"
FILES = [
    "/home/claude/crush/corpus/text_english.txt",
    "/home/claude/crush/corpus/code_python.py",
    "/home/claude/crush/corpus/code_c.h",
    "/home/claude/crush/corpus/data_records.json",
    "/home/claude/crush/corpus/data_xml.xml",
    "/home/claude/crush/corpus/binary_linux_elf",
    "/home/claude/crush/corpus2/struct/server.log",
]
CAP = 4 << 20
T = D / "tmp"

def run(exe, mode, i, o):
    subprocess.run([str(exe), mode, str(i), str(o)], check=True, capture_output=True)

fails = []
print(f"{'file':<26}{'bytes':>11}{'ref':>11}{'lstm':>11}{'change':>9}"
      f"{'ref MB/s':>10}{'new MB/s':>10}  checks")
print("-" * 100)
tr = tn = tin = 0
for f in FILES:
    data = open(f, "rb").read()[:CAP]
    src = T / "v_in"; src.write_bytes(data)
    a, b, c = T / "v_ref.cm", T / "v_new.cm", T / "v_back"

    t0 = time.time(); run(REF, "c", src, a); tref = time.time() - t0
    t0 = time.time(); run(O2, "c", src, b);  tnew = time.time() - t0

    run(O2, "d", b, c)
    rt = c.read_bytes() == data

    b2 = T / "v_new2.cm"; run(O2, "c", src, b2)
    repeat = b2.read_bytes() == b.read_bytes()

    b3 = T / "v_new_o0.cm"; run(O0, "c", src, b3)
    same_opt = b3.read_bytes() == b.read_bytes()

    sa, sb = a.stat().st_size, b.stat().st_size
    tr += sa; tn += sb; tin += len(data)
    ok = rt and repeat and same_opt
    if not ok:
        fails.append(f)
    marks = ("round-trip " + ("ok" if rt else "FAIL") + " · repeat " +
             ("ok" if repeat else "FAIL") + " · -O0==-O2 " + ("ok" if same_opt else "FAIL"))
    print(f"{Path(f).name:<26}{len(data):>11,}{sa:>11,}{sb:>11,}{100*(sb-sa)/sa:>8.2f}%"
          f"{len(data)/1e6/tref:>10.2f}{len(data)/1e6/tnew:>10.2f}  {marks}", flush=True)

print("-" * 100)
print(f"{'TOTAL':<26}{tin:>11,}{tr:>11,}{tn:>11,}{100*(tn-tr)/tr:>8.2f}%")
print("\n" + ("ALL CHECKS PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
