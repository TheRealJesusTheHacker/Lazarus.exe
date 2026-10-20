# Changelog

All notable changes to LAZARUS.EXE are recorded here, newest first.

## [1.0.0] — 2026-10-20

Phase 5 — the animated interface, packaging, and the first public
release of LAZARUS.EXE.

### Added

- **The Lazarus window** — a PySide6 interface, green on black,
  over the whole Phase 1–4 engine; every control works, nothing
  is decoration. Five pages: **Restore** (the flagship — pick a
  snapshot by date, browse what it holds, choose a file, a
  folder, or everything, and restore it to where it lived or
  somewhere new, with the engine's safety copy taken first and
  the plain-language report at the end); **Snapshots** (take one,
  label it, verify the store byte for byte); **Deleted files**
  (scan a drive or image with the honest odds beside every file,
  bring back the ones you choose onto a different drive, and
  rescue a failing drive to an image with the resumable progress
  map live on screen); **Locked files** (open a BitLocker volume
  with your own recovery key — asked once, tried once, never
  kept; identify which known ransomware family locked a folder,
  run its published decryptor on copies only, and read the
  one-screen honest assessment); **Backup** (the loud status
  first, then set up, run now, history, and the tidying rule with
  its preview).
- **Self-installing Lazarus.exe** — the shipped one-file program
  offers, once, to install itself: a copy in Program Files and a
  Start Menu entry, behind one Windows approval prompt, each step
  verified and reported in plain language. Saying no costs
  nothing — the copy you ran keeps working. Removing the
  installed copy is one click in a portable copy's window, or
  `--lazarus-uninstall`.
- **Lazarus Portable** — LazarusPortable.zip: the same program as
  a no-install edition that runs from a folder or USB drive.
- **Release checksums** — SHA256SUMS.txt covers both downloads.

### Fixed

- **Rescue image writes are byte-exact on Windows** — the rescue
  image file is now opened in binary mode (`O_BINARY`) at both the
  fresh-image and resume opens. Without it, Windows text mode
  translated every 0x0A byte in rescued data to 0x0D 0x0A as it was
  written, corrupting the image. Found by the fleet-wide Windows
  text-mode audit (2026-10-20); regression tests model the Windows
  runtime and drive a real rescue end to end
  (`tests/test_rescue_binary_mode.py`).
- **Scanning and rescue now run on Windows at all** — the
  drive-reading and image-writing code used two system
  operations that only some systems provide (positioned read
  and write); on Windows they do not exist, so a scan or a
  rescue there would have failed before reading a single byte.
  Both now use the positioned read and write each system
  offers, with the same bytes landing in the same places, and
  the release workflow's full-suite run on Windows proves it.

### Notes

- 544 passed, 2 skipped in the full suite, including 133 tests
  added for the interface, the installer, and the audit fixes;
  the suite also runs on Windows in the release workflow before
  anything is published.
- Audit: an independent adversarial pass over the new surface
  found 16 items (5 of them serious — a stale decrypt-family
  picker, a prune that could outrun its own preview, quitting
  mid-job, a pipe source that could hang a scan, and a multi-root
  restore report that could understate what came back). Every
  one was fixed and pinned with a regression test; the re-audit
  then caught one more serious item the first pass had missed (a
  shortcut pointing at a pipe slipped past the pipe check), which
  was fixed and pinned the same way. A final re-audit verified
  those fixes and found no regressions — and the release proof
  build then caught one last interaction (the packaged program's
  start-up self-check could wait forever on a close-confirmation
  dialog nobody could see), which was fixed, pinned, and proven
  on a rebuilt packaged program before release. The release gate
  then did its own work: the first Windows runs of the full
  suite caught a test that could not start on Windows, window
  tests waiting on real Windows tools, and the
  positioned-read/write problem above — each fixed and re-proven
  before this release shipped.
- Still seam-only until a real Windows run: Task Scheduler
  registration firing, manage-bde accepting a key, the installer
  and the window on a real machine — the full list is in the
  roadmap, and nothing here claims it early.

