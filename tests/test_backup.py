"""The Phase 4 backup engine: settings, history, and the run itself.

A backup is a promise kept on a schedule, so these tests drive
:mod:`lazarus.backup` the way the schedule will: settings saved and
loaded back, runs handed an explicit moment and a told-you-so free
space answer, locks held by processes whose liveness is decided by
the test — never by the wall clock, the real disk, or the machine
this happens to run on.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lazarus.backup import (
    RUN_LOCK_FILE,
    backup_snapshot_ids,
    backup_status,
    default_config_path,
    estimate_backup_bytes,
    history_path_for,
    load_config,
    load_history,
    run_backup,
    save_config,
    append_history,
)
from lazarus.backup_model import (
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_REFUSED,
    RETENTION_COUNT,
    STATE_NEVER_RUN,
    STATE_OVERDUE,
    STATE_READY,
    STATE_STORE_DAMAGED,
    STATE_TARGET_FULL,
    STATE_TARGET_MISSING,
    TRIGGER_MANUAL,
    BackupConfig,
    History,
    HistoryEntry,
    RetentionSpec,
    ScheduleSpec,
    human_bytes,
)
from lazarus.errors import BackupConfigError
from lazarus.restore import restore
from lazarus.store import SnapshotStore

UTC = timezone.utc
BIG_SPACE = lambda path: 10**12  # noqa: E731 - a told-you-so disk


def _moment(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def _source(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    root = tmp_path / name
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _config(
    store_root,
    sources,
    *,
    retention: RetentionSpec | None = None,
    verify_every_runs: int = 7,
) -> BackupConfig:
    return BackupConfig(
        store=str(store_root),
        sources=[str(source) for source in sources],
        schedule=ScheduleSpec(every="daily", at="02:00"),
        retention=retention or RetentionSpec(mode=RETENTION_COUNT, count=10, days=30),
        verify_every_runs=verify_every_runs,
        created_utc="2026-10-20T00:00:00.000000Z",
        tool_version="0.4.0",
    )


def _run(config, config_path, now, **kwargs):
    kwargs.setdefault("free_space_fn", BIG_SPACE)
    return run_backup(config, config_path, now=now, **kwargs)


def _history(config_path: Path) -> History:
    return load_history(history_path_for(config_path))


# -- where things live, and the settings file -------------------------


def test_config_and_history_paths_are_the_promised_ones(tmp_path):
    assert default_config_path().name == "backup.json"
    assert default_config_path().parent.name == ".lazarus"
    config_path = tmp_path / "somewhere" / "backup.json"
    assert history_path_for(config_path) == tmp_path / "somewhere" / "backup-history.jsonl"


def test_config_round_trip_save_then_load_is_equal(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "cfg" / "backup.json"
    save_config(config, config_path)
    assert config_path.exists()
    assert load_config(config_path) == config


def _valid_dict(store, tree) -> dict:
    return _config(store.root, [tree]).to_dict()


def _write_settings(tmp_path: Path, payload) -> Path:
    path = tmp_path / "backup.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_config_missing_file_is_a_plain_refusal(tmp_path):
    with pytest.raises(BackupConfigError) as caught:
        load_config(tmp_path / "never-written.json")
    assert "No backup is set up yet" in str(caught.value)


def test_load_config_non_json_file_is_damaged(tmp_path):
    path = _write_settings(tmp_path, "this is not json {")
    with pytest.raises(BackupConfigError):
        load_config(path)


def test_load_config_json_list_instead_of_a_record(tmp_path):
    path = _write_settings(tmp_path, [1, 2, 3])
    with pytest.raises(BackupConfigError):
        load_config(path)


def _mutate(key_path, value):
    def apply(data: dict) -> None:
        target = data
        for key in key_path[:-1]:
            target = target[key]
        target[key_path[-1]] = value

    return apply


_VALIDATION_CASES = [
    ("wrong-format", _mutate(("format",), 99)),
    ("format-as-text", _mutate(("format",), "1")),
    ("empty-store", _mutate(("store",), "")),
    ("blank-store", _mutate(("store",), "   ")),
    ("sources-not-a-list", _mutate(("sources",), "/just/a/string")),
    ("sources-empty", _mutate(("sources",), [])),
    ("sources-non-string-member", _mutate(("sources",), ["/fine", 42])),
    ("sources-blank-member", _mutate(("sources",), [""])),
    ("every-hourly", _mutate(("schedule", "every"), "hourly")),
    ("at-24-00", _mutate(("schedule", "at"), "24:00")),
    ("at-single-digit-hour", _mutate(("schedule", "at"), "2:00")),
    ("at-60-minutes", _mutate(("schedule", "at"), "02:60")),
    ("weekday-funday", _mutate(("schedule", "weekday"), "funday")),
    ("retention-forever", _mutate(("retention", "mode"), "forever")),
    ("retention-count-zero", _mutate(("retention", "count"), 0)),
    ("retention-count-bool", _mutate(("retention", "count"), True)),
    ("retention-days-negative", _mutate(("retention", "days"), -3)),
    ("verify-every-zero", _mutate(("verify_every_runs",), 0)),
    ("verify-every-366", _mutate(("verify_every_runs",), 366)),
]


@pytest.mark.parametrize(
    "mutate", [case for _name, case in _VALIDATION_CASES],
    ids=[name for name, _case in _VALIDATION_CASES],
)
def test_load_config_validation_battery(store, tree, tmp_path, mutate):
    data = _valid_dict(store, tree)
    mutate(data)
    path = _write_settings(tmp_path, data)
    with pytest.raises(BackupConfigError):
        load_config(path)


# -- the history file ---------------------------------------------------


def _entry(run_id: str, **overrides) -> HistoryEntry:
    fields = dict(
        run_id=run_id,
        started_utc="2026-10-20T02:00:00.000000Z",
        finished_utc="2026-10-20T02:00:01.000000Z",
        trigger=TRIGGER_MANUAL,
        outcome=OUTCOME_COMPLETE,
        snapshots=["snap-one"],
        sources=["/somewhere"],
        files_saved=3,
        bytes_saved=30,
        bytes_copied=30,
        seconds=1.0,
    )
    fields.update(overrides)
    return HistoryEntry(**fields)


def test_history_append_and_reload_preserves_entries(tmp_path):
    history_path = tmp_path / "backup-history.jsonl"
    first, second = _entry("run-1"), _entry("run-2", outcome=OUTCOME_PARTIAL)
    append_history(history_path, first)
    append_history(history_path, second)
    loaded = load_history(history_path)
    assert loaded.damaged_lines == 0
    assert loaded.entries == [first, second]


def test_history_damaged_middle_line_is_skipped_and_counted(tmp_path):
    history_path = tmp_path / "backup-history.jsonl"
    first, third = _entry("run-1"), _entry("run-3")
    lines = [
        json.dumps(first.to_dict()),
        "this line is not a record {{{",
        json.dumps(third.to_dict()),
    ]
    history_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    loaded = load_history(history_path)
    assert loaded.damaged_lines == 1
    assert [entry.run_id for entry in loaded.entries] == ["run-1", "run-3"]


def test_history_survives_a_fresh_load(tmp_path):
    history_path = tmp_path / "backup-history.jsonl"
    append_history(history_path, _entry("run-1"))
    assert [e.run_id for e in load_history(history_path).entries] == ["run-1"]
    # A later, separate reading — the way a new invocation sees it —
    # still holds the first run after a second one is appended.
    append_history(history_path, _entry("run-2"))
    reloaded = load_history(history_path)
    assert [e.run_id for e in reloaded.entries] == ["run-1", "run-2"]
    assert backup_snapshot_ids(reloaded) == {"snap-one"}


# -- one-click runs, end to end -----------------------------------------


def test_one_click_run_backs_up_two_sources_and_restores(store, tmp_path):
    source_one = _source(tmp_path, "one", {"data/known.txt": "known content, byte for byte"})
    source_two = _source(tmp_path, "two", {"other.txt": "the second folder"})
    config = _config(store.root, [source_one, source_two])
    config_path = tmp_path / "backup.json"
    save_config(config, config_path)

    result = _run(config, config_path, _moment(20))

    assert result.outcome == OUTCOME_COMPLETE
    assert result.state == STATE_READY
    entry = result.history_entry
    assert len(entry.snapshots) == 2  # one snapshot per source
    assert entry.files_saved > 0
    assert entry.bytes_saved > 0
    assert entry.bytes_copied > 0
    for snapshot_id in entry.snapshots:
        assert store.snapshot_dir(snapshot_id).is_dir()
        assert store.load_manifest(snapshot_id)

    history = _history(config_path)
    assert len(history.entries) == 1
    assert history.entries[0].snapshots == entry.snapshots
    assert backup_snapshot_ids(history) == set(entry.snapshots)

    # The restore spot-check: what the first snapshot holds comes
    # back byte for byte into a fresh folder.
    brought_back = tmp_path / "brought-back"
    report = restore(store, destination=brought_back, snapshot_id=entry.snapshots[0])
    assert report.status == "complete"
    assert (brought_back / "data" / "known.txt").read_bytes() == (
        source_one / "data" / "known.txt"
    ).read_bytes()


def test_second_run_copies_only_what_changed(store, tmp_path):
    source = _source(
        tmp_path, "src", {"a.txt": "a" * 100, "b.txt": "b" * 50}
    )
    config = _config(store.root, [source])
    config_path = tmp_path / "backup.json"

    first = _run(config, config_path, _moment(20))
    assert first.outcome == OUTCOME_COMPLETE
    assert first.history_entry.bytes_copied == 150

    (source / "b.txt").write_text("c" * 30, encoding="utf-8")
    second = _run(config, config_path, _moment(20, hour=13))
    assert second.outcome == OUTCOME_COMPLETE
    assert 0 < second.history_entry.bytes_copied < first.history_entry.bytes_copied
    assert second.history_entry.bytes_copied == 30

    third = _run(config, config_path, _moment(20, hour=14))
    assert third.outcome == OUTCOME_COMPLETE
    assert third.history_entry.bytes_copied == 0


def test_run_with_one_source_missing_is_partial_and_names_it(store, tmp_path):
    present = _source(tmp_path, "present", {"here.txt": "still here"})
    missing = tmp_path / "not-there"
    config = _config(store.root, [present, missing])
    config_path = tmp_path / "backup.json"

    result = _run(config, config_path, _moment(20))

    assert result.outcome == OUTCOME_PARTIAL
    entry = result.history_entry
    assert len(entry.snapshots) == 1  # the present source still backed up
    told = entry.reason + "\n" + "\n".join(entry.notes)
    assert str(missing) in told


def test_run_with_all_sources_missing_fails_and_saves_nothing(store, tmp_path):
    config = _config(store.root, [tmp_path / "gone-one", tmp_path / "gone-two"])
    config_path = tmp_path / "backup.json"

    result = _run(config, config_path, _moment(20))

    assert result.outcome == OUTCOME_FAILED
    assert result.history_entry.snapshots == []
    assert store.list_snapshots() == []
    assert len(_history(config_path).entries) == 1


# -- pre-flight refusals: nothing written, history still told -------


def test_run_refused_when_the_store_folder_is_absent(tmp_path):
    source = _source(tmp_path, "src", {"a.txt": "data"})
    config = _config(tmp_path / "absent-store", [source])
    config_path = tmp_path / "backup.json"

    result = _run(config, config_path, _moment(20))

    assert result.outcome == OUTCOME_REFUSED
    assert result.state == STATE_TARGET_MISSING
    history = _history(config_path)
    assert len(history.entries) == 1
    assert history.entries[0].outcome == OUTCOME_REFUSED


def test_run_refused_when_the_store_path_is_a_plain_folder(tmp_path):
    source = _source(tmp_path, "src", {"a.txt": "data"})
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    (plain / "unrelated.txt").write_text("not a store", encoding="utf-8")
    config = _config(plain, [source])
    config_path = tmp_path / "backup.json"

    result = _run(config, config_path, _moment(20))

    assert result.outcome == OUTCOME_REFUSED
    assert result.state == STATE_STORE_DAMAGED
    history = _history(config_path)
    assert len(history.entries) == 1
    assert history.entries[0].outcome == OUTCOME_REFUSED


def test_run_refused_when_there_is_no_room(store, tmp_path):
    source = _source(tmp_path, "src", {"a.txt": "some real data"})
    config = _config(store.root, [source])
    config_path = tmp_path / "backup.json"
    estimate, _basis = estimate_backup_bytes(config, History(entries=[]))
    assert estimate > 0

    result = _run(config, config_path, _moment(20), free_space_fn=lambda path: 0)

    assert result.outcome == OUTCOME_REFUSED
    assert result.state == STATE_TARGET_FULL
    # The refusal names the human numbers, not raw counts alone.
    assert human_bytes(estimate) in result.message
    assert human_bytes(0) in result.message
    history = _history(config_path)
    assert len(history.entries) == 1
    assert history.entries[0].outcome == OUTCOME_REFUSED
    assert store.list_snapshots() == []


def test_run_refused_when_the_store_is_not_writable(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "backup.json"
    store.root.chmod(0o500)
    try:
        probe = store.root / ".writability-check"
        try:
            probe.write_bytes(b"x")
        except OSError:
            pass  # genuinely read-only for this user: the test can run
        else:
            probe.unlink()
            pytest.skip(
                "the store stayed writable despite chmod 0o500 "
                "(likely running as root), so a read-only store "
                "cannot be simulated this way here"
            )
        result = _run(config, config_path, _moment(20))
        assert result.outcome == OUTCOME_REFUSED
        history = _history(config_path)
        assert len(history.entries) == 1
        assert history.entries[0].outcome == OUTCOME_REFUSED
    finally:
        store.root.chmod(0o700)


# -- the run lock -------------------------------------------------------


def _place_lock(store: SnapshotStore, pid: int = 4242) -> Path:
    lock = store.root / RUN_LOCK_FILE
    lock.write_text(
        json.dumps(
            {
                "pid": pid,
                "started_utc": "2026-10-20T00:00:00.000000Z",
                "run_id": "an-earlier-run",
            }
        ),
        encoding="utf-8",
    )
    return lock


def test_run_refused_while_a_living_run_holds_the_lock(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "backup.json"
    lock = _place_lock(store)
    before = lock.read_bytes()

    result = _run(config, config_path, _moment(20), pid_alive=lambda pid: True)

    assert result.outcome == OUTCOME_REFUSED
    assert store.list_snapshots() == []  # nothing was written
    assert lock.exists()
    assert lock.read_bytes() == before  # the other run's lock is untouched
    assert _history(config_path).entries[0].outcome == OUTCOME_REFUSED


def test_run_clears_a_dead_runs_lock_and_says_so(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "backup.json"
    lock = _place_lock(store)

    result = _run(config, config_path, _moment(20), pid_alive=lambda pid: False)

    assert result.outcome == OUTCOME_COMPLETE
    assert any(
        "leftover lock was cleared" in note
        for note in result.history_entry.notes
    )
    assert not lock.exists()  # this run let its own lock go at the end


# -- the spot-check cadence ---------------------------------------------


def _stored_object_files(store: SnapshotStore) -> list[Path]:
    return sorted(
        path for path in store.objects_dir.rglob("*") if path.is_file()
    )


def _flip_one_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    assert data
    data[0] ^= 0xFF
    path.write_bytes(bytes(data))


def test_first_run_spot_checks_when_verify_every_run_is_one(store, tmp_path):
    """Every run means every run — the first one included."""
    source = _source(tmp_path, "src", {"a.txt": "checked from the start"})
    config = _config(store.root, [source], verify_every_runs=1)
    config_path = tmp_path / "backup.json"

    result = _run(config, config_path, _moment(20))

    assert result.outcome == OUTCOME_COMPLETE
    assert result.history_entry.verified is True


def test_spot_check_fires_once_unverified_runs_reach_the_cadence(
    store, tmp_path
):
    source = _source(tmp_path, "src", {"a.txt": "checked in due course"})
    config = _config(store.root, [source], verify_every_runs=1)
    config_path = tmp_path / "backup.json"

    _run(config, config_path, _moment(20))
    second = _run(config, config_path, _moment(20, hour=13))

    assert second.outcome == OUTCOME_COMPLETE
    assert second.history_entry.verified is True


def test_damaged_stored_copy_refuses_the_run_when_a_spot_check_is_due(
    store, tmp_path
):
    source = _source(tmp_path, "src", {"a.txt": "one copy, kept honestly"})
    config = _config(store.root, [source], verify_every_runs=1)
    config_path = tmp_path / "backup.json"

    first = _run(config, config_path, _moment(20))
    assert first.outcome == OUTCOME_COMPLETE
    objects = _stored_object_files(store)
    assert len(objects) == 1
    _flip_one_byte(objects[0])

    # While the spot-check is due, status must never call this READY.
    status = backup_status(
        config, config_path, now=_moment(20, hour=13), free_space_fn=BIG_SPACE
    )
    assert status.state == STATE_STORE_DAMAGED

    refused = _run(config, config_path, _moment(20, hour=14))
    assert refused.outcome == OUTCOME_REFUSED
    assert refused.state == STATE_STORE_DAMAGED
    assert refused.history_entry.verified is False
    assert refused.history_entry.verify_problems >= 1
    # Nothing new was written on the refused run.
    assert len(store.list_snapshots()) == len(first.history_entry.snapshots)


def test_status_never_ready_after_a_failed_spot_check(store, tmp_path):
    """A spot-check that found damage is not 'used up': with a
    check-every-run cadence, status must keep saying the store is
    damaged — never READY — until a spot-check passes again."""
    source = _source(tmp_path, "src", {"a.txt": "one copy, kept honestly"})
    config = _config(store.root, [source], verify_every_runs=1)
    config_path = tmp_path / "backup.json"

    _run(config, config_path, _moment(20))
    _flip_one_byte(_stored_object_files(store)[0])
    refused = _run(config, config_path, _moment(20, hour=13))
    assert refused.history_entry.verified is False

    status = backup_status(
        config, config_path, now=_moment(20, hour=14), free_space_fn=BIG_SPACE
    )
    assert status.state == STATE_STORE_DAMAGED


# -- estimates ------------------------------------------------------------


def test_estimate_before_any_run_is_everything(store, tmp_path):
    source = _source(
        tmp_path, "src", {"a.txt": "x" * 11, "sub/b.txt": "y" * 7}
    )
    config = _config(store.root, [source])

    estimate, basis = estimate_backup_bytes(config, History(entries=[]))

    assert estimate == 18
    assert "first backup" in basis


def test_estimate_after_a_run_is_what_last_time_copied(store, tmp_path):
    source = _source(tmp_path, "src", {"a.txt": "x" * 25})
    config = _config(store.root, [source])
    config_path = tmp_path / "backup.json"
    result = _run(config, config_path, _moment(20))
    assert result.history_entry.bytes_copied == 25

    estimate, basis = estimate_backup_bytes(config, _history(config_path))

    assert estimate == result.history_entry.bytes_copied
    assert "last time" in basis


# -- status -----------------------------------------------------------------


def test_status_never_run(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "backup.json"
    save_config(config, config_path)

    status = backup_status(
        config, config_path, now=_moment(20), free_space_fn=BIG_SPACE
    )

    assert status.state == STATE_NEVER_RUN


def test_status_ready_after_a_run(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "backup.json"
    _run(config, config_path, _moment(20))

    status = backup_status(
        config, config_path, now=_moment(20, hour=13), free_space_fn=BIG_SPACE
    )

    assert status.state == STATE_READY


def test_status_target_missing_when_the_store_is_deleted(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "backup.json"
    _run(config, config_path, _moment(20))
    shutil.rmtree(store.root)

    status = backup_status(
        config, config_path, now=_moment(20, hour=13), free_space_fn=BIG_SPACE
    )

    assert status.state == STATE_TARGET_MISSING


def test_status_overdue_three_days_after_a_daily_backup(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "backup.json"
    _run(config, config_path, _moment(20))

    status = backup_status(
        config, config_path, now=_moment(23), free_space_fn=BIG_SPACE
    )

    assert status.state == STATE_OVERDUE


def test_status_target_full_with_a_run_behind_it(store, tree, tmp_path):
    config = _config(store.root, [tree])
    config_path = tmp_path / "backup.json"
    result = _run(config, config_path, _moment(20))
    assert result.history_entry.bytes_copied > 0

    status = backup_status(
        config,
        config_path,
        now=_moment(20, hour=13),
        free_space_fn=lambda path: 0,
    )

    assert status.state == STATE_TARGET_FULL
    # Imported for the shared vocabulary; the overdue window above
    # is a day and a half for a daily backup.
    assert timedelta(hours=36) < timedelta(days=3)
