# Changelog

All notable changes to LAZARUS.EXE are recorded here, newest first.

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
