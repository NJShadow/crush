"""Every Crush bundle codec, encoded by the engine's own encoders on real
data, for the WASM decoders to reproduce byte for byte."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "engine"))
import crush_engine as ce
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "corpus", "")
inputs = {"text": open(C + "text_english.txt", "rb").read()[:300000],
          "elf": open(C + "binary_linux_elf", "rb").read()[:400000],
          "floats": open(C + "data_floats.bin", "rb").read()[:300000],
          "json": open(C + "data_records.json", "rb").read()[:200000],
          "one-byte": b"x"}
man = []
for cid, (enc, dec, label, needs) in ce.CODECS.items():
    for name, d in inputs.items():
        if cid.startswith("crushcm"):
            d = d[:60000]
        prior = ce._prior("text", ".prime" if cid == "crushcm_p" else ".zdict") if needs else None
        try:
            blob = enc(d, prior)
        except Exception as e:
            print("skip", cid, name, e); continue
        if blob is None or dec(blob, prior) != d:
            print("python roundtrip fails", cid, name); continue
        k = f"{len(man):03d}"
        open(f"{out}/{k}.in", "wb").write(d); open(f"{out}/{k}.blob", "wb").write(blob)
        man.append({"k": k, "codec": cid, "input": name, "prior": os.path.basename(prior) if prior else None})
json.dump(man, open(f"{out}/manifest.json", "w"))
print("codec cases", len(man))
