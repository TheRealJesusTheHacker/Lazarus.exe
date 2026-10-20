"""Checking the store against its own records.

Verification answers one question: "is what the snapshot promises
actually here, byte for byte?" Every file entry's contents are
re-hashed and compared with the manifest. Anything missing or changed
is named, in plain language — a store that cannot prove its contents
does not get to call itself verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

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


def verify_snapshot(
    store: SnapshotStore,
    snapshot: Union[str, SnapshotManifest],
) -> VerifyResult:
    """Re-hash every file a snapshot promises and compare."""
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
        if entry.path in seen_paths:
            result.problems.append(
                Problem(
                    path=entry.path,
                    message="This item is listed twice in the snapshot record.",
                )
            )
            continue
        seen_paths.add(entry.path)
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
        blob = store.object_path(entry.sha256)
        if not blob.exists():
            result.problems.append(
                Problem(
                    path=entry.path,
                    message="This file's saved copy is missing from the store.",
                )
            )
            continue
        try:
            actual = hash_file(blob)
        except OSError:
            result.problems.append(
                Problem(
                    path=entry.path,
                    message="This file's saved copy could not be read.",
                )
            )
            continue
        if actual != entry.sha256:
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
    for info in store.list_snapshots():
        if not info.has_manifest:
            result.problems.append(
                Problem(
                    path=info.id,
                    message="This snapshot never finished saving and cannot be used.",
                )
            )
            continue
        checked = verify_snapshot(store, info.id)
        result.snapshots_checked += 1
        for problem in checked.problems:
            result.problems.append(
                Problem(path=f"{info.id}:{problem.path}", message=problem.message)
            )
    return result
