"""Tidying old backups away: plan first, apply only if the plan holds.

Retention is the one place Lazarus deletes on purpose, so these
tests hold it to its own contract: :func:`plan_retention` changes
nothing on disk, :func:`apply_retention` removes exactly what the
plan named — and no stored copy that any remaining snapshot still
points at may ever go with them. Ages are fabricated by editing a
snapshot's recorded time, never by trusting the wall clock.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lazarus.backup import (
    backup_snapshot_ids,
    history_path_for,
    load_history,
    run_backup,
    save_config,
)
from lazarus.backup_model import (
    BackupConfig,
    RetentionSpec,
    ScheduleSpec,
)
from lazarus.errors import StoreError
from lazarus.model import KIND_PRE_RESTORE
from lazarus.retention import apply_retention, plan_retention
from lazarus.snapshot import create_snapshot
from lazarus.store import SnapshotStore, format_utc
from lazarus.verify import verify_snapshot

UTC = timezone.utc
PLAN_NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
BIG_SPACE = lambda path: 10**12  # noqa: E731 - a told-you-so disk


def _snapshot_at(
    store: SnapshotStore,
    source: Path,
    when: datetime,
    files: dict[str, str] | None = None,
    **create_kwargs,
) -> str:
    """Write files into the source, snapshot it, and backdate the record."""
    if files:
        for rel, text in files.items():
            target = source / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    manifest = create_snapshot(store, source, **create_kwargs)
    manifest.created_utc = format_utc(when)
    store.save_manifest(manifest)
    return manifest.id


def _object_names(store: SnapshotStore) -> set[str]:
    return {
        str(path.relative_to(store.objects_dir))
        for path in store.objects_dir.rglob("*")
        if path.is_file()
    }


def _snapshot_ids(store: SnapshotStore) -> set[str]:
    return {info.id for info in store.list_snapshots() if info.has_manifest}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- keep-count, driven by real runs ---------------------------------


def test_keep_count_removes_exactly_the_oldest(store, tmp_path):
    """Five real runs, keep 3: the two oldest backups go, no more."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("seed", encoding="utf-8")
    config = BackupConfig(
        store=str(store.root),
        sources=[str(source)],
        schedule=ScheduleSpec(every="daily", at="02:00"),
        retention=RetentionSpec(mode="count", count=10, days=30),
        verify_every_runs=365,  # keep the spot-check out of this story
    )
    config_path = tmp_path / "backup.json"
    save_config(config, config_path)

    made: list[str] = []
    for day in range(5):
        (source / "f.txt").write_text(f"version-{day}-padding", encoding="utf-8")
        result = run_backup(
            config,
            config_path,
            now=datetime(2026, 10, 1 + day, 12, tzinfo=UTC),
            free_space_fn=BIG_SPACE,
        )
        assert result.outcome == "complete"
        made.extend(result.history_entry.snapshots)

    backup_ids = backup_snapshot_ids(load_history(history_path_for(config_path)))
    assert backup_ids == set(made)
    assert len(made) == 5

    plan = plan_retention(
        store,
        backup_ids,
        RetentionSpec(mode="count", count=3),
        datetime(2026, 10, 6, tzinfo=UTC),
    )
    assert [item.id for item in plan.remove] == made[:2]
    assert all("newest 3" in item.reason for item in plan.remove)
    assert plan.keep_ids == made[2:]

    result = apply_retention(store, plan)
    assert result.removed_ids == made[:2]
    assert _snapshot_ids(store) == set(made[2:])
    for snapshot_id in made[:2]:
        assert not (store.root / "snapshots" / snapshot_id).exists()