## [0.5.0] — 2026-10-20

Phase 4 — backup automation: the versioned store, made automatic.

### Added

- **One-click backup** — `lazarus backup setup` records, in a
  plain configuration file, which folders matter, where the
  backup store lives, and how many restore points to keep;
  `lazarus backup run` then snapshots every configured folder
  into the Phase 1 versioned store in one step. Each run is a
  new dated restore point, restorable with the Phase 1 engine
  exactly like any other snapshot. Setup checks every answer
  before anything is saved — a backup time that is not a time,
  or a keep-count of zero, is refused up front in plain language
  instead of failing later. Folder and store paths are saved in
  full, never as shortcuts relative to wherever the command
  happened to run, so a scheduled run backs up the folders that
  were chosen — not a same-named folder somewhere else.
- **Scheduled backup with honest catch-up** — a daily or weekly
  schedule, the owner's choice. `lazarus backup due` is the
  entry point the system scheduler calls: it works out what was
  due, runs at most one catch-up backup, and the history names
  the trigger honestly — *scheduled* when it ran on time,
  *catch-up* when the computer was off and it ran late. A
  backup that already covered a scheduled time never runs
  twice for it. Registration with Windows Task Scheduler is
  built as tested `schtasks` commands behind a platform seam —
  the commands are proven here; registering them for real
  happens on a Windows machine.
- **Loud target status, before disaster** — `lazarus backup
  status` answers in one of a few plain states: READY, TARGET
  MISSING (the backup drive is not there), TARGET FULL (with
  the free space and the estimated need, both as numbers),
  STORE DAMAGED (with what to do next), NEVER RUN, OVERDUE, and
  — when an earlier backup was cut off mid-save — a state that
  says exactly that and names the one command that clears it.
  Every run pre-flights the same checks before copying a byte:
  target present, store reachable and writable, enough room for
  an honest estimate (based on what the last run actually
  copied, and labelled an estimate), and a spot-check of the
  store's integrity on a regular cadence — the same "is a check
  due" rule drives both running and status, so status can never
  say READY on a day a run would have caught damage. A forged
  or clock-skewed history entry dated in the future cannot
  suppress those checks.
- **A run history that survives restarts** — every attempt is
  recorded: when it started, what triggered it (manual,
  scheduled, catch-up), the outcome, which snapshots were made,
  how many bytes were copied, how long it took, and — on
  failure — the plain reason. Refused runs are recorded too;
  there are no silent gaps. Damaged lines in the history file
  are counted and skipped, never trusted and never fatal.
- **Retention that cannot remove the wrong backup** —
  keep-how-many or keep-how-long, applied only after a fully
  successful run, never during or after a failed one, and
  reported exactly: what was removed, what was left alone, and
  why. The newest backup is never removed. Safety copies taken
  before a restore are never retention candidates. A backup
  whose date cannot be read is left alone, always — Lazarus
  never removes a backup it cannot date. Copies shared between
  snapshots are kept while any snapshot still needs them, and
  brand-new copies are given a day's grace before any tidy-up
  may sweep them, so a snapshot still being written can never
  be tidied out from under itself. `lazarus backup prune`
  previews the exact plan as a dry run before anything is
  removed, and the same command clears away the leftovers of a
  backup that never finished saving, once they are a day old.
- **One backup at a time** — a run lock makes a second backup
  started while one is already running refuse plainly, naming
  the lock file and how to clear it if no backup is actually
  running. Overlapping scheduled and manual runs were proven to
  produce exactly one backup, one refusal, and an intact store.

### Notes

