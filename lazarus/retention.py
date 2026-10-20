"""Tidying old backups away: plan first, then — carefully — apply.

Retention is the one place Lazarus deletes on purpose, so it is
built in two halves that never blur. :func:`plan_retention` only
*looks*: it reads the store, applies the owner's own rule (keep
the newest few, or keep the last so-many days), and returns a plan
naming every snapshot it would remove and why. It changes nothing
on disk. :func:`apply_retention` carries a plan out through the
store's own doors — snapshot folders out by id, then a sweep for
stored copies nothing points at any more — and if that sweep
cannot be done safely, it says so in the result instead of failing
a tidy-up that already succeeded.

Two snapshots are untouchable no matter what the rule says: the
newest finished snapshot in the whole store, and the newest backup
snapshot. They are the freshest way back, and a tidying rule never
gets to spend them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .backup_model import RETENTION_COUNT, RETENTION_DAYS, RetentionSpec
from .errors import (
    BackupError,
    LazarusError,
    SnapshotNotFoundError,
    StoreError,
)
from .model import KIND_SNAPSHOT
from .store import (
    SnapshotStore,
    is_valid_snapshot_id,
    parse_utc,
    safe_instant,
)

#: An unfinished save is only swept up once it is plainly abandoned:
#: a save that stopped less than a day ago might still be finishing.
_UNFINISHED_MAX_AGE = timedelta(hours=24)


@dataclass
class PlannedRemoval:
    """One snapshot a plan would remove, and the plain reason why."""

    id: str
    created_utc: str
    reason: str


#: Why a backup whose date cannot be read is held back from any
#: tidying: a backup Lazarus cannot date might be the newest one
#: there is, and it is never removed on a guess.
_HELD_UNDATEABLE_REASON = (
    "Its date could not be read, so it was left alone — "
    "Lazarus never removes a backup it cannot date."
)

#: The earliest moment a days-cutoff can name: a keeping rule that
#: reaches back further than time itself simply keeps everything.
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class RetentionPlan:
    """A tidying plan: what goes, what stays, what was untouchable.

    ``held`` names finished backups that were not even considered
    for removal — their recorded date could not be read — as
    ``(id, reason)`` pairs, so the caller can say they were left
    alone, and why.
    """

    remove: list[PlannedRemoval] = field(default_factory=list)
    keep_ids: list[str] = field(default_factory=list)
    protected_ids: list[str] = field(default_factory=list)
    held: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class RetentionResult:
    """What applying a plan actually did.

    ``gc_note`` is "" when the sweep for now-spare stored copies ran
    clean. When it could not run, the note says so in plain
    language: the snapshot removal still stands, but the space was
    not reclaimed, and no other way of reclaiming it was attempted.
    ``skipped_ids`` names plan entries that could not be carried
    out at all — a bad id, or a folder already gone — skipped
    rather than allowed to stop the rest of the tidy-up.
    """

    removed_ids: list[str] = field(default_factory=list)
    objects_removed: int = 0
    bytes_freed: int = 0
    gc_note: str = ""
    skipped_ids: list[str] = field(default_factory=list)


def _folder_mtime(store: SnapshotStore, snapshot_id: str) -> datetime | None:
    """When a snapshot folder last changed, or None if unreadable."""
    try:
        stamp = Path(store.snapshot_dir(snapshot_id)).stat().st_mtime
    except (OSError, LazarusError):  # a bad id must not break planning
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc)


def plan_retention(
    store: SnapshotStore,
    backup_ids: set[str],
    retention: RetentionSpec,
    now: datetime,
) -> RetentionPlan:
    """Work out which backup snapshots the owner's rule would remove.

    Only finished snapshots history says a backup made — ids in
    ``backup_ids`` — are ever candidates, and only ordinary
    snapshots at that: safety copies and snapshots taken by hand
    are not the backup's to tidy. A finished backup whose recorded
    date cannot be read is not a candidate at all: it is held back
    in the plan's ``held`` list, counts toward nothing, and can
    never be the protected newest. Unfinished saves are candidates
    only once their folder has sat untouched for more than a day,
    whoever started them: they hold nothing that can be restored,
    but a save might still be finishing. Planning changes nothing
    on disk.
    """
    if retention.mode not in (RETENTION_COUNT, RETENTION_DAYS):
        raise BackupError(
            "The backup settings name a way of tidying old backups "
            "that Lazarus does not know, so no tidying was planned. "
            "The settings need fixing before old backups can be "
            "tidied away."
        )
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    infos = store.list_snapshots()  # oldest first, by true instant
    finished = [info for info in infos if info.has_manifest]
    candidates = [
        info
        for info in finished
        if info.id in backup_ids and info.kind == KIND_SNAPSHOT
    ]

    # Partition the undateable candidates out before any of the
    # keeping logic runs: a backup whose date cannot be read is
    # never planned for removal, never counts toward the kept
    # population, and is never the protected newest.
    held: list[tuple[str, str]] = []
    backups: list = []
    for info in candidates:
        try:
            parse_utc(info.created_utc)
        except StoreError:
            held.append((info.id, _HELD_UNDATEABLE_REASON))
        else:
            backups.append(info)

    dateable = []
    for info in finished:
        try:
            parse_utc(info.created_utc)
        except StoreError:
            continue
        dateable.append(info)

    protected: list[str] = []
    if dateable:
        protected.append(dateable[-1].id)  # newest finished, whole store
    if backups:
        newest_backup = backups[-1].id  # newest backup snapshot
        if newest_backup not in protected:
            protected.append(newest_backup)
    protected_set = set(protected)

    remove: list[PlannedRemoval] = []
    remove_ids: set[str] = set()

    if retention.mode == RETENTION_COUNT:
        keep_set = (
            {info.id for info in backups[-retention.count :]}
            if retention.count > 0
            else set()
        )
        reason = (
            f"Beyond the newest {retention.count} backups you chose to keep."
        )
        for info in backups:
            if info.id in keep_set or info.id in protected_set:
                continue
            remove.append(
                PlannedRemoval(id=info.id, created_utc=info.created_utc, reason=reason)
            )
            remove_ids.add(info.id)
    else:  # RETENTION_DAYS
        try:
            cutoff = moment - timedelta(days=retention.days)
        except OverflowError:
            # A keeping rule that reaches back further than time
            # itself: the cutoff is the earliest moment there is,
            # so nothing is old enough to remove by age.
            cutoff = _EARLIEST
        reason = f"Older than the {retention.days} days you chose to keep."
        for info in backups:
            if info.id in protected_set:
                continue
            instant = safe_instant(info.created_utc)
            if instant > moment:
                continue  # dated in the future: its age counts as zero
            if instant < cutoff:
                remove.append(
                    PlannedRemoval(
                        id=info.id, created_utc=info.created_utc, reason=reason
                    )
                )
                remove_ids.add(info.id)

    # Unfinished saves, whoever started them: swept only when
    # plainly abandoned (folder untouched for over a day).
    for info in infos:
        if info.has_manifest:
            continue
        if info.id in protected_set:
            continue
        changed = _folder_mtime(store, info.id)
        if changed is None:
            continue
        if moment - changed > _UNFINISHED_MAX_AGE:
            remove.append(
                PlannedRemoval(
                    id=info.id,
                    created_utc=info.created_utc,
                    reason=(
                        "An unfinished save that never completed; "
                        "it holds nothing that can be restored."
                    ),
                )
            )
            remove_ids.add(info.id)

    keep_ids = [info.id for info in backups if info.id not in remove_ids]
    return RetentionPlan(
        remove=remove, keep_ids=keep_ids, protected_ids=protected, held=held
    )


def apply_retention(store: SnapshotStore, plan: RetentionPlan) -> RetentionResult:
    """Carry a plan out: folders first, then the spare-copy sweep.

    Snapshot folders go through the store's own
    :meth:`SnapshotStore.delete_snapshot`. The plan is checked as
    it is carried out — a plan is data, and the store may have
    changed since it was made — so an entry whose id is not a
    plain snapshot id, or whose folder is no longer there, is
    skipped and reported in ``skipped_ids`` rather than stopping
    the tidy-up or raising. The sweep for stored copies nothing
    points at runs last, through
    :meth:`SnapshotStore.remove_unreferenced_objects` and no other
    way — and if it refuses (a record it cannot read means it
    cannot know what is spare), the refusal is reported in
    ``gc_note`` rather than failing the whole tidy-up: the
    snapshots are already gone, honestly, and pretending otherwise
    would help no one.
    """
    removed: list[str] = []
    skipped: list[str] = []
    for item in plan.remove:
        if not is_valid_snapshot_id(item.id):
            skipped.append(item.id)
            continue
        try:
            folder = store.snapshot_dir(item.id)
        except SnapshotNotFoundError:
            skipped.append(item.id)
            continue
        if not folder.is_dir():
            skipped.append(item.id)
            continue
        try:
            store.delete_snapshot(item.id)
        except SnapshotNotFoundError:
            # The folder went away between the check and the
            # removal: already gone is not a failure.
            skipped.append(item.id)
            continue
        removed.append(item.id)
    objects_removed = 0
    bytes_freed = 0
    gc_note = ""
    try:
        objects_removed, bytes_freed, skipped_young = (
            store.remove_unreferenced_objects()
        )
        if skipped_young > 0:
            gc_note = (
                f"{skipped_young} new copies were left alone "
                "because they may still be arriving."
            )
    except StoreError:
        gc_note = (
            "Old backups were removed, but the space their saved "
            "copies held was not reclaimed, because part of the "
            "store could not be read. Nothing else was deleted. "
            "The store can be checked with 'lazarus verify'."
        )
    return RetentionResult(
        removed_ids=removed,
        objects_removed=objects_removed,
        bytes_freed=bytes_freed,
        gc_note=gc_note,
        skipped_ids=skipped,
    )