def test_shared_objects_survive_pruning_the_older_snapshot(store, tmp_path):
    """The object area keeps one copy per content: pruning an old
    snapshot reclaims only what nothing else points at."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "shared.txt").write_text("shared content", encoding="utf-8")

    old = _snapshot_at(store, source, PLAN_NOW - timedelta(days=10),
                       {"changing.txt": "version-old"})
    mid = _snapshot_at(store, source, PLAN_NOW - timedelta(days=5),
                       {"changing.txt": "version-mid"})
    new = _snapshot_at(store, source, PLAN_NOW - timedelta(days=1),
                       {"changing.txt": "version-new"})

    plan = plan_retention(
        store, {old, mid, new}, RetentionSpec(mode="count", count=2), PLAN_NOW
    )
    assert [item.id for item in plan.remove] == [old]
    assert plan.protected_ids == [new]

    shared_hash, old_hash = _hash("shared content"), _hash("version-old")
    assert store.object_path(shared_hash).exists()
    assert store.object_path(old_hash).exists()

    # The sweep now leaves copies younger than a day alone, in case
    # they belong to a snapshot still being written outside the
    # backup lock (the F-10 in-flight grace). This snapshot's unique
    # copy is minutes old as written, so backdate it by two days —
    # the same trick the days-mode tests use on manifests — before
    # the sweep may fairly reclaim it.
    old_object = store.object_path(old_hash)
    old_stamp = (PLAN_NOW - timedelta(days=2)).timestamp()
    os.utime(old_object, (old_stamp, old_stamp))

    result = apply_retention(store, plan)
    assert result.removed_ids == [old]
    # The shared content is still needed by the kept snapshots.
    assert store.object_path(shared_hash).exists()
    assert verify_snapshot(store, mid).ok
    assert verify_snapshot(store, new).ok
    # The older snapshot's own unique content went with it.
    assert not store.object_path(old_hash).exists()
    assert result.objects_removed >= 1


# -- keep-days, with fabricated ages ----------------------------------


def test_keep_days_prunes_only_what_is_old(store, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")

    old = _snapshot_at(store, source, PLAN_NOW - timedelta(days=40),
                       {"f.txt": "forty days old"})
    mid = _snapshot_at(store, source, PLAN_NOW - timedelta(days=10),
                       {"f.txt": "ten days old"})
    young = _snapshot_at(store, source, PLAN_NOW - timedelta(days=1),
                         {"f.txt": "one day old"})

    plan = plan_retention(
        store, {old, mid, young}, RetentionSpec(mode="days", days=30), PLAN_NOW
    )
    assert [item.id for item in plan.remove] == [old]
    assert "30 days" in plan.remove[0].reason

    result = apply_retention(store, plan)
    assert result.removed_ids == [old]
    assert _snapshot_ids(store) == {mid, young}


# -- what is never a candidate, no matter the pressure ---------------


def test_newest_backup_and_newest_snapshot_are_untouchable(store, tmp_path):
    """With keep-count 1, the newest backup survives — and a newer
    manual snapshot, which is not the backup's to tidy, is protected
    as the store's newest finished snapshot."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")

    first_backup = _snapshot_at(store, source, PLAN_NOW - timedelta(days=10),
                                {"f.txt": "first backup"})
    newest_backup = _snapshot_at(store, source, PLAN_NOW - timedelta(days=5),
                                 {"f.txt": "newest backup"})
    manual = _snapshot_at(store, source, PLAN_NOW - timedelta(days=1),
                          {"f.txt": "taken by hand"})

    plan = plan_retention(
        store, {first_backup, newest_backup}, RetentionSpec(mode="count", count=1), PLAN_NOW
    )
    assert [item.id for item in plan.remove] == [first_backup]
    assert newest_backup not in [item.id for item in plan.remove]
    assert manual not in [item.id for item in plan.remove]
    # The newest manual snapshot is protected as the store's newest;
    # the newest backup is protected as the backup's newest.
    assert set(plan.protected_ids) == {manual, newest_backup}


def test_pre_restore_safety_copies_are_never_planned(store, tmp_path):
    """A safety copy taken before a restore is not the backup's to
    tidy — not even when its id is (wrongly) among the backup ids."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")

    safety = _snapshot_at(
        store,
        source,
        PLAN_NOW - timedelta(days=20),
        {"f.txt": "before the restore"},
        kind=KIND_PRE_RESTORE,
        protects={"restoring_snapshot": "some-snapshot"},
    )
    first_backup = _snapshot_at(store, source, PLAN_NOW - timedelta(days=10),
                                {"f.txt": "first backup"})
    newest_backup = _snapshot_at(store, source, PLAN_NOW - timedelta(days=5),
                                 {"f.txt": "newest backup"})
    assert store.load_manifest(safety).kind == KIND_PRE_RESTORE

    plan = plan_retention(
        store,
        {safety, first_backup, newest_backup},  # the safety id should not be here
        RetentionSpec(mode="count", count=1),
        PLAN_NOW,
    )
    removed = [item.id for item in plan.remove]
    assert safety not in removed
    assert removed == [first_backup]


# -- dry-run accuracy --------------------------------------------------


def test_dry_run_changes_nothing_and_apply_removes_exactly_the_plan(
    store, tmp_path
):
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")
    made = [
        _snapshot_at(store, source, PLAN_NOW - timedelta(days=n),
                     {"f.txt": f"version-{n}"})
        for n in (9, 7, 5, 3)
    ]

    plan = plan_retention(
        store, set(made), RetentionSpec(mode="count", count=2), PLAN_NOW
    )
    assert [item.id for item in plan.remove] == made[:2]

    snapshot_dirs = {info.id: info for info in store.list_snapshots()}
    object_files = _object_names(store)
    # Planning is a dry run: nothing on disk may move.
    assert {info.id: info for info in store.list_snapshots()} == snapshot_dirs
    assert _object_names(store) == object_files

    result = apply_retention(store, plan)
    assert result.removed_ids == [item.id for item in plan.remove]
    assert _snapshot_ids(store) == set(made[2:])
    for snapshot_id in made[2:]:
        assert (store.root / "snapshots" / snapshot_id).is_dir()


# -- clock skew ----------------------------------------------------------


def test_future_dated_backup_is_never_age_pruned_and_stays_protected(
    store, tmp_path
):
    """A backup dated one day in the future has an age of zero for
    keep-days purposes, and it is the protected newest."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")

    old = _snapshot_at(store, source, PLAN_NOW - timedelta(days=40),
                       {"f.txt": "old"})
    mid = _snapshot_at(store, source, PLAN_NOW - timedelta(days=10),
                       {"f.txt": "mid"})
    future = _snapshot_at(store, source, PLAN_NOW + timedelta(days=1),
                          {"f.txt": "from tomorrow"})

    plan = plan_retention(
        store, {old, mid, future}, RetentionSpec(mode="days", days=30), PLAN_NOW
    )
    removed = [item.id for item in plan.remove]
    assert removed == [old]
    assert future not in removed
    assert future in plan.protected_ids
    assert future in plan.keep_ids


