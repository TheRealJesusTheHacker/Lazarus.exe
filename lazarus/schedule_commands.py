"""Command lines for Windows Task Scheduler, built and never run.

Phase 4's scheduled backups are kept by Windows itself: Lazarus
asks Windows Task Scheduler — through its own tool, ``schtasks.exe``
— to run ``lazarus backup due`` at the time the owner chose, and
the due check inside Lazarus decides whether a backup is actually
owed when that moment comes.

This module only *builds* those commands. Every function here is
pure, so the exact words Lazarus would hand to Windows can be
checked in a test without a Windows machine, and a built command
is safe to display: it holds the program name, the settings file,
and the schedule — nothing secret, ever. Running the commands is
the platform seam's job, in :mod:`lazarus.schedule`.
"""

from __future__ import annotations

from .backup_model import CADENCE_WEEKLY, ScheduleSpec

#: The name the backup task is registered under in Windows Task
#: Scheduler. One backup, one task: registering again replaces it.
TASK_NAME = "LAZARUS.EXE Backup"

#: How each weekday is spelled for schtasks' ``/D`` flag.
_WEEKDAY_CODES = {
    "monday": "MON",
    "tuesday": "TUE",
    "wednesday": "WED",
    "thursday": "THU",
    "friday": "FRI",
    "saturday": "SAT",
    "sunday": "SUN",
}


def create_task_argv(
    program: str, config_path: str, schedule: ScheduleSpec
) -> list[str]:
    """The command that registers the scheduled backup with Windows.

    The task runs ``<program> backup due --config <config_path>``:
    the due check, not the scheduler, decides whether a backup is
    owed, so a task that fires after the computer slept through
    its moment still produces exactly one catch-up run. The whole
    run line is one string after ``/TR`` — that is how schtasks
    stores a command line.
    """
    tr = f'"{program}" backup due --config "{config_path}"'
    weekly = schedule.every == CADENCE_WEEKLY
    argv = [
        "schtasks.exe",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        tr,
        "/SC",
        "WEEKLY" if weekly else "DAILY",
    ]
    if weekly:
        code = _WEEKDAY_CODES.get(str(schedule.weekday).strip().lower(), "MON")
        argv += ["/D", code]
    argv += ["/ST", schedule.at, "/F"]
    return argv


def delete_task_argv() -> list[str]:
    """The command that removes the scheduled backup task."""
    return ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"]


def query_task_argv() -> list[str]:
    """The command that asks Windows whether the task is registered."""
    return ["schtasks.exe", "/Query", "/TN", TASK_NAME]


def run_task_argv() -> list[str]:
    """The command that asks Windows to run the task right away."""
    return ["schtasks.exe", "/Run", "/TN", TASK_NAME]
