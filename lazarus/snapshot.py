"""Taking snapshots: copying a file or folder into the store.

The writer walks the source once. File contents stream into the
store's object area under their SHA-256 hash — a file whose contents
are already stored is not copied twice, so version after version of a
folder costs only what actually changed. The manifest is written
last and atomically: a snapshot with no manifest is an unfinished
snapshot, and Lazarus says so rather than trusting it.

A file that cannot be copied does not fail the whole snapshot and is
never silently skipped: it is left out of the manifest, named in the
snapshot's problem list, and the snapshot is marked incomplete so
every later restore can say so.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Iterable, Optional

from .errors import SourceNotFoundError, StoreError
from .hashing import fsync_dir, fsync_file, hash_file, hash_file_into
from .model import (
    KIND_SNAPSHOT,
    FileEntry,
    Problem,
    SnapshotManifest,
    TYPE_DIR,
    TYPE_FILE,
    TYPE_SYMLINK,
)
from .store import SnapshotStore, format_utc, new_snapshot_id, utc_now


def _copy_object(store: SnapshotStore, source_file: Path) -> tuple[str, int]:
    """Copy one file's contents into the object area, by hash.

    Returns ``(sha256, size)``. If those exact contents are already
    stored *and still check out*, the copy is thrown away — the
    object area keeps one copy of any content, however many snapshots
    point at it. A stored object that no longer matches its name is
    torn or tampered with, and is healed by the fresh copy rather
    than trusted on existence alone.
    """
    fanout = store.objects_dir
    tmp = fanout / f".incoming-{secrets.token_hex(6)}"
    try:
        sha256, size = hash_file_into(source_file, tmp)
        fsync_file(tmp)
        final = store.object_path(sha256)
        if final.exists():
            try:
                intact = hash_file(final) == sha256
            except OSError:
                intact = False
            if intact:
                tmp.unlink()
                return sha256, size
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        fsync_dir(final.parent)
        return sha256, size
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _is_regular_file(path: Path) -> bool:
    """True for ordinary files only — never pipes, devices, or sockets.

    Opening a named pipe blocks until a writer shows up; opening a
    device can do worse. A snapshot records such things as problems
    instead of touching them.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _wanted(rel_path: str, include: Optional[set[str]]) -> bool:
    """Include filter: everything, or named items plus their parents."""
    if include is None:
        return True
    if rel_path in include:
        return True
    # Ancestor folders of a wanted item are wanted too, so a restore
    # of the safety copy rebuilds the same folder structure.
    return any(item.startswith(rel_path + "/") for item in include)


