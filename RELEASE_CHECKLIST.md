# Release checklist — Crush 1.0.0

Everything that could be checked in the build environment has been. The steps
below need your PC or your GitHub account.

## 1. On your PC, before publishing

1. Run **`Run-Tests.bat`** in the Crush folder (about 10 minutes). It runs the
   engine self-test and the safety, unwrapping, learned-model and encryption
   suites with *your* Python and the Windows helpers. These are the only runs
   on real Windows, so this step matters. Every suite should end in
   `0 failed` / `ALL SUITES PASSED`.
2. Open the app (`Crush.bat`) and try one archive end to end:
   - compress a folder;
   - open the archive and extract it into a new folder;
   - extract it again into the same folder. You should be asked before
     anything is replaced.
3. Optional: try your 3.2 GB video. It is streamed (a few MB of RAM) and should
   come back byte-exact. It could not be copied into the test environment.

## 2. Make the repository

1. Create an empty repository on GitHub (no README, no licence; Crush already
   has both).
2. In the GitHub folder (`C:\Github\crush`, which holds exactly the files
   meant for the repository):
   ```
   cd C:\Github\crush
   git init
   git add .
   git status                     # check: no "Claude outputs", no crush-debug.log, no corpus/
   git commit -m "Crush 1.0.0"
   git branch -M main
   git remote add origin https://github.com/<you>/crush.git
   git push -u origin main
   ```
   `.gitignore` already leaves out logs, caches, build output, generated
   corpora and your "Claude outputs" folder. `.gitattributes` keeps the `.bat`
   files in Windows line endings.
3. Or, with **GitHub Desktop** instead of the command line: File → Add local
   repository → choose `C:\Github\crush` → "create a repository" → commit
   ("Crush 1.0.0") → Publish repository.

## 3. Publish the release

1. On GitHub: **Releases → Draft a new release**, tag `v1.0.0`, title
   "Crush 1.0.0".
2. Paste the 1.0.0 section of `CHANGELOG.md` as the description.
3. Attach `Crush-1.0.0-windows.zip`. It is the app ready to run: unzip, then
   double-click `Crush.bat`.

## Known limits to state openly (already in the README)

- Tested on real Windows only through `Run-Tests.bat` on your PC.
- `Build-Crush-exe.bat` (a standalone .exe) was updated but has not been
  rebuilt or tested. Leave it out of the release notes until it has been.
- Share pages were tested in Chromium only. They were not tried in Safari,
  Firefox or on real phones.
- The QR, paper and sound channels were tested against simulated cameras,
  printers and rooms.
- The shared priors (`priors/*.prime`, `*.zdict`) are verbatim open-source
  files from a Linux system, each under its own licence (see
  `THIRD_PARTY_NOTICES.md`). If you want zero licence ambiguity, a future
  version could rebuild them from permissively licensed sources only.
  Archives made with the current priors would still need these files.
