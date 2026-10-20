"""When a backup is due — and asking Windows to keep the rhythm.

The engine in :mod:`lazarus.backup` knows how to run a backup; this
module knows *when*. A schedule says "every day at 02:00" or "every
Monday at 02:00", in the owner's local time, and three questions
follow from it: when was the last moment a backup should have run,
when is the next one, and — given the history — is one owed right
now?

The due rule is forgiving on purpose. If the computer was asleep
or switched off at the scheduled moment, the missed backup is
still owed, and it is owed exactly once: a run that happens within
fifteen minutes of its moment counts as the scheduled run, and a
run later than that is a catch-up — but never a burst of make-up
runs for every missed day. One missed moment, one backup.

Registering the rhythm with the computer is a separate job, behind
the same kind of platform seam as the unlock work: Windows Task
Scheduler does the waking, through :mod:`lazarus.schedule_commands`,
on Windows; on anything else the seam says so plainly instead of
pretending. The due check itself needs no scheduler at all — it is
the history, the schedule, and the clock, worked out in the open.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from .backup import (
    _entry_is_future_dated,
    history_path_for,
    load_history,
    run_backup,
)
from .backup_model import (
    CADENCE_DAILY,
    CADENCE_WEEKLY,
    OUTCOME_COMPLETE,
    OUTCOME_PARTIAL,
    TRIGGER_CATCHUP,
    TRIGGER_SCHEDULED,
    BackupConfig,
    History,
    HistoryEntry,
    RunResult,
    ScheduleSpec,
)
from .errors import BackupError
from .schedule_commands import (
    create_task_argv,
    delete_task_argv,
    query_task_argv,
)
from .store import safe_instant

_AT_PATTERN = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")

_WEEKDAY_NUMBERS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

#: How late a run may start after its scheduled moment and still
#: count as the scheduled run rather than a catch-up.
_SCHEDULED_WINDOW = timedelta(minutes=15)

_NOT_WINDOWS_REGISTER_MESSAGE = (
    "Scheduled backups are arranged through Windows Task Scheduler, "
    "and this computer is not running Windows, so the scheduled "
    "backup cannot be set up here. On a Windows computer Lazarus "
    "can register it; until then, backups can still be run by hand."
)

_NOT_WINDOWS_UNREGISTER_MESSAGE = (
    "Scheduled backups are arranged through Windows Task Scheduler, "
    "and this computer is not running Windows, so the scheduled "
    "backup cannot be removed here."
)


# -- the schedule math ----------------------------------------------------
#
# All of it is worked out in the timezone of the ``now`` handed in:
# the schedule's "at" is a local wall-clock time, so "02:00" means
# 02:00 wherever — and whenever — the asking happens. Production
# passes the computer's local time; tests pass fixed offsets. The
# math never reaches for a timezone of its own.


def _parse_at(schedule: ScheduleSpec) -> Optional[tuple[int, int]]:
    """The schedule's time of day as ``(hour, minute)``, or None."""
    match = _AT_PATTERN.fullmatch((schedule.at or "").strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _aware(now: datetime) -> datetime:
    """``now`` as an aware moment; a bare one is read as UTC."""
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _at_time(moment: datetime, hour: int, minute: int) -> datetime:
    return moment.replace(hour=hour, minute=minute, second=0, microsecond=0)


def last_scheduled_instant(
    schedule: ScheduleSpec, now: datetime
) -> Optional[datetime]:
    """The most recent scheduled moment at or before ``now``.

    Returns None only when the inputs cannot be read at all — a
    cadence Lazarus does not know, a time of day that is not a
    time, a weekday that is not a day. A schedule that can be read
    always has a last moment: today's, or an earlier day's.
    """
    if now is None or schedule is None:
        return None
    parsed = _parse_at(schedule)
    if parsed is None:
        return None
    hour, minute = parsed
    moment = _aware(now)
    if schedule.every == CADENCE_DAILY:
        candidate = _at_time(moment, hour, minute)
        if candidate <= moment:
            return candidate
        return candidate - timedelta(days=1)
    if schedule.every == CADENCE_WEEKLY:
        target = _WEEKDAY_NUMBERS.get(
            (schedule.weekday or "").strip().lower()
        )
        if target is None:
            return None
        days_back = (moment.weekday() - target) % 7
        candidate = _at_time(moment - timedelta(days=days_back), hour, minute)
        if candidate <= moment:
            return candidate
        return candidate - timedelta(days=7)
    return None


def next_scheduled_instant(schedule: ScheduleSpec, now: datetime) -> datetime:
    """The first scheduled moment strictly after ``now``.

    The schedule is expected to have been checked already (the
    settings loader checks every schedule it hands out); one that
    cannot be read raises a plain :class:`BackupError` rather than
    inventing a moment.
    """
    if now is None or schedule is None:
        raise BackupError(
            "Lazarus could not work out when the next backup is due, "
            "because the schedule could not be read. Check the backup "
            "settings and try again."
        )
    parsed = _parse_at(schedule)
    if parsed is None:
        raise BackupError(
            "Lazarus could not work out when the next backup is due, "
            "because the time of day in the backup settings could not "
            "be read. Check the backup settings and try again."
        )
    hour, minute = parsed
    moment = _aware(now)
    if schedule.every == CADENCE_DAILY:
        candidate = _at_time(moment, hour, minute)
        if candidate > moment:
            return candidate
        return candidate + timedelta(days=1)
    if schedule.every == CADENCE_WEEKLY:
        target = _WEEKDAY_NUMBERS.get(
            (schedule.weekday or "").strip().lower()
        )
        if target is None:
            raise BackupError(
                "Lazarus could not work out when the next backup is "
                "due, because the weekday in the backup settings "
                "could not be read. Check the backup settings and "
                "try again."
            )
        days_forward = (target - moment.weekday()) % 7
        candidate = _at_time(
            moment + timedelta(days=days_forward), hour, minute
        )
        if candidate > moment:
            return candidate
        return candidate + timedelta(days=7)
    raise BackupError(
        "Lazarus could not work out when the next backup is due, "
        "because the backup settings name a schedule it does not "
        "know. Check the backup settings and try again."
    )


def last_productive_entry(
    history: History, now: Optional[datetime] = None
) -> Optional[HistoryEntry]:
    """The latest history entry that actually saved something.

    "Latest" is by the instant the run started, read honestly: an
    entry whose start time cannot be read sorts earliest, so a
    damaged line never outranks a run Lazarus can place in time.
    Only runs that finished complete or partial count — a refused
    or failed run saved nothing, so it settles no debt. When
    ``now`` is given, entries dated well into the future are
    ignored: a run that has not happened yet settles no debt
    either. An entry whose start cannot be read is never ignored
    that way — it keeps its sorts-earliest place above.
    """
    best: Optional[HistoryEntry] = None
    best_key: Optional[tuple[datetime, int]] = None
    for index, entry in enumerate(history.entries):
        if entry.outcome not in (OUTCOME_COMPLETE, OUTCOME_PARTIAL):
            continue
        if now is not None and _entry_is_future_dated(entry, now):
            continue
        key = (safe_instant(entry.started_utc), index)
        if best_key is None or key > best_key:
            best = entry
            best_key = key
    return best


@dataclass
class DueState:
    """Whether a backup is owed, and the moment that says so."""

    due: bool
    instant: Optional[datetime]
    trigger: Optional[str]
    next_instant: datetime


def due_state(
    config: BackupConfig, history: History, now: datetime
) -> DueState:
    """Work out whether the schedule is owed a run at ``now``.

    A backup is due when a scheduled moment has arrived and no run
    that saved anything has started at or after it. The owed run is
    a scheduled run when it starts within fifteen minutes of its
    moment, and a catch-up after that — either way, exactly one run
    settles the moment, however many moments were missed before it.
    """
    moment = _aware(now)
    next_instant = next_scheduled_instant(config.schedule, moment)
    instant = last_scheduled_instant(config.schedule, moment)
    if instant is None:
        return DueState(
            due=False, instant=None, trigger=None, next_instant=next_instant
        )
    productive = last_productive_entry(history, moment)
    if productive is not None:
        if safe_instant(productive.started_utc) >= instant:
            return DueState(
                due=False,
                instant=instant,
                trigger=None,
                next_instant=next_instant,
            )
    if moment - instant <= _SCHEDULED_WINDOW:
        trigger = TRIGGER_SCHEDULED
    else:
        trigger = TRIGGER_CATCHUP
    return DueState(
        due=True, instant=instant, trigger=trigger, next_instant=next_instant
    )


# -- the scheduler seam -----------------------------------------------------


class SchedulerPlatform(Protocol):
    """The seam between Lazarus and the computer's own scheduler.

    An implementation registers (or removes) the one backup task,
    and can say whether it is there. Failures are plain
    :class:`BackupError` sentences — the tool's own output never
    becomes user-facing text.
    """

    def register(
        self, program: str, config_path: str, schedule: ScheduleSpec
    ) -> None:
        """Register the backup task with the computer's scheduler."""
        ...

    def unregister(self) -> None:
        """Remove the backup task from the computer's scheduler."""
        ...

    def is_registered(self) -> bool:
        """True if the backup task is registered."""
        ...


#: No Windows tool gets forever. A hung manage-bde, PowerShell, or
#: schtasks must become a designed, plain-language failure (the
#: callers translate every executor failure), never a Lazarus that
#: hangs on a spinning page with no way back.
_EXECUTE_TIMEOUT_SECONDS = 30


def _subprocess_execute(argv: list[str]) -> tuple[int, str]:
    """The real run: one command, no shell, words captured not shown."""
    result = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_EXECUTE_TIMEOUT_SECONDS,
    )
    return result.returncode, result.stdout


