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

**Status: COMPLETE — shipped in v0.2.0.**

As built: a content-addressed snapshot store (file contents kept
once, named by SHA-256; per-snapshot manifests written atomically,
so an interrupted save reads as unfinished, never as trustworthy),
and a restore engine that picks a snapshot by name or by date,
re-hashes every saved copy it needs *before* writing anything,
snapshots the current state of whatever it is about to overwrite
(the undo — and if that safety copy cannot be completed, the
restore stops before changing a byte), writes each file to a
temporary name and hash-checks it before it lands, and finishes
with the plain-language report. The safety rules live in the
engine, not the interface: a restore never writes outside its
destination (doctored records, backslash paths, duplicate entries,
and shortcut write-throughs are all refused), the store never
snapshots itself, and restores never delete newer work. Proven by
a 63-test suite and an independent audit round — 17 findings, all
fixed and re-probed. A command-line interface ships now; the
animated HUD is Phase 5. Verified on Linux; the first
Windows-hardware run is still to come.

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

**Status: COMPLETE — shipped in v0.3.0.**

As built: two filesystem parsers behind one odds engine. The FAT
parser walks every folder of a FAT12/16/32 drive and gathers the
entries deletion marked, saying openly that the stretch of pieces
it reports is the usual unbroken-run assumption — deletion erased
the map that would prove it — with each piece judged by whether
the table now counts it as back in use. The NTFS parser walks the
Master File Table record by record, applying each record's own
fixup before trusting it; small files come back with the contents
that live inside their record, and larger files are reported as
the stretches they sat in, split by the drive's own in-use map
($BITMAP). The odds engine weighs only that evidence — likely
whole, partly there (damage stated in bytes), or gone — and counts
anything the drive cannot testify about as at risk, never as safe.
Recovery writes only to a folder on a different device than the
source's — the same-device refusal fires before a byte is written
— flattens names from drive records into safe single file names,
and hashes everything it writes. The rescue engine clones through
a progress map of regions and holes: first pass one read per
region, later passes in smaller pieces, the map saved before the
first read and after every region, resumable across runs, and
refusing — in plain language — corrupt maps and any placement of
the progress file on the image or the source. Every source is
opened read-only through a single seam with no write path.
Proven by a 79-test Phase 2 suite (142 in all) including a
hostile-input audit battery kept as permanent tests.

Filesystem coverage, proven vs pending — proven means the parser
is exercised end-to-end against ground-truth images built
independently of it:

| Filesystem | State | How it is proven |
|---|---|---|
| FAT12 / FAT16 / FAT32 | Proven | Independent spec-built images: intact, partly reused, and fully reused deletions; long names; subfolders; erased start records |
| NTFS (MFT-record scan) | Proven | Independent spec-built images: resident and non-resident files, $BITMAP split verdicts, fixup-damaged records skipped and counted |
| NTFS folder-index / journal recovery | Pending | Records the MFT scan cannot see are out of reach until a later phase walks indexes and the journal |
| ext2 / ext4 and others | Pending | Not yet read; such a drive is refused in plain language, never guessed at |

Verified on Linux against synthetic images and synthetic failing
drives. The first run against real Windows hardware — a real
failing drive, real deleted NTFS system volumes — is still to
come, and nothing above claims otherwise.

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
- **Portable edition (named deliverable):** a portable no-install
  edition of Lazarus — runs from a folder or USB drive with no
  installation, following the Argus Portable pattern
  (ArgusPortable.zip) — ships alongside the installer at Phase 5.
  It is a committed Phase 5 deliverable, not a backlog option, and
  is part of the Technician's Toolkit USB set.
- Every failure state a designed screen with one clear next action,
  in plain language.

**Status: PLANNED.**

## Later / backlog

- Technician's Toolkit USB bundling — the Phase 5 portable edition
  (see above) packaged into the Technician's Toolkit USB set,
  alongside Argus Portable and Charon Portable, for technician
  field work.
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
