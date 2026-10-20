"""The restore engine: bringing a file, a folder, or everything back.

The order of operations is the safety story, and it never changes:

1. **Choose** the snapshot (by name, or the newest one at or before a
   date) and check its record is safe to act on.
2. **Check before writing**: every saved copy this restore needs is
   re-hashed first. A copy that is missing or damaged is named in the
   report and left out — it is never written to disk half-trusted.
3. **Snapshot the present**: before anything is overwritten, the way
   things are right now is saved as a safety snapshot. A restore can
   always be undone by restoring that one.
4. **Write and prove**: each file is written to a temporary name,
   hash-checked against the snapshot, and only then moved into place.

Nothing here deletes: a restore puts back what the snapshot held and
leaves anything newer alone. Failures are per-item, plain-language,
and counted in the report — never silent, never dressed up.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional

from .errors import (
    NothingToRestoreError,
    RestoreRefusedError,
    StoreError,
)
from .hashing import hash_file
from .model import (
    KIND_PRE_RESTORE,
    TYPE_DIR,
    TYPE_FILE,
    TYPE_SYMLINK,
    FileEntry,
    SnapshotManifest,
    is_safe_relative_path,
)
from .report import (
    OUTCOME_ALREADY_CURRENT,
    OUTCOME_FAILED,
    OUTCOME_NOT_RESTORED,
    OUTCOME_RESTORED,
    RestoreItem,
    RestoreReport,
)
from .snapshot import create_snapshot
from .store import SnapshotStore


def _blob_problem(store: SnapshotStore, entry: FileEntry) -> Optional[str]:
    """Plain-language reason this entry's saved copy is unusable, or None."""
    blob = store.object_path(entry.sha256 or "")
    if not blob.exists():
        return "Its saved copy is missing from the store, so it cannot come back."
    try:
        actual = hash_file(blob)
    except OSError:
        return "Its saved copy could not be read, so it cannot come back."
    if actual != entry.sha256:
        return (
            "Its saved copy has changed since it was saved and no longer "
            "matches the snapshot's record, so it was not trusted."
        )
    return None


def _select_entries(
    manifest: SnapshotManifest, path: Optional[str]
) -> list[FileEntry]:
    if path is None:
        return list(manifest.entries)
    wanted = path.replace("\\", "/").strip("/")
    if not wanted or not is_safe_relative_path(wanted):
        raise RestoreRefusedError(
            f"'{path}' is not a place inside the snapshot. "
            "Give the name of a file or folder the snapshot holds."
        )
    selected = [
        e
        for e in manifest.entries
        if e.path == wanted or e.path.startswith(wanted + "/")
    ]
    if not selected:
        raise NothingToRestoreError(
            f"The snapshot does not hold anything called '{wanted}'. "
            "Check the name, or restore everything instead."
        )
    return selected


def _check_destination(store: SnapshotStore, destination: Path) -> Path:
    dest = destination.resolve()
    root = store.root
    if dest == root or root in dest.parents:
        raise RestoreRefusedError(
            "That destination is inside the Lazarus store itself. "
            "Restoring there could damage the very copies you are restoring "
            "from, so Lazarus will not do it. Pick a different folder."
        )
    if dest in root.parents:
        raise RestoreRefusedError(
            "The Lazarus store sits inside that destination. Restoring "
            "there could overwrite the store, so Lazarus will not do it. "
            "Pick a different folder."
        )
    if dest.exists() and not dest.is_dir():
        raise RestoreRefusedError(
            f"'{dest}' is a file, not a folder. "
            "Pick a folder to restore into."
        )
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise StoreError(
            f"Lazarus could not use '{dest}' as the restore destination. "
            "Check the drive is connected and the folder can be written to."
        ) from None
    return dest


def _blocked_reason(dest: Path, entry: FileEntry) -> Optional[str]:
    """Why writing this item could escape the destination, or None.

    A restore writes only inside the folder the user chose. If any
    folder along the item's path is a shortcut (symlink), writing
    through it would land somewhere else entirely — so the item is
    refused, plainly, instead. A plain file in the way of a folder in
    the path is reported for what it is, too.
    """
    parts = entry.path.split("/")
    current = dest
    for part in parts[:-1]:
        current = current / part
        if os.path.islink(current):
            return (
                "A shortcut is sitting where a folder in its path "
                "belongs, so it was left alone. Lazarus never writes "
                "through shortcuts."
            )
        if os.path.lexists(current) and not current.is_dir():
            return (
                "A file is sitting where a folder in its path belongs, "
                "so it was left alone."
            )
    return None


