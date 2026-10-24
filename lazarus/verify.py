"""Checking the store against its own records.

Verification answers one question: "is what the snapshot promises
actually here, byte for byte?" Every file entry's contents are
re-hashed and compared with the manifest. Anything missing or changed
is named, in plain language — a store that cannot prove its contents
does not get to call itself verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from .hashing import hash_file, is_valid_hash
from .model import TYPE_FILE, Problem, SnapshotManifest, is_safe_relative_path
from .store import SnapshotStore


@dataclass
class VerifyResult:
    snapshot_id: str
    checked_files: int = 0
    checked_bytes: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        if self.ok:
            return (
                f"Snapshot {self.snapshot_id} checked out: "
                f"{self.checked_files} file(s) verified, byte for byte."
            )
        return (
            f"Snapshot {self.snapshot_id} has problems: "
            f"{len(self.problems)} item(s) are missing or damaged. "
            "Those items cannot be restored from this snapshot."
        )


def _blob_outcome(
    store: SnapshotStore, sha256: str, cache: Optional[dict] = None
) -> str:
    """How one stored copy stands: ok | missing | unreadable | changed.

    The verdict is worked out by hashing the copy once. When a
    ``cache`` dict is handed in, the verdict is kept in it under the
    copy's hash and reused for every later entry — in this snapshot
    or a later one — that points at the same copy: a store holds one
    copy of any content however many snapshots name it, and hashing
    those same bytes again for every entry proves nothing new. The
    messages each caller reports stay exactly the per-entry ones;
    only the repeated hashing goes away.
    """
    if cache is not None and sha256 in cache:
        return cache[sha256]
    blob = store.object_path(sha256)
    if not blob.exists():
        outcome = "missing"
    else:
        try:
            actual = hash_file(blob)
        except OSError:
            outcome = "unreadable"
        else:
            outcome = "ok" if actual == sha256 else "changed"
    if cache is not None:
        cache[sha256] = outcome
    return outcome


def verify_snapshot(
    store: SnapshotStore,
    snapshot: Union[str, SnapshotManifest],
    _blob_cache: Optional[dict] = None,
) -> VerifyResult:
    """Re-hash every file a snapshot promises and compare.

    ``_blob_cache`` is shared by :func:`verify_store` across all the
    snapshots it checks (see :func:`_blob_outcome`); a lone call
    leaves it out and gets a cache of its own for the one snapshot.
    """
    if _blob_cache is None:
        _blob_cache = {}
    manifest = (
        snapshot if isinstance(snapshot, SnapshotManifest) else store.load_manifest(snapshot)
    )
    result = VerifyResult(snapshot_id=manifest.id)
    seen_paths: set[str] = set()
    for entry in manifest.entries:
        if not is_safe_relative_path(entry.path):
            result.problems.append(
                Problem(
                    path=str(entry.path),
                    message="This entry points somewhere a restore must never write.",
                )
            )
            continue
        folded = entry.path.casefold()
        if folded in seen_paths:
            result.problems.append(
                Problem(
                    path=entry.path,
                    message="This item is listed twice in the snapshot record.",
                )
            )
            continue
        seen_paths.add(folded)
        if entry.type != TYPE_FILE:
            continue
        if not is_valid_hash(entry.sha256):
            result.problems.append(
                Problem(
                    path=entry.path,
                    message="This file's record is damaged, so its copy cannot be trusted.",
                )
            )
            continue
        outcome = _blob_outcome(store, entry.sha256, _blob_cache)
        if outcome == "missing":
            result.problems.append(
                Problem(
                    path=entry.path,
                    message="This file's saved copy is missing from the store.",
                )
            )
            continue
        if outcome == "unreadable":
            result.problems.append(
                Problem(
                    path=entry.path,
                    message="This file's saved copy could not be read.",
                )
            )
            continue
        if outcome == "changed":
            result.problems.append(
                Problem(
                    path=entry.path,
                    message=(
                        "This file's saved copy has changed since it was saved "
                        "and no longer matches its record."
                    ),
                )
            )
            continue
        result.checked_files += 1
        result.checked_bytes += entry.size
    return result


@dataclass
class StoreVerifyResult:
    snapshots_checked: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify_store(store: SnapshotStore) -> StoreVerifyResult:
    """Verify every finished snapshot in the store."""
    result = StoreVerifyResult()
    if not store.snapshots_dir.is_dir():
        result.problems.append(
            Problem(
                path="",
                message=(
                    "The store's snapshots folder is missing. "
                    "The store is damaged."
                ),
            )
        )
        return result
    # One cache for the whole store: the same stored copy is hashed
    # once, however many snapshots point at it.
    blob_cache: dict = {}
    for info in store.list_snapshots():
        if not info.has_manifest:
            result.problems.append(
                Problem(
                    path=info.id,
                    message="This snapshot never finished saving and cannot be used.",
                )
            )
            continue
        checked = verify_snapshot(store, info.id, _blob_cache=blob_cache)
        result.snapshots_checked += 1
        for problem in checked.problems:
            result.problems.append(
                Problem(path=f"{info.id}:{problem.path}", message=problem.message)
            )
    return result