class WindowsSchedulerPlatform:
    """The scheduler seam, backed by Windows Task Scheduler.

    ``execute`` performs the real run and returns
    ``(returncode, output)``; it is injectable so tests can stand
    in for Windows. Tool output is never carried into a message:
    the owner gets Lazarus's plain sentence instead.
    """

    def __init__(
        self,
        execute: Callable[[list[str]], tuple[int, str]] | None = None,
    ) -> None:
        self._execute = execute if execute is not None else _subprocess_execute

    _EXECUTE_MESSAGE = (
        "Lazarus could not run Windows Task Scheduler on this "
        "computer, so it could not arrange the scheduled backup. "
        "Task Scheduler may be missing or blocked."
    )

    def _run(self, argv: list[str]) -> tuple[int, str]:
        """Run one command, translating any failure to run it.

        The executor is a seam, and whatever crosses it gets
        checked: a tool that cannot be started, an executor that
        fails in any way, or an answer in the wrong shape all
        become the same designed Lazarus error in plain language
        — never a raw complaint from underneath.
        """
        try:
            returncode, output = self._execute(argv)
        except Exception:
            raise BackupError(self._EXECUTE_MESSAGE) from None
        if not isinstance(returncode, int) or not isinstance(output, str):
            raise BackupError(self._EXECUTE_MESSAGE)
        return returncode, output

    def register(
        self, program: str, config_path: str, schedule: ScheduleSpec
    ) -> None:
        argv = create_task_argv(program, str(config_path), schedule)
        returncode, _output = self._run(argv)
        if returncode != 0:
            raise BackupError(
                "Windows could not set up the scheduled backup. "
                "The task was not registered; try again, or check "
                "Windows Task Scheduler."
            )

    def unregister(self) -> None:
        returncode, _output = self._run(delete_task_argv())
        if returncode != 0:
            raise BackupError(
                "Windows could not remove the scheduled backup. "
                "The task may not be set up, or Windows Task "
                "Scheduler could not be reached."
            )

    def is_registered(self) -> bool:
        returncode, _output = self._run(query_task_argv())
        return returncode == 0