def create_snapshot(
    store: SnapshotStore,
    source: Path,
    label: str = "",
    kind: str = KIND_SNAPSHOT,
    include: Optional[Iterable[str]] = None,
    protects: Optional[dict] = None,
) -> SnapshotManifest:
    """Snapshot a file or folder into the store and return its manifest.

    ``include`` (relative paths, forward slashes) limits the snapshot
    to named items and their parent folders; the restore engine uses
    it for the safety copy it takes before overwriting anything.
    """
    from ._version import __version__

    raw = Path(source)
    if not raw.exists() and not raw.is_symlink():
        raise SourceNotFoundError(
            f"'{source}' is not there. Check the name, and that the "
            "drive it lives on is plugged in."
        )

    # The store never snapshots itself: not the store folder, not a
    # folder inside it, not one of its own files. Copying the copies
    # into the copies helps no one and buries the real data.
    store_root = store.root
    try:
        resolved = raw.resolve()
    except (RuntimeError, OSError):
        # A shortcut that loops back on itself can never be
        # followed to a real place, and resolve() raises a raw
        # RuntimeError for one — not a designed answer.
        raise SourceNotFoundError(
            f"'{source}' is a shortcut that points back on "
            "itself (a loop), so there is no real file or folder "
            "at the end of it to save. Pick the real file or "
            "folder instead."
        ) from None
    if resolved == store_root or store_root in resolved.parents:
        raise StoreError(
            "That is the Lazarus store itself, or something inside it. "
            "Lazarus does not save its own store into a snapshot — pick "
            "the files you actually want to keep safe."
        )

    include_set = set(include) if include is not None else None
    moment = utc_now()
    snapshot_id = new_snapshot_id(moment)

    # Classify by what the item itself is (lstat: a link is a link,
    # even when it points at a folder).
    try:
        mode = os.lstat(raw).st_mode
    except OSError:
        mode = 0
    if stat.S_ISLNK(mode):
        item_kind = "symlink"
        abs_item = Path(os.path.abspath(raw))
        root = abs_item.parent
        root_name = abs_item.name
    elif stat.S_ISREG(mode):
        item_kind = "file"
        abs_item = resolved
        root = resolved.parent
        root_name = resolved.name
    elif stat.S_ISDIR(mode):
        item_kind = "dir"
        abs_item = resolved
        root = resolved
        root_name = root.name or str(root)
    else:
        item_kind = "special"
        abs_item = Path(os.path.abspath(raw))
        root = abs_item.parent
        root_name = abs_item.name
    entries: list[FileEntry] = []
    problems: list[Problem] = []
    copy_failures = 0  # items that exist but could not be saved

    def record_file(abs_path: Path, rel_path: str) -> None:
        nonlocal copy_failures
        try:
            sha256, size = _copy_object(store, abs_path)
        except OSError:
            copy_failures += 1
            problems.append(
                Problem(
                    path=rel_path,
                    message="This file could not be read, so it was not saved.",
                )
            )
            return
        mtime_ns = None
        try:
            mtime_ns = abs_path.stat().st_mtime_ns
        except OSError:
            pass
        entries.append(
            FileEntry(
                path=rel_path,
                type=TYPE_FILE,
                size=size,
                sha256=sha256,
                mtime_ns=mtime_ns,
            )
        )

    def record_symlink(abs_path: Path, rel_path: str) -> None:
        nonlocal copy_failures
        try:
            target = os.readlink(abs_path)
        except OSError:
            copy_failures += 1
            problems.append(
                Problem(
                    path=rel_path,
                    message="This shortcut could not be read, so it was not saved.",
                )
            )
            return
        mtime_ns = None
        try:
            mtime_ns = abs_path.lstat().st_mtime_ns
        except OSError:
            pass
        entries.append(
            FileEntry(path=rel_path, type=TYPE_SYMLINK, target=target, mtime_ns=mtime_ns)
        )

    if item_kind == "symlink":
        if _wanted(root_name, include_set):
            record_symlink(abs_item, root_name)
    elif item_kind == "file":
        if _wanted(root_name, include_set):
            record_file(abs_item, root_name)
    elif item_kind == "special":
        if _wanted(root_name, include_set):
            copy_failures += 1
            problems.append(
                Problem(
                    path=root_name,
                    message=(
                        "This is not an ordinary file (it is a pipe, "
                        "device, or socket), so it was not saved."
                    ),
                )
            )
    else:
        excluded_store = False

        def _walk_error(error: OSError) -> None:
            # A folder that cannot be opened must not vanish quietly:
            # whatever lives inside it is not in this snapshot.
            nonlocal copy_failures
            copy_failures += 1
            failed = getattr(error, "filename", "") or ""
            try:
                rel_path = _rel(Path(failed), root) if failed else ""
            except ValueError:
                rel_path = ""
            problems.append(
                Problem(
                    path=rel_path,
                    message=(
                        "This folder could not be opened, so anything "
                        "inside it was not saved."
                    ),
                )
            )

        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=_walk_error
        ):
            dirnames.sort()
            filenames.sort()
            current = Path(dirpath)
            keep_dirs: list[str] = []
            for name in dirnames:
                abs_path = current / name
                rel_path = _rel(abs_path, root)
                if abs_path.is_symlink() or (
                    callable(getattr(abs_path, "is_junction", None))
                    and abs_path.is_junction()
                ):
                    # A junction is a shortcut too (Windows): record
                    # it as one, never walk through it into whatever
                    # it points at.
                    if _wanted(rel_path, include_set):
                        record_symlink(abs_path, rel_path)
                    continue
                if abs_path.resolve() == store_root:
                    # The store never snapshots itself; that way lies
                    # an endless hall of mirrors. Say so, once.
                    excluded_store = True
                    continue
                keep_dirs.append(name)
                if _wanted(rel_path, include_set):
                    entries.append(FileEntry(path=rel_path, type=TYPE_DIR))
            dirnames[:] = keep_dirs
            for name in filenames:
                abs_path = current / name
                rel_path = _rel(abs_path, root)
                if not _wanted(rel_path, include_set):
                    continue
                if abs_path.is_symlink():
                    record_symlink(abs_path, rel_path)
                elif _is_regular_file(abs_path):
                    record_file(abs_path, rel_path)
                else:
                    copy_failures += 1
                    problems.append(
                        Problem(
                            path=rel_path,
                            message=(
                                "This is not an ordinary file (it is a "
                                "pipe, device, or socket), so it was not saved."
                            ),
                        )
                    )
        if excluded_store:
            # Excluding the store is by design, not a failure: it is
            # noted in the record but does not mark the snapshot
            # incomplete (copy_failures is left alone).
            problems.append(
                Problem(
                    path="",
                    message=(
                        "The Lazarus store folder sits inside this folder, "
                        "so the store itself was not copied into the snapshot."
                    ),
                )
            )

    entries.sort(key=lambda e: e.path)
    manifest = SnapshotManifest(
        id=snapshot_id,
        kind=kind,
        label=label,
        created_utc=format_utc(moment),
        tool_version=__version__,
        source_root=str(root),
        root_name=root_name,
        complete=copy_failures == 0,
        entries=entries,
        problems=problems,
        protects=protects,
    )
    store.save_manifest(manifest)
    return manifest
