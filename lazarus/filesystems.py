"""The filesystems Lazarus can read, and how it tells them apart.

Phase 2 reads two families: **FAT** (FAT12, FAT16, FAT32 — the
cards and sticks and older drives) and **NTFS** (the Windows system
drives). Each family has a parser module with the same small shape —
``detect(source)`` answers "is this mine?" from the boot sector
alone, and ``scan(source)`` returns what it found as
:mod:`lazarus.recovery_model` records. Anything else is refused in
plain language, never guessed at: reading a filesystem wrong is
worse than not reading it.
"""

from __future__ import annotations

from . import fs_fat, fs_ntfs
from .errors import UnsupportedFilesystemError
from .source import BlockReader

# NTFS first: its boot sector carries an explicit maker's mark,
# the strongest signal there is. FAT's is structural.
PARSERS = (fs_ntfs, fs_fat)

READABLE = "FAT drives (FAT12, FAT16, FAT32) and NTFS drives"


def detect_filesystem(source: BlockReader):
    """The parser for this source, or None if it is not a kind
    Lazarus can read."""
    for parser in PARSERS:
        if parser.detect(source):
            return parser
    return None


def require_filesystem(source: BlockReader):
    """The parser for this source, or a plain refusal."""
    parser = detect_filesystem(source)
    if parser is None:
        raise UnsupportedFilesystemError(
            "Lazarus cannot read this kind of drive yet — it does "
            f"not recognise the filesystem in it. Lazarus reads {READABLE}. "
            "If this is a failing drive, rescue it to an image first "
            "so the original stays untouched while you decide what "
            "to try next."
        )
    return parser
