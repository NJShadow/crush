"""Format-valid large archives for the size-limit test, written in streaming
fashion (the engine holds every payload in memory and was killed by the
container's memory limit at 250 MB). Same container as crush_engine writes:
store codec, one bundle per 10 MB file, zstd index - read back and checked
with the engine's own extract() before use."""
import hashlib, json, os, struct, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "engine"))
import crush_engine as ce, zstandard as zstd
out = sys.argv[1]
for mb in map(int, sys.argv[2:]):
    arch = os.path.join(out, f"big{mb}.crush")
    files, bundles, exp = [], [], {}
    with open(arch, "wb") as f:
        f.write(ce.MAGIC + bytes([1, 0]) + b"\0" * 20)
        for i in range(mb // 10):
            data = os.urandom(10 << 20)
            h = hashlib.sha256(data).hexdigest()
            rel = f"src{mb}/part{i:03d}.bin"
            bundles.append({"id": i, "cls": "generic", "codec": "store", "off": f.tell(), "clen": len(data), "rawlen": len(data), "n": 1})
            f.write(data)
            files.append({"p": rel, "s": len(data), "h": h[:32], "m": time.time(), "b": i, "l": len(data)})
            exp[rel] = {"sha": h, "size": len(data)}
        ib = json.dumps({"app": "Crush", "fmt": 1, "tier": "quick", "files": files, "bundles": bundles,
                         "stats": {}, "total_in": mb << 20}, separators=(",", ":")).encode()
        ic = zstd.ZstdCompressor(level=19).compress(ib)
        ioff = f.tell(); f.write(ic); f.seek(8); f.write(struct.pack("<QQI", ioff, len(ic), 1))
    json.dump({"files": exp}, open(arch[:-6] + ".json", "w"))
    r = ce.extract(arch, os.path.join(out, "chk"), select=[files[0]["p"], files[-1]["p"]])
    print(mb, os.path.getsize(arch), "engine check:", r["verified"], "verified", r["failed"], flush=True)
