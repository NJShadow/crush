#!/bin/sh
# Full verification of the share page. Run from share/. Archives come from
# tests/build_archives.py (work/arch); ~15 min on 2 shared cores.
set -e
cd "$(dirname "$0")/.."
sh crypto/build.sh >/dev/null
python3 tests/gen_crypto_vectors.py work/cv.json && node tests/test_crypto_kat.js work/cv.json
python3 tests/gen_transform_vectors.py work/tv >/dev/null && node tests/test_transforms.js work/tv
python3 tests/gen_codec_vectors.py work/cv && node tests/test_codecs.js work/cv
node tests/test_container.js work/arch/small.crush work/arch/gen.crush work/arch/locked.crush work/arch/mixed.crush \
     work/arch/dups.crush work/arch/struct.crush work/arch/locked_bal.crush work/arch/pf.crush work/arch/singp.crush
for a in small gen mixed dups struct pf singp; do python3 tools/make_share.py work/arch/$a.crush -o work/$a.html; done
python3 tools/make_share.py work/arch/small.crush -o work/small_b64.html --encoding base64
CRUSH_PASSWORD="correct horse battery staple été" python3 tools/make_share.py work/arch/locked.crush -o work/locked.html
python3 tools/make_share.py work/arch/locked.crush -o work/locked_nopw.html
CRUSH_PASSWORD="Søme passphrase" python3 tools/make_share.py work/arch/locked_bal.crush -o work/locked_bal.html
python3 tests/e2e_share.py "html=work/small.html,arch=work/arch/small.json" "html=work/gen.html,arch=work/arch/gen.json" \
  "html=work/small_b64.html,arch=work/arch/small.json" \
  "html=work/locked.html,arch=work/arch/locked.json,pw=correct horse battery staple été,wrongpw=1" \
  "html=work/locked_nopw.html,arch=work/arch/locked.json,pw=correct horse battery staple été" \
  "html=work/locked_bal.html,arch=work/arch/locked_bal.json,pw=Søme passphrase,wrongpw=1" \
  "html=work/mixed.html,arch=work/arch/mixed.json,singles=8" "html=work/dups.html,arch=work/arch/dups.json" \
  "html=work/struct.html,arch=work/arch/struct.json" "html=work/pf.html,arch=work/arch/pf.json,singles=8" \
  "html=work/singp.html,arch=work/arch/singp.json" \
  "html=work/mixed.html,arch=work/arch/mixed.json,device=Pixel 7" \
  "html=work/locked_bal.html,arch=work/arch/locked_bal.json,pw=Søme passphrase,device=iPhone 13"
python3 tests/e2e_tamper.py
node tests/fuzz_reader.js work/tv work/arch/mixed.crush work/arch/small.crush work/arch/gen.crush