- The suite stands at 411 tests passing (2 skipped) — 408 from
  Phase 4's own work, plus 3 from a fleet-wide rescue fix that
  landed on main while this phase was in build. Phase 4
  went through an independent adversarial audit — 16 findings,
  including a sweep that could follow a shortcut folder out of
  the store, relative paths that could back up the wrong folder
  from a scheduled task's working directory, and a damaged
  snapshot record that could blind the integrity check. Every
  code-fixable finding was fixed and pinned by a permanent
  test; the few accepted residuals (for example, a lock held by
  a recycled process number fails safe and says how to clear
  it) are recorded in the roadmap's Phase 4 as-built note.
  The test suite itself caught one real bug before the audit:
  the integrity spot-check cadence was off by one, and a failed
  check could stop future checks from running.
- Windows Task Scheduler registration is seam-only until a
  Windows-hardware run: the commands are built and tested, but
  no real task has been registered yet, and nothing here claims
  otherwise. The full Windows verification list is in the
  roadmap's Phase 4 note.

## [0.4.0] — 2026-10-20

Phase 3 — decrypt & unlock files off drives.

### Added

- **Owner-key BitLocker unlock** — `lazarus volumes` lists the
  BitLocker volumes Windows reports, and `lazarus unlock E:`
  opens one with the owner's own recovery key, through Windows'
  own `manage-bde`. The cryptography is Windows' job; Lazarus
  orchestrates. The key is typed at a prompt that does not echo
  (never taken as a command argument), tried exactly once, and
  never stored, printed, or retried — Lazarus does not
  brute-force, does not cycle through keys, and says so. A
  volume Lazarus cannot positively identify as BitLocker is
  refused plainly, never guessed at. On a computer that is not
  running Windows the commands refuse plainly too: the
  orchestration is built and tested, and the unlock itself
  runs only where Windows' tools exist.
- **A maintained list of published ransomware decryptors** —
  six families, every entry verified against its publisher's
  own page on 2026-10-20 and carrying the publisher, the
  source address, the publisher's credit line, and its honest
  limits: STOP/Djvu (Emsisoft; offline keys and pre-August-2019
  file pairs only), GandCrab (Bitdefender; versions 1, 4, and
  5–5.2), Avaddon (Emsisoft), REvil/Sodinokibi (Bitdefender;
  attacks before 13 July 2021 only), Babuk (Avast), and
  Maze/Sekhmet/Egregor (Emsisoft). The list is data, checked
  strictly when read — a damaged or tampered list is refused
  whole, never partly trusted. `lazarus decryptors` shows it.
- **A decryptor runner that only ever touches copies** —
  `lazarus decrypt` prepares a work area (copies plus a
  fingerprint record), re-verifies that work area at run time —
  the folders themselves, symbolic and hard links, the record,
  every copy's hash, an empty output folder — and only then
  runs the decryptor, on the copies, with the originals hashed
  before and after. The report states plainly what was
  produced, whether the originals changed (and whether any
  change predates the run), and the fingerprint of the exact
  program that ran. Anything that does not check out is a
  refusal before the program starts, never a quiet workaround.
