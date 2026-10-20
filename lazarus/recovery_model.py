"""What a filesystem scan finds, in one shared shape.

Every filesystem parser — FAT, NTFS, and whichever joins later —
hands its deleted files back as :class:`DeletedFile` records built
from these pieces, so the odds engine and the reports never need to
know which filesystem a file came from. The record carries the
*evidence*, not a verdict: where the file's data sat, which of those
places the filesystem says are back in use, and the parser's own
plain-language notes. Deciding what the evidence means is the odds
engine's job (see :mod:`lazarus.undelete`), and it may never claim
more than this record supports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Extent:
    """One stretch of the source where a deleted file's data sat.

    ``in_use`` is the filesystem's own testimony: True means those
    bytes have been handed to something else since the deletion —
    the strongest evidence there is that this stretch is overwritten.
    False means the filesystem says the stretch is still free, which
    is evidence (never proof) that the old bytes survive. None means
    the filesystem could not say, and the odds engine treats unknown
    accordingly: it never calls a file whole on unknown evidence.
    """

    start: int  # byte offset within the source; -1 = never stored
    # (a sparse stretch: it reads back as zeros, exactly as recorded)
    length: int  # bytes
    in_use: Optional[bool]


@dataclass
class DeletedFile:
    """A deleted file a parser found, and the evidence about it."""

    name: str  # best-effort name (see parser notes for lost letters)
    fs_type: str  # "FAT12" | "FAT16" | "FAT32" | "NTFS"
    size: int  # the size recorded when the file was deleted
    extents: list[Extent] = field(default_factory=list)
    resident_data: Optional[bytes] = None  # data living in the record itself
    evidence: list[str] = field(default_factory=list)  # plain language
    directory: str = ""  # folder it lived in, best effort; "" if unknown
    modified_utc: Optional[str] = None  # as recorded on the drive

    @property
    def display_name(self) -> str:
        if self.directory:
            return f"{self.directory}/{self.name}"
        return self.name


@dataclass
class FsScan:
    """Everything one parser found in one source."""

    files: list[DeletedFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # damage, skips, limits
