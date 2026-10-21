# Crush encryption — design, threat model, and what the audit found

Encryption is optional. Everything below applies only when a passphrase is set.

This document exists so the design can be **checked** rather than trusted. It
states what is protected, what is not, what was attacked, what broke, and what
was changed as a result.

---

## The design

```
  passphrase + 16-byte random salt
     └─ Argon2id (t, m, p)  ──────────────────────────────►  32-byte KEK
                                                              │
  32-byte file key, from the OS CSPRNG                        │
     ├─ wrapped with XChaCha20-Poly1305 under the KEK, AAD = the whole header
     └─ HKDF-SHA256(info="crush/stream/v1")  ─────────────►  32-byte stream key
                                                              │
  payload, in 256 KiB chunks, XChaCha20-Poly1305 STREAM  ◄────┘
     nonce = 16-byte random prefix ‖ 7-byte counter ‖ 1-byte final flag
```

**Argon2id** because it is memory-hard: an attacker guessing passphrases pays
256 MB and about a second *per guess* on the default setting, which is what turns
a GPU farm's billions of guesses per second into a few thousand. `scrypt` is
used only if `argon2-cffi` is not installed, and the interface says so.

**XChaCha20-Poly1305** because its 192-bit nonce leaves room for an explicit
counter without any risk of collision, and because it is fast in software on
machines with no AES acceleration.

**The STREAM construction** (Hoang–Reyhanitabar–Rogaway–Vizár — the same one
`age` uses) because a plain per-chunk AEAD does not stop truncation: cut the file
short and every remaining chunk still authenticates perfectly. The counter in the
nonce defeats reordering, duplication and deletion; the final flag — `0x01` on
the last chunk, `0x00` on every other — defeats truncation.

**A fresh salt and a fresh file key per archive**, so no key and no nonce is ever
reused even if you use the same passphrase for a thousand archives. This is also
why the key wrap can safely use a fixed all-zero nonce: that KEK enciphers
exactly one message in its life.

**The header is the wrap's associated data**, so changing any byte of it — the
version, the suite, the KDF cost, the salt — makes the unwrap fail. The header
needs no MAC of its own.

**A key commitment** (`HKDF(KEK, "crush/commit/v1")`, compared in constant time
before the unwrap). AEADs in this family are not key-committing: a ciphertext can
be built that decrypts under two different keys. Nothing here gives an attacker a
decryption oracle, so it was not exploitable — but the design promises that a
wrong key yields a hard error, and that promise should hold for *any* key rather
than hold by accident.

**Compress, then encrypt.** The other order cannot work: ciphertext is
indistinguishable from noise and does not compress.

---

## What is protected, and what is not

**Hidden.** Every file's name, size, timestamp, permissions and contents; the
folder structure; the number of files; the file types; the per-file hashes. The
index is inside the encryption, so a locked archive cannot be *listed* — only
unlocked.

**Not hidden.** That the file is a Crush archive (it begins `CRUSHENC`), the
format version, the key-stretching parameters, and the archive's total size.

**The length leak, stated plainly.** Because compression happens before
encryption, the size of the output tells you roughly how well the input
compressed. An attacker who can get you to archive a file of their choosing
alongside a secret can, in principle, learn something from how well the two
compress together — the effect behind CRIME and BREACH. This is inherent to
compressing before encrypting. No archive format avoids it.

**Out of scope.** An attacker with code running on your machine while Crush is
open. Key material in swap or hibernation files — Python cannot reliably wipe
`bytes` from memory. Anything that binds an archive to a name, a time or an
author: two archives made by the same person with the same passphrase can be
swapped for one another and every check will still pass.

---

## Opening an archive someone else made

This section is about the compression side, not the cipher, but it belongs in
the threat model because of what it means for archives you receive.