class UnavailableSchedulerPlatform:
    """The scheduler seam on any computer that is not running Windows.

    Registering and removing raise the same plain refusal: the
    scheduling is arranged through Windows Task Scheduler, and
    there is nothing honest to do on a machine without it. Asking
    whether the task is there gets an honest False.
    """

    def register(
        self, program: str, config_path: str, schedule: ScheduleSpec
    ) -> None:
        raise BackupError(_NOT_WINDOWS_REGISTER_MESSAGE)

    def unregister(self) -> None:
        raise BackupError(_NOT_WINDOWS_UNREGISTER_MESSAGE)

    def is_registered(self) -> bool:
        return False


def default_scheduler() -> SchedulerPlatform:
    """The scheduler for this computer: Windows', or an honest no."""
    if sys.platform == "win32":
        return WindowsSchedulerPlatform()
    return UnavailableSchedulerPlatform()


def _cadence_words(schedule: ScheduleSpec) -> str:
    """The schedule in words: 'every day at 02:00', and the like."""
    if schedule.every == CADENCE_WEEKLY:
        weekday = (schedule.weekday or "monday").strip().lower().capitalize()
        return f"every {weekday} at {schedule.at}"
    return f"every day at {schedule.at}"


def register_schedule(
    config: BackupConfig,
    config_path,
    program: str,
    platform: Optional[SchedulerPlatform] = None,
) -> str:
    """Register the backup's schedule and say so, in plain words."""
    seam = platform if platform is not None else default_scheduler()
    seam.register(program, str(config_path), config.schedule)
    return f"Backup will run {_cadence_words(config.schedule)}."


