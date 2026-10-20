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

Exit codes: 0 = done, fully; 1 = done, but something in the report
needs your attention (a partial restore, a failed check); 2 = Lazarus
stopped before doing the work, and says why.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from ._version import __version__
from .assess import assess_options
from .decrypt_run import SubprocessRunner, prepare_work_area, run_decryptor
from .decryptors import find_entry, identify_family, load_registry
from .errors import DecryptorError, LazarusError
from .rescue import rescue_to_image
from .restore import restore
from .snapshot import create_snapshot
from .store import SnapshotStore
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
