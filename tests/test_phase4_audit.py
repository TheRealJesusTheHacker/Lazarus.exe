"""Phase 4 audit pins: the hostile-input checks from the build's
own audit round, kept as tests so the fixes cannot quietly rot.

Covered: a fan-out folder swapped for a symlink (the object sweep
must never follow it), the sweep's day of grace for copies that may
still be arriving, manifests whose inner id does not match the
folder they sit in, relative paths in the settings and on the setup
command line, setup values that must refuse before anything is
created, retention numbers too large to do arithmetic on, a lock
file naming a process id no system could hold, backups whose dates
cannot be read, history entries forged from the future, unfinished
saves blocking — and then, once pruned, unblocking — later runs,
tidying plans carrying an id that would escape the store, and a
history limit of zero. Two findings are re-probed end to end: a
backup set up with a relative folder from one working directory
still backs up that same folder when run from another, and the
unfinished-save deadlock can be escaped through the product's own
prune, never by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lazarus import cli
from lazarus.backup import (
    RUN_LOCK_FILE,
    append_history,
    backup_snapshot_ids,
    backup_status,
    history_path_for,
    load_config,
    load_history,
    run_backup,
    save_config,
)
from lazarus.backup_model import (
    OUTCOME_COMPLETE,
    OUTCOME_REFUSED,
    STATE_OVERDUE,
    STATE_READY,
    STATE_UNFINISHED,
    BackupConfig,
    HistoryEntry,
    RetentionSpec,
    ScheduleSpec,
)
from lazarus.errors import BackupConfigError
from lazarus.restore import restore
from lazarus.retention import (
    PlannedRemoval,
    RetentionPlan,
    apply_retention,
    plan_retention,
)
from lazarus.schedule import due_state
from lazarus.snapshot import create_snapshot
from lazarus.store import SnapshotStore, format_utc
from lazarus.verify import verify_store

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
BIG_SPACE = lambda path: 10**12  # noqa: E731 - a told-you-so disk


def _config(store_root, sources, *, retention=None, verify_every_runs=7):
    return BackupConfig(
        store=str(store_root),
        sources=[str(source) for source in sources],
        schedule=ScheduleSpec(every="daily", at="02:00"),
        retention=retention or RetentionSpec(mode="count", count=10, days=30),
        verify_every_runs=verify_every_runs,
        created_utc="2026-10-20T00:00:00.000000Z",
        tool_version="0.4.0",
    )


def _source(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    root = tmp_path / name
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _run(config, config_path, now, **kwargs):
    kwargs.setdefault("free_space_fn", BIG_SPACE)
    return run_backup(config, config_path, now=now, **kwargs)


def _snapshot_at(store, source, when, files):
    """Write files into the source, snapshot it, backdate the record."""
    for rel, text in files.items():
        target = source / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    manifest = create_snapshot(store, source)
    manifest.created_utc = format_utc(when)
    store.save_manifest(manifest)
    return manifest.id


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _backdate_days(path: Path, days: int) -> None:
    """Move a path's mtime back by whole days, from the real clock —
    the clock the object sweep itself reads."""
    stamp = time.time() - days * 24 * 60 * 60
    os.utime(path, (stamp, stamp))


def _rewrite_manifest(store, snapshot_id: str, **changes) -> None:
    path = store.manifest_path(snapshot_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


# -- F-01: the object sweep never follows a fan-out symlink -----------


def test_fanout_folder_replaced_by_symlink_is_never_swept_through(
    store, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "precious.txt"
    victim.write_text("do not touch", encoding="utf-8")
    link = store.objects_dir / "ab"
    os.symlink(outside, link)

    removed, freed, skipped_young = store.remove_unreferenced_objects()

    assert (removed, freed, skipped_young) == (0, 0, 0)
    assert victim.read_text(encoding="utf-8") == "do not touch"
    assert link.is_symlink()


# -- F-10: a day of grace for copies that may still be arriving -------


def test_fresh_unreferenced_object_survives_the_sweep_and_is_noted(store):
    orphan_hash = _hash("orphan content nobody references")
    orphan = store.object_path(orphan_hash)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan content nobody references")

    removed, _freed, skipped_young = store.remove_unreferenced_objects()
    assert removed == 0
    assert skipped_young >= 1
    assert orphan.exists()

    # The same grace, reported the way a tidy-up reports it.
    result = apply_retention(store, RetentionPlan())
    assert "left alone" in result.gc_note
    assert orphan.exists()

    # Two days old, the grace has run out and the copy is spare.
    _backdate_days(orphan, 2)
    removed, _freed, skipped_young = store.remove_unreferenced_objects()
    assert removed >= 1
    assert not orphan.exists()


# -- F-03: a manifest whose inner id is not its folder's --------------


@pytest.mark.parametrize(
    "steal_from_other", [False, True], ids=["nonexistent-id", "another-snapshots-id"]
)
def test_manifest_with_a_mismatched_inner_id_is_unfinished_not_trusted(
    store, tmp_path, steal_from_other
):
    source = _source(tmp_path, "src", {"a.txt": "first version"})
    first = create_snapshot(store, source).id
    target = first
    if steal_from_other:
        (source / "a.txt").write_text("second version", encoding="utf-8")
        target = create_snapshot(store, source).id
        _rewrite_manifest(store, target, id=first)
    else:
        _rewrite_manifest(store, target, id="20990101-000000-00000000")

    infos = {info.id: info for info in store.list_snapshots()}
    assert infos[target].has_manifest is False

    check = verify_store(store)
    assert not check.ok
    assert any(problem.path == target for problem in check.problems)

    config = _config(store.root, [source], verify_every_runs=1)
    config_path = tmp_path / "backup.json"
    status = backup_status(
        config, config_path, now=NOW, free_space_fn=BIG_SPACE
    )
    assert status.state == STATE_UNFINISHED

    result = _run(config, config_path, NOW)
    assert result.outcome == OUTCOME_REFUSED
    assert result.state == STATE_UNFINISHED
    history = load_history(history_path_for(config_path))
    assert len(history.entries) == 1
    assert history.entries[0].outcome == OUTCOME_REFUSED


def test_verify_store_is_not_ok_when_the_stolen_ids_snapshot_is_damaged(
    store, tmp_path
):
    """The corrupted cousin: one manifest steals another snapshot's
    id, and the victim snapshot's own copy is then torn. Verification
    must not pass this store blind."""
    source = _source(tmp_path, "src", {"a.txt": "the victim's content"})
    victim = create_snapshot(store, source)
    (source / "a.txt").write_text("the thief's content", encoding="utf-8")
    thief = create_snapshot(store, source).id
    _rewrite_manifest(store, thief, id=victim.id)

    victim_entry = next(
        entry for entry in victim.entries if entry.type == "file"
    )
    store.object_path(victim_entry.sha256).write_bytes(b"torn by a crash")

    check = verify_store(store)
    assert not check.ok
    assert check.problems


# -- F-02: settings hold full paths only -------------------------------


def test_load_config_rejects_a_relative_store_path(store, tmp_path):
    source = _source(tmp_path, "src", {"a.txt": "data"})
    config = _config("relative-store", [source])
    config_path = tmp_path / "backup.json"
    save_config(config, config_path)
    with pytest.raises(BackupConfigError):
        load_config(config_path)


def test_load_config_rejects_a_relative_source_path(store, tmp_path):
    config = _config(store.root, ["relative/src"])
    config_path = tmp_path / "backup.json"
    save_config(config, config_path)
    with pytest.raises(BackupConfigError):
        load_config(config_path)


def test_cli_setup_with_a_relative_add_saves_an_absolute_source(
    tmp_path, capsys, monkeypatch
):
    source = _source(tmp_path, "data", {"a.txt": "from here"})
    store_path = tmp_path / "store"
    config_path = tmp_path / "cfg" / "backup.json"
    monkeypatch.chdir(tmp_path)

    code = cli.main(
        [
            "backup", "setup",
            "--store", str(store_path),
            "--add", "data",
            "--config", str(config_path),
        ]
    )

    assert code == 0
    capsys.readouterr()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["sources"] == [str(source.resolve())]
    assert Path(saved["sources"][0]).is_absolute()
    assert load_config(config_path).sources == [str(source.resolve())]


def test_relative_setup_still_backs_up_the_original_folder_from_another_cwd(
    tmp_path, capsys, monkeypatch
):
    """F-02, end to end: set up from folder A with a relative name,
    run from folder B — where a different folder wears the same
    name — and what comes back must be A's data, never B's."""
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    data_a = _source(folder_a, "data", {"file.txt": "A's own data, byte for byte"})
    _source(folder_b, "data", {"file.txt": "B's different data entirely"})
    store_path = tmp_path / "store"
    config_path = tmp_path / "cfg" / "backup.json"

    monkeypatch.chdir(folder_a)
    code = cli.main(
        [
            "backup", "setup",
            "--store", str(store_path),
            "--add", "data",
            "--config", str(config_path),
        ]
    )
    assert code == 0
    capsys.readouterr()

    monkeypatch.chdir(folder_b)
    assert cli.main(["backup", "run", "--config", str(config_path)]) == 0
    capsys.readouterr()

    store = SnapshotStore.open(store_path)
    brought_back = tmp_path / "brought-back"
    report = restore(store, destination=brought_back)
    assert report.status == "complete"
    assert (brought_back / "file.txt").read_bytes() == (
        data_a / "file.txt"
    ).read_bytes()
    assert (brought_back / "file.txt").read_bytes() != (
        folder_b / "data" / "file.txt"
    ).read_bytes()


# -- F-08: a bad setup refuses before anything is created --------------


@pytest.mark.parametrize(
    "extra",
    [["--at", "99:99"], ["--keep-count", "0"], ["--verify-every", "0"]],
    ids=["bad-time", "keep-count-zero", "verify-every-zero"],
)
def test_cli_setup_with_unusable_values_creates_nothing(
    tmp_path, capsys, extra
):
    source = _source(tmp_path, "src", {"a.txt": "data"})
    store_path = tmp_path / "store"
    config_path = tmp_path / "cfg" / "backup.json"

    code = cli.main(
        [
            "backup", "setup",
            "--store", str(store_path),
            "--add", str(source),
            "--config", str(config_path),
            *extra,
        ]
    )

    assert code == 2
    capsys.readouterr()
    assert not store_path.exists()
    assert not config_path.exists()


# -- F-05: retention numbers have a ceiling, and planning clamps ------


@pytest.mark.parametrize(
    "retention",
    [
        RetentionSpec(mode="days", count=10, days=36501),
        RetentionSpec(mode="count", count=10001, days=30),
    ],
    ids=["days-36501", "count-10001"],
)
def test_load_config_rejects_oversized_retention(store, tmp_path, retention):
    source = _source(tmp_path, "src", {"a.txt": "data"})
    config = _config(store.root, [source], retention=retention)
    config_path = tmp_path / "backup.json"
    save_config(config, config_path)
    with pytest.raises(BackupConfigError):
        load_config(config_path)


def test_plan_retention_with_astronomical_days_does_not_raise(store, tmp_path):
    source = _source(tmp_path, "src", {"f.txt": "data"})
    old = _snapshot_at(
        store, source, NOW - timedelta(days=40), {"f.txt": "forty days old"}
    )

    plan = plan_retention(
        store, {old}, RetentionSpec(mode="days", count=10, days=10**9), NOW
    )

    # A rule reaching back further than time itself keeps everything.
    assert plan.remove == []
    assert old in plan.keep_ids


# -- F-04: a lock naming a process id no system could hold ------------


def test_huge_pid_in_the_lock_file_does_not_crash_a_run(store, tmp_path):
    source = _source(tmp_path, "src", {"a.txt": "data"})
    config = _config(store.root, [source])
    config_path = tmp_path / "backup.json"
    lock = store.root / RUN_LOCK_FILE
    lock.write_text(
        json.dumps(
            {
                "pid": 10**18,
                "started_utc": "2026-10-20T00:00:00.000000Z",
                "run_id": "an-earlier-run",
            }
        ),
        encoding="utf-8",
    )

    # No liveness seam is injected: the run meets the impossible pid
    # through the real check, and it must not escape as an exception.
    result = run_backup(
        config, config_path, now=NOW, free_space_fn=BIG_SPACE
    )

    assert result.outcome == OUTCOME_COMPLETE
    assert any(
        "leftover lock was cleared" in note
        for note in result.history_entry.notes
    )
    history = load_history(history_path_for(config_path))
    assert len(history.entries) == 1
    assert history.entries[0].outcome == OUTCOME_COMPLETE


# -- F-11: a live lock's refusal names the lock to delete --------------


def test_live_lock_refusal_names_the_lock_file_to_delete(store, tmp_path):
    source = _source(tmp_path, "src", {"a.txt": "data"})
    config = _config(store.root, [source])
    config_path = tmp_path / "backup.json"
    lock = store.root / RUN_LOCK_FILE
    lock.write_text(
        json.dumps(
            {
                "pid": 4242,
                "started_utc": "2026-10-20T00:00:00.000000Z",
                "run_id": "an-earlier-run",
            }
        ),
        encoding="utf-8",
    )

    result = _run(config, config_path, NOW, pid_alive=lambda pid: True)

    assert result.outcome == OUTCOME_REFUSED
    assert "delete" in result.message
    assert str(lock) in result.message


# -- F-06: a backup whose date cannot be read is held, never removed ---


def test_undateable_backup_is_held_back_in_count_and_days_modes(
    store, tmp_path
):
    source = _source(tmp_path, "src", {"f.txt": "data"})
    old = _snapshot_at(
        store, source, NOW - timedelta(days=10), {"f.txt": "ten days old"}
    )
    middle = _snapshot_at(
        store, source, NOW - timedelta(days=5), {"f.txt": "five days old"}
    )
    new = _snapshot_at(
        store, source, NOW - timedelta(days=1), {"f.txt": "one day old"}
    )
    _rewrite_manifest(store, middle, created_utc="NOT-A-DATE")

    for retention in (
        RetentionSpec(mode="count", count=1, days=30),
        RetentionSpec(mode="days", count=10, days=7),
    ):
        plan = plan_retention(store, {old, middle, new}, retention, NOW)
        removed = [item.id for item in plan.remove]
        assert [held_id for held_id, _reason in plan.held] == [middle]
        assert "date could not be read" in plan.held[0][1]
        assert middle not in removed
        # The plan still works on what it *can* date.
        assert removed == [old]


# -- F-07: a forged run from the future settles nothing ----------------


def test_forged_future_history_entry_does_not_make_status_ready(
    store, tmp_path
):
    source = _source(tmp_path, "src", {"a.txt": "real data"})
    config = _config(store.root, [source])
    config_path = tmp_path / "backup.json"
    legit_now = NOW - timedelta(days=30)
    result = _run(config, config_path, legit_now)
    assert result.outcome == OUTCOME_COMPLETE

    append_history(
        history_path_for(config_path),
        HistoryEntry(
            run_id="forged-run",
            started_utc="2099-01-01T02:00:00.000000Z",
            finished_utc="2099-01-01T02:00:01.000000Z",
            outcome=OUTCOME_COMPLETE,
            snapshots=[],
            sources=[str(source)],
            verified=True,
        ),
    )

    status = backup_status(
        config, config_path, now=NOW, free_space_fn=BIG_SPACE
    )
    assert status.state != STATE_READY
    assert status.state == STATE_OVERDUE

    history = load_history(history_path_for(config_path))
    assert due_state(config, history, NOW).due is True


# -- F-09: unfinished saves block, are swept, and then unblock ---------


def _plant_unfinished(store, snapshot_id: str, folder_time: datetime) -> Path:
    folder = store.snapshots_dir / snapshot_id
    folder.mkdir(parents=True)
    stamp = folder_time.timestamp()
    os.utime(folder, (stamp, stamp))
    return folder


def test_unfinished_backup_blocks_and_is_planned_for_the_no_membership_sweep(
    store, tmp_path
):
    source = _source(tmp_path, "src", {"a.txt": "good data"})
    config = _config(store.root, [source], verify_every_runs=1)
    config_path = tmp_path / "backup.json"
    good = _run(config, config_path, NOW - timedelta(days=1))
    assert good.outcome == OUTCOME_COMPLETE

    unfinished_id = "20261018-120000-abcdef01"
    _plant_unfinished(store, unfinished_id, NOW - timedelta(days=2))

    status = backup_status(
        config, config_path, now=NOW, free_space_fn=BIG_SPACE
    )
    assert status.state == STATE_UNFINISHED
    assert "prune" in status.next_action
    assert "prune" in status.to_text()

    refused = _run(config, config_path, NOW)
    assert refused.outcome == OUTCOME_REFUSED
    assert refused.state == STATE_UNFINISHED

    history = load_history(history_path_for(config_path))
    assert unfinished_id not in backup_snapshot_ids(history)

    # History never named it — it is no backup's snapshot — yet the
    # sweep for abandoned saves still plans its removal.
    plan = plan_retention(
        store, set(), RetentionSpec(mode="count", count=10, days=30), NOW
    )
    assert unfinished_id in [item.id for item in plan.remove]


def test_fresh_unfinished_folder_is_not_planned_for_removal(store):
    fresh_id = "20261020-110000-fresh0001"
    _plant_unfinished(store, fresh_id, NOW - timedelta(hours=1))

    plan = plan_retention(
        store, set(), RetentionSpec(mode="count", count=10, days=30), NOW
    )

    assert plan.remove == []


def test_pruning_the_unfinished_leftover_unblocks_the_next_backup(
    store, tmp_path
):
    """F-09, end to end: the deadlock has a product exit. A run is
    refused while the leftover stands; the prune clears it; the
    next run completes — no folder was deleted by hand."""
    source = _source(tmp_path, "src", {"a.txt": "good data"})
    config = _config(store.root, [source], verify_every_runs=1)
    config_path = tmp_path / "backup.json"
    good = _run(config, config_path, NOW - timedelta(days=1))
    assert good.outcome == OUTCOME_COMPLETE

    unfinished_id = "20261018-120000-abcdef01"
    folder = _plant_unfinished(store, unfinished_id, NOW - timedelta(days=2))

    refused = _run(config, config_path, NOW)
    assert refused.outcome == OUTCOME_REFUSED
    assert refused.state == STATE_UNFINISHED
    assert folder.is_dir()

    history = load_history(history_path_for(config_path))
    plan = plan_retention(
        store, backup_snapshot_ids(history), config.retention, NOW
    )
    assert unfinished_id in [item.id for item in plan.remove]
    tidy = apply_retention(store, plan)
    assert unfinished_id in tidy.removed_ids
    assert not folder.exists()

    unblocked = _run(config, config_path, NOW + timedelta(hours=1))
    assert unblocked.outcome == OUTCOME_COMPLETE


# -- F-15: a plan entry that would escape the store is skipped --------


def test_apply_retention_skips_a_bogus_escape_id_and_removes_the_real_one(
    store, tmp_path
):
    source = _source(tmp_path, "src", {"f.txt": "data"})
    old = _snapshot_at(
        store, source, NOW - timedelta(days=10), {"f.txt": "old version"}
    )
    new = _snapshot_at(
        store, source, NOW - timedelta(days=1), {"f.txt": "new version"}
    )
    # Where "../escape" would land, joined from the snapshots folder.
    outside = store.root / "escape"
    outside.write_text("do not touch", encoding="utf-8")

    plan = RetentionPlan(
        remove=[
            PlannedRemoval(id="../escape", created_utc="", reason="bogus"),
            PlannedRemoval(
                id=old,
                created_utc=store.load_manifest(old).created_utc,
                reason="a real removal",
            ),
        ],
        keep_ids=[new],
        protected_ids=[new],
    )

    result = apply_retention(store, plan)

    assert result.removed_ids == [old]
    assert result.skipped_ids == ["../escape"]
    assert outside.read_text(encoding="utf-8") == "do not touch"
    assert not (store.snapshots_dir / old).exists()
    assert (store.snapshots_dir / new).is_dir()


# -- F-14a: a history limit of zero is a plain error --------------------


def test_cli_backup_history_limit_zero_is_an_error(tmp_path, capsys):
    config_path = tmp_path / "cfg" / "backup.json"

    code = cli.main(
        ["backup", "history", "--config", str(config_path), "--limit", "0"]
    )

    assert code == 2
    capsys.readouterr()