def _restore_file(
    store: SnapshotStore, entry: FileEntry, dest_path: Path
) -> RestoreItem:
    if dest_path.is_dir() and not dest_path.is_symlink():
        return RestoreItem(
            entry.path,
            OUTCOME_FAILED,
            "A folder is sitting where this file belongs, so the folder was left alone.",
        )
    if dest_path.exists() and not dest_path.is_symlink():
        try:
            if hash_file(dest_path) == entry.sha256:
                return RestoreItem(entry.path, OUTCOME_ALREADY_CURRENT)
        except OSError:
            pass  # fall through and try to write it fresh
    blob = store.object_path(entry.sha256 or "")
    tmp = dest_path.with_name(f".lazarus-restore-{secrets.token_hex(6)}")
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(blob, "rb") as src, open(tmp, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        if hash_file(tmp) != entry.sha256:
            return RestoreItem(
                entry.path,
                OUTCOME_FAILED,
                "What was written did not match the snapshot's record, so it was not kept.",
            )
        os.replace(tmp, dest_path)
    except OSError:
        return RestoreItem(
            entry.path,
            OUTCOME_FAILED,
            "It could not be written. The drive may be full or the file may be in use.",
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if entry.mtime_ns is not None:
        try:
            os.utime(dest_path, ns=(entry.mtime_ns, entry.mtime_ns))
        except (OSError, OverflowError, ValueError):
            pass
    return RestoreItem(entry.path, OUTCOME_RESTORED)


def _restore_dir(entry: FileEntry, dest_path: Path) -> RestoreItem:
    if dest_path.is_symlink():
        return RestoreItem(
            entry.path,
            OUTCOME_FAILED,
            "A shortcut is sitting where this folder belongs, so it was left alone.",
        )
    if dest_path.exists() and not dest_path.is_dir():
        return RestoreItem(
            entry.path,
            OUTCOME_FAILED,
            "A file is sitting where this folder belongs, so the file was left alone.",
        )
    existed = dest_path.is_dir()
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return RestoreItem(
            entry.path,
            OUTCOME_FAILED,
            "The folder could not be created at the destination.",
        )
    return RestoreItem(
        entry.path, OUTCOME_ALREADY_CURRENT if existed else OUTCOME_RESTORED
    )


def _restore_symlink(entry: FileEntry, dest_path: Path) -> RestoreItem:
    if dest_path.is_symlink():
        try:
            if os.readlink(dest_path) == entry.target:
                return RestoreItem(entry.path, OUTCOME_ALREADY_CURRENT)
        except OSError:
            pass
    if os.path.lexists(dest_path):
        if dest_path.is_dir() and not dest_path.is_symlink():
            return RestoreItem(
                entry.path,
                OUTCOME_FAILED,
                "A folder is sitting where this shortcut belongs, so the folder was left alone.",
            )
        try:
            dest_path.unlink()
        except OSError:
            return RestoreItem(
                entry.path,
                OUTCOME_FAILED,
                "What is sitting in its place could not be moved aside.",
            )
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(entry.target, dest_path)
    except (OSError, ValueError):
        return RestoreItem(
            entry.path,
            OUTCOME_FAILED,
            "This shortcut could not be recreated on this computer.",
        )
    if entry.mtime_ns is not None:
        try:
            os.utime(
                dest_path,
                ns=(entry.mtime_ns, entry.mtime_ns),
                follow_symlinks=False,
            )
        except (OSError, OverflowError, ValueError, NotImplementedError):
            pass
    return RestoreItem(entry.path, OUTCOME_RESTORED)


def restore(
    store: SnapshotStore,
    destination: Optional[Path] = None,
    snapshot_id: Optional[str] = None,
    as_of: Optional[str] = None,
    path: Optional[str] = None,
) -> RestoreReport:
    """Restore from a snapshot and report exactly what came back.

    ``destination`` defaults to where the snapshot was taken from.
    ``path`` limits the restore to one file or folder inside the
    snapshot (its path as the snapshot recorded it).
    """
    manifest = store.resolve_snapshot(snapshot_id=snapshot_id, as_of=as_of)

    seen_paths: set[str] = set()
    for entry in manifest.entries:
        if not is_safe_relative_path(entry.path):
            raise RestoreRefusedError(
                "This snapshot's record lists a location outside the "
                "restore folder, so the whole restore was stopped before "
                "anything was written. The snapshot cannot be trusted."
            )
        if entry.path in seen_paths:
            raise RestoreRefusedError(
                "This snapshot's record lists the same item twice, so "
                "the whole restore was stopped before anything was "
                "written. The snapshot cannot be trusted."
            )
        seen_paths.add(entry.path)

    if destination is None:
        if not manifest.source_root:
            raise RestoreRefusedError(
                "This snapshot does not record where it was taken from. "
                "Say where to restore to."
            )
        destination = Path(manifest.source_root)
    dest = _check_destination(store, Path(destination))

    selected = _select_entries(manifest, path)

    # Step 2: prove every needed saved copy before writing anything.
    usable: list[FileEntry] = []
    broken: dict[str, str] = {}
    for entry in selected:
        if entry.type == TYPE_FILE:
            problem = _blob_problem(store, entry)
            if problem is None:
                usable.append(entry)
            else:
                broken[entry.path] = problem
        else:
            usable.append(entry)

    # Step 3: snapshot the present state of everything we are about
    # to overwrite. Items already exactly as the snapshot holds them
    # are not being overwritten, so they need no safety copy — and a
    # restore that changes nothing takes none.
    safety_id: Optional[str] = None
    affected: list[str] = []
    for entry in usable:
        if entry.type not in (TYPE_FILE, TYPE_SYMLINK):
            continue
        dest_path = dest / entry.path
        if not os.path.lexists(dest_path):
            continue
        if dest_path.is_dir() and not dest_path.is_symlink():
            continue  # in the way, but a restore never overwrites a folder
        if entry.type == TYPE_FILE:
            try:
                if hash_file(dest_path) == entry.sha256:
                    continue  # already current
            except OSError:
                pass
        elif dest_path.is_symlink():
            try:
                if os.readlink(dest_path) == entry.target:
                    continue  # already current
            except OSError:
                pass
        affected.append(entry.path)
    if affected:
        safety = create_snapshot(
            store,
            dest,
            label=f"Safety copy — before restoring the snapshot from {manifest.created_utc[:10]}",
            kind=KIND_PRE_RESTORE,
            include=affected,
            protects={
                "restoring_snapshot": manifest.id,
                "destination": str(dest),
            },
        )
        if not safety.complete:
            # The whole point of the safety copy is that this restore
            # can be undone. If part of the present state could not be
            # saved, proceeding would quietly destroy it — so the
            # restore stops here, before anything is overwritten.
            unsaved = ", ".join(
                sorted({p.path for p in safety.problems if p.path}) or ["(unknown)"]
            )
            raise RestoreRefusedError(
                "Lazarus could not save a complete safety copy of what "
                "this restore would overwrite, so nothing was changed. "
                f"It could not read: {unsaved}. Close anything using "
                "those files, or restore somewhere else, and try again."
            )
        safety_id = safety.id

    # Step 4: write, parents first, proving each file as it lands.
    report = RestoreReport(
        snapshot_id=manifest.id,
        snapshot_label=manifest.label,
        snapshot_created_utc=manifest.created_utc,
        destination=str(dest),
        snapshot_complete=manifest.complete,
        safety_snapshot_id=safety_id,
    )
    for entry in sorted(usable, key=lambda e: (e.path.count("/"), e.path)):
        dest_path = dest / entry.path
        blocked = _blocked_reason(dest, entry)
        if blocked is not None:
            report.items.append(RestoreItem(entry.path, OUTCOME_FAILED, blocked))
        elif entry.type == TYPE_DIR:
            report.items.append(_restore_dir(entry, dest_path))
        elif entry.type == TYPE_SYMLINK:
            report.items.append(_restore_symlink(entry, dest_path))
        else:
            report.items.append(_restore_file(store, entry, dest_path))
    for path_key in sorted(broken):
        report.items.append(
            RestoreItem(path_key, OUTCOME_NOT_RESTORED, broken[path_key])
        )
    report.items.sort(key=lambda i: i.path)
    return report
