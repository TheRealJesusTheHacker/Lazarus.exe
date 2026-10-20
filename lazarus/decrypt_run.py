"""Running a published decryptor on copies, never the only copies.

A decryptor is someone else's program, fetched by the owner from its
publisher. Lazarus does not vouch for that program, and it never puts
the owner's only copies anywhere near it. The rule is enforced by
construction, not by politeness:

1. :func:`prepare_work_area` copies a folder into a fresh work area
   Lazarus makes itself — one that did not exist a moment ago, fully
   separate from the originals — hashing every file as it copies and
   leaving a marker file on disk that says so.
2. :func:`run_decryptor` re-reads that marker from disk before it
   runs anything. No marker, a damaged marker, or a marker whose
   folder names do not match the work area in hand: nothing runs.
   There is no way through this API to point a decryptor at live
   originals; the program is only ever handed the copies folder.
3. The originals are hashed before and after the run, from the
   source recorded in the marker, and the report says plainly
   whether they changed. Lazarus never writes to them either way.

The runner itself is deliberately narrow. A decryptor is invoked
with exactly three arguments — the program, the copies folder, the
output folder — and its own words are never repeated: a stranger's
program output is not Lazarus's voice, so the report speaks only in
plain outcomes Lazarus can stand behind.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from . import hashing
from .decrypt_model import DecryptorEntry
from .decryptors import _printable
from .errors import DecryptorError, DecryptorRefusedError

WORK_AREA_MARKER = ".lazarus-work-area.json"
COPIES_DIRNAME = "copies"
OUTPUT_DIRNAME = "decrypted"

_REFUSAL = (
    "This is not a work area Lazarus prepared, so nothing will run. "
    "Lazarus only runs a decryptor on copies inside a work area it "
    "made — never on your only copies. Prepare one with: "
    "lazarus decrypt <folder> --to <new-folder>"
)


@dataclass
class CopiedFile:
    """One file Lazarus copied into a work area."""

    relpath: str  # POSIX-style, relative to the source folder
    size: int
    sha256: str


@dataclass
class WorkArea:
    """A folder of copies Lazarus prepared for a decryptor to work on."""

    root: Path
    copies_dir: Path
    output_dir: Path
    source_dir: Path
    files: list[CopiedFile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _is_inside(path: Path, folder: Path) -> bool:
    """True if ``path`` is ``folder`` itself or lives inside it."""
    return path == folder or path.is_relative_to(folder)


def prepare_work_area(source_dir: Path, work_root: Path) -> WorkArea:
    """Copy ``source_dir`` into a fresh work area at ``work_root``.

    ``work_root`` must not exist yet, and it must not be the source
    folder, inside it, or around it — the work area and the originals
    stay completely separate, so nothing that runs on the copies can
    reach the originals. Links and anything that is not a plain file
    are skipped and named in the work area's problems, never silently
    dropped. Every copied file is hashed as it lands, and the marker
    file records those hashes against the source folder.
    """
    source = Path(source_dir)
    work = Path(work_root)
    if not source.is_dir():
        raise DecryptorError(
            f"Lazarus could not find a folder at {source} to copy from. "
            "Check the name and try again."
        )
    source_resolved = source.resolve()
    work_resolved = work.resolve()
    if _is_inside(work_resolved, source_resolved) or _is_inside(
        source_resolved, work_resolved
    ):
        raise DecryptorRefusedError(
            "The work folder cannot be the folder itself, inside it, or "
            "around it. Pick a new folder somewhere else — Lazarus keeps "
            "its work area completely separate from the originals, so a "
            "decryptor can never reach them."
        )
    if os.path.lexists(work):
        raise DecryptorRefusedError(
            f"{work} is already there. Lazarus prepares an empty work "
            "folder of its own — pick a new place that does not exist yet."
        )

    copies_dir = work / COPIES_DIRNAME
    output_dir = work / OUTPUT_DIRNAME
    try:
        copies_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
    except OSError:
        raise DecryptorError(
            f"Lazarus could not prepare a work folder at {work}. "
            "Check the drive is connected and the folder can be "
            "written to."
        ) from None

    copied: list[CopiedFile] = []
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(
        source, topdown=True, followlinks=False
    ):
        dirnames.sort()
        filenames.sort()
        current = Path(dirpath)
        keep: list[str] = []
        for name in dirnames:
            full = current / name
            rel = full.relative_to(source).as_posix()
            if full.is_symlink():
                problems.append(
                    f"Skipped {_printable(rel)}: it is a link, not a real folder — "
                    "Lazarus copies only real files."
                )
            elif not full.is_dir():
                problems.append(
                    f"Skipped {_printable(rel)}: it is not a regular folder, so "
                    "Lazarus left it alone."
                )
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            full = current / name
            rel = full.relative_to(source).as_posix()
            if full.is_symlink():
                problems.append(
                    f"Skipped {_printable(rel)}: it is a link, not a real file — "
                    "Lazarus copies only real files."
                )
                continue
            if not full.is_file():
                problems.append(
                    f"Skipped {_printable(rel)}: it is not a regular file, so "
                    "Lazarus left it alone."
                )
                continue
            destination = copies_dir.joinpath(*Path(rel).parts)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                sha256, size = hashing.hash_file_into(full, destination)
            except OSError:
                problems.append(
                    f"Could not copy {_printable(rel)}: the file could not be read, "
                    "so it was left out of the work area."
                )
                try:
                    destination.unlink()
                except OSError:
                    pass
                continue
            copied.append(CopiedFile(relpath=rel, size=size, sha256=sha256))

    copied.sort(key=lambda item: item.relpath)
    if not copied:
        # No litter: a work area with nothing to work on is not left
        # behind for the owner to puzzle over.
        shutil.rmtree(work, ignore_errors=True)
        raise DecryptorError(
            f"There are no files in {source} for Lazarus to copy."
        )

    marker = {
        "kind": "lazarus-work-area",
        "version": 1,
        "source": str(source),
        "created_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "copies_dir": COPIES_DIRNAME,
        "output_dir": OUTPUT_DIRNAME,
        "files": {item.relpath: item.sha256 for item in copied},
    }
    marker_path = work / WORK_AREA_MARKER
    try:
        marker_path.write_text(
            json.dumps(marker, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        shutil.rmtree(work, ignore_errors=True)
        raise DecryptorError(
            f"Lazarus could not finish preparing the work folder at "
            f"{work}, so it tidied the half-made folder away. Check "
            "the drive is connected and the folder can be written to."
        ) from None
    hashing.fsync_file(marker_path)
    return WorkArea(
        root=work,
        copies_dir=copies_dir,
        output_dir=output_dir,
        source_dir=source,
        files=copied,
        problems=problems,
    )


@dataclass
class RunnerOutcome:
    """How a decryptor program ended, in Lazarus's own words."""

    ok: bool
    message: str = ""  # plain language; never the program's raw output


