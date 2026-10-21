# Reproducing the Crush benchmarks

These are the scripts that produced BENCHMARKS.md. They were written to run on
Linux and use absolute paths; change the `C = ...` / `CORPUS = ...` constants at
the top of each to point at wherever you put them.

    build_corpus.py    the original 133 MB corpus
    build_corpus2.py   the extended 195 MB corpus (325 files)
    entropy_wall.py    the four limit experiments
    baseline.py        every competitor, every file, timed
    make_report.py     the comparison tables
    edge_tests.py      429 adversarial files, v1 layers
    edge_tests2.py     41 hostile files against the v2/v3 layers

`results/` holds the raw logs and JSON from the runs reported in BENCHMARKS.md,
including edge1_v3.log and edge2_v3.log from the final v3 verification.
