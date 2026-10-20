"""Command-line interface: Lazarus Phase 1, usable end to end.

    lazarus init    <store>
    lazarus snap    <store> <file-or-folder> [--label TEXT]
    lazarus list    <store>
    lazarus verify  <store> [snapshot-id]
    lazarus restore <store> [--to FOLDER] [--snapshot ID | --as-of DATE]
                    [--path FILE-OR-FOLDER]

Exit codes: 0 = done, fully; 1 = done, but something in the report
needs your attention (a partial restore, a failed check); 2 = Lazarus
stopped before doing the work, and says why.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._version import __version__
from .errors import LazarusError
from .restore import restore
from .snapshot import create_snapshot
from .store import SnapshotStore
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
