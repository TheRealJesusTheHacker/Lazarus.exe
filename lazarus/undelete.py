"""Undelete: finding deleted files, and saying honestly what can
come back.

The scan asks a filesystem parser what it can see — the records a
deletion left behind — and then this module weighs the evidence for
every file, in the open:

- **strong** — the record survived, and the filesystem says none of
  the places its data sat have been handed to anything else.
- **partial** — some of those places *have* been reused. What is
  left can still come back, and the report says how much is gone.
- **gone** — there is nothing honest left to recover: either every
  place its data sat has been reused, or the record of where it sat
  did not survive at all.

The verdicts are computed from the filesystem's own testimony —
which clusters it now counts as in use — never from hope, and never
rounded up. A stretch the filesystem *cannot* testify about counts
as at risk, not as safe. Recovery writes to a different drive than
the one being read, always: recovering onto the same drive could
overwrite the very files being looked for, so the engine refuses
before it writes a byte.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import filesystems
from .errors import RecoveryRefusedError, UndeleteError
from .hashing import hash_bytes
from .recovery_model import DeletedFile, FsScan
from .source import BlockReader, ReadOnlySource, device_id_of

VERDICT_STRONG = "strong"
VERDICT_PARTIAL = "partial"
VERDICT_GONE = "gone"

OUTCOME_RECOVERED = "recovered"
OUTCOME_PARTIAL = "partial"
OUTCOME_GONE = "gone"
OUTCOME_FAILED = "failed"
OUTCOME_NOT_FOUND = "not_found"


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


# ---------------------------------------------------------------------
# Weighing the evidence


@dataclass
class FileAssessment:
    """One deleted file, and what the evidence says about it."""

    candidate: DeletedFile
    verdict: str  # VERDICT_STRONG | VERDICT_PARTIAL | VERDICT_GONE
    surviving_bytes: int  # bytes the evidence says are still there
    missing_bytes: int  # bytes known overwritten, or unaccounted for
    summary: str  # the verdict in one plain sentence
    evidence: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.candidate.display_name

    @property
    def size(self) -> int:
        return self.candidate.size


def assess(candidate: DeletedFile) -> FileAssessment:
    """Weigh one deleted file's evidence. This is the only place a
    verdict is decided, and it may never claim more than the
    record in :mod:`lazarus.recovery_model` supports."""
    evidence = list(candidate.evidence)
    size = candidate.size

    if candidate.resident_data is not None:
        held = len(candidate.resident_data)
        if held >= size:
            return FileAssessment(
                candidate, VERDICT_STRONG, size, 0,
                "Its contents live inside its own surviving record, "
                "so it should come back whole.",
                evidence,
            )
        return FileAssessment(
            candidate, VERDICT_PARTIAL, held, size - held,
            "Only part of it survives inside its own record — "
            f"{_human_bytes(size - held)} of it is missing.",
            evidence,
        )

    if size == 0:
        return FileAssessment(
            candidate, VERDICT_STRONG, 0, 0,
            "It held nothing, so nothing was lost — it can be "
            "brought back as the empty file it was.",
            evidence,
        )

    if not candidate.extents:
        return FileAssessment(
            candidate, VERDICT_GONE, 0, size,
            "It is gone: the record of where its data sat did not "
            "survive, so there is nothing left to read.",
            evidence,
        )

    surviving = 0
    missing = 0
    unknown = 0
    accounted = 0
    for extent in candidate.extents:
        if accounted >= size:
            break
        take = min(extent.length, size - accounted)
        if take <= 0:
            continue
        accounted += take
        if extent.start == -1:
            # Never stored on the drive at all: it reads back as
            # zeros, which is exactly what it always was.
            surviving += take
        elif extent.in_use is True:
            missing += take
        elif extent.in_use is False:
            surviving += take
        else:
            unknown += take
    if accounted < size:
        missing += size - accounted
        evidence.append(
            "The surviving record accounts for only part of the "
            "file — the rest of it left no trace to follow."
        )
    if unknown:
        evidence.append(
            f"The drive could not say whether {_human_bytes(unknown)} "
            "of it has been reused — that part is counted as at "
            "risk, not as safe."
        )
        missing += unknown

    if missing == 0:
        verdict = VERDICT_STRONG
        summary = (
            "Its record survived, and the drive shows none of its "
            "data as reused — it should come back whole."
        )
    elif surviving == 0:
        verdict = VERDICT_GONE
        summary = (
            "It is gone: every place its data sat has been handed "
            "to other files. What is there now is theirs, not its."
        )
    else:
        verdict = VERDICT_PARTIAL
        summary = (
            f"Part of it survives: about {_human_bytes(surviving)} "
            f"of {_human_bytes(size)} has not been reused; the "
            "rest has been overwritten."
        )
    return FileAssessment(candidate, verdict, surviving, missing, summary, evidence)


# ---------------------------------------------------------------------
# Scanning


@dataclass
class ScanReport:
    """What a scan found, with the odds for every file."""

    source_description: str
    fs_type: str
    assessments: list[FileAssessment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def _count(self, verdict: str) -> int:
        return sum(1 for a in self.assessments if a.verdict == verdict)

    @property
    def strong_count(self) -> int:
        return self._count(VERDICT_STRONG)

    @property
    def partial_count(self) -> int:
        return self._count(VERDICT_PARTIAL)

    @property
    def gone_count(self) -> int:
        return self._count(VERDICT_GONE)

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        if not self.assessments:
            lines.append(
                f"No deleted files were found in {self.source_description}."
            )
            lines.append(
                "Either nothing was deleted from it, or what was "
                "deleted left no records behind to find."
            )
        else:
            lines.append(
                f"{len(self.assessments)} deleted file(s) found in "
                f"{self.source_description} ({self.fs_type}):"
            )
            for a in self.assessments:
                lines.append(f"  {a.name} — {_human_bytes(a.size)}")
                lines.append(f"    {a.summary}")
            lines.append(
                f"Odds: {self.strong_count} likely whole, "
                f"{self.partial_count} partly there, "
                f"{self.gone_count} gone."
            )
        for note in self.notes:
            lines.append(f"Note: {note}")
        return lines

    def to_text(self) -> str:
        return "\n".join(self.summary_lines())

    def to_dict(self) -> dict:
        return {
            "source": self.source_description,
            "filesystem": self.fs_type,
            "files": [
                {
                    "name": a.name,
                    "size": a.size,
                    "verdict": a.verdict,
                    "surviving_bytes": a.surviving_bytes,
                    "missing_bytes": a.missing_bytes,
                    "summary": a.summary,
                    "evidence": a.evidence,
                }
                for a in self.assessments
            ],
            "notes": self.notes,
        }


def _open_source(source) -> tuple[BlockReader, str, bool]:
    """(reader, description, owned) — a path becomes a ReadOnlySource
    that the caller must close; a reader is used as it stands."""
    if isinstance(source, (str, Path)):
        opened = ReadOnlySource(source)
        return opened, str(opened.path), True
    return source, getattr(source, "path", "the drive"), False


def scan_for_deleted(source) -> ScanReport:
    """Scan a drive or image for deleted files, with honest odds.

    ``source`` is a path to a drive or image file, or any
    :class:`~lazarus.source.BlockReader`. The source is only read.
    """
    reader, description, owned = _open_source(source)
    try:
        parser = filesystems.require_filesystem(reader)
        scan: FsScan = parser.scan(reader)
    finally:
        if owned:
            reader.close()
    assessments = [assess(candidate) for candidate in scan.files]
    fs_type = scan.files[0].fs_type if scan.files else parser.FS_FAMILY
    return ScanReport(
        source_description=description,
        fs_type=fs_type,
        assessments=assessments,
        notes=list(scan.notes),
    )


# ---------------------------------------------------------------------
# Recovery


@dataclass
class RecoveryItem:
    name: str  # the deleted file's name, as found
    verdict: str
    outcome: str  # OUTCOME_*
    written_to: Optional[str] = None  # path inside the destination
    bytes_written: int = 0
    missing_bytes: int = 0
    sha256: Optional[str] = None  # of what was written
    message: str = ""


@dataclass
class RecoveryReport:
    """What a recovery brought back, file by file."""

    source_description: str
    fs_type: str
    destination: str
    items: list[RecoveryItem] = field(default_factory=list)

    def _count(self, outcome: str) -> int:
        return sum(1 for i in self.items if i.outcome == outcome)

    @property
    def recovered_count(self) -> int:
        return self._count(OUTCOME_RECOVERED)

    @property
    def partial_count(self) -> int:
        return self._count(OUTCOME_PARTIAL)

    @property
    def gone_count(self) -> int:
        return self._count(OUTCOME_GONE)

    @property
    def failed_count(self) -> int:
        return self._count(OUTCOME_FAILED) + self._count(OUTCOME_NOT_FOUND)

    @property
    def status(self) -> str:
        """complete | partial | failed — the whole recovery in one word."""
        if self.recovered_count and not (
            self.partial_count or self.gone_count or self.failed_count
        ):
            return "complete"
        if self.recovered_count or self.partial_count:
            return "partial"
        return "failed"

    def summary_lines(self) -> list[str]:
        lines: list[str] = [f"Recovery from {self.source_description}:"]
        for item in self.items:
            if item.outcome == OUTCOME_RECOVERED:
                lines.append(f"  {item.name}: back whole, as {item.written_to}.")
            elif item.outcome == OUTCOME_PARTIAL:
                lines.append(
                    f"  {item.name}: partly back, as {item.written_to} — "
                    f"{_human_bytes(item.missing_bytes)} of it could not "
                    "be recovered and is zeros in the copy."
                )
            elif item.outcome == OUTCOME_GONE:
                lines.append(f"  {item.name}: gone. {item.message}")
            elif item.outcome == OUTCOME_NOT_FOUND:
                lines.append(f"  {item.name}: not among the deleted files found.")
            else:
                lines.append(f"  {item.name}: could not be recovered. {item.message}")
        if self.status == "complete":
            lines.append("Everything asked for came back whole.")
        elif self.status == "partial":
            lines.append(
                "Some of what was asked for came back. The rest is "
                "listed above, in plain terms."
            )
        else:
            lines.append("Nothing asked for could be brought back.")
        return lines

    def to_text(self) -> str:
        return "\n".join(self.summary_lines())

    def to_dict(self) -> dict:
        return {
            "source": self.source_description,
            "filesystem": self.fs_type,
            "destination": self.destination,
            "status": self.status,
            "recovered": self.recovered_count,
            "partial": self.partial_count,
            "gone": self.gone_count,
            "failed": self.failed_count,
            "items": [
                {
                    "name": i.name,
                    "verdict": i.verdict,
                    "outcome": i.outcome,
                    "written_to": i.written_to,
                    "bytes_written": i.bytes_written,
                    "missing_bytes": i.missing_bytes,
                    "sha256": i.sha256,
                    "message": i.message,
                }
                for i in self.items
            ],
        }


_UNSAFE_CHARS = set('/\\\x00')


def _safe_output_name(raw: str, taken: set[str]) -> str:
    """A name from a drive's records is data, not an instruction:
    it is flattened to a single safe file name before it is ever
    joined to the destination folder, and made unique against
    everything already written or already there."""
    cleaned = "".join(
        "_" if (ch in _UNSAFE_CHARS or ord(ch) < 32) else ch for ch in raw
    ).strip()
    if cleaned in ("", ".", ".."):
        cleaned = "recovered-file"
    stem, dot, ext = cleaned.rpartition(".")
    candidate = cleaned
    n = 2
    while candidate.lower() in taken:
        if dot and stem:
            candidate = f"{stem} ({n}){dot}{ext}"
        else:
            candidate = f"{cleaned} ({n})"
        n += 1
    taken.add(candidate.lower())
    return candidate


def _destination_device(destination: Path, source_device: int) -> int:
    """The device the destination writes would land on — checking
    the folder itself, or the nearest folder above it that exists."""
    probe = destination
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    return device_id_of(probe)


def _read_extent(reader: BlockReader, start: int, length: int) -> bytes:
    """Read one extent, returning what arrived (possibly short)."""
    if length <= 0:
        return b""
    try:
        return reader.read_at(start, length)
    except Exception:
        return b""


def _recover_one(reader: BlockReader, assessment: FileAssessment, out_path: Path) -> RecoveryItem:
    candidate = assessment.candidate
    size = candidate.size
    written = bytearray()
    missing = 0

    if candidate.resident_data is not None:
        data = candidate.resident_data[:size]
        missing += size - len(data)
        written.extend(data)
        written.extend(b"\x00" * (size - len(written)))
    else:
        accounted = 0
        for extent in candidate.extents:
            if accounted >= size:
                break
            take = min(extent.length, size - accounted)
            if take <= 0:
                continue
            accounted += take
            if extent.start == -1:
                # Never stored on the drive: zeros are its true
                # contents, exactly as assess() weighed them.
                written.extend(b"\x00" * take)
                continue
            if extent.in_use is True:
                missing += take
                written.extend(b"\x00" * take)
                continue
            data = _read_extent(reader, extent.start, take)
            if len(data) < take:
                missing += take - len(data)
                data = data + b"\x00" * (take - len(data))
            written.extend(data)
        if accounted < size:
            missing += size - accounted
            written.extend(b"\x00" * (size - accounted))

    try:
        with open(out_path, "wb") as handle:
            handle.write(bytes(written))
    except OSError as error:
        return RecoveryItem(
            name=assessment.name,
            verdict=assessment.verdict,
            outcome=OUTCOME_FAILED,
            missing_bytes=assessment.missing_bytes,
            message=f"Writing it out failed: {error.strerror or 'the folder could not be written to'}.",
        )
    outcome = OUTCOME_RECOVERED if missing == 0 else OUTCOME_PARTIAL
    return RecoveryItem(
        name=assessment.name,
        verdict=assessment.verdict,
        outcome=outcome,
        written_to=out_path.name,
        bytes_written=len(written),
        missing_bytes=missing,
        sha256=hash_bytes(bytes(written)),
        message="" if missing == 0 else assessment.summary,
    )


def recover_deleted(source, destination, names=None) -> RecoveryReport:
    """Recover deleted files from a drive or image into a folder
    on a DIFFERENT drive.

    ``names``: recover only these files (matched against the names
    the scan shows, with or without their folder). Without it,
    every deleted file the scan finds is recovered — including the
    ones whose odds are partial; the gone ones are reported, never
    faked. Raises :class:`RecoveryRefusedError` before writing
    anything when the destination is on the same device as the
    source.
    """
    reader, description, owned = _open_source(source)
    destination = Path(destination)
    try:
        if isinstance(source, (str, Path)):
            source_device = reader.device_id
            if destination.resolve() == Path(source).resolve():
                raise RecoveryRefusedError(
                    "That destination is the source itself. Recovered "
                    "files are always written somewhere else — writing "
                    "them back could overwrite what is being recovered."
                )
        else:
            source_device = getattr(reader, "device_id", None)
        if source_device is not None:
            dest_device = _destination_device(destination, source_device)
            if dest_device == source_device:
                raise RecoveryRefusedError(
                    "The folder you chose is on the same drive as the "
                    "one being recovered from. Writing recovered files "
                    "there could overwrite other deleted files before "
                    "they can be read. Choose a folder on a different "
                    "drive."
                )

        parser = filesystems.require_filesystem(reader)
        scan = parser.scan(reader)
        if not scan.files:
            raise UndeleteError(
                "No deleted files were found in this drive or image, "
                "so there is nothing to recover."
            )
        assessments = [assess(c) for c in scan.files]

        wanted = None
        if names:
            wanted = {n.replace("\\", "/").strip("/") for n in names}
        chosen: list[FileAssessment] = []
        missing_names: list[str] = []
        if wanted is None:
            chosen = assessments
        else:
            by_name: dict[str, FileAssessment] = {}
            for a in assessments:
                by_name.setdefault(a.candidate.display_name, a)
                by_name.setdefault(a.candidate.name, a)
            seen: set[int] = set()
            for name in names:
                key = name.replace("\\", "/").strip("/")
                match = by_name.get(key)
                if match is None:
                    missing_names.append(name)
                elif id(match) not in seen:
                    seen.add(id(match))
                    chosen.append(match)

        if destination.exists() and not destination.is_dir():
            raise RecoveryRefusedError(
                f"'{destination}' is a file, not a folder. Recovered "
                "files need a folder to land in — pick one on a "
                "different drive."
            )
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RecoveryRefusedError(
                "The folder for the recovered files could not be "
                f"made at '{destination}': "
                f"{error.strerror or 'it could not be created'}. "
                "Pick a folder Lazarus can write to, on a different "
                "drive."
            ) from error
        taken = {p.name.lower() for p in destination.iterdir()}
        report = RecoveryReport(
            source_description=description,
            fs_type=scan.files[0].fs_type,
            destination=str(destination),
        )
        for a in chosen:
            if a.verdict == VERDICT_GONE:
                report.items.append(
                    RecoveryItem(
                        name=a.name, verdict=a.verdict,
                        outcome=OUTCOME_GONE, message=a.summary,
                    )
                )
                continue
            out_name = _safe_output_name(a.candidate.name, taken)
            report.items.append(_recover_one(reader, a, destination / out_name))
        for name in missing_names:
            report.items.append(
                RecoveryItem(
                    name=name, verdict="", outcome=OUTCOME_NOT_FOUND,
                    message="It was not among the deleted files the scan found.",
                )
            )
        return report
    finally:
        if owned:
            reader.close()
