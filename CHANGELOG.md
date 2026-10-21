# Changelog

## 1.0.0 — first public release

The release audit (round 12) attacked every path where Crush reads your files,
writes an archive, or writes files back out: a data-safety code audit, a
hostile-archive campaign (about 13,000 damaged or crafted archives pushed
through the full extract path, then 10,212 more against the fixed engine), and a review of the fixes themselves.
Everything below was found, fixed, and then covered by a test
(`tests/test_safety.py`, 29 checks).

### Data safety

- **Extraction no longer replaces existing files without asking.** If any target
  exists, nothing is written; the app offers *Replace them* or *Choose another
  folder*, and the command line exits 3 unless given `--overwrite`.
- **Nothing is half-written under a real name.** Archives and restored files go
  to a temporary name and are renamed into place only when complete and
  verified. A crash, a full disk or a kill used to leave a truncated archive in
  place of the previous backup, or a truncated file in place of the user's file.
- **Compressing refuses to overwrite a file that is not an archive.**
  `crush c notes.txt todo.txt` (with the output name forgotten) used to replace
  notes.txt.
- **Two inputs stored under one name are refused.** Two `report.txt` files from
  different folders were stored under one name, and extraction kept only the
  last of them while reporting both as restored.
- **A file that changes while it is read is archived as read.** It used to be
  hashed and read separately, and then refused at extraction.
- **Unreadable folders and missing paths stop the run** with a message, instead
  of being skipped silently. Links are skipped and counted, and the count is
  shown.
- **A duplicate is restored from its verified twin's bytes in memory**, never
  copied back off the disk, where a colliding name could have replaced it.
- **One bad entry or damaged block costs only its own files.** It used to end
  the whole extraction.
- **Nothing is written through a link in the destination** that leads outside
  it. On Windows, names that Windows would silently change are reported
  instead of written: `CON`, `aux.c`, names with a trailing dot or space, and
  names containing `:`.
- **Share pages, paper backups and sound files never overwrite an existing
  file.** A page that needs re-packing is built from the archive's own verified
  contents, not from whatever is queued.

### Damaged or hostile archives

- **The index now carries a checksum.** Before, a single flipped bit could
  rename a file or alter `--harder` container metadata, and the restore still
  reported success.
- **Codec headers are checked against the index before any decoder runs.**
  Fuzzing found a use-after-free in pyppmd, a heap overflow in crushcm (a
  `CRUSH PATCH` bound was added in `crushcm.c`; the rebuilt `crushcm.exe` gives
  byte-identical output) and Echo headers that could ask for gigabytes of
  memory.
- **The command line prints one clear line and an exit code**, never a
  traceback (`CRUSH_DEBUG=1` shows it).

### Size and memory

- **Files over 256 MB are streamed.** They are hashed, compressed or stored,
  read back and verified, and restored in 4 MB chunks. 1.6 GB of test data
  compressed in 66 MB of RAM. A real 4 GB+ round trip was byte-exact.
- **Large folders spill to a temporary file** instead of holding everything in
  memory until the codec stage.
- **Echo no longer needs 24 bytes of RAM per input byte** (a 200 MB bundle
  needed 4.8 GB).
- **Already-compressed media skips the slow codecs** when sampling shows that
  nothing compresses.

### Compatibility

Every archive made by earlier versions still extracts byte-exact (41 archives
across every codec and tier were checked). Archives made by 1.0.0 also open in
the previous version, including streamed big files.