def unregister_schedule(
    platform: Optional[SchedulerPlatform] = None,
) -> str:
    """Remove the backup's schedule and say so, in plain words."""
    seam = platform if platform is not None else default_scheduler()
    seam.unregister()
    return (
        "The scheduled backup has been removed. Backups will no "
        "longer run on their own; you can still run one by hand "
        "whenever you like."
    )


# -- the due entry point ----------------------------------------------------


@dataclass
class DueResult:
    """How a due check ended: nothing owed, or the run it made."""

    due: bool
    trigger: Optional[str]
    result: Optional[RunResult]
    next_instant: datetime

    def to_text(self) -> str:
        if not self.due:
            when = self.next_instant.strftime("%A %Y-%m-%d at %H:%M")
            return (
                "No backup is due right now. "
                f"The next scheduled backup is {when}."
            )
        if self.result is not None:
            return self.result.to_text()
        when = self.next_instant.strftime("%A %Y-%m-%d at %H:%M")
        return (
            "No backup is due right now. "
            f"The next scheduled backup is {when}."
        )


def run_due_backup(
    config: BackupConfig,
    config_path,
    *,
    now: Optional[datetime] = None,
    free_space_fn: Optional[Callable[[Path], int]] = None,
    pid_alive: Optional[Callable[[int], bool]] = None,
) -> DueResult:
    """Run a backup if — and only if — the schedule says one is due.

    This is the entry point the registered Windows task calls.
    ``now`` is local time for the schedule math; the run itself is
    handed the same moment in UTC, because the engine speaks UTC
    throughout — the conversion happens here, at the boundary.
    """
    moment = now if now is not None else datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    config_file = Path(config_path)
    history = load_history(history_path_for(config_file))
    state = due_state(config, history, moment)
    if not state.due:
        return DueResult(
            due=False,
            trigger=None,
            result=None,
            next_instant=state.next_instant,
        )
    result = run_backup(
        config,
        config_file,
        # Schedule math speaks local; run_backup speaks UTC.
        now=moment.astimezone(timezone.utc),
        trigger=state.trigger or TRIGGER_SCHEDULED,
        free_space_fn=free_space_fn,
        pid_alive=pid_alive,
    )
    return DueResult(
        due=True,
        trigger=state.trigger,
        result=result,
        next_instant=state.next_instant,
    )
