# LAZARUS.EXE

**Sentinel Safeguard LLC — backup, restore, and file recovery for Windows.**

[![Version](https://img.shields.io/badge/version-v0.5.0-2ee06f?style=flat-square)](https://github.com/TheRealJesusTheHacker/Lazarus.exe/blob/main/lazarus/__init__.py)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6?style=flat-square&logo=windows&logoColor=white)](https://github.com/TheRealJesusTheHacker/Lazarus.exe)
[![Python](https://img.shields.io/badge/Python-3-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Proprietary-b03a2e?style=flat-square)](https://github.com/TheRealJesusTheHacker/Lazarus.exe/blob/main/LICENSE)

*Dead isn't gone.*

---

## Overview

Lazarus of Bethany was four days in the tomb when he walked back out.
The story stuck for two thousand years because it says the thing
everyone who has ever lost a file wants to hear: **gone is not always
gone.**

LAZARUS.EXE is Sentinel Safeguard's recovery tool for Windows. It
brings back files that were deleted, folders that were overwritten,
and data trapped on drives that are failing or locked — and it backs
everything up *before* disaster, versioned by date, so the next loss
is a restore instead of a funeral.

Its first job is restoration. Everything else in the design exists to
serve it.

## The three jobs

### 1. Restoration — the key

This is the flagship. Everything else is support.

- **Restore to a date** — pick a file, a folder, or everything, and
  roll it back to how it was on the day you choose. Not just the
  latest copy: the version from *that* day.
- **Undelete** — recover files deleted from a drive, with **honest
  odds shown per file** before you start. If a file's data has been
  overwritten, Lazarus says so plainly — it never promises a miracle
  it can't deliver.
- **Drive rescue** — clone and salvage data off a failing drive
  before it dies for good, working from a copy so the dying drive is
  stressed as little as possible.

### 2. Decrypt files off drives

- **Owner-key unlock** — open encrypted volumes (including BitLocker)
  using **your own** recovery key or password, and recover the files
  inside. Your drives, your keys. Lazarus never tries to defeat
  encryption you don't hold the key to.
- **Known ransomware families** — where security researchers have
  published a working decryptor for a ransomware family, Lazarus can
  put it to work on your files, with the odds stated up front. Where
  no published decryptor exists, it says exactly that. No tool can
  honestly promise more.

### 3. Backup

- **One-click and scheduled** backups of the folders that matter, to
  an external or second drive.
- **Versioned by date** — every run is kept as its own dated version,
  so restoring "how it was on Tuesday" is always possible. Backup
  exists to serve restoration; that's the whole point.
- **Local-first** — no forced cloud account, no subscription leash.
  Your backup lives on your drive.
- **Loud, honest status** — a backup drive that's unplugged, full, or
  failing is reported **before** you need it, in plain language —
  never discovered after the disaster.

## How it will work

Under the hood, Lazarus is built around a **versioned snapshot
store**: dated, restorable versions of your files, kept on a drive
you own. The restore engine is designed first — the store exists to
serve it, not the other way around.

Three rules hold everywhere, no exceptions:

1. **Recovery never writes to the drive it's recovering from.**
   Writing to the source can overwrite the very data you're trying
   to save.
2. **Odds are stated before work begins**, per file where files are
   concerned — and a bad outlook is reported as plainly as a good
   one.
3. **Owner keys only.** Lazarus unlocks what you can prove is yours
   with a key you hold. It does not break encryption.

## The medic kit

LAZARUS.EXE joins Sentinel Safeguard's medic-kit line of Windows
tools: **NitroCore** (system optimizer), **VectorZip** (archive
tool), **ARGUS.EXE** (antivirus), and **CHARON.EXE** (driver
updater). Each tool does one job at a premium standard: plain
language, no corners cut, nothing promised that isn't delivered.

## Status

**v0.5.0 — Phase 4 complete: backup automation.** The Phase 1
snapshot store now runs itself. `lazarus backup setup` saves, in
plain language, which folders matter and where the backup drive
is; `lazarus backup run` takes a dated restore point of all of
them in one step. Backups can run on a daily or weekly schedule
— if the computer was off at the scheduled time, the next start
catches up and the history says so; there are no silent gaps.
`lazarus backup status` is loud before disaster, not after:
READY, TARGET MISSING, TARGET FULL (with the numbers), STORE
DAMAGED (with what to do next), NEVER RUN, or OVERDUE — checked
before every run, with the backup drive's free space compared
against an honest estimate and the store itself spot-checked for
damage on a regular cadence. Every attempt, success or failure,
is written to a run history that survives restarts. Old restore
points are tidied by a keep-how-many / keep-how-long rule that
never touches the newest backup, never runs after a failed
backup, and can be previewed before anything is removed.
Registration with Windows Task Scheduler is built and tested
through a platform seam; the registration itself is proven on a
Windows machine, not claimed before then — the roadmap's
Phase 4 note says exactly which parts those are. Phases 1–3
stand as shipped: the snapshot store and restore engine,
undelete with honest odds and rescue cloning, and owner-key
unlock with the published-decryptor list. It all runs today
from the command line; the animated interface arrives in
Phase 5 — see the [roadmap](docs/ROADMAP.md) and the
[changelog](CHANGELOG.md).

A portable no-install edition (folder/USB, Argus Portable pattern) is a named Phase 5 deliverable and will ship alongside the installer — see the roadmap.

## License

Proprietary — see [LICENSE](LICENSE). © 2026 Sentinel Safeguard
LLC. All rights reserved.

---

*LAZARUS.EXE is a Sentinel Safeguard LLC product. Sentinel is the
pretty face: cybersecurity, IT, and recovery services for the Kansas
City metro.*