class DecryptorRunner(Protocol):
    """Anything that can run a decryptor program once."""

    def run(
        self, executable: Path, input_dir: Path, output_dir: Path
    ) -> RunnerOutcome:
        """Run ``executable`` on ``input_dir``, writing to ``output_dir``."""
        ...


@dataclass
class SubprocessRunner:
    """Run the decryptor as a child process, with nothing extra.

    The command is exactly the program, the copies folder, and the
    output folder — no shell, no added flags, nothing the publisher
    did not ask for. The program's own output is sent nowhere:
    Lazarus does not read it, hold it, or repeat it, because a
    stranger's program output is not Lazarus's voice.
    """

    timeout_seconds: Optional[float] = None

    def run(
        self, executable: Path, input_dir: Path, output_dir: Path
    ) -> RunnerOutcome:
        argv = [str(executable), str(input_dir), str(output_dir)]
        try:
            # The program's output goes straight to nowhere
            # (DEVNULL) and its input comes from nowhere too: the
            # output is never buffered in memory or decoded, so
            # nothing the program prints — in any encoding, or at
            # any volume — can break the run, swell it, or leak
            # into a message, and a program that stops to ask a
            # question gets an empty answer at once instead of
            # waiting on a person who is not there.
            proc = subprocess.run(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return RunnerOutcome(
                ok=False,
                message="The decryptor program ran for too long and "
                "was stopped.",
            )
        except OSError:
            return RunnerOutcome(
                ok=False,
                message="The decryptor program could not be started.",
            )
        if proc.returncode == 0:
            return RunnerOutcome(ok=True)
        return RunnerOutcome(
            ok=False,
            message=(
                "The decryptor program ended with an error "
                f"(code {proc.returncode})."
            ),
        )


@dataclass
class DecryptRunReport:
    """One decryptor run: what ran, what it made, what state the
    originals are in — all in plain language."""

    family: str
    publisher: str
    decryptor_name: str
    program_path: str
    program_sha256: str
    program_note: Optional[str]
    files_copied: int
    files_produced: int
    produced_bytes: int
    runner_message: str
    originals_untouched: bool
    problems: list[str] = field(default_factory=list)
    status: str = "attention"  # complete | attention | failed
    output_dir: str = ""
    # The two ways "not untouched" can happen, kept apart so the
    # report never blames the run for damage that predates it:
    originals_changed_during: bool = False
    originals_already_different: bool = False

    def to_text(self) -> str:
        lines: list[str] = []
        if self.status == "complete":
            lines.append(
                f"The decryptor finished: {self.decryptor_name}, "
                f"published by {self.publisher}, for files locked by "
                f"{self.family}."
            )
        elif self.status == "failed":
            lines.append(
                f"The decryptor did not finish: {self.decryptor_name}, "
                f"published by {self.publisher}, for files locked by "
                f"{self.family}."
            )
        else:
            lines.append(
                f"The decryptor ran, but check the notes below: "
                f"{self.decryptor_name}, published by {self.publisher}, "
                f"for files locked by {self.family}."
            )
        lines.append(
            f"Program run: {self.program_path} "
            f"(SHA-256 fingerprint {self.program_sha256})."
        )
        if self.program_note:
            lines.append(self.program_note)
        lines.append(
            f"The decryptor was handed copies of "
            f"{self.files_copied} file(s) — never your originals."
        )
        if self.runner_message:
            lines.append(self.runner_message)
        lines.append(
            f"It produced {self.files_produced} file(s)."
        )
        if self.files_produced:
            lines.append(
                f"The recovered files are in {self.output_dir}. Look "
                "them over before you trust them; your originals are "
                "exactly as they were."
                if self.originals_untouched
                else f"The recovered files are in {self.output_dir}. "
                "Look them over before you trust them."
            )
        if self.originals_changed_during:
            lines.append(
                "Something outside Lazarus changed the original files "
                "while this ran. Lazarus never wrote to them — the "
                "decryptor was only ever given the copies."
            )
        elif self.originals_already_different:
            lines.append(
                "Some of the original files had already changed "
                "before this run began — see the notes below. The "
                "run itself changed none of them."
            )
        else:
            lines.append(
                "Your original files were checked before and after, "
                "and did not change."
            )
        if self.problems:
            lines.append("Notes:")
            for problem in self.problems:
                lines.append(f"  - {problem}")
        return "\n".join(lines)


def _read_marker(work_area: WorkArea) -> dict:
    """Read the work-area marker from disk, or refuse the run.

    This is the enforcement, not a courtesy check: the in-memory
    work area proves nothing, because anything could have built it.
    Only a marker Lazarus itself wrote — present, readable, of the
    right kind and version, and naming the same folders this work
    area names — lets a run proceed.
    """
    marker_path = work_area.root / WORK_AREA_MARKER
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DecryptorRefusedError(_REFUSAL) from None
    if not isinstance(data, dict) or data.get("kind") != "lazarus-work-area":
        raise DecryptorRefusedError(_REFUSAL)
    version = data.get("version")
    if type(version) is not int or version != 1:
        raise DecryptorRefusedError(_REFUSAL)
    if data.get("copies_dir") != work_area.copies_dir.name:
        raise DecryptorRefusedError(_REFUSAL)
    if data.get("output_dir") != work_area.output_dir.name:
        raise DecryptorRefusedError(_REFUSAL)
    return data


_COPIES_REFUSAL = (
    "The copies in the work area are not the copies Lazarus made "
    "and checked, so nothing will run. A copy may have been "
    "changed, added, or swapped since the work area was prepared. "
    "Prepare a fresh work area and try again."
)

_OUTPUT_REFUSAL = (
    "The work area's recovered-files folder is not empty, so "
    "Lazarus could not tell what this run produced from what was "
    "already there. Nothing will run. Prepare a fresh work area "
    "and try again."
)


def _valid_relpath(relpath: object) -> bool:
    """True for a plain relative file path, safe to join under a root.

    Marker records are data, and data gets checked: an absolute
    path, a ``..`` step, or a backslash could point the originals
    check outside the folder it belongs to, so none of them are
    accepted — the same traversal rules the rest of Lazarus lives
    by.
    """
    if not isinstance(relpath, str) or not relpath or "\\" in relpath:
        return False
    parts = Path(relpath).parts
    if Path(relpath).is_absolute() or not parts:
        return False
    return all(part not in ("", ".", "..") for part in parts)


def _verify_work_area(work_area: WorkArea) -> dict:
    """Prove, from disk, that this is a work area Lazarus prepared.

    Every part of the claim is re-checked at run time, because the
    run's promises rest on it:

    * the folders are exactly the work area's own ``copies`` and
      ``decrypted`` folders — not links, not stand-ins elsewhere;
    * the marker names the same source folder as the work area in
      hand, and its record of the copies matches the record the
      preparation returned, entry for entry;
    * the copies on disk still hash to exactly that record —
      nothing changed, nothing added, nothing swapped.

    Any disagreement is a refusal. Returns the manifest (relative
    path → SHA-256) the rest of the run checks against.
    """
    root = work_area.root
    copies = work_area.copies_dir
    output = work_area.output_dir
    if copies != root / COPIES_DIRNAME or output != root / OUTPUT_DIRNAME:
        raise DecryptorRefusedError(_REFUSAL)
    if root.is_symlink() or copies.is_symlink() or output.is_symlink():
        raise DecryptorRefusedError(_REFUSAL)
    resolved_root = root.resolve()
    if copies.resolve() != resolved_root / COPIES_DIRNAME:
        raise DecryptorRefusedError(_REFUSAL)
    if output.resolve() != resolved_root / OUTPUT_DIRNAME:
        raise DecryptorRefusedError(_REFUSAL)
    if not copies.is_dir() or not output.is_dir():
        raise DecryptorRefusedError(_REFUSAL)

    marker = _read_marker(work_area)
    source_text = marker.get("source")
    if not isinstance(source_text, str) or not source_text:
        raise DecryptorRefusedError(_REFUSAL)
    if Path(source_text).resolve() != work_area.source_dir.resolve():
        raise DecryptorRefusedError(_REFUSAL)

    raw_manifest = marker.get("files")
    if not isinstance(raw_manifest, dict) or not raw_manifest:
        raise DecryptorRefusedError(_REFUSAL)
    manifest: dict[str, str] = {}
    for relpath, sha256 in raw_manifest.items():
        if not _valid_relpath(relpath) or not hashing.is_valid_hash(sha256):
            raise DecryptorRefusedError(_REFUSAL)
        manifest[relpath] = sha256
    expected = {item.relpath: item.sha256 for item in work_area.files}
    if manifest != expected:
        raise DecryptorRefusedError(_REFUSAL)

    on_disk: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(
        copies, topdown=True, followlinks=False
    ):
        current = Path(dirpath)
        for name in dirnames:
            if (current / name).is_symlink():
                raise DecryptorRefusedError(_COPIES_REFUSAL)
        for name in filenames:
            full = current / name
            if full.is_symlink() or not full.is_file():
                raise DecryptorRefusedError(_COPIES_REFUSAL)
            relpath = full.relative_to(copies).as_posix()
            try:
                # A copy that shares its storage with another
                # file — a hard link — is not a copy at all: a
                # program writing to it would write through to
                # the file it is linked to, which may be the
                # original itself. Same contents are not enough;
                # the copy must stand on its own.
                if full.stat().st_nlink != 1:
                    raise DecryptorRefusedError(_COPIES_REFUSAL)
                on_disk[relpath] = hashing.hash_file(full)
            except OSError:
                raise DecryptorRefusedError(_COPIES_REFUSAL) from None
    if on_disk != manifest:
        raise DecryptorRefusedError(_COPIES_REFUSAL)

    # The recovered-files folder starts empty — preparation made
    # it that way — so anything already in it did not come from
    # this run and must not be counted as this run's work.
    try:
        output_occupied = any(output.iterdir())
    except OSError:
        raise DecryptorRefusedError(_OUTPUT_REFUSAL) from None
    if output_occupied:
        raise DecryptorRefusedError(_OUTPUT_REFUSAL)
    return manifest


def _hash_originals(
    source_dir: Path, manifest: dict, problems: list[str], *, before: bool
) -> dict:
    """Hash every manifest original, returning {relpath: hash|None}.

    None means the file is missing, unreadable, or a link — each
    counts as "not the file Lazarus copied". Problems found on the
    *before* pass are appended in plain language; the caller
    compares the two maps to say what the run itself changed.
    """
    current_map: dict[str, Optional[str]] = {}
    for relpath in sorted(manifest):
        expected = manifest[relpath]
        original = source_dir.joinpath(*Path(relpath).parts)
        try:
            current = (
                hashing.hash_file(original)
                if original.is_file() and not original.is_symlink()
                else None
            )
        except OSError:
            current = None
        current_map[relpath] = current
        if before and current is None:
            problems.append(
                f"Before the run, {_printable(relpath)} was already missing "
                "from the originals, so Lazarus could not check it."
            )
        elif before and current != expected:
            problems.append(
                f"Before the run, {_printable(relpath)} in the originals did "
                "not match the copy Lazarus made — something "
                "changed it outside Lazarus."
            )
    return current_map


def _count_outputs(output_dir: Path) -> tuple[int, int]:
    """Count regular files a run produced, and their total bytes."""
    count = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(
        output_dir, topdown=True, followlinks=False
    ):
        dirnames[:] = [
            name
            for name in dirnames
            if not (Path(dirpath) / name).is_symlink()
        ]
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            try:
                total += full.stat().st_size
            except OSError:
                continue
            count += 1
    return count, total


def run_decryptor(
    entry: DecryptorEntry,
    work_area: WorkArea,
    decryptor_path: Path,
    runner: DecryptorRunner,
) -> DecryptRunReport:
    """Run one published decryptor on the copies in ``work_area``.

    The work area is re-verified against its marker on disk first
    (see :func:`_verify_work_area`); on any disagreement the run is
    refused and the runner is never invoked. The decryptor
    program is fingerprinted before it runs, so the report can say
    exactly which program did the work. Originals are checked before
    and after, and the runner is invoked exactly once, with the
    copies folder as its input and the work area's output folder as
    its output.
    """
    manifest = _verify_work_area(work_area)
    source_dir = work_area.source_dir

    program = Path(decryptor_path)
    if not program.is_file():
        raise DecryptorError(
            f"Lazarus could not find the decryptor program at "
            f"{program}. Check the path and try again."
        )
    try:
        program_sha256 = hashing.hash_file(program)
    except OSError:
        raise DecryptorError(
            f"Lazarus could not read the decryptor program at "
            f"{program}. Check the file and try again."
        ) from None

    program_note: Optional[str] = None
    if entry.executable_name and (
        program.name.lower() != entry.executable_name.lower()
    ):
        program_note = (
            f"The program you gave is named {program.name}; the "
            f"publisher ships it as {entry.executable_name}. Lazarus "
            "ran the program you gave it — its fingerprint (SHA-256) "
            "is recorded in this report."
        )

    problems: list[str] = list(work_area.problems)
    before_map = _hash_originals(source_dir, manifest, problems, before=True)

    outcome = runner.run(program, work_area.copies_dir, work_area.output_dir)

    after_map = _hash_originals(
        source_dir, manifest, problems, before=False
    )
    changed_during = before_map != after_map
    already_different = any(
        before_map.get(relpath) != sha256
        for relpath, sha256 in manifest.items()
    )
    originals_untouched = not changed_during and not already_different
    files_produced, produced_bytes = _count_outputs(work_area.output_dir)

    if not outcome.ok:
        status = "failed"
    elif files_produced == 0:
        status = "attention"
    elif not originals_untouched:
        status = "attention"
    else:
        status = "complete"

    return DecryptRunReport(
        family=entry.family,
        publisher=entry.publisher,
        decryptor_name=entry.decryptor_name,
        program_path=str(program),
        program_sha256=program_sha256,
        program_note=program_note,
        files_copied=len(manifest),
        files_produced=files_produced,
        produced_bytes=produced_bytes,
        runner_message=outcome.message,
        originals_untouched=originals_untouched,
        problems=problems,
        status=status,
        output_dir=str(work_area.output_dir),
        originals_changed_during=changed_during,
        originals_already_different=already_different,
    )