# -- damaged records: the sweep must never guess ------------------------


def test_damaged_manifest_stops_the_object_sweep_but_not_the_plan(store, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")
    old = _snapshot_at(store, source, PLAN_NOW - timedelta(days=10),
                       {"f.txt": "old version"})
    mid = _snapshot_at(store, source, PLAN_NOW - timedelta(days=5),
                       {"f.txt": "mid version"})
    new = _snapshot_at(store, source, PLAN_NOW - timedelta(days=1),
                       {"f.txt": "new version"})

    plan = plan_retention(
        store, {old, mid, new}, RetentionSpec(mode="count", count=2), PLAN_NOW
    )
    assert [item.id for item in plan.remove] == [old]

    # A remaining snapshot's record is damaged after planning.
    damaged_manifest = store.manifest_path(mid)
    assert damaged_manifest.exists()
    damaged_manifest.write_text("this is not a record {{{", encoding="utf-8")

    objects_before = _object_names(store)

    result = apply_retention(store, plan)

    # The planned snapshot folder still goes: the plan itself was sound.
    assert result.removed_ids == [old]
    assert not (store.root / "snapshots" / old).exists()
    # But no stored copy is reclaimed when the referenced set cannot
    # be proved — the refusal is reported, not hidden.
    assert result.objects_removed == 0
    assert result.bytes_freed == 0
    assert result.gc_note != ""
    assert _object_names(store) == objects_before


# -- empty input, empty output -------------------------------------------


def test_empty_store_and_no_backup_ids_plan_and_apply_nothing(store):
    plan = plan_retention(
        store, set(), RetentionSpec(mode="count", count=3), PLAN_NOW
    )
    assert plan.remove == []
    assert plan.keep_ids == []
    assert plan.protected_ids == []

    result = apply_retention(store, plan)
    assert result.removed_ids == []
    assert result.objects_removed == 0
    assert result.bytes_freed == 0
    assert result.gc_note == ""


def test_snapshots_not_in_backup_ids_are_not_candidates(store, tmp_path):
    """Snapshots history never named — e.g. taken by hand — are not
    the backup's to tidy, even when the backup ids list is empty."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")
    manual_one = _snapshot_at(store, source, PLAN_NOW - timedelta(days=40),
                              {"f.txt": "by hand"})
    manual_two = _snapshot_at(store, source, PLAN_NOW - timedelta(days=2),
                              {"f.txt": "by hand again"})

    plan = plan_retention(store, set(), RetentionSpec(mode="days", days=30), PLAN_NOW)
    assert plan.remove == []
    assert _snapshot_ids(store) == {manual_one, manual_two}


def test_damaged_manifest_makes_the_referenced_set_unprovable(store, tmp_path):
    """The store's own sweep gate: a record it cannot read means it
    cannot tell what is spare, and it refuses rather than guessing."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")
    snapshot_id = _snapshot_at(store, source, PLAN_NOW - timedelta(days=3),
                               {"f.txt": "v"})
    store.manifest_path(snapshot_id).write_text("{ broken", encoding="utf-8")
    with pytest.raises(StoreError):
        store.referenced_hashes()
