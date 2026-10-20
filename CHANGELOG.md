# Changelog

All notable changes to LAZARUS.EXE are recorded here, newest first.

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
