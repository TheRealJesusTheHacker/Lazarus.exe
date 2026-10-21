# LAZARUS.EXE

**Sentinel Safeguard LLC — backup, restore, and file recovery for Windows.**

[![Version](https://img.shields.io/badge/version-v1.0.3-f0a13c?style=flat-square)](https://github.com/TheRealJesusTheHacker/Lazarus.exe/blob/main/lazarus/__init__.py)
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

**v1.0.0 — the first public release. Phases 1–5 complete.**
Everything the command line could do, the Lazarus window now
does, in five pages. **Restore** leads: pick a snapshot by date,
browse what it holds, choose a file, a folder, or everything,
and put it back where it lived or somewhere new — the engine
takes its safety copy first and ends with a plain-language
report of exactly what came back. **Snapshots** takes a new
dated restore point and can verify the whole store byte for
byte. **Deleted files** scans a drive or an image and shows the
honest odds beside every file before anything is recovered —
recovery always lands on a different drive — and a failing
drive can be rescued to an image with its progress map live on
screen, safe to stop and resume. **Locked files** opens a
BitLocker volume with your own recovery key (asked once, tried
once, never kept) and, for folders locked by ransomware, says
which known family it looks like, runs that family's published
decryptor on copies only, and gives a one-screen honest
assessment. **Backup** opens on the loud status — READY, TARGET
MISSING, TARGET FULL, STORE DAMAGED, NEVER RUN, OVERDUE, or
UNFINISHED, with the numbers and what to do — then setup, run
now, history, and the tidying rule with its preview. The
command line remains, unchanged.

**v1.0.3** is fleet sweep #3's patch: the engine refuses a
pipe or a socket before any open can hang on it, the window
no longer keeps an unlock's recovery key reachable after the
attempt, shortcut loops get plain refusals instead of crashes
in rescue, recovery, snapshot, and restore, recovery streams
large files instead of loading them whole into memory, and a
backup run reports its own outcome even when its history line
cannot be written. See [CHANGELOG.md](CHANGELOG.md).

**v1.0.2** gives the window Lazarus's own colours — warm
stone-black, bone-white words, and ember amber, the first light
of the story it is named for — where it opened in the shared
Sentinel green before. Same pages, same words, nothing moved;
danger still wears the one red every Sentinel tool shares.
(v1.0.1 before it carried the Windows fixes from the kit-wide
bug hunt.)

Downloads, on the releases page: **Lazarus.exe** — one file;
run it and it offers, once, to install itself (a copy in
Program Files and a Start Menu entry, behind one Windows
approval prompt, every step verified and reported); saying no
costs nothing. **LazarusPortable.zip** — the same program with
no install at all, for a folder or a USB drive.
**SHA256SUMS.txt** covers both downloads.

What is still proven only through its test seams until a real
Windows run — Task Scheduler registration firing on a real
machine, manage-bde accepting a key, the installer end to
end, the first interactive run of the window — is listed in
full in the [roadmap](docs/ROADMAP.md), and nothing here
claims it early. See the [changelog](CHANGELOG.md) for what
each phase added.

## License

Proprietary — see [LICENSE](LICENSE). © 2026 Sentinel Safeguard
LLC. All rights reserved.

---

*LAZARUS.EXE is a Sentinel Safeguard LLC product. Sentinel is the
pretty face: cybersecurity, IT, and recovery services for the Kansas
City metro.*
