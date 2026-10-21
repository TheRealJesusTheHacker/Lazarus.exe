"""The backup engine: keeping the promise, run after run.

Phase 1 taught Lazarus to take a snapshot. This module teaches it
to keep taking them: a settings file says which folders matter,
where their snapshots live, how often they are saved, and how much
history to keep — and a run does exactly that, in the open.

Every run is careful in the same order. The store must be there and
must open; no other backup may be running (a lock file, checked
honestly — a leftover lock from a crashed run is cleared, and the
run says so); the store must accept a write; there must be room.
Only then does anything get copied. Every few runs the store is
spot-checked byte for byte before a run trusts it. Tidying old
backups away happens only after a run that finished cleanly, never
after a troubled one — a bad day is not when you throw history out.

Every run, however it ends — finished, partial, or refused before
it wrote anything — writes one line to the history file saying what
happened. The history is how status, estimates, and the spot-check
rhythm all know what they know.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .backup_model import (
    CADENCE_DAILY,
    CONFIG_FORMAT,
    CADENCE_WEEKLY,
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_REFUSED,
    RETENTION_COUNT,
    RETENTION_DAYS,
    STATE_NEVER_RUN,
    STATE_OVERDUE,
    STATE_READY,
    STATE_STORE_DAMAGED,
    STATE_TARGET_FULL,
    STATE_TARGET_MISSING,
    STATE_UNFINISHED,
    TRIGGER_MANUAL,
    BackupConfig,
    BackupStatus,
    History,
    HistoryEntry,
    RunResult,
    human_bytes,
)
from .errors import (
    BackupConfigError,
    BackupError,
    BackupRefusedError,
    LazarusError,
    StoreError,
)
from .model import SnapshotManifest
from .retention import apply_retention, plan_retention
from .snapshot import create_snapshot
from .store import (
    SnapshotStore,
    atomic_write_json,
    format_utc,
    new_snapshot_id,
    parse_utc,
    safe_instant,
    utc_now,
)
from .verify import verify_store

CONFIG_DIR_NAME = ".lazarus"
CONFIG_FILE_NAME = "backup.json"
HISTORY_FILE_NAME = "backup-history.jsonl"
RUN_LOCK_FILE = "backup-run.lock"

_AT_PATTERN = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

#: How long past its cadence a backup may drift before status calls
#: it overdue. This is the coarse, loud rule — a day and a half for
#: a daily backup, eight days for a weekly one. The precise
#: next-run moment is the schedule layer's job (see
#: :mod:`lazarus.schedule`); status only needs to know when to
#: shout.
_OVERDUE_AFTER = {
    CADENCE_DAILY: timedelta(hours=36),
    CADENCE_WEEKLY: timedelta(days=8),
}


# -- where the settings and history live -------------------------------


def default_config_path() -> Path:
    """The backup settings file Lazarus uses unless told otherwise."""
    return Path.home() / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def history_path_for(config_path: Path) -> Path:
    """The history file that sits beside a settings file."""
    return Path(config_path).with_name(HISTORY_FILE_NAME)


# -- the settings file ---------------------------------------------------


def load_config(path: Path) -> BackupConfig:
    """Read and check a backup settings file.

    A settings file Lazarus cannot use is worse than none, so this
    is strict in two passes: the record's shape first (a damaged
    record is a damaged record), then its sense — a real store, at
    least one thing to back up, a schedule and a tidying rule
    Lazarus actually knows, numbers that could work. Every refusal
    is plain language; settings are fixed by editing or setting the
    backup up again, never by guessing.
    """
    path = Path(path)
    if not path.exists():
        raise BackupConfigError(
            f"No backup is set up yet: there is no settings file at "
            f"'{path}'. Set a backup up first, then it can run."
        )
    try:
        raw = path.read_bytes()
    except OSError:
        raise BackupConfigError(
            f"The backup settings at '{path}' could not be read. "
            "Check the drive or folder they live on, then try again."
        ) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise BackupConfigError(
            f"The backup settings at '{path}' are damaged and cannot "
            "be used. Set the backup up again to replace them."
        ) from None
    if not isinstance(data, dict):
        raise BackupConfigError(
            f"The backup settings at '{path}' do not hold the record "
            "Lazarus expected. Set the backup up again to replace them."
        )
    fmt = data.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt != CONFIG_FORMAT:
        raise BackupConfigError(
            f"The backup settings at '{path}' were written in a "
            "format Lazarus does not understand. They may belong to "
            "a different version; set the backup up again to replace "
            "them."
        )
    try:
        config = BackupConfig.from_dict(data)
    except (TypeError, ValueError, KeyError):
        raise BackupConfigError(
            f"The backup settings at '{path}' are damaged and cannot "
            "be used. Set the backup up again to replace them."
        ) from None

    validate_config(config)
    return config


def validate_config(config: BackupConfig) -> None:
    """Check a backup settings record makes sense, in plain words.

    This is the second, semantic pass of :func:`load_config`,
    factored out so a settings record built by hand — the
    command line's setup flow builds one — can be checked with
    exactly the same strictness *before* anything is created or
    saved from it. Raises :class:`BackupConfigError` for the first
    problem found; returns None when the settings could work.

    Paths must be full paths, starting from the drive root: a
    backup may be started from anywhere, and a short, relative
    path would point at a different folder depending on where it
    happened to be started from.
    """
    if not config.store.strip():
        raise BackupConfigError(
            "The backup settings do not say where backups are kept. "
            "Set the backup up again and choose a store."
        )
    if not Path(config.store).is_absolute():
        raise BackupConfigError(
            f"The backup settings give the store as "
            f"'{config.store}', which is not a full path. Settings "
            "must use full paths, starting from the drive root, so "
            "the backup finds the same folders no matter where it "
            "is started from. Set the backup up again with a full "
            "path for the store."
        )
    if not config.sources or any(not s.strip() for s in config.sources):
        raise BackupConfigError(
            "The backup settings do not name anything to back up. "
            "Set the backup up again and choose at least one file "
            "or folder to keep safe."
        )
    for source in config.sources:
        if not Path(source).is_absolute():
            raise BackupConfigError(
                f"The backup settings give '{source}' as something "
                "to back up, which is not a full path. Settings "
                "must use full paths, starting from the drive root, "
                "so the backup finds the same folders no matter "
                "where it is started from. Set the backup up again "
                "with a full path for everything it backs up."
            )
    if config.schedule.every not in (CADENCE_DAILY, CADENCE_WEEKLY):
        raise BackupConfigError(
            f"The backup settings ask for backups "
            f"'{config.schedule.every}', which Lazarus does not "
            "know. They can run 'daily' or 'weekly' — fix the "
            "settings and try again."
        )
    if not _AT_PATTERN.fullmatch(config.schedule.at):
        raise BackupConfigError(
            f"The backup settings give the time of day as "
            f"'{config.schedule.at}', which Lazarus cannot read. "
            "Use a 24-hour time like 21:30."
        )
    if config.schedule.weekday not in _WEEKDAYS:
        raise BackupConfigError(
            f"The backup settings give the weekday as "
            f"'{config.schedule.weekday}', which Lazarus cannot "
            "read. Use a day name like monday."
        )
    if config.retention.mode not in (RETENTION_COUNT, RETENTION_DAYS):
        raise BackupConfigError(
            f"The backup settings say to tidy old backups by "
            f"'{config.retention.mode}', which Lazarus does not "
            "know. They can be tidied by 'count' or by 'days' — "
            "fix the settings and try again."
        )
    if config.retention.count < 1:
        raise BackupConfigError(
            "The backup settings say to keep fewer than one backup, "
            "which would leave nothing to restore. Keep at least one."
        )
    if config.retention.count > 10000:
        raise BackupConfigError(
            "The backup settings say to keep more than 10,000 "
            "backups, which is more than Lazarus will keep track "
            "of. Keep 10,000 or fewer."
        )
    if config.retention.days < 1:
        raise BackupConfigError(
            "The backup settings say to keep backups for fewer than "
            "one day, which would leave nothing to restore. Keep at "
            "least one day."
        )
    if config.retention.days > 36500:
        raise BackupConfigError(
            "The backup settings say to keep backups for more than "
            "36,500 days, which is more than Lazarus will keep "
            "track of. Keep 36,500 days or fewer."
        )
    if not 1 <= config.verify_every_runs <= 365:
        raise BackupConfigError(
            "The backup settings give an unusable spot-check "
            "interval. Lazarus can check the store every 1 to 365 "
            "runs — fix the settings and try again."
        )


def save_config(config: BackupConfig, path: Path) -> None:
    """Write backup settings atomically, making the folder if needed."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise BackupConfigError(
            f"Lazarus could not create the folder for the backup "
            f"settings at '{path}'. Check the drive is connected "
            "and can be written to."
        ) from None
    try:
        atomic_write_json(path, config.to_dict())
    except OSError:
        raise BackupConfigError(
            f"Lazarus could not save the backup settings at "
            f"'{path}'. Check the drive is connected and not full."
        ) from None