**What reads archive data.** To give back a file's original bytes, extraction
runs decoders on data taken from the archive: the pinned zlib (`crushz.dll`,
which only *writes* deflate and never parses archive data), the preflate
reconstructor (`crushpf.exe`, which reads a correction record from the
archive), the JPEG decoder (`crushjpg.exe`: Brunsli, and Crush's own jcm model),
and the context-mixing codec (`crushcm.exe`). A deliberately malformed archive
is input to all of them.

**Encrypted archives are authenticated first.** With a passphrase, every chunk
of the archive — index and data — is authenticated before a single byte is
used, so a tampered encrypted archive never reaches any decoder. What remains
is an archive *made* by someone who knows the passphrase: you are trusting them
the way you trust any file they send.

**Unencrypted archives from strangers deserve the care any file from a
stranger does.** The decoders run as separate processes, so a malformed record
that crashes one costs that one file (reported by name, with the reason), never
Crush itself. A separate process is containment, not a sandbox: it limits the
damage from a crash, not from an exploit. So the decoders were attacked
directly, as described next.

**Fuzzing, and what it found.** Mutated inputs were fed to each decoder's
extraction path, built with AddressSanitizer and UndefinedBehaviorSanitizer,
one process per case with a 60-second limit:

| Decoder | Cases | Before fixes | After fixes |
|---|---:|---|---|
| crushjpg, jcm records | 6,000 | 0 findings | — |
| crushjpg, Brunsli records | 1,500 | 0 findings | — |
| crushpf, framing itself mutated (first run, stopped early to fix) | not recorded | **127 sanitizer reports** (below) | re-run of 1,500: 0 - every malformed frame refused cleanly |
| crushpf (preflate), correction records, framing kept valid | ~7,500 + 10,000 | **6 more crash inputs** after the first fixes | **0 findings in 10,000** |
| crushpf, mutated raw deflate at compression time | 2,000 | | 0 findings |
| crushjpg v2 (round 11), jcm v2 records / v1 records | 13,000 / 2,000 | 0 findings | — |
| crushnn (round 11), mutated streams, length kept small so cases finish | 250 | 0 findings (17 hit the 60 s limit: valid-looking long streams, slow under ASan) | — |
| **Whole extract path** (release audit): mutated archives covering every tier, codec and transform, encrypted and portable; full `extract` in a subprocess, every output hashed | 12,941 | **44 silent index cases** (wrong name / container metadata), a pyppmd use-after-free, a crushcm heap overflow and 2-minute hang, an Echo memory blow-up | 10,212 re-run on the fixed engine: **0 silent, 0 crashes** |

preflate's reconstructor trusted its correction record. A crafted record could
drive the input position past the end of the data (heap out-of-bounds reads),
declare tree sizes that overran fixed-size stack arrays (an out-of-bounds
**write**), index a 7-entry table with an out-of-range id (a write), make the
arithmetic decoder shift by 32 or divide by zero, or request a multi-gigabyte
allocation. All of these are fixed in `engine/crushpf-src/preflate/` — every
change is marked `CRUSH PATCH` — by checking each decoded length, distance, tree
size and table index against the data it applies to, and failing the record
instead. The patched build produces byte-identical output to the unpatched one
on every valid stream measured (93 streams, Linux and Windows).

**crushnn** (round 11, the learned code model) is crushcm's coder plus a
small integer-only network. Its stream header carries a 64-bit length that the
helper used to trust; the length is now bounded twice — the engine refuses any
`crushnn_m` stream claiming more than 256 KB before the helper starts, and the
helper itself refuses more than 256 MB (`CRUSH PATCH` in
`engine/crushnn-src/crushnn.c`). Worst case for a hostile archive is therefore
about 45 seconds of decoding one bundle, then a failed verification. The model
file `priors/code.cnn` is pinned by SHA-256 in the engine; a different file is
never used, for compression or extraction.

**Share pages** (round 11, `share/`): a self-opening HTML archive runs its
decoders as WebAssembly inside the browser's sandbox, with a
Content-Security-Policy in the page that forbids every network request. An
encrypted page authenticates every chunk before using any of it, exactly as
the app does (19/19 tamper cases refused; 1,800 mutated payloads and 120
mutated archives through the reader, 0 files reported correct with wrong
bytes). A hostile page can make a tab use a lot of memory; it cannot produce a
file that passes the SHA-256 check, and it cannot reach the network. File
names from the archive are only ever inserted as text, never as HTML.
The QR, paper and sound channels (`channels/`) carry the archive unchanged and
release it only after its SHA-256 matches; 3,120 forged or mutated packets
produced no crash and no output.

**The release audit's whole-path campaign** found that the per-file SHA-256
held everywhere it applied — no corrupted *data* region ever produced a wrong
file reported as good — but the index itself had no checksum, so a damaged
index could still rename a file or change `--harder` container metadata while
reporting success. The index now carries a zstd frame checksum (archives made
from 1.0.0 on) and is type-checked before use. Three decoders trusted lengths
from the archive: pyppmd (a use-after-free, `SIGSEGV`), crushcm (a heap
overflow past 4 GB, and a 2-minute hang), and Echo's chunk table (gigabytes of
allocation from a few hundred bytes). Each length is now checked against the
index before the decoder runs, and `crushcm.c` bounds it again (`CRUSH PATCH`;
the rebuilt `crushcm.exe` was checked to give byte-identical output to the
previous build). Encrypted archives were unaffected: all 1,480 encrypted
mutants were refused by the AEAD before any decoder ran.