- **Family identification with honest confidence** —
  `lazarus identify` weighs ransom-note names and file
  extensions against the list and answers *looks like* (the
  family's own note is present) or *may be* (weaker signs),
  with the evidence listed. It never says "confirmed". The
  decrypt command chooses a decryptor by itself only on a
  single *looks like*; anything less, or two families at once,
  stops and asks the owner to name the family.
- **One honest assessment** — `lazarus assess` answers "what
  are my options for this drive or folder" on one screen: an
  identified family and whether a decryptor exists, an
  encrypted volume and the owner-key path, or a plain statement
  that neither applies, with the standing alternatives (restore
  from a snapshot, undelete, salvage) named either way.

### Notes

- The suite stands at 290 tests passing (1 skipped). Phase 3
  went through three independent audit rounds — 18 findings,
  then 11, then 4 of low severity — every one fixed and
  re-probed, including forged work areas, link swaps, tampered
  records, poisoned registry data, and crafted tool output.
- The BitLocker unlock itself — Windows accepting the key on a
  real volume — is the one part that cannot be proven on this
  machine; it is listed in the roadmap's Phase 3 as-built note
  as seam-only until a Windows-hardware run, not claimed as
  proven.

## [0.3.0] — 2026-10-20

Phase 2 — undelete and drive rescue.

### Added

- **Undelete for FAT and NTFS drives** — `lazarus scan` walks a
  drive or a drive image and lists the deleted files whose records
  survive: FAT12, FAT16, and FAT32 (memory cards, USB sticks,
  older drives) and NTFS (Windows system drives, scanned by their
  file records). Files inside deleted folders are found by their
  own records.
- **Honest recovery odds, per file** — every file the scan finds is
  weighed from the filesystem's own evidence and labelled *likely
  whole*, *partly there*, or *gone*, with the evidence stated.
  Parts of a file the drive now counts as reused count against it;
  a part the drive cannot testify about counts as at risk, never
  as safe. An overwritten file is reported as gone, plainly.
- **Recovery onto a different drive** — `lazarus recover` brings
  deleted files back into a folder on a *different* drive than the
  one being read, and refuses — before writing a byte — when the
  folder chosen is on the same drive, because writing there could
  overwrite other deleted files first. Partly-recovered files come
  back with the missing places as zeros, and the report says which
  files, and how much of each, could not come back. Every recovered
  file's hash is reported.
- **Drive rescue** — `lazarus rescue` clones a failing drive to an
  image gently: one fast sequential pass that asks each stretch
  once and skips whatever does not answer, then later passes that
  return for the gaps with smaller reads. A progress file beside
  the image records what was copied and what is still missing, is
  saved before the first read and after every stretch, and lets a
  later run resume exactly where an earlier one stopped. Places
  that could not be read are zeros in the image — never invented
  bytes.
- **Read-only by construction** — sources are opened read-only
  through a single seam with no write path at all, and the test
  suite pins it. A rescue's progress file can never be placed on
  the image or on the source, and a corrupt progress file is a
  plain refusal, never a mixed-up rescue.
- A 79-test Phase 2 suite (142 in all): the two filesystem
  parsers proven against ground-truth images built independently
  of them, the rescue engine against synthetic failing drives,
  the odds engine against hostile records, and a hostile-input
  audit battery (mutated images, corrupt progress files,
  traversal-proof output names) kept as permanent tests.

## [0.2.0] — 2026-10-20

Phase 1 — the flagship: the versioned snapshot store and the
restore engine.

### Added

- **Snapshot store** — dated, versioned snapshots of a file or a
  folder, kept on a drive the user owns. File contents are stored
  once, named by their SHA-256 hash, so a new version costs only
  what changed. Store records are written atomically: a save that
  is interrupted reads as unfinished, never as trustworthy.
- **Point-in-time restore** — restore a single file, a folder, or
  everything, from a chosen snapshot or the newest one taken on or
  before a chosen date.
- **Reversible restores** — before a restore overwrites anything,
  the current state of what it would overwrite is saved as a safety
  snapshot. Restoring that snapshot undoes the restore. If the
  safety copy cannot be completed, the restore stops before
  anything is changed.
- **Hash verification, before and after** — every saved copy a
  restore needs is re-hashed before a single byte is written;
  damaged or missing copies are named in the report and never
  written out as if they were whole. Each restored file is written
  to a temporary name, hash-checked, and only then moved into
  place.
- **Plain-language restore reports** — what came back, what was
  already exactly as saved, and what could not come back, with the
  reason for each failure in plain words.
- **Safety rules enforced in the engine** — a restore never writes
  outside the folder chosen for it (including through shortcuts or
  doctored snapshot records), never overwrites the store, and the
  store never snapshots itself.
- **Command-line interface** — `lazarus init`, `snap`, `list`,
  `verify`, and `restore`, with plain-language messages and honest
  exit codes. The animated interface arrives in Phase 5.
- A 63-test suite covering the store, the snapshot writer, the
  restore engine, verification, and the audit fixes.

## [0.1.0] — 2026-10-20

Project kickoff: the plan, the package scaffold, and the license.
