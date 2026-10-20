"""What a Phase 4 backup works with, in one shared shape.

A backup is a promise kept on a schedule: these folders, saved into
that store, this often, keeping this much history. The records here
carry that promise (:class:`BackupConfig`), what each run actually
did (:class:`HistoryEntry`), and what a run or a status check has
to say about it (:class:`RunResult`, :class:`BackupStatus`) — all
in plain language, because the person reading them at 2 a.m. with a
dead drive is not in the mood for codes.

The model stays dumb but strict: it checks that every field is the
*type* it claims to be, and leaves questions of sense (is that a
real time of day? is that a sensible number of backups to keep?)
to the config loader in :mod:`lazarus.backup`. A record with a
wrong-typed field is a damaged record, and it fails here, at load,
rather than exploding later in some other module's hands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

CONFIG_FORMAT = 1

CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"

RETENTION_COUNT = "count"
RETENTION_DAYS = "days"

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_CATCHUP = "catch-up"

OUTCOME_COMPLETE = "complete"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"

STATE_READY = "ready"
STATE_TARGET_MISSING = "target-missing"
STATE_TARGET_FULL = "target-full"
STATE_STORE_DAMAGED = "store-damaged"
STATE_NEVER_RUN = "never-run"
STATE_OVERDUE = "overdue"
STATE_UNFINISHED = "unfinished-backup"


def human_bytes(size: int) -> str:
    """A byte count the way a person would say it: '12.3 MB'."""
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def _human_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.1f} minutes"
    return f"{seconds / 3600:.1f} hours"


# -- strict field checks ------------------------------------------------
#
# Same bargain as the snapshot model: data on disk can be damaged or
# tampered with, so a wrong-typed field is rejected at load with a
# plain complaint instead of being trusted into a crash later.


def _req_str(data: dict, key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise TypeError(f"a backup record has no usable {what}")
    return value


def _opt_str(data: dict, key: str, default: str, what: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"a backup record has an unusable {what}")
    return value


def _req_int(data: dict, key: str, what: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"a backup record has no usable {what}")
    return value


def _opt_int(data: dict, key: str, default: int, what: str) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"a backup record has an unusable {what}")
    return value


def _req_str_list(data: dict, key: str, what: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise TypeError(f"a backup record has an unusable {what}")
    return list(value)


def _opt_str_list(data: dict, key: str, what: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise TypeError(f"a backup record has an unusable {what}")
    return list(value)


def _count_field(data: dict, key: str, what: str) -> int:
    value = _opt_int(data, key, 0, what)
    if value < 0:
        raise TypeError(f"a backup record has an unusable {what}")
    return value


@dataclass
class ScheduleSpec:
    """When backups run: ``every`` cadence, at local time ``at``.

    ``at`` is "HH:MM" on a 24-hour clock; ``weekday`` (lowercase,
    monday..sunday) only matters for a weekly cadence. Whether the
    words name a real cadence, a real time, and a real weekday is
    the config loader's job to check — here they are only strings.
    """

    every: str  # CADENCE_DAILY | CADENCE_WEEKLY
    at: str  # "HH:MM", 24-hour
    weekday: str = "monday"

    def to_dict(self) -> dict:
        return {"every": self.every, "at": self.at, "weekday": self.weekday}

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleSpec":
        if not isinstance(data, dict):
            raise TypeError("a backup schedule is not a record")
        return cls(
            every=_req_str(data, "every", "schedule"),
            at=_req_str(data, "at", "time of day"),
            weekday=_opt_str(data, "weekday", "monday", "weekday"),
        )


@dataclass
class RetentionSpec:
    """How much backup history to keep.

    ``mode`` is "count" (keep the newest ``count`` backups) or
    "days" (keep backups from the last ``days`` days). Both numbers
    ride along either way, so switching modes never loses the other
    setting.
    """

    mode: str  # RETENTION_COUNT | RETENTION_DAYS
    count: int = 10
    days: int = 30

    def to_dict(self) -> dict:
        return {"mode": self.mode, "count": self.count, "days": self.days}

    @classmethod
    def from_dict(cls, data: dict) -> "RetentionSpec":
        if not isinstance(data, dict):
            raise TypeError("a backup retention setting is not a record")
        return cls(
            mode=_req_str(data, "mode", "retention mode"),
            count=_opt_int(data, "count", 10, "number of backups to keep"),
            days=_opt_int(data, "days", 30, "number of days to keep"),
        )


@dataclass
class BackupConfig:
    """The whole backup promise, as the settings file holds it."""

    store: str  # folder the snapshots live in (usually on another drive)
    sources: list[str]  # files or folders to keep safe
    schedule: ScheduleSpec
    retention: RetentionSpec
    verify_every_runs: int = 7  # spot-check the store every N runs
    created_utc: str = ""
    tool_version: str = ""

    def to_dict(self) -> dict:
        return {
            "format": CONFIG_FORMAT,
            "product": "LAZARUS.EXE",
            "store": self.store,
            "sources": list(self.sources),
            "schedule": self.schedule.to_dict(),
            "retention": self.retention.to_dict(),
            "verify_every_runs": self.verify_every_runs,
            "created_utc": self.created_utc,
            "tool_version": self.tool_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BackupConfig":
        if not isinstance(data, dict):
            raise TypeError("the backup settings are not a record")
        fmt = data.get("format")
        if fmt is not None:
            if not isinstance(fmt, int) or isinstance(fmt, bool):
                raise TypeError("the backup settings have an unusable format")
            if fmt != CONFIG_FORMAT:
                raise ValueError(f"unknown backup settings format: {fmt!r}")
        schedule_raw = data.get("schedule")
        if not isinstance(schedule_raw, dict):
            raise TypeError("the backup settings have no usable schedule")
        retention_raw = data.get("retention")
        if not isinstance(retention_raw, dict):
            raise TypeError("the backup settings have no usable retention setting")
        return cls(
            store=_req_str(data, "store", "backup store"),
            sources=_req_str_list(data, "sources", "list of what to back up"),
            schedule=ScheduleSpec.from_dict(schedule_raw),
            retention=RetentionSpec.from_dict(retention_raw),
            verify_every_runs=_opt_int(
                data, "verify_every_runs", 7, "spot-check interval"
            ),
            created_utc=_opt_str(data, "created_utc", "", "creation date"),
            tool_version=_opt_str(data, "tool_version", "", "tool version"),
        )


@dataclass
class HistoryEntry:
    """What one backup run did, kept in the history file.

    ``verified`` is the honest heart of the spot-check: True means
    the store was checked before this run and passed, False means
    the check found problems (``verify_problems`` says how many),
    and None means no spot-check ran this time — which is not the
    same as passing one, and is never written as if it were.
    """

    run_id: str
    started_utc: str = ""
    finished_utc: str = ""
    trigger: str = TRIGGER_MANUAL  # TRIGGER_* — who asked for this run
    outcome: str = OUTCOME_FAILED  # OUTCOME_* — how it honestly ended
    snapshots: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    files_saved: int = 0
    bytes_saved: int = 0
    bytes_copied: int = 0  # new data actually written this run
    seconds: float = 0.0
    pruned: list[str] = field(default_factory=list)  # snapshots tidied away
    verified: Optional[bool] = None  # None = no spot-check ran this run
    verify_problems: int = 0
    reason: str = ""  # plain language; "" when the run was complete
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "trigger": self.trigger,
            "outcome": self.outcome,
            "snapshots": list(self.snapshots),
            "sources": list(self.sources),
            "files_saved": self.files_saved,
            "bytes_saved": self.bytes_saved,
            "bytes_copied": self.bytes_copied,
            "seconds": self.seconds,
            "pruned": list(self.pruned),
            "verified": self.verified,
            "verify_problems": self.verify_problems,
            "reason": self.reason,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        if not isinstance(data, dict):
            raise TypeError("a backup history entry is not a record")
        seconds_raw = data.get("seconds", 0.0)
        if (
            not isinstance(seconds_raw, (int, float))
            or isinstance(seconds_raw, bool)
            or seconds_raw < 0
        ):
            raise TypeError("a backup history entry has an unusable duration")
        verified = data.get("verified", None)
        if verified is not None and not isinstance(verified, bool):
            raise TypeError("a backup history entry has an unusable check result")
        return cls(
            run_id=_req_str(data, "run_id", "run id"),
            started_utc=_opt_str(data, "started_utc", "", "start time"),
            finished_utc=_opt_str(data, "finished_utc", "", "finish time"),
            trigger=_opt_str(data, "trigger", TRIGGER_MANUAL, "trigger"),
            outcome=_opt_str(data, "outcome", OUTCOME_FAILED, "outcome"),
            snapshots=_opt_str_list(data, "snapshots", "snapshot list"),
            sources=_opt_str_list(data, "sources", "source list"),
            files_saved=_count_field(data, "files_saved", "file count"),
            bytes_saved=_count_field(data, "bytes_saved", "saved size"),
            bytes_copied=_count_field(data, "bytes_copied", "copied size"),
            seconds=float(seconds_raw),
            pruned=_opt_str_list(data, "pruned", "pruned list"),
            verified=verified,
            verify_problems=_count_field(
                data, "verify_problems", "problem count"
            ),
            reason=_opt_str(data, "reason", "", "reason"),
            notes=_opt_str_list(data, "notes", "notes"),
        )


@dataclass
class RunResult:
    """One finished backup run, ready to be told about.

    ``state`` is the store's state as the run leaves it (one of the
    STATE_* words), ``message`` is the one-line summary, and the
    full story — numbers, snapshots, notes — lives in the history
    entry the run wrote.
    """

    config_path: str
    outcome: str  # OUTCOME_*
    history_entry: HistoryEntry
    state: str  # STATE_*
    message: str

    def to_text(self) -> str:
        entry = self.history_entry
        headlines = {
            OUTCOME_COMPLETE: "Backup complete — everything was saved.",
            OUTCOME_PARTIAL: "Backup finished, but not everything was saved.",
            OUTCOME_FAILED: "Backup failed — nothing was saved.",
            OUTCOME_REFUSED: (
                "Backup did not run — a safety check stopped it "
                "before anything was written."
            ),
        }
        lines: list[str] = [headlines.get(self.outcome, f"Backup outcome: {self.outcome}")]
        if self.message:
            lines.append(self.message)
        if entry.sources:
            lines.append("Folders in this backup: " + ", ".join(entry.sources))
        if self.outcome != OUTCOME_REFUSED:
            lines.append(
                f"Files saved: {entry.files_saved} ({human_bytes(entry.bytes_saved)})"
            )
            lines.append(f"New data copied: {human_bytes(entry.bytes_copied)}")
            lines.append(f"Time taken: {_human_seconds(entry.seconds)}")
            if entry.snapshots:
                lines.append("Snapshots created:")
                for snapshot_id in entry.snapshots:
                    lines.append(f"  - {snapshot_id}")
            else:
                lines.append("No new snapshots were created.")
        if entry.pruned:
            lines.append("Old backups removed, as your settings ask:")
            for snapshot_id in entry.pruned:
                lines.append(f"  - {snapshot_id}")
        if entry.verified is not None:
            if entry.verified:
                lines.append(
                    "Spot-check: the backup store was checked before "
                    "this run and everything verified, byte for byte."
                )
            else:
                lines.append(
                    f"Spot-check: checking the store found "
                    f"{entry.verify_problems} problem(s)."
                )
        if entry.reason and entry.reason != self.message:
            lines.append(f"Reason: {entry.reason}")
        for note in entry.notes:
            lines.append(f"Note: {note}")
        return "\n".join(lines)


#: The state words a status block leads with — loud, so the state of
#: the backups can be read off a screen at a glance.
_STATE_WORDS = {
    STATE_READY: "READY",
    STATE_TARGET_MISSING: "BACKUP DRIVE NOT CONNECTED",
    STATE_TARGET_FULL: "BACKUP DRIVE FULL",
    STATE_STORE_DAMAGED: "BACKUP STORE DAMAGED",
    STATE_NEVER_RUN: "NO BACKUP HAS RUN YET",
    STATE_OVERDUE: "BACKUP OVERDUE",
    STATE_UNFINISHED: "AN EARLIER BACKUP NEVER FINISHED SAVING",
}


@dataclass
class BackupStatus:
    """The state of a backup, stated loudly and in full.

    ``next_due_local`` is left for the schedule-aware layer to fill
    in: the exact next-run moment needs the local clock and the
    schedule math, which live one layer up. Everything else here is
    worked out from the settings, the history, and the store itself.
    """

    state: str  # STATE_*
    headline: str
    message: str
    next_action: str  # "" when nothing needs doing
    store: str
    sources: int  # how many places are being backed up
    snapshots_in_store: int
    last_run_utc: Optional[str]
    last_outcome: Optional[str]
    next_due_local: Optional[str]
    free_bytes: Optional[int]
    estimate_bytes: Optional[int]
    history_damaged_lines: int

    def to_text(self) -> str:
        words = _STATE_WORDS.get(self.state, self.state.upper())
        lines: list[str] = [f"Backup status: {words}"]
        if self.headline:
            lines.append(self.headline)
        if self.message:
            lines.append(self.message)
        lines.append(f"Backup store: {self.store}")
        lines.append(f"Folders being backed up: {self.sources}")
        lines.append(f"Snapshots in the store: {self.snapshots_in_store}")
        if self.last_run_utc:
            outcome = f" (outcome: {self.last_outcome})" if self.last_outcome else ""
            lines.append(f"Last backup run: {self.last_run_utc}{outcome}")
        else:
            lines.append("Last backup run: never")
        if self.free_bytes is not None:
            lines.append(
                f"Free space on the backup drive: {human_bytes(self.free_bytes)}"
            )
        if self.estimate_bytes is not None:
            lines.append(
                f"Next backup needs about: {human_bytes(self.estimate_bytes)} "
                "(an estimate)"
            )
        if self.next_due_local:
            lines.append(f"Next backup due: {self.next_due_local}")
        if self.history_damaged_lines:
            lines.append(
                f"Warning: {self.history_damaged_lines} line(s) in the "
                "backup history could not be read and were left out."
            )
        if self.next_action:
            lines.append(f"What to do: {self.next_action}")
        return "\n".join(lines)


@dataclass
class History:
    """The backup history file, as loaded: entries plus honesty.

    ``damaged_lines`` counts history lines that could not be read
    and were skipped. They are counted, never hidden — a gap in the
    record is itself a fact about the backup.
    """

    entries: list[HistoryEntry] = field(default_factory=list)
    damaged_lines: int = 0