# -- the history file ----------------------------------------------------


def load_history(path: Path) -> History:
    """Read the backup history: one JSON record per line.

    A line that cannot be read is skipped and *counted*, never
    silently dropped and never allowed to take the whole history
    down with it — the damaged count rides along in the result,
    where status can say it out loud.
    """
    path = Path(path)
    if not path.exists():
        return History(entries=[], damaged_lines=0)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise BackupError(
            f"The backup history at '{path}' could not be read. "
            "Check the drive or folder it lives on, then try again."
        ) from None
    entries: list[HistoryEntry] = []
    damaged = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise TypeError("a history line is not a record")
            entries.append(HistoryEntry.from_dict(data))
        except (ValueError, TypeError, KeyError):
            damaged += 1
    return History(entries=entries, damaged_lines=damaged)


def append_history(path: Path, entry: HistoryEntry) -> None:
    """Append one run to the history file, and flush it to disk."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise BackupError(
            f"Lazarus could not write to the backup history at "
            f"'{path}'. Check the drive is connected and not full."
        ) from None


def backup_snapshot_ids(history: History) -> set[str]:
    """Every snapshot id any recorded run ever made."""
    ids: set[str] = set()
    for entry in history.entries:
        ids.update(entry.snapshots)
    return ids


# -- the run lock ---------------------------------------------------------
#
# One backup at a time: two runs writing one store would tangle
# their copies and their tidying. The lock is a small file in the
# store naming the process that holds it. A lock whose process is
# gone is a leftover from a run that stopped suddenly — it is
# cleared, and the run that clears it says so in its notes.


def _pid_alive_windows(pid: int) -> bool:
    """True if ``pid`` is a living process, asked of Windows itself.

    ``os.kill(pid, 0)`` is NOT a liveness probe on Windows: Python
    hands any signal other than the console control events to
    TerminateProcess, so the "probe" would kill the very backup it
    was checking for — and, if the pid had been reused, an unrelated
    program. Ask the kernel instead: open the process for querying
    and check whether it has exited.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong,
        ctypes.c_bool,
        ctypes.c_ulong,
    ]
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        # 5 = access denied: the process is there, just not ours to
        # open. 87 = invalid parameter: there is no such process.
        return ctypes.get_last_error() == 5
    try:
        exit_code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # cannot be read; assume it is still there
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _default_pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` seems to be running."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it is there; it is simply not ours to signal
    except OverflowError:
        return False  # too large to be a process id this system holds
    except OSError:
        return False
    return True


def _acquire_run_lock(
    store_root: Path,
    run_id: str,
    pid_alive: Optional[Callable[[int], bool]] = None,
) -> tuple[Path, Optional[str]]:
    """Take the store's run lock; returns ``(lock_path, stale_note)``.

    ``stale_note`` is the plain sentence a run should carry when it
    had to clear an earlier run's leftover lock, and None when the
    way was simply clear. Raises :class:`BackupRefusedError` when a
    living run already holds the lock.
    """
    alive = pid_alive or _default_pid_alive
    lock_path = Path(store_root) / RUN_LOCK_FILE
    note: Optional[str] = None
    if lock_path.exists():
        stale = False
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stale = True  # unreadable (bad JSON or bad encoding)
            data = None
        if not stale:
            if not isinstance(data, dict):
                stale = True
            else:
                pid = data.get("pid")
                if (
                    not isinstance(pid, int)
                    or isinstance(pid, bool)
                    or pid <= 0
                    or pid > 2**31
                ):
                    # Not a plausible process id at all: the lock
                    # cannot belong to a living run, so it is a
                    # leftover to clear — and it is never handed to
                    # the liveness check, which expects a real pid.
                    stale = True
                elif alive(pid):
                    started = data.get("started_utc")
                    when = (
                        f" (it started at {started})"
                        if isinstance(started, str) and started
                        else ""
                    )
                    raise BackupRefusedError(
                        f"A backup is already running{when}. Wait "
                        "for it to finish, then try again. If you "
                        f"are sure no backup is running, delete "
                        f"'{lock_path}' and try again."
                    )
                else:
                    stale = True
        if stale:
            try:
                lock_path.unlink()
            except OSError:
                pass
            note = (
                "An earlier backup did not finish cleanly, so its "
                "leftover lock was cleared."
            )
    payload = {
        "pid": os.getpid(),
        "started_utc": format_utc(utc_now()),
        "run_id": run_id,
    }
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise BackupRefusedError(
            "A backup is already running. Wait for it to finish, "
            "then try again."
        ) from None
    except OSError:
        raise BackupRefusedError(
            "Lazarus could not take the backup lock in the store, "
            "so this backup did not run. Check the drive is "
            "connected and can be written to."
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            lock_path.unlink()
        except OSError:
            pass
        raise BackupRefusedError(
            "Lazarus could not take the backup lock in the store, "
            "so this backup did not run. Check the drive is "
            "connected and can be written to."
        ) from None
    return lock_path, note


def _release_run_lock(path: Path) -> None:
    """Let the run lock go. A lock already gone is not a problem."""
    try:
        Path(path).unlink()
    except OSError:
        pass


# -- estimates ------------------------------------------------------------


def _source_total_bytes(sources: list[str]) -> int:
    """Total size of the ordinary files under each source, best effort.

    Anything that cannot be looked at counts as nothing rather than
    stopping the estimate: this number guides a space check, it is
    not a promise, and it is always called an estimate.
    """
    total = 0
    for source in sources:
        root = Path(source)
        try:
            mode = os.lstat(root).st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode):
            try:
                total += os.lstat(root).st_size
            except OSError:
                pass
            continue
        if not stat.S_ISDIR(mode):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                candidate = Path(dirpath) / name
                try:
                    info = os.lstat(candidate)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


def estimate_backup_bytes(
    config: BackupConfig, history: History
) -> tuple[int, str]:
    """Roughly how much new data the next run will write, and why.

    After a first run, the honest guide is how much actually
    changed last time: most of a folder does not change between
    backups, so copying is usually a fraction of the whole. Before
    any run has saved anything, everything is new by definition.
    The second return value is the basis, in plain words, so the
    number is never presented as more certain than it is.
    """
    current_total = _source_total_bytes(config.sources)
    # The same future-dating rule every other derivation here
    # follows: an entry claiming to be from well in the future
    # has not happened yet, so what it copied cannot be the
    # guide for the next run's size.
    moment = utc_now()
    productive = [
        entry
        for entry in history.entries
        if entry.outcome in (OUTCOME_COMPLETE, OUTCOME_PARTIAL)
        and not _entry_is_future_dated(entry, moment)
    ]
    if productive:
        estimate = productive[-1].bytes_copied
        basis = "based on how much changed last time"
        if estimate == 0 and config.sources:
            # Nothing new was copied last time, so "last time" is a
            # shaky guide on its own: say so, in the basis itself.
            basis = (
                "nothing new was copied last time; if files have "
                "changed since, this backup may need more room "
                "than the estimate says"
            )
    else:
        estimate = current_total
        basis = "first backup — everything is new"
    if current_total == 0:
        estimate = 0
    return (max(0, estimate), basis)


def _free_space_for(
    store_path: Path,
    free_space_fn: Optional[Callable[[Path], int]] = None,
) -> int:
    """Free bytes for a store path, via its nearest folder that exists."""
    fn = free_space_fn or (lambda p: shutil.disk_usage(p).free)
    candidate = Path(store_path)
    while not candidate.exists():
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return int(fn(candidate))


#: How far ahead of the caller's clock a history entry may be
#: dated before it stops counting as a record of the past. A few
#: minutes is ordinary clock skew between writers; beyond that,
#: an entry claiming to be from the future cannot settle questions
#: about what has already happened, and the honest derivations
#: below ignore it.
_FUTURE_TOLERANCE = timedelta(minutes=5)


def _entry_start(entry: HistoryEntry) -> Optional[datetime]:
    """When a history entry's run started, or None if unreadable.

    Shared by every derivation that reasons about run times, so
    they all read a start time the same way. An entry whose start
    cannot be read is not hidden by this helper: callers treat
    None as "cannot be placed in time", which everywhere here
    keeps the entry's existing sorts-earliest behaviour.
    """
    started = getattr(entry, "started_utc", None)
    if not isinstance(started, str) or not started:
        return None
    try:
        return parse_utc(started)
    except StoreError:
        return None


def _entry_is_future_dated(entry: HistoryEntry, now: datetime) -> bool:
    """True if an entry claims to start well after ``now``.

    An entry whose start cannot be read is never future-dated:
    it keeps its existing behaviour instead of being dropped.
    """
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    start = _entry_start(entry)
    return start is not None and start > moment + _FUTURE_TOLERANCE


def _runs_since_verify(
    history: History, now: Optional[datetime] = None
) -> int:
    """Runs since the last spot-check that passed.

    Counts every recorded run after the most recent entry whose
    ``verified`` is True — a spot-check that passed — or every
    recorded run, if no spot-check has ever passed. A failed check
    does not clear suspicion and never resets the cadence: the
    store stays suspect until a check passes. A refused run
    counts: it was still a run that trusted the store enough to
    start. Entries dated well into the future are ignored
    entirely when ``now`` is given: they have not happened yet,
    so they can neither count as runs nor settle the cadence.
    """
    count = 0
    for entry in reversed(history.entries):
        if now is not None and _entry_is_future_dated(entry, now):
            continue
        if entry.verified is True:
            break
        count += 1
    return count


def _object_sizes(store: SnapshotStore) -> dict[str, int]:
    """The object area as ``{path: size}`` — the copied-bytes yardstick."""
    sizes: dict[str, int] = {}
    objects = store.objects_dir
    if not objects.is_dir():
        return sizes
    for fanout in objects.iterdir():
        if not fanout.is_dir() or len(fanout.name) != 2:
            continue
        for entry in fanout.iterdir():
            try:
                info = os.lstat(entry)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                sizes[str(entry)] = info.st_size
    return sizes


def _only_unfinished_problems(check, store: SnapshotStore) -> bool:
    """True if a failed spot-check found only unfinished snapshots.

    A store whose only problem is a leftover unfinished save is
    not damaged in the way a torn copy is, and it gets its own
    state and its own remedy. The unfinished set is recomputed
    from the store itself — the snapshot folders with no readable
    manifest — and a problem counts as an unfinished one only
    when it names one of those snapshots and carries the
    spot-check's never-finished-saving message. Any other
    problem, alongside or alone, makes this False.
    """
    if not check.problems:
        return False
    unfinished = {
        info.id for info in store.list_snapshots() if not info.has_manifest
    }
    if not unfinished:
        return False
    return all(
        problem.path in unfinished
        and "never finished saving" in problem.message
        for problem in check.problems
    )


# -- running a backup -----------------------------------------------------


def run_backup(
    config: BackupConfig,
    config_path: Path,
    *,
    now: Optional[datetime] = None,
    trigger: str = TRIGGER_MANUAL,
    free_space_fn: Optional[Callable[[Path], int]] = None,
    pid_alive: Optional[Callable[[int], bool]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> RunResult:
    """Run one backup, end to end, and write its history entry.

    The safety order is fixed and every early exit honors it: a
    refused run writes its history entry and lets go of any lock it
    holds before it returns, exactly like a finished one. Nothing
    here raises for the ordinary ways a backup can go wrong — the
    drive is out, the store is damaged, another run owns the lock —
    those come back as a refused :class:`RunResult` saying why.
    """
    config_path = Path(config_path)
    history_file = history_path_for(config_path)
    moment = now or utc_now()
    tick = clock or time.monotonic
    started_perf = tick()
    run_id = new_snapshot_id(moment)
    notes: list[str] = []

    def finish(
        outcome: str,
        state: str,
        message: str,
        *,
        snapshots: Optional[list[str]] = None,
        files_saved: int = 0,
        bytes_saved: int = 0,
        bytes_copied: int = 0,
        pruned: Optional[list[str]] = None,
        verified: Optional[bool] = None,
        verify_problems: int = 0,
        reason: str = "",
        extra_notes: Optional[list[str]] = None,
    ) -> RunResult:
        entry = HistoryEntry(
            run_id=run_id,
            started_utc=format_utc(moment),
            finished_utc=format_utc(utc_now()),
            trigger=trigger,
            outcome=outcome,
            snapshots=list(snapshots or []),
            sources=list(config.sources),
            files_saved=files_saved,
            bytes_saved=bytes_saved,
            bytes_copied=bytes_copied,
            seconds=max(0.0, tick() - started_perf),
            pruned=list(pruned or []),
            verified=verified,
            verify_problems=verify_problems,
            reason=reason,
            notes=list(notes) + list(extra_notes or []),
        )
        try:
            append_history(history_file, entry)
        except BackupError:
            # The run's own truth stands: a backup that finished
            # is finished even when its history line cannot be
            # written, and a refusal is still a refusal. Raising
            # here would report the run as a crash over the top
            # of its real outcome — so the failure is carried in
            # the result instead, said out loud in both the entry
            # and the message, never swallowed silently.
            entry.notes.append(
                "This run's place in the backup history could "
                f"not be written to '{history_file}'. The run "
                "itself stands as reported; the next status check "
                "may not know it happened."
            )
            message = (
                f"{message} One more thing: this run could not "
                "be written into the backup history — the run "
                "itself stands as reported."
            )
        return RunResult(
            config_path=str(config_path),
            outcome=outcome,
            history_entry=entry,
            state=state,
            message=message,
        )

    store_path = Path(config.store)

    # The drive itself: no folder, no backup. This is the most
    # common refusal of all, and it gets the plainest words.
    if not store_path.is_dir():
        reason = (
            f"The backup drive is not connected: there is no folder "
            f"at '{config.store}'. Plug the drive in and try again."
        )
        return finish(OUTCOME_REFUSED, STATE_TARGET_MISSING, reason, reason=reason)

    try:
        store = SnapshotStore.open(store_path)
    except StoreError as error:
        reason = str(error)
        return finish(OUTCOME_REFUSED, STATE_STORE_DAMAGED, reason, reason=reason)

    try:
        lock_path, stale_note = _acquire_run_lock(store.root, run_id, pid_alive)
    except BackupRefusedError as error:
        reason = str(error)
        return finish(OUTCOME_REFUSED, STATE_READY, reason, reason=reason)
    if stale_note:
        notes.append(stale_note)

    try:
        # Can the store be written to at all? Prove it with a probe
        # file rather than finding out halfway through a snapshot.
        probe = store.root / f".lazarus-write-probe-{secrets.token_hex(6)}"
        try:
            with open(probe, "wb") as handle:
                handle.write(b"lazarus")
            probe.unlink()
        except OSError:
            try:
                probe.unlink()
            except OSError:
                pass
            reason = (
                f"Lazarus could not write to the backup store at "
                f"'{store.root}'. Check the drive is connected, not "
                "full, and not set to read-only, then try again."
            )
            return finish(
                OUTCOME_REFUSED, STATE_STORE_DAMAGED, reason, reason=reason
            )

        history = load_history(history_file)

        # Room first: running out of space mid-snapshot helps no one.
        estimate, basis = estimate_backup_bytes(config, history)
        free = _free_space_for(store.root, free_space_fn)
        if free < estimate:
            reason = (
                f"This backup needs about {human_bytes(estimate)} "
                f"(an estimate); only {human_bytes(free)} is free on "
                "the backup drive. Free up space or remove old "
                "backups, then try again."
            )
            return finish(
                OUTCOME_REFUSED,
                STATE_TARGET_FULL,
                reason,
                reason=reason,
                extra_notes=[f"How the estimate was worked out: {basis}."],
            )

        # The spot-check: every so many runs, prove the store before
        # trusting it with one more snapshot. The run now starting
        # counts toward the cadence.
        verified: Optional[bool] = None
        verify_problems = 0
        if _runs_since_verify(history, moment) + 1 >= config.verify_every_runs:
            check = verify_store(store)
            verify_problems = len(check.problems)
            if not check.ok:
                if _only_unfinished_problems(check, store):
                    reason = (
                        f"A spot-check of the backup store found "
                        f"{verify_problems} problem(s), so this "
                        "backup was stopped before anything was "
                        "written. An earlier backup never finished "
                        "saving, and what it left behind is in the "
                        "way. The leftover is cleared by 'lazarus "
                        "backup prune' once it is more than a day "
                        "old; if it is newer, a backup may still be "
                        "writing — wait and try again."
                    )
                    return finish(
                        OUTCOME_REFUSED,
                        STATE_UNFINISHED,
                        reason,
                        reason=reason,
                        verified=False,
                        verify_problems=verify_problems,
                    )
                reason = (
                    f"A spot-check of the backup store found "
                    f"{verify_problems} problem(s), so this backup "
                    "was stopped before anything was written. Run "
                    f"'lazarus verify {config.store}' to see what "
                    "is damaged, and check the backup drive."
                )
                return finish(
                    OUTCOME_REFUSED,
                    STATE_STORE_DAMAGED,
                    reason,
                    reason=reason,
                    verified=False,
                    verify_problems=verify_problems,
                )
            verified = True

        # The snapshots themselves, one source at a time. A source
        # that fails is named and the run goes on with the rest —
        # saving most of what matters beats saving none of it.
        before_objects = _object_sizes(store)
        created: list[SnapshotManifest] = []
        incomplete = False
        for source in config.sources:
            source_path = Path(source)
            if not source_path.exists() and not source_path.is_symlink():
                notes.append(
                    f"'{source}' is not there, so it was not backed "
                    "up. Check the name, and that the drive it lives "
                    "on is plugged in."
                )
                incomplete = True
                continue
            try:
                manifest = create_snapshot(
                    store,
                    source_path,
                    label=f"Backup — {source_path.name or source}",
                )
            except LazarusError as error:
                notes.append(f"'{source}' could not be backed up: {error}")
                incomplete = True
                continue
            created.append(manifest)
            if not manifest.complete:
                incomplete = True
                notes.append(
                    f"The snapshot of '{source}' is incomplete: some "
                    f"items could not be saved. Its record "
                    f"({manifest.id}) names what was missed."
                )
        after_objects = _object_sizes(store)
        bytes_copied = sum(
            size
            for path_key, size in after_objects.items()
            if path_key not in before_objects
        )
        files_saved = sum(m.file_count for m in created)
        bytes_saved = sum(m.total_bytes for m in created)
        snapshot_ids = [m.id for m in created]

        if not created:
            outcome = OUTCOME_FAILED
            reason = (
                "Nothing could be backed up. The notes say what went "
                "wrong for each folder — fix the problem and try again."
            )
        elif incomplete:
            outcome = OUTCOME_PARTIAL
            reason = (
                "Some things could not be backed up. The notes say "
                "what was missed."
            )
        else:
            outcome = OUTCOME_COMPLETE
            reason = ""

        # Tidying is a reward for a clean run, never a consolation
        # after a troubled one.
        pruned: list[str] = []
        if outcome == OUTCOME_COMPLETE:
            try:
                known_ids = backup_snapshot_ids(history) | set(snapshot_ids)
                plan = plan_retention(store, known_ids, config.retention, moment)
                if plan.held:
                    notes.append(
                        f"{len(plan.held)} backup(s) were left alone "
                        "because their dates could not be read."
                    )
                tidy = apply_retention(store, plan)
                pruned = list(tidy.removed_ids)
                if tidy.gc_note:
                    notes.append(tidy.gc_note)
                elif tidy.objects_removed:
                    noun = "copy" if tidy.objects_removed == 1 else "copies"
                    notes.append(
                        f"Tidying up removed {tidy.objects_removed} "
                        f"saved {noun} that no snapshot needs any "
                        f"more, reclaiming {human_bytes(tidy.bytes_freed)}."
                    )
            except LazarusError as error:
                outcome = OUTCOME_PARTIAL
                reason = (
                    f"The backup itself is safe, but tidying up old "
                    f"backups failed: {error} Old backups were left "
                    "as they are."
                )
        else:
            try:
                known_ids = backup_snapshot_ids(history) | set(snapshot_ids)
                plan = plan_retention(store, known_ids, config.retention, moment)
                if plan.held:
                    notes.append(
                        f"{len(plan.held)} backup(s) were left alone "
                        "because their dates could not be read."
                    )
                if plan.remove:
                    notes.append(
                        "Old backups were left as they are: tidying "
                        "up only happens after a backup that finishes "
                        "cleanly."
                    )
            except LazarusError:
                pass  # planning a note must never change the outcome

        # The headline in the report already says a clean run
        # succeeded; the message line carries the reason otherwise.
        message = "" if outcome == OUTCOME_COMPLETE else reason
        return finish(
            outcome,
            STATE_READY,
            message,
            snapshots=snapshot_ids,
            files_saved=files_saved,
            bytes_saved=bytes_saved,
            bytes_copied=bytes_copied,
            pruned=pruned,
            verified=verified,
            verify_problems=verify_problems,
            reason=reason,
        )
    finally:
        _release_run_lock(lock_path)


# -- status -----------------------------------------------------------------


def backup_status(
    config: BackupConfig,
    config_path: Path,
    *,
    now: Optional[datetime] = None,
    free_space_fn: Optional[Callable[[Path], int]] = None,
) -> BackupStatus:
    """Say, loudly, how the backup stands right now.

    The checks run in the order a person would worry about them:
    is the drive there at all, is the store itself sound (including
    a spot-check when one is due), is there room for the next run,
    has anything ever been saved — and only then, is the last good
    backup older than the cadence allows. ``next_due_local`` is
    left None here: the exact next-run moment belongs to the
    schedule layer, which owns the local-clock math.
    """
    config_path = Path(config_path)
    history = load_history(history_path_for(config_path))
    moment = now or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # Entries dated well into the future are ignored here, as
    # everywhere: a run that has not happened yet says nothing
    # about when the last real run was, or whether one is overdue.
    visible = [
        entry
        for entry in history.entries
        if not _entry_is_future_dated(entry, moment)
    ]
    productive = [
        entry
        for entry in visible
        if entry.outcome in (OUTCOME_COMPLETE, OUTCOME_PARTIAL)
    ]
    last_entry = visible[-1] if visible else None
    last_run_utc = last_entry.started_utc if last_entry else None
    last_outcome = last_entry.outcome if last_entry else None

    def status(
        state: str,
        headline: str,
        message: str,
        next_action: str,
        *,
        snapshots_in_store: int = 0,
        free_bytes: Optional[int] = None,
        estimate_bytes: Optional[int] = None,
    ) -> BackupStatus:
        return BackupStatus(
            state=state,
            headline=headline,
            message=message,
            next_action=next_action,
            store=config.store,
            sources=len(config.sources),
            snapshots_in_store=snapshots_in_store,
            last_run_utc=last_run_utc,
            last_outcome=last_outcome,
            next_due_local=None,
            free_bytes=free_bytes,
            estimate_bytes=estimate_bytes,
            history_damaged_lines=history.damaged_lines,
        )

    store_path = Path(config.store)
    if not store_path.is_dir():
        return status(
            STATE_TARGET_MISSING,
            "The backup drive is not connected.",
            f"There is no folder at '{config.store}', so Lazarus "
            "cannot reach the backups at all.",
            "Plug the backup drive in, then check again.",
        )
    try:
        store = SnapshotStore.open(store_path)
    except StoreError as error:
        return status(
            STATE_STORE_DAMAGED,
            "The backup store is damaged.",
            str(error),
            "See what is damaged: in the window, open the store "
            "on the Restore page and choose 'Verify this store'; "
            f"on the command line, run 'lazarus verify "
            f"{config.store}'. Also check the backup drive.",
        )
    snapshots_in_store = len(
        [info for info in store.list_snapshots() if info.has_manifest]
    )
    estimate, basis = estimate_backup_bytes(config, history)
    free = _free_space_for(store.root, free_space_fn)

    # Status uses the same due rule as a run: if the next run would
    # spot-check, status spot-checks now. A failed check leaves the
    # check due on purpose, until one passes.
    if _runs_since_verify(history, moment) + 1 >= config.verify_every_runs:
        check = verify_store(store)
        if not check.ok:
            if _only_unfinished_problems(check, store):
                return status(
                    STATE_UNFINISHED,
                    "AN EARLIER BACKUP NEVER FINISHED SAVING",
                    f"A spot-check of the store found "
                    f"{len(check.problems)} problem(s), and every "
                    "one of them is a backup that never finished "
                    "saving. What it left behind is in the way, "
                    "but the finished backups are not damaged.",
                    "The leftover is cleared by tidying (the "
                    "Backup page's 'Tidy them away', or 'lazarus "
                    "backup prune' on the command line) once it "
                    "is more than a day old; if it is newer, a "
                    "backup may still be writing — wait and try "
                    "again.",
                    snapshots_in_store=snapshots_in_store,
                    free_bytes=free,
                    estimate_bytes=estimate,
                )
            return status(
                STATE_STORE_DAMAGED,
                "The backup store is damaged.",
                f"A spot-check of the store found "
                f"{len(check.problems)} problem(s). Backups should "
                "not add to a store that cannot prove what it holds.",
                "See what is damaged: in the window, open the "
                "store on the Restore page and choose 'Verify "
                "this store'; on the command line, run 'lazarus "
                f"verify {config.store}'. Also check the backup "
                "drive.",
                snapshots_in_store=snapshots_in_store,
                free_bytes=free,
                estimate_bytes=estimate,
            )
    if free < estimate:
        return status(
            STATE_TARGET_FULL,
            "The backup drive is full.",
            f"The next backup needs about {human_bytes(estimate)} "
            f"(an estimate, {basis}); only {human_bytes(free)} is "
            "free on the backup drive.",
            "Free up space on the backup drive or remove old "
            "backups, then run a backup.",
            snapshots_in_store=snapshots_in_store,
            free_bytes=free,
            estimate_bytes=estimate,
        )
    if not productive:
        return status(
            STATE_NEVER_RUN,
            "No backup has run yet.",
            "This backup is set up, but nothing has been saved "
            "into the store yet. Until a first run finishes, these "
            "files have no safety net.",
            "Run a backup now to keep these files safe.",
            snapshots_in_store=snapshots_in_store,
            free_bytes=free,
            estimate_bytes=estimate,
        )
    last_good = productive[-1]
    window = _OVERDUE_AFTER.get(config.schedule.every, timedelta(hours=36))
    if moment - safe_instant(last_good.started_utc) > window:
        return status(
            STATE_OVERDUE,
            "A backup is overdue.",
            f"The last backup that saved anything ran at "
            f"{last_good.started_utc}. On a {config.schedule.every} "
            "schedule, that is longer ago than it should be.",
            "Run a backup now.",
            snapshots_in_store=snapshots_in_store,
            free_bytes=free,
            estimate_bytes=estimate,
        )
    return status(
        STATE_READY,
        "Backups are running as they should.",
        f"The last backup that saved anything ran at "
        f"{last_good.started_utc} and finished as "
        f"'{last_good.outcome}'.",
        "",
        snapshots_in_store=snapshots_in_store,
        free_bytes=free,
        estimate_bytes=estimate,
    )
