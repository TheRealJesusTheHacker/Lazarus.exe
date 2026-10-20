"""Command-line interface: Lazarus Phase 1, usable end to end.

    lazarus init    <store>
    lazarus snap    <store> <file-or-folder> [--label TEXT]
    lazarus list    <store>
    lazarus verify  <store> [snapshot-id]
    lazarus restore <store> [--to FOLDER] [--snapshot ID | --as-of DATE]
                    [--path FILE-OR-FOLDER]
    lazarus scan    <drive-or-image>
    lazarus recover <drive-or-image> --to FOLDER [--name FILE ...]
    lazarus rescue  <drive-or-image> <image-file> [--map FILE]
    lazarus volumes
    lazarus unlock  <volume>
    lazarus decryptors
    lazarus identify <folder>
    lazarus decrypt  <folder> --to WORK --decryptor PROGRAM
                    [--family NAME]
    lazarus assess   <path>
    lazarus backup setup --store STORE --add FOLDER
                    [--every daily|weekly] [--weekday DAY] [--at HH:MM]
                    [--keep-count N | --keep-days N] [--verify-every N]
                    [--register] [--program NAME] [--config FILE]
    lazarus backup run     [--config FILE]
    lazarus backup due     [--config FILE]
    lazarus backup status  [--config FILE]
    lazarus backup history [--config FILE] [--limit N]
    lazarus backup prune   [--config FILE] [--dry-run]
    lazarus backup register   [--config FILE] [--program NAME]
    lazarus backup unregister [--config FILE]

Exit codes: 0 = done, fully; 1 = done, but something in the report
needs your attention (a partial restore, a failed check); 2 = Lazarus
stopped before doing the work, and says why.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

from ._version import __version__
from .assess import assess_options
from .backup import (
    _acquire_run_lock,
    _release_run_lock,
    backup_snapshot_ids,
    backup_status,
    default_config_path,
    history_path_for,
    load_config,
    load_history,
    run_backup,
    save_config,
    validate_config,
)
from .backup_model import (
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
    TRIGGER_CATCHUP,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULED,
    BackupConfig,
    RetentionSpec,
    ScheduleSpec,
    human_bytes,
)
from .decrypt_run import SubprocessRunner, prepare_work_area, run_decryptor
from .decryptors import find_entry, identify_family, load_registry
from .errors import (
    BackupError,
    DecryptorError,
    LazarusError,
    SourceNotFoundError,
    StoreError,
)
from .rescue import rescue_to_image
from .restore import restore
from .retention import apply_retention, plan_retention
from .schedule import (
    next_scheduled_instant,
    register_schedule,
    run_due_backup,
    unregister_schedule,
)
from .snapshot import create_snapshot
from .store import SnapshotStore, format_utc, new_snapshot_id, utc_now
from .undelete import recover_deleted, scan_for_deleted
from .unlock import default_platform, unlock_volume
from .verify import verify_snapshot, verify_store


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def _cmd_init(args) -> int:
    store = SnapshotStore.create(Path(args.store))
    print(f"Store created at {store.root}.")
    print("Snapshots you take will live here, dated and ready to restore.")
    return 0


def _cmd_snap(args) -> int:
    store = SnapshotStore.open(Path(args.store))
    manifest = create_snapshot(store, Path(args.source), label=args.label or "")
    when = manifest.created_utc.replace("T", " ")[:19]
    print(f"Snapshot saved: {manifest.id}")
    print(f"  Taken:  {when} (UTC)")
    print(f"  Holds:  {manifest.file_count} file(s), {_human_bytes(manifest.total_bytes)}")
    if manifest.label:
        print(f"  Label:  {manifest.label}")
    if not manifest.complete:
        print("  Note:   this snapshot is INCOMPLETE. These could not be saved:")
        for problem in manifest.problems:
            if problem.path:
                print(f"    - {problem.path}: {problem.message}")
    return 0 if manifest.complete else 1


def _cmd_list(args) -> int:
    store = SnapshotStore.open(Path(args.store))
    infos = store.list_snapshots()
    if not infos:
        print("This store holds no snapshots yet.")
        return 0
    print(f"{len(infos)} snapshot(s) in {store.root}:")
    for info in infos:
        if not info.has_manifest:
            print(f"  {info.id}  — unfinished snapshot (never fully saved; cannot be used)")
            continue
        when = info.created_utc.replace("T", " ")[:19]
        state = "complete" if info.complete else "INCOMPLETE"
        kind = "safety copy" if info.kind == "pre-restore" else "snapshot"
        label = f"  “{info.label}”" if info.label else ""
        print(
            f"  {info.id}  {when} UTC  {kind}, {state}, "
            f"{info.file_count} file(s), {_human_bytes(info.total_bytes)}{label}"
        )
    return 0


def _cmd_verify(args) -> int:
    store = SnapshotStore.open(Path(args.store))
    if args.snapshot:
        result = verify_snapshot(store, args.snapshot)
        print(result.summary())
        for problem in result.problems:
            print(f"  - {problem.path}: {problem.message}")
        return 0 if result.ok else 1
    result = verify_store(store)
    if result.ok:
        print(
            f"The whole store checked out: {result.snapshots_checked} "
            "snapshot(s) verified, byte for byte."
        )
        return 0
    print("The store has problems:")
    for problem in result.problems:
        print(f"  - {problem.path}: {problem.message}")
    return 1


def _cmd_scan(args) -> int:
    report = scan_for_deleted(Path(args.source))
    print(report.to_text())
    return 0


def _cmd_recover(args) -> int:
    report = recover_deleted(Path(args.source), Path(args.to), names=args.name)
    print(report.to_text())
    return {"complete": 0, "partial": 1, "failed": 2}[report.status]


def _cmd_rescue(args) -> int:
    report = rescue_to_image(
        Path(args.source),
        Path(args.destination),
        map_path=Path(args.map) if args.map else None,
    )
    print(report.to_text())
    return 0 if report.status == "complete" else 1


def _read_key(prompt: str) -> str:
    """Read a recovery key without echoing it to the screen."""
    return getpass.getpass(prompt)


def _cmd_volumes(args) -> int:
    platform = default_platform()
    rows = platform.list_volumes()
    if not rows:
        print("No volumes could be listed.")
        return 0
    for volume in rows:
        if (volume.kind or "").strip().lower() == "bitlocker":
            if volume.locked is True:
                state = "locked"
            elif volume.locked is False:
                state = "open"
            else:
                state = "lock state unknown"
            print(f"{volume.ref} — BitLocker, {state}")
        else:
            print(f"{volume.ref} — {volume.kind}")
    return 0


def _cmd_unlock(args) -> int:
    outcome = unlock_volume(
        default_platform(),
        args.volume,
        lambda: _read_key("Type the volume's recovery key: "),
    )
    print(outcome.to_text())
    return 0 if outcome.status in ("unlocked", "already-unlocked") else 1


def _cmd_decryptors(args) -> int:
    for entry in load_registry():
        print(
            f"{entry.family} — {entry.decryptor_name}, free from "
            f"{entry.publisher}. Checked {entry.verified_utc}. "
            f"{entry.source_url}"
        )
    return 0


def _cmd_identify(args) -> int:
    if not str(args.folder).strip():
        raise DecryptorError(
            "No folder was named, so there is nothing to look at."
        )
    folder = Path(args.folder)
    identification = identify_family(folder, load_registry())
    if identification.matches:
        for match in identification.matches:
            entry = match.entry
            print(
                f"{entry.family} — this {match.confidence} the family "
                "that locked these files."
            )
            for piece in match.evidence:
                print(f"  - {piece}")
            print(
                f"  Published decryptor: {entry.decryptor_name} "
                f"({entry.publisher}). {entry.conditions}"
            )
    else:
        print(
            f"Lazarus does not recognize the signs in {folder} as any "
            "family on its list."
        )
    for note in identification.notes:
        print(note)
    return 0


#: The longest a decryptor run may take before Lazarus stops it:
#: four hours. Real decryptors can work for a long while on a big
#: folder, so the limit is generous — but it exists, so a program
#: that hangs cannot hang Lazarus forever.
_DECRYPT_TIMEOUT_SECONDS = 4 * 60 * 60


def _cmd_decrypt(args) -> int:
    if not str(args.folder).strip():
        raise DecryptorError(
            "No folder was named, so there is nothing to work on."
        )
    registry = load_registry()
    if args.family:
        entry = find_entry(registry, args.family)
        if entry is None:
            raise DecryptorError(
                f"Lazarus has no decryptor listed for '{args.family}'. "
                "Run lazarus decryptors to see the families it knows, "
                "and use one of those names."
            )
    else:
        identification = identify_family(Path(args.folder), registry)
        # Only a "looks like" match — the family's own ransom
        # note — is enough for Lazarus to choose a decryptor by
        # itself, and only when exactly one family looks likely:
        # two plain readings point to one family no more plainly
        # than a suggestion does. A "may be" is a suggestion, and
        # Lazarus does not run a stranger's program on a
        # suggestion: the owner names the family, or nobody runs.
        looks_like = [
            match
            for match in identification.matches
            if match.confidence == "looks like"
        ]
        if len(looks_like) > 1:
            names = " and ".join(
                match.entry.family for match in looks_like
            )
            raise DecryptorError(
                f"The signs in this folder point plainly at more "
                f"than one family — {names} — and Lazarus will "
                f"not choose between families by itself. Name "
                f"the one to use with --family."
            )
        entry = looks_like[0].entry if looks_like else None
        if entry is None:
            if identification.matches:
                raise DecryptorError(
                    "The signs in this folder only suggest which "
                    "family locked these files — they do not point "
                    "to one plainly, so Lazarus will not choose a "
                    "decryptor by itself. If you know the family, "
                    "name it with --family. Run lazarus identify "
                    "to see what Lazarus found."
                )
            raise DecryptorError(
                "Lazarus could not tell which family locked these "
                "files, so it will not run a decryptor blind. Run "
                "lazarus identify first, or pass --family."
            )
    work = prepare_work_area(Path(args.folder), Path(args.to))
    report = run_decryptor(
        entry,
        work,
        Path(args.decryptor),
        SubprocessRunner(timeout_seconds=_DECRYPT_TIMEOUT_SECONDS),
    )
    print(report.to_text())
    return {"complete": 0, "attention": 1, "failed": 2}[report.status]


def _cmd_assess(args) -> int:
    if not str(args.path).strip():
        raise DecryptorError(
            "Nothing was named, so there is nothing to assess."
        )
    assessment = assess_options(Path(args.path), platform=default_platform())
    print(assessment.to_text())
    return 0


def _cmd_restore(args) -> int:
    store = SnapshotStore.open(Path(args.store))
    report = restore(
        store,
        destination=Path(args.to) if args.to else None,
        snapshot_id=args.snapshot,
        as_of=args.as_of,
        path=args.path,
    )
    print(report.to_text())
    return {"complete": 0, "partial": 1, "failed": 2}[report.status]


# -- backup automation (Phase 4) -------------------------------------------
#
# Handlers here raise LazarusError for anything that stops the work
# before it starts; main() prints that message plainly and returns
# 2, exactly as it does for every other command. A handler's own
# return value carries the finer verdict: 0 when the work finished
# fully, 1 when it finished but needs attention.


def _backup_config_path(args) -> Path:
    """The settings file a backup command should use."""
    config = getattr(args, "config", None)
    return Path(config) if config else default_config_path()


def _backup_outcome_exit(outcome: str) -> int:
    return {
        OUTCOME_COMPLETE: 0,
        OUTCOME_PARTIAL: 1,
        OUTCOME_FAILED: 1,
        OUTCOME_REFUSED: 2,
    }.get(outcome, 1)


def _backup_status_exit(state: str) -> int:
    return {
        STATE_READY: 0,
        STATE_NEVER_RUN: 1,
        STATE_OVERDUE: 1,
        STATE_TARGET_FULL: 1,
        STATE_UNFINISHED: 1,
        STATE_TARGET_MISSING: 2,
        STATE_STORE_DAMAGED: 2,
    }.get(state, 1)


def _schedule_words(schedule: ScheduleSpec) -> str:
    """A schedule in words: 'every day at 02:00', and the like."""
    if schedule.every == "weekly":
        weekday = (schedule.weekday or "monday").strip().lower().capitalize()
        return f"every {weekday} at {schedule.at}"
    return f"every day at {schedule.at}"


def _retention_words(retention: RetentionSpec) -> str:
    """A tidying rule in words, the way the owner chose it."""
    if retention.mode == RETENTION_DAYS:
        return f"keep backups for the last {retention.days} days"
    return f"keep the newest {retention.count} backups"


def _cmd_backup_setup(args) -> int:
    config_path = _backup_config_path(args)
    store_path = Path(args.store)
    # The settings are built and checked first, before any store
    # is created or opened and before anything is saved: a bad
    # time, keeping rule, or spot-check interval refuses the whole
    # setup up front and leaves nothing behind. Paths are saved in
    # their full, resolved form, so the backup finds the same
    # folders no matter where it is started from.
    sources: list[str] = []
    for source in getattr(args, "add") or []:
        source_path = Path(source)
        if not source_path.exists() and not source_path.is_symlink():
            raise SourceNotFoundError(
                f"'{source}' is not there, so it cannot be backed "
                "up. Check the name, and that the drive it lives on "
                "is plugged in, then set the backup up again."
            )
        sources.append(str(source_path.resolve()))
    if getattr(args, "keep_count", None) is not None:
        retention = RetentionSpec(
            mode=RETENTION_COUNT, count=args.keep_count, days=30
        )
    elif getattr(args, "keep_days", None) is not None:
        retention = RetentionSpec(
            mode=RETENTION_DAYS, count=10, days=args.keep_days
        )
    else:
        retention = RetentionSpec(mode=RETENTION_COUNT, count=10, days=30)
    schedule = ScheduleSpec(
        every=args.every, at=args.at, weekday=args.weekday
    )
    config = BackupConfig(
        store=str(store_path.resolve()),
        sources=sources,
        schedule=schedule,
        retention=retention,
        verify_every_runs=args.verify_every,
        created_utc=format_utc(utc_now()),
        tool_version=__version__,
    )
    validate_config(config)
    if not store_path.exists():
        store = SnapshotStore.create(store_path)
    elif (store_path / "store.json").exists():
        store = SnapshotStore.open(store_path)
    elif store_path.is_dir():
        try:
            is_empty = not any(store_path.iterdir())
        except OSError:
            is_empty = False
        if is_empty:
            store = SnapshotStore.create(store_path)
        else:
            raise StoreError(
                f"'{store_path}' is not a Lazarus store, and it "
                "already holds other files. Pick an empty folder "
                "or a new folder for the backup store, so backups "
                "never mix with other files."
            )
    else:
        raise StoreError(
            f"'{store_path}' is a file, not a folder. "
            "Pick a folder for the backup store."
        )
    config.store = str(store.root)
    save_config(config, config_path)
    print("Backup is set up.")
    print(f"Backup store: {config.store}")
    print("What will be backed up:")
    for source in config.sources:
        print(f"  - {source}")
    print(f"When: {_schedule_words(config.schedule)}")
    print(f"Tidying rule: {_retention_words(config.retention)}")
    print(
        "Spot-check: the backup store will be checked every "
        f"{config.verify_every_runs} runs."
    )
    print(f"Settings saved to: {config_path}")
    print("Next step: Take the first backup now: lazarus backup run")
    if args.register:
        # Registration comes after the settings are saved and said
        # so: on a computer that is not running Windows the
        # registration raises BackupError, main() reports it and
        # returns 2, and the saved-settings lines above are the
        # honest record — the backup is set up; only the Windows
        # task could not be registered here.
        confirmation = register_schedule(
            config, config_path, args.program or "lazarus"
        )
        print(confirmation)
    return 0


def _cmd_backup_run(args) -> int:
    config_path = _backup_config_path(args)
    config = load_config(config_path)
    result = run_backup(config, config_path, trigger=TRIGGER_MANUAL)
    print(result.to_text())
    return _backup_outcome_exit(result.outcome)


def _cmd_backup_due(args) -> int:
    config_path = _backup_config_path(args)
    config = load_config(config_path)
    due_result = run_due_backup(config, config_path)
    print(due_result.to_text())
    if not due_result.due or due_result.result is None:
        return 0
    return _backup_outcome_exit(due_result.result.outcome)


def _cmd_backup_status(args) -> int:
    config_path = _backup_config_path(args)
    config = load_config(config_path)
    status = backup_status(config, config_path)
    next_instant = next_scheduled_instant(
        config.schedule, datetime.now().astimezone()
    )
    status.next_due_local = next_instant.strftime("%A %Y-%m-%d at %H:%M")
    print(status.to_text())
    return _backup_status_exit(status.state)


def _cmd_backup_history(args) -> int:
    config_path = _backup_config_path(args)
    limit = getattr(args, "limit", None)
    if limit is not None and limit < 1:
        raise BackupError("The history limit must be at least 1.")
    history = load_history(history_path_for(config_path))
    if not history.entries:
        print(
            "No backups have run yet. Run 'lazarus backup run' "
            "to take the first one."
        )
        return 0
    if limit is None:
        limit = 10
    trigger_words = {
        TRIGGER_MANUAL: "by hand",
        TRIGGER_SCHEDULED: "scheduled",
        TRIGGER_CATCHUP: "catch-up",
    }
    outcome_words = {
        OUTCOME_COMPLETE: "complete",
        OUTCOME_PARTIAL: "partial",
        OUTCOME_FAILED: "failed",
        OUTCOME_REFUSED: "refused",
    }
    blocks: list[str] = []
    for entry in list(reversed(history.entries))[:limit]:
        lines: list[str] = []
        when = entry.started_utc if entry.started_utc else "unknown time"
        lines.append(f"{when} — {outcome_words.get(entry.outcome, entry.outcome)}")
        lines.append(
            "  How it was started: "
            f"{trigger_words.get(entry.trigger, entry.trigger)}"
        )
        lines.append(f"  Outcome: {outcome_words.get(entry.outcome, entry.outcome)}")
        lines.append(
            f"  Files saved: {entry.files_saved} "
            f"({human_bytes(entry.bytes_saved)})"
        )
        lines.append(f"  Snapshots made: {len(entry.snapshots)}")
        for snapshot_id in entry.snapshots:
            lines.append(f"    - {snapshot_id}")
        if entry.pruned:
            lines.append(
                f"  Old backups tidied away: {len(entry.pruned)}"
            )
        if entry.verified is not None:
            if entry.verified:
                lines.append(
                    "  Spot-check: the backup store was checked "
                    "before this run and everything verified."
                )
            else:
                lines.append(
                    "  Spot-check: checking the backup store found "
                    f"{entry.verify_problems} problem(s)."
                )
        if entry.reason:
            lines.append(f"  Reason: {entry.reason}")
        for note in entry.notes:
            lines.append(f"  Note: {note}")
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
    return 0


def _cmd_backup_prune(args) -> int:
    config_path = _backup_config_path(args)
    config = load_config(config_path)
    store_path = Path(config.store)
    if not store_path.is_dir():
        print(
            "The backup drive is not connected: there is no folder "
            f"at '{config.store}'. Plug the drive in and try again."
        )
        return 2
    store = SnapshotStore.open(store_path)
    history = load_history(history_path_for(config_path))
    plan = plan_retention(
        store, backup_snapshot_ids(history), config.retention, utc_now()
    )
    if not plan.remove:
        print(
            "Nothing to tidy up: every backup in the store is "
            "inside the keeping rule you chose."
        )
    else:
        print("These backups are outside the keeping rule you chose:")
        for item in plan.remove:
            when = item.created_utc if item.created_utc else "unknown date"
            print(f"  - {item.id} (made {when}): {item.reason}")
        print(f"Backups to remove: {len(plan.remove)}")
    if plan.held:
        print("Left alone:")
        for held_id, reason in plan.held:
            print(f"  - {held_id}: {reason}")
    if not plan.remove:
        return 0
    if args.dry_run:
        print("This was a dry run — nothing was removed.")
        return 0
    # A manual prune writes no history entry: history is the record
    # of backup runs, and this prune reports itself here, on screen.
    lock_path, stale_note = _acquire_run_lock(
        store.root, new_snapshot_id(utc_now())
    )
    try:
        if stale_note:
            print(stale_note)
        result = apply_retention(store, plan)
    finally:
        _release_run_lock(lock_path)
    print(f"Removed {len(result.removed_ids)} old backup(s).")
    print(f"Saved copies removed: {result.objects_removed}")
    print(f"Space freed: {human_bytes(result.bytes_freed)}")
    if result.gc_note:
        print(result.gc_note)
    return 0


def _cmd_backup_register(args) -> int:
    config_path = _backup_config_path(args)
    config = load_config(config_path)
    confirmation = register_schedule(
        config, config_path, args.program or "lazarus"
    )
    print(confirmation)
    return 0


def _cmd_backup_unregister(args) -> int:
    confirmation = unregister_schedule()
    print(confirmation)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazarus",
        description="LAZARUS.EXE — bring files back, restored to the day you choose.",
    )
    parser.add_argument("--version", action="version", version=f"LAZARUS.EXE {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a new snapshot store")
    p.add_argument("store")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("snap", help="take a snapshot of a file or folder")
    p.add_argument("store")
    p.add_argument("source")
    p.add_argument("--label", default="")
    p.set_defaults(func=_cmd_snap)

    p = sub.add_parser("list", help="list the snapshots in a store")
    p.add_argument("store")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("verify", help="check snapshots against their records")
    p.add_argument("store")
    p.add_argument("snapshot", nargs="?")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("restore", help="restore a file, a folder, or everything")
    p.add_argument("store")
    p.add_argument("--to", help="folder to restore into (default: where it came from)")
    choice = p.add_mutually_exclusive_group()
    choice.add_argument("--snapshot", help="snapshot id to restore")
    choice.add_argument("--as-of", help="newest snapshot on or before this date")
    p.add_argument("--path", help="only this file or folder inside the snapshot")
    p.set_defaults(func=_cmd_restore)

    p = sub.add_parser("scan", help="look for deleted files on a drive or in an image")
    p.add_argument("source")
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("recover", help="bring deleted files back, onto a different drive")
    p.add_argument("source")
    p.add_argument("--to", required=True, help="folder on a DIFFERENT drive to recover into")
    p.add_argument(
        "--name", action="append", default=None,
        help="only this deleted file (may be given more than once)",
    )
    p.set_defaults(func=_cmd_recover)

    p = sub.add_parser("rescue", help="clone a failing drive to an image, gently")
    p.add_argument("source")
    p.add_argument("destination", help="the image file to create (or resume)")
    p.add_argument("--map", default=None, help="progress file (default: beside the image)")
    p.set_defaults(func=_cmd_rescue)

    p = sub.add_parser("volumes", help="list the volumes Lazarus can see")
    p.set_defaults(func=_cmd_volumes)

    p = sub.add_parser(
        "unlock", help="open a locked volume with your own recovery key"
    )
    p.add_argument("volume")
    p.set_defaults(func=_cmd_unlock)

    p = sub.add_parser(
        "decryptors", help="list the published decryptors Lazarus knows"
    )
    p.set_defaults(func=_cmd_decryptors)

    p = sub.add_parser(
        "identify",
        help="tell which ransomware family locked a folder's files",
    )
    p.add_argument("folder")
    p.set_defaults(func=_cmd_identify)

    p = sub.add_parser(
        "decrypt",
        help="run a published decryptor on copies of a folder's files",
    )
    p.add_argument("folder")
    p.add_argument(
        "--to", required=True,
        help="new folder Lazarus will prepare as its work area",
    )
    p.add_argument(
        "--decryptor", required=True,
        help="the decryptor program to run, fetched from its publisher",
    )
    p.add_argument(
        "--family", default=None,
        help="the family that locked the files, as listed by "
        "lazarus decryptors (default: tell from the folder's signs)",
    )
    p.set_defaults(func=_cmd_decrypt)

    p = sub.add_parser(
        "assess", help="what Lazarus can do for a drive or folder"
    )
    p.add_argument("path")
    p.set_defaults(func=_cmd_assess)

    p_backup = sub.add_parser(
        "backup", help="scheduled backups into the snapshot store"
    )
    backup_sub = p_backup.add_subparsers(
        dest="backup_command", required=True
    )

    bp = backup_sub.add_parser("setup", help="set a backup up")
    bp.add_argument("--store", required=True, help="folder the backups live in")
    bp.add_argument(
        "--add", action="append", required=True,
        help="a file or folder to back up (may be given more than once)",
    )
    bp.add_argument(
        "--every", choices=["daily", "weekly"], default="daily",
        help="how often backups run (default: daily)",
    )
    bp.add_argument(
        "--weekday",
        choices=[
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ],
        default="monday",
        help="the day for a weekly backup (default: monday)",
    )
    bp.add_argument(
        "--at", default="02:00",
        help="the local time backups run, like 02:00 (default: 02:00)",
    )
    keep = bp.add_mutually_exclusive_group()
    keep.add_argument(
        "--keep-count", type=int, default=None,
        help="keep the newest this many backups",
    )
    keep.add_argument(
        "--keep-days", type=int, default=None,
        help="keep backups for the last this many days",
    )
    bp.add_argument(
        "--verify-every", type=int, default=7, dest="verify_every",
        help="spot-check the store every this many runs (default: 7)",
    )
    bp.add_argument(
        "--register", action="store_true",
        help="also register the schedule with Windows Task Scheduler",
    )
    bp.add_argument(
        "--program", default="lazarus",
        help="the program name the scheduled task runs (default: lazarus)",
    )
    bp.add_argument("--config", default=None, help="where the settings live")
    bp.set_defaults(func=_cmd_backup_setup)

    bp = backup_sub.add_parser("run", help="run a backup now")
    bp.add_argument("--config", default=None, help="where the settings live")
    bp.set_defaults(func=_cmd_backup_run)

    bp = backup_sub.add_parser(
        "due", help="run a backup only if the schedule says one is due"
    )
    bp.add_argument("--config", default=None, help="where the settings live")
    bp.set_defaults(func=_cmd_backup_due)

    bp = backup_sub.add_parser("status", help="say how the backup stands")
    bp.add_argument("--config", default=None, help="where the settings live")
    bp.set_defaults(func=_cmd_backup_status)

    bp = backup_sub.add_parser("history", help="show past backup runs")
    bp.add_argument("--config", default=None, help="where the settings live")
    bp.add_argument(
        "--limit", type=int, default=10,
        help="how many runs to show (default: 10)",
    )
    bp.set_defaults(func=_cmd_backup_history)

    bp = backup_sub.add_parser(
        "prune", help="tidy away old backups outside the keeping rule"
    )
    bp.add_argument("--config", default=None, help="where the settings live")
    bp.add_argument(
        "--dry-run", action="store_true",
        help="show what would be removed, and remove nothing",
    )
    bp.set_defaults(func=_cmd_backup_prune)

    bp = backup_sub.add_parser(
        "register", help="register the schedule with Windows Task Scheduler"
    )
    bp.add_argument("--config", default=None, help="where the settings live")
    bp.add_argument(
        "--program", default="lazarus",
        help="the program name the scheduled task runs (default: lazarus)",
    )
    bp.set_defaults(func=_cmd_backup_register)

    bp = backup_sub.add_parser(
        "unregister",
        help="remove the schedule from Windows Task Scheduler",
    )
    bp.add_argument("--config", default=None, help="where the settings live")
    bp.set_defaults(func=_cmd_backup_unregister)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LazarusError as error:
        print(f"Lazarus stopped: {error}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