Not fuzzed yet: `crushcm.exe` on its own (unchanged since v3; crushnn shares
its coder, so the 250 crushnn cases cover part of it) and the Brunsli *parser*
on hostile JPEGs at compression time (Brunsli is fuzzed continuously by its
authors).

---

## The audit

Three independent adversarial reviews were run against the first working
version: one on the cryptographic design and implementation, one on the
application and transport layer, and one that built a 79-check, 402-case
tamper suite. They were told to assume the code was wrong.

They found real problems. These are all of them, and what was done.

### Fixed before shipping

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | **High** | `EncryptedReader` authenticated only the chunks a caller happened to read, and the `verify()` that was supposed to cover the rest had **zero call sites**. Listing an archive checked 2 of 10 chunks; extracting one file of eight checked 3. A proof-of-concept overwrote every chunk the operation skipped and Crush still reported the archive healthy. | The final chunk — whose flag is the entire anti-truncation defence — is now authenticated in the constructor, and the engine opens every encrypted archive with `verify_all=True`, so all chunks are checked before a byte is used. |
| 2 | **High** | The KDF ceiling accepted `t=32, m=4 GiB, p=64` from an *unauthenticated* header. Authentication here **is** key derivation, so one byte flipped in an intercepted archive turned any unlock into a ~15-minute, 4 GB stall that ended in an unhandled `ValueError`. | The ceiling is now twice the most expensive profile Crush writes, derived from the profile table so the two cannot drift; the **product** of passes and memory is budgeted, not each field; Argon2's own `m ≥ 8p` rule is enforced; library exceptions are normalised to `CryptoError`; and `header_info` applies the same bounds so the interface never promises a free unlock it cannot deliver. |
| 3 | **High** | The page carrying the API token was served to **anyone**, and the port was written to a world-readable log. A second process that knew only the port scraped the token from an unauthenticated `GET /` and then drove every method — arbitrary file read, arbitrary file write, subprocess spawn. | The page is released only to a request carrying a per-run launch key, which lives in the URL the window is opened with and is never written to the log. The log itself is created `0600`. |
| 4 | **Medium** | `encrypt_stream` assumed `read()` returns exactly what was asked for. It does not, for pipes, sockets or TLS — producing an archive that is silently, permanently undecryptable. | A `_read_exact` loop in all three code paths. |
| 5 | **Medium** | `decrypt_file` opened the destination `O_TRUNC` *before* authenticating, so one flipped bit destroyed a pre-existing file of that name and then deleted it. | Output goes to a scratch name (`O_EXCL`, `O_NOFOLLOW`, `0600`), is fsynced, and is moved into place only after the final chunk authenticates. |
| 6 | **Medium** | Every encrypted compression was reported to the user as a **failure**: the report path re-opened the finished archive with no passphrase. Encryption had never actually worked end to end through the interface. | The passphrase is passed through to the report. |
| 7 | **Medium** | The passphrase strength meter's leetspeak table mapped `$`→`a` and `!`→`s`. Both are wrong (`$` is `s`, `!` is `i`/`l`), so `Pa$$w0rd`, `$hadow`, `!loveyou` and `$un$hine` were all rated *reasonable*. | Table corrected, and the de-leet now tries identity-or-substitution per character rather than one global swap. The common-password list went from 33 entries to 218. |
| 8 | **Low** | No key commitment. | Added, as described above. |
| 9 | **Low** | Worker-thread tracebacks reached the interface (no passphrase in them, but full internal paths). | Replaced with a one-line message; details go to the log. |
| 10 | **Low** | `crush x` exited 0 even when files failed verification — a scripted restore could not tell that it had failed. | Exits 2 and names the failures. |
| 11 | **Low** | `assert` statements enforced the header length and the word-list invariant; `python -O` strips those. | Real checks that raise. |
| 12 | **Low** | A mistyped passphrase on the command line produced an archive nobody could ever open. | `--ask` prompts twice; passing `-k` interactively now warns that the process list is public. |

One fix broke the app, and the end-to-end test caught it: blanket-refusing any
request with an `Origin` header also refuses the app itself, because browsers
send `Origin` on same-origin POSTs. The check now compares against this server's
own origin.

