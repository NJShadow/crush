import sys, os, time, json, collections
sys.path.insert(0, "/home/claude/crush/engine")
import crush_engine as ce
T = {"pdf": ce.PdfUnwrap, "gz": ce.GzipUnwrap, "zip": ce.DeflateUnwrap, "png": ce.PngUnwrap}[sys.argv[1]]
files = [l.strip() for l in open(sys.argv[2]) if l.strip()]
agg = collections.Counter(); t0 = time.time(); bad = 0; per = []
for f in files:
    d = open(f, "rb").read()
    t = time.time(); r = T.forward(d); dt = time.time() - t
    agg["files"] += 1; agg["bytes"] += len(d)
    if not r: per.append((f, len(d), 0, 0, 0, 0, dt)); continue
    ok = T.inverse(r[0], r[1]) == d
    bad += not ok
    if T is ce.PngUnwrap:
        z = int(r[1]["pi"] >= 0); pf = 1 - z; zb = len(d) if z else 0; pb = len(d) if pf else 0; corr = r[1].get("dl", 0)
    else:
        rows = r[1]["r"]
        z = sum(1 for x in rows if len(x) != 6); pf = sum(1 for x in rows if len(x) == 6)
        zb = sum(x[1] for x in rows if len(x) != 6); pb = sum(x[1] for x in rows if len(x) == 6)
        corr = sum(x[5] for x in rows if len(x) == 6)
    agg["unwrapped_files"] += 1; agg["zlib_streams"] += z; agg["pf_streams"] += pf
    agg["zlib_bytes"] += zb; agg["pf_bytes"] += pb; agg["corr"] += corr
    per.append((f, len(d), z, pf, pb, corr, dt))
print(dict(agg), "roundtrip failures:", bad, "time %.1fs" % (time.time() - t0))
if agg["pf_bytes"]: print("correction / preflate-rebuilt bytes = %.2f%%" % (100 * agg["corr"] / agg["pf_bytes"]))
json.dump(per, open(sys.argv[3], "w"))
