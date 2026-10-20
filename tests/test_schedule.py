"""When a backup is due: the schedule math, the due rule, the commands.

The schedule speaks in the owner's local wall-clock time, so every
test works in one fixed-offset timezone (UTC-5) and computes its
expectations in that same frame — never the machine's timezone,
never the wall clock. The Windows side is a fake executor returning
the answers the test chooses: Lazarus's plain sentences are what
get checked, never schtasks' raw output.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lazarus.backup import append_history, history_path_for, load_history, save_config
from lazarus.backup_model import (
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_REFUSED,
    TRIGGER_CATCHUP,
    TRIGGER_SCHEDULED,
    BackupConfig,
    History,
    HistoryEntry,
    RetentionSpec,
    ScheduleSpec,
)
from lazarus.errors import BackupError
from lazarus.schedule import (
    UnavailableSchedulerPlatform,
    WindowsSchedulerPlatform,
    due_state,
    last_productive_entry,
    last_scheduled_instant,
    next_scheduled_instant,
    register_schedule,
    run_due_backup,
    unregister_schedule,
)
from lazarus.schedule_commands import (
    TASK_NAME,
    create_task_argv,
    delete_task_argv,
    query_task_argv,
    run_task_argv,
)
from lazarus.store import format_utc

TZ = timezone(timedelta(hours=-5))
BIG_SPACE = lambda path: 10**12  # noqa: E731 - a told-you-so disk


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


# 2026-10-19 is a Monday; 2026-10-21 is the Wednesday of that week.
DAILY = ScheduleSpec(every="daily", at="02:00")
WEEKLY_MONDAY = ScheduleSpec(every="weekly", at="02:00", weekday="monday")


def _config(store_root, sources) -> BackupConfig:
    return BackupConfig(
        store=str(store_root),
        sources=[str(source) for source in sources],
        schedule=DAILY,
        retention=RetentionSpec(mode="count", count=10, days=30),
        verify_every_runs=365,
    )


def _entry(start: datetime, outcome: str = OUTCOME_COMPLETE) -> HistoryEntry:
    return HistoryEntry(
        run_id=f"run-{start.isoformat()}-{outcome}",
        started_utc=format_utc(start),
        outcome=outcome,
        snapshots=["a-snapshot"] if outcome == OUTCOME_COMPLETE else [],
    )


def _due(config, entries, now):
    return due_state(config, History(entries=list(entries)), now)


# -- daily math ---------------------------------------------------------


def test_daily_before_the_time_last_was_yesterday():
    now = _at(2026, 10, 21, 1)
    assert last_scheduled_instant(DAILY, now) == _at(2026, 10, 20, 2)
    assert next_scheduled_instant(DAILY, now) == _at(2026, 10, 21, 2)


def test_daily_after_the_time_last_was_today():
    now = _at(2026, 10, 21, 3)
    assert last_scheduled_instant(DAILY, now) == _at(2026, 10, 21, 2)
    assert next_scheduled_instant(DAILY, now) == _at(2026, 10, 22, 2)


def test_daily_exactly_at_the_time_is_due_that_moment():
    now = _at(2026, 10, 21, 2)
    assert last_scheduled_instant(DAILY, now) == _at(2026, 10, 21, 2)
    assert next_scheduled_instant(DAILY, now) == _at(2026, 10, 22, 2)


# -- weekly math ----------------------------------------------------------


def test_weekly_from_wednesday_last_and_next_are_mondays():
    now = _at(2026, 10, 21, 12)  # a Wednesday
    assert last_scheduled_instant(WEEKLY_MONDAY, now) == _at(2026, 10, 19, 2)
    assert next_scheduled_instant(WEEKLY_MONDAY, now) == _at(2026, 10, 26, 2)


def test_weekly_on_monday_before_the_time():
    now = _at(2026, 10, 19, 1)  # Monday, an hour early
    assert last_scheduled_instant(WEEKLY_MONDAY, now) == _at(2026, 10, 12, 2)
    assert next_scheduled_instant(WEEKLY_MONDAY, now) == _at(2026, 10, 19, 2)


def test_weekly_on_monday_after_the_time():
    now = _at(2026, 10, 19, 3)  # Monday, an hour late
    assert last_scheduled_instant(WEEKLY_MONDAY, now) == _at(2026, 10, 19, 2)
    assert next_scheduled_instant(WEEKLY_MONDAY, now) == _at(2026, 10, 26, 2)


# -- the due rule ------------------------------------------------------------


def test_due_with_no_history_inside_the_window_is_scheduled():
    config = _config("/store", ["/src"])
    state = _due(config, [], _at(2026, 10, 21, 2, 10))
    assert state.due is True
    assert state.instant == _at(2026, 10, 21, 2)
    assert state.trigger == TRIGGER_SCHEDULED
    assert state.next_instant == _at(2026, 10, 22, 2)


def test_due_with_no_history_hours_late_is_a_catch_up():
    config = _config("/store", ["/src"])
    state = _due(config, [], _at(2026, 10, 21, 5))
    assert state.due is True
    assert state.trigger == TRIGGER_CATCHUP


def test_not_due_when_a_productive_run_started_after_the_instant():
    config = _config("/store", ["/src"])
    state = _due(config, [_entry(_at(2026, 10, 21, 2, 5))], _at(2026, 10, 21, 3))
    assert state.due is False
    assert state.trigger is None
    assert state.instant == _at(2026, 10, 21, 2)
    assert state.next_instant == _at(2026, 10, 22, 2)


def test_due_when_the_last_productive_run_is_before_the_instant():
    config = _config("/store", ["/src"])
    state = _due(config, [_entry(_at(2026, 10, 21, 1))], _at(2026, 10, 21, 5))
    assert state.due is True
    assert state.trigger == TRIGGER_CATCHUP


def test_refused_run_after_the_instant_does_not_settle_the_debt():
    config = _config("/store", ["/src"])
    refused = _entry(_at(2026, 10, 21, 2, 5), outcome=OUTCOME_REFUSED)
    state = _due(config, [refused], _at(2026, 10, 21, 2, 10))
    assert state.due is True
    assert state.trigger == TRIGGER_SCHEDULED


def test_failed_run_after_the_instant_does_not_settle_the_debt():
    config = _config("/store", ["/src"])
    failed = _entry(_at(2026, 10, 21, 2, 5), outcome=OUTCOME_FAILED)
    state = _due(config, [failed], _at(2026, 10, 21, 5))
    assert state.due is True
    assert state.trigger == TRIGGER_CATCHUP


def test_last_productive_entry_ignores_runs_that_saved_nothing():
    older = _entry(_at(2026, 10, 20, 2, 5))
    newer = _entry(_at(2026, 10, 21, 2, 5))
    refused = _entry(_at(2026, 10, 21, 3), outcome=OUTCOME_REFUSED)
    history = History(entries=[older, refused, newer])
    assert last_productive_entry(history) is newer
    assert last_productive_entry(History(entries=[refused])) is None
    assert last_productive_entry(History(entries=[])) is None


# -- run_due_backup, end to end ----------------------------------------------


def _setup_backup(store, tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    (source / "a.txt").write_text("due and owing", encoding="utf-8")
    config = _config(store.root, [source])
    config_path = tmp_path / "backup.json"
    save_config(config, config_path)
    return config, config_path


def test_run_due_backup_runs_a_scheduled_backup(store, tmp_path):
    config, config_path = _setup_backup(store, tmp_path)

    outcome = run_due_backup(
        config, config_path, now=_at(2026, 10, 21, 2, 10), free_space_fn=BIG_SPACE
    )

    assert outcome.due is True
    assert outcome.trigger == TRIGGER_SCHEDULED
    assert outcome.result is not None
    assert outcome.result.outcome == OUTCOME_COMPLETE
    history = load_history(history_path_for(config_path))
    assert len(history.entries) == 1
    assert history.entries[0].trigger == TRIGGER_SCHEDULED


def test_run_due_backup_runs_a_catch_up_backup(store, tmp_path):
    config, config_path = _setup_backup(store, tmp_path)

    outcome = run_due_backup(
        config, config_path, now=_at(2026, 10, 21, 5), free_space_fn=BIG_SPACE
    )

    assert outcome.due is True
    assert outcome.trigger == TRIGGER_CATCHUP
    assert outcome.result is not None
    assert outcome.result.outcome == OUTCOME_COMPLETE
    history = load_history(history_path_for(config_path))
    assert len(history.entries) == 1
    assert history.entries[0].trigger == TRIGGER_CATCHUP


def test_run_due_backup_not_due_adds_no_history_entry(store, tmp_path):
    config, config_path = _setup_backup(store, tmp_path)
    history_path = history_path_for(config_path)
    append_history(history_path, _entry(_at(2026, 10, 21, 2, 5)))

    outcome = run_due_backup(
        config, config_path, now=_at(2026, 10, 21, 3), free_space_fn=BIG_SPACE
    )

    assert outcome.due is False
    assert outcome.trigger is None
    assert outcome.result is None
    assert outcome.next_instant == _at(2026, 10, 22, 2)
    assert len(load_history(history_path).entries) == 1  # unchanged
    assert store.list_snapshots() == []


def test_missed_days_are_owed_once_never_a_burst(store, tmp_path):
    """Three days of silence settle with a single run: the immediate
    second due-check owes nothing and runs nothing."""
    config, config_path = _setup_backup(store, tmp_path)
    history_path = history_path_for(config_path)
    append_history(history_path, _entry(_at(2026, 10, 18, 2, 5)))
    now = _at(2026, 10, 21, 5)

    first = run_due_backup(config, config_path, now=now, free_space_fn=BIG_SPACE)
    assert first.due is True
    assert first.trigger == TRIGGER_CATCHUP
    assert first.result is not None
    assert first.result.outcome == OUTCOME_COMPLETE
    snapshots_after_first = len(store.list_snapshots())
    assert snapshots_after_first == 1
    assert len(load_history(history_path).entries) == 2  # exactly one new run

    second = run_due_backup(config, config_path, now=now, free_space_fn=BIG_SPACE)
    assert second.due is False
    assert second.result is None
    assert len(load_history(history_path).entries) == 2
    assert len(store.list_snapshots()) == snapshots_after_first


# -- the schtasks command builders --------------------------------------------


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_create_daily_task_argv():
    argv = create_task_argv("lazarus", "C:\\cfg\\backup.json", DAILY)
    assert argv[0] == "schtasks.exe"
    assert _flag(argv, "/TN") == TASK_NAME
    run_line = _flag(argv, "/TR")
    assert "lazarus" in run_line
    assert "backup due" in run_line
    assert "C:\\cfg\\backup.json" in run_line
    assert _flag(argv, "/SC") == "DAILY"
    assert "/D" not in argv
    assert _flag(argv, "/ST") == "02:00"


def test_create_weekly_task_argv_carries_the_weekday_code():
    argv = create_task_argv("lazarus", "cfg.json", WEEKLY_MONDAY)
    assert _flag(argv, "/TN") == TASK_NAME
    assert _flag(argv, "/SC") == "WEEKLY"
    assert _flag(argv, "/D") == "MON"
    assert _flag(argv, "/ST") == "02:00"

    friday = ScheduleSpec(every="weekly", at="21:30", weekday="friday")
    argv = create_task_argv("lazarus", "cfg.json", friday)
    assert _flag(argv, "/D") == "FRI"
    assert _flag(argv, "/ST") == "21:30"

    sunday = ScheduleSpec(every="weekly", at="02:00", weekday="sunday")
    assert _flag(create_task_argv("lazarus", "cfg.json", sunday), "/D") == "SUN"


def test_delete_query_and_run_task_argv_shapes():
    assert delete_task_argv() == ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"]
    assert query_task_argv() == ["schtasks.exe", "/Query", "/TN", TASK_NAME]
    assert run_task_argv() == ["schtasks.exe", "/Run", "/TN", TASK_NAME]
    # The one task name is used consistently everywhere it appears.
    for argv in (
        create_task_argv("lazarus", "cfg.json", DAILY),
        delete_task_argv(),
        query_task_argv(),
        run_task_argv(),
    ):
        assert _flag(argv, "/TN") == TASK_NAME


# -- the scheduler platform seam ------------------------------------------------

RAW_TOOL_OUTPUT = "schtasks.exe raw complaint: ERROR XYZ should never be shown"


class FakeExecute:
    """Stands in for schtasks: records the words, returns the verdict."""

    def __init__(self, returncode: int, output: str = ""):
        self.returncode = returncode
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.returncode, self.output


def test_windows_platform_register_success_builds_the_create_command():
    fake = FakeExecute(0)
    platform = WindowsSchedulerPlatform(execute=fake)
    platform.register("lazarus", "cfg.json", DAILY)
    assert fake.calls == [create_task_argv("lazarus", "cfg.json", DAILY)]


def test_windows_platform_register_failure_is_plain_and_hides_tool_output():
    fake = FakeExecute(1, output=RAW_TOOL_OUTPUT)
    platform = WindowsSchedulerPlatform(execute=fake)
    with pytest.raises(BackupError) as caught:
        platform.register("lazarus", "cfg.json", DAILY)
    assert RAW_TOOL_OUTPUT not in str(caught.value)
    assert "Windows could not set up the scheduled backup" in str(caught.value)


def test_windows_platform_is_registered_follows_the_return_code():
    assert WindowsSchedulerPlatform(execute=FakeExecute(0)).is_registered() is True
    assert WindowsSchedulerPlatform(execute=FakeExecute(1)).is_registered() is False


def test_windows_platform_unregister_failure_is_plain_and_hides_tool_output():
    fake = FakeExecute(1, output=RAW_TOOL_OUTPUT)
    platform = WindowsSchedulerPlatform(execute=fake)
    with pytest.raises(BackupError) as caught:
        platform.unregister()
    assert RAW_TOOL_OUTPUT not in str(caught.value)
    assert fake.calls == [delete_task_argv()]


def test_windows_platform_unregister_success():
    fake = FakeExecute(0)
    WindowsSchedulerPlatform(execute=fake).unregister()
    assert fake.calls == [delete_task_argv()]


def test_register_and_unregister_schedule_words_with_a_fake_platform():
    fake = FakeExecute(0)
    platform = WindowsSchedulerPlatform(execute=fake)
    config = _config("/store", ["/src"])
    assert (
        register_schedule(config, "cfg.json", "lazarus", platform=platform)
        == "Backup will run every day at 02:00."
    )
    message = unregister_schedule(platform=platform)
    assert "scheduled backup has been removed" in message


def test_unavailable_platform_says_windows_plainly():
    platform = UnavailableSchedulerPlatform()
    with pytest.raises(BackupError) as caught:
        platform.register("lazarus", "cfg.json", DAILY)
    assert "Windows" in str(caught.value)
    with pytest.raises(BackupError) as caught:
        platform.unregister()
    assert "Windows" in str(caught.value)
    assert platform.is_registered() is False