### Known and accepted

- **The strength meter is a heuristic, not zxcvbn.** It recognises 218 common
  passwords, leetspeak variants of them, and Crush's own word list. An uncommon
  dictionary word — `Tr0ub4dor&3` is the canonical example — still scores higher
  than it deserves. The interface says so, and offers **Suggest**, which is
  provably 72 bits. Shipping a full cracking dictionary is the only real fix.
- **Corrupted header fields are distinguishable from a wrong passphrase.** The
  version, suite and KDF costs are cleartext in every archive, so naming the
  problem tells an attacker nothing they did not already have, and it makes real
  corruption diagnosable. The secret-dependent fields — salt, prefix,
  commitment, wrapped key — all raise the same indistinguishable `WrongPassword`.
- **The plaintext archive briefly exists on disk during encryption.** Crush
  builds it into a temporary file and then streams it through the cipher, because
  the archive format needs to seek backwards to write its index. On Linux with
  `O_TMPFILE` that file has no name anything can open; on Windows, and on
  filesystems without `O_TMPFILE`, it has a directory entry for its lifetime.
  Either way the blocks land on the output filesystem and unlinking is not
  erasure. If that matters to you, encrypt to a volume that is itself encrypted.
  The same applies to two more temporary files: when the input is larger than
  512 MB, transformed file contents wait for the codec stage in a temporary
  file beside the output; and "Send it anywhere → web page" on an archive that
  needs re-packing restores its contents into a hidden scratch folder beside
  the archive for the length of the re-pack. Both are removed afterwards,
  including on failure.
- **Nothing binds an archive to a name, a version or a time.** Two archives from
  the same person with the same passphrase can be swapped wholesale and every
  check will pass.
- **A passphrase given as `-k` on the command line is visible in `ps`.** Use
  `--ask` or `CRUSH_PASSWORD`.

---

## What was verified as correct

The audits also checked a great deal that turned out to be right, and that is
worth recording as evidence of coverage rather than only listing complaints.

- **HChaCha20 is correct** — and it is hand-built on top of `cryptography`'s
  ChaCha20 stream cipher, which is exactly where a subtle error would hide. It
  reproduces the RFC draft-irtf-cfrg-xchacha-03 §2.2.1 vector, and one auditor
  re-derived that vector with a from-scratch ChaCha20 rather than trusting it.
  The load-bearing assumption — that the library treats its 16-byte nonce as
  state words 12–15, little-endian — was verified over 300 random (key, nonce)
  pairs with zero mismatches, plus deliberate edge cases including a counter word
  at `0xFFFFFFFF`.
- **No nonce is ever reused.** Instrumented across eight archives built with the
  same passphrase: 34 encryptions, zero repeated (key, nonce) pairs.
- **The header is fully authenticated**; every single-byte mutation of every
  field produces a hard failure and no output.
- **402 tamper cases, zero silent forgeries.** 204 bit-flips across all header
  bytes, 42 body and tag mutations, 47 truncation offsets, 9 append variants, 12
  structural mutations, 9 cross-archive splices including with the *same*
  passphrase. Every one was rejected by an exception, in **both** decryption
  paths. Not one produced plaintext, right or wrong.
- **Known-answer tests** reproduce exactly: HChaCha20 and XChaCha20-Poly1305 from
  draft-irtf-cfrg-xchacha-03, ChaCha20-Poly1305 from RFC 8439, and Argon2id *and*
  Argon2d from RFC 9106.
- **No oracle.** Right passphrase and six kinds of wrong all land within
  91.6–97.3 ms and raise the identical error. No exception, log line or
  traceback anywhere contains the passphrase or any derived key.
- **Key separation is clean**, HKDF is used correctly, and the file key is never
  used directly for anything.
- **The browser threat model holds**: cross-origin requests, rebound `Host`
  headers and missing tokens are all refused; the token comparison is
  constant-time; session ids are 144 bits; private methods are unreachable.
- **NFC normalisation is correctly NFC, not NFKC** — a passphrase typed in NFD
  opens an archive created in NFC, and nothing beyond Unicode canonical
  equivalence is merged.

---

## Running the checks yourself

```
python lab/audit/tests/test_crypt.py          # 79 checks, 402 tamper cases
python lab/audit/tests/test_localauth.py      # the loopback server's auth model
python lab/audit/tests/e2e_crypt.py           # the whole flow, in a real browser
```

The tamper suite writes its own `RESULTS.md` as it runs, so the table can never
drift from the code that produced it.
