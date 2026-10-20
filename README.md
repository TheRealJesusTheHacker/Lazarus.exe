# LAZARUS.EXE

**Sentinel Safeguard LLC — backup, restore, and file recovery for Windows.**

[![Version](https://img.shields.io/badge/version-v0.4.0-2ee06f?style=flat-square)](https://github.com/TheRealJesusTheHacker/Lazarus.exe/blob/main/lazarus/__init__.py)
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

**v0.4.0 — Phase 3 complete: decrypt & unlock.** Lazarus can now
open a BitLocker volume with the owner's own recovery key:
Windows' own tools do the opening, and Lazarus asks once, with
the one key it is given — a key is never kept, never shown, and
never retried. It also keeps a short, hand-checked list of
published ransomware decryptors — six families at this release:
STOP/Djvu, GandCrab, Avaddon, REvil/Sodinokibi, Babuk, and
Maze/Sekhmet/Egregor, each credited to its publisher and dated
when it was last checked against the publisher's own page — and
it runs a decryptor only on copies, inside a work area it
prepared and re-checks before anything runs, with the original
files hash-checked before and after. `lazarus identify` weighs
the signs in a folder and answers honestly — *looks like* or
*may be*, never "confirmed" — and `lazarus assess` lays out,
for one drive or folder, what can actually be done. Where no
published decryptor exists, Lazarus says so plainly and points
at what is possible instead; it never implies it can break
encryption. Phases 1–2 stand as shipped: the snapshot store
and restore engine, and undelete with honest odds plus rescue
cloning for dying drives. The unlock path drives Windows' own
tools, so its last step of proof waits for a Windows machine —
the roadmap's Phase 3 note says exactly which parts those are.
It all runs today from the command line; the animated
interface arrives in Phase 5 — see the
[roadmap](docs/ROADMAP.md) and the [changelog](CHANGELOG.md).

A portable no-install edition (folder/USB, Argus Portable pattern) is a named Phase 5 deliverable and will ship alongside the installer — see the roadmap.

## License

Proprietary — see [LICENSE](LICENSE). © 2026 Sentinel Safeguard
LLC. All rights reserved.

---

*LAZARUS.EXE is a Sentinel Safeguard LLC product. Sentinel is the
pretty face: cybersecurity, IT, and recovery services for the Kansas
City metro.*
