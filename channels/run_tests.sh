#!/bin/sh
# Fast verification suite (~5-10 min on a quiet 2-core box). Long measurements are separate (see REPORT.md).
set -e
cd "$(dirname "$0")"
export TMPDIR=${TMPDIR:-/tmp}
python3 build.py
python3 tests/test_core_cross.py | tail -1
python3 tests/test_hostile.py | tail -1
python3 tests/test_audio_cross.py | tail -1
python3 tests/e2e_stream_send.py | tail -1
python3 tests/e2e_link.py | tail -1
python3 tests/e2e_camera.py testdata/a_docx.crush --version 20 --ecl l --profile typical --loss 0.3 --codes 80 | tail -1
python3 tests/e2e_audio.py testdata/a_tiny.crush --cond combo --snr 10 | tail -3
(cd py && python3 -m crushchan.paper make ../testdata/a_docx.crush "$TMPDIR/cc_paper.pdf" >/dev/null && python3 -m crushchan.paper restore "$TMPDIR/cc_paper.out" --pdf "$TMPDIR/cc_paper.pdf" | tail -1 && cmp "$TMPDIR/cc_paper.out" ../testdata/a_docx.crush)
(cd py && python3 -m crushchan.stream make ../testdata/a_docx.crush "$TMPDIR/cc_s.mp4" >/dev/null && python3 -m crushchan.stream decode "$TMPDIR/cc_s.mp4" "$TMPDIR/cc_s.out" | tail -1 && cmp "$TMPDIR/cc_s.out" ../testdata/a_docx.crush)
echo ALL PASSED
