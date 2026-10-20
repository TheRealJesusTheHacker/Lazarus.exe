# LAZARUS.EXE — Build Roadmap

The resurrection chart. Phases run in order, and the order is the
point: **restoration is designed first** — the snapshot store exists
to serve the restore engine, never the other way around.

**The three jobs (fixed priority):**
Restoration → Decrypt & unlock → Backup automation.

**Safety law:** recovery never writes to the drive it is recovering
from, odds are stated before work begins, and Lazarus unlocks only
with a key the owner holds. No exceptions, no settings that weaken
them.

---

## Phase 1 — Versioned snapshot store + restore engine (THE FLAGSHIP)

The key phase. Lazarus's reason to exist.

- **Snapshot store design, restore-first:** dated, versioned copies
  of files and folders on a target drive the user owns. Every design
  question is answered from the restore side: "can we get Tuesday's
  version of this folder back, exactly as it was?"
- **Point-in-time restore:** restore a single file, a folder, or
  everything, rolled back to a chosen date/version.
- **Restore is itself reversible:** before a restore overwrites
  anything, the current state is snapshotted — an undo for the undo.
- **Integrity verification:** restored data is verified against the
  snapshot (hashes), and the restore report says plainly what came
  back whole, what came back partial, and what could not come back.
- **Plain-language restore reports** — designed failure states, no
  raw OS errors ever surfaced.

**Status: PLANNED.**

## Phase 2 — Undelete + drive rescue

- **Undelete:** scan a drive for deleted files and recover them to a
  *different* drive. Per-file **honest recovery odds** computed from
  the evidence — filesystem, overwrite state, time since deletion —
  shown before recovery starts. An overwritten file is reported as
  gone, plainly. Odds are never dressed up.
- **Failing-drive rescue:** image-first workflow — clone/salvage the
  drive to an image with gentle, error-tolerant reads (skip bad
  areas, come back for them last), then run recovery against the
  image, not the dying hardware.
- Read-only against the source, always — enforced by the same kind
  of test-pinned safety assertions the rest of the medic kit uses.

**Status: PLANNED.**

## Phase 3 — Decrypt & unlock files off drives

- **Owner-key volume unlock:** open encrypted volumes — BitLocker
  first — using the owner's own recovery key or password, through
  Windows' own unlock mechanisms, then recover files normally.
  Owner-only framing throughout: your drives, your keys.
- **Published ransomware decryptors:** for families where security
  researchers have published working decryptors, integrate them from
  a maintained, sourced list (each decryptor credited to its
  publisher), and run them against copies of the encrypted files —
  never the only copy.
- **Honest odds up front:** family identified → decryptor exists or
  it doesn't. If no published decryptor exists, Lazarus says so
  plainly and offers what *is* possible (restore from backup,
  undelete, salvage). It never implies it can break encryption —
  because it can't, and neither can anyone else honestly selling
  that promise.

**Status: PLANNED.**

## Phase 4 — Backup automation

- **One-click backup** of the folders that matter, to an external or
  second drive, into the Phase 1 versioned store.
- **Scheduled backups** (daily/weekly, user's choice) with the same
  dated versioning — a backup run *is* a new restore point.
- **Local-first:** no cloud account required, ever. (A user pointing
  the target at a synced folder is their business; Lazarus itself
  phones nowhere.)
- **Loud, honest drive status:** target drive unplugged, full, or
  failing is surfaced *before* it matters — clear status in the app,
  not a silent log line after the disaster.

**Status: PLANNED.**

## Phase 5 — The animated UI + packaging

- Futuristic animated HUD interface (PySide6), green on black, on the
  real pipeline — every control functional, no decorative buttons.
  Restore flows lead the design; recovery odds and drive status are
  first-class screens, not fine print.
- Packaging: the single self-installing exe pattern the medic kit
  standardised on (one file, one admin prompt, verified install).
- Every failure state a designed screen with one clear next action,
  in plain language.

**Status: PLANNED.**

## Later / backlog

- **Portable edition** for the future IT Toolkit USB (alongside
  Argus Portable and Charon Portable), for technician field work.
- Whole-disk/partition restore (bare-metal recovery) — after the
  file-level engine has proven itself on hardware.
- Additional published-decryptor coverage as researchers release
  new ones.

## Standing rules

- **Honest odds, always.** Recovery and decryption chances are
  computed from evidence and stated before work begins — never
  faked, never rounded up for morale.
- **Owner keys only.** Lazarus unlocks with keys the owner holds.
  It does not defeat encryption, and its copy never implies it can.
- **Never write to the source.** Recovery, undelete, and decryption
  attempts work on copies or read from the source and write
  elsewhere.
- **Plain language.** Every user-visible word is written for the
  non-technical people Sentinel serves. No raw OS errors, ever.
- **Audit everything.** Build → audit → fix → re-audit, per
  Sentinel's standing rule across the fleet.
- Brand: green and black. No purple anywhere.
