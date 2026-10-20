"""What Phase 3 works with, in one shared shape.

Three jobs share these records: opening a locked volume with the
owner's own key (:mod:`lazarus.unlock`), telling which ransomware
family locked a set of files (:mod:`lazarus.decryptors`), and
running a published decryptor against copies
(:mod:`lazarus.decrypt_run`). The records carry *evidence* — what a
volume says about itself, what signs a folder shows — and the
engines decide what the evidence honestly supports. Nothing in
this module keeps a key: a volume record describes a lock, never
the thing that opens it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VolumeInfo:
    """One volume, as the platform itself describes it.

    ``kind`` is the encryption the platform reports — "bitlocker"
    is the only kind this version of Lazarus can open, and only
    with the owner's own key. "unknown" means the platform could
    not say, and Lazarus treats unknown as a stop, never a guess.
    ``locked`` is None when the platform cannot say either way.
    """

    ref: str  # how the owner points at it: a drive letter or a path
    kind: str  # "bitlocker" | "unknown" | whatever else the OS reports
    label: str = ""
    locked: Optional[bool] = None
    evidence: list[str] = field(default_factory=list)  # plain language


@dataclass
class DecryptorEntry:
    """One published decryptor, from the maintained list.

    Every field a family match or a decryptor run relies on comes
    from the publisher's own page, checked on ``verified_utc``.
    ``extensions`` are lowercase with the leading dot;
    ``note_filenames`` are the exact ransom-note names the family
    leaves (matched without regard to case); ``note_patterns`` are
    filename patterns in the same spirit, and count for less — a
    pattern many families share is weaker evidence than a name
    only one family uses.
    """

    family: str
    publisher: str
    decryptor_name: str
    source_url: str  # where this entry was verified — the publisher's page
    publisher_url: str
    credit: str  # the publisher's terms and credit, in plain language
    verified_utc: str  # the day this entry was last checked at its source
    aliases: list[str] = field(default_factory=list)
    executable_name: Optional[str] = None  # the file the publisher ships
    extensions: list[str] = field(default_factory=list)
    note_filenames: list[str] = field(default_factory=list)
    note_patterns: list[str] = field(default_factory=list)
    conditions: str = ""  # the honest limits, in plain language


@dataclass
class FamilyMatch:
    """One family a folder's signs point to, and how strongly.

    ``confidence`` is "looks like" (the family's own ransom note,
    or the note together with its extensions) or "may be" (weaker
    signs only). Lazarus never prints "confirmed" from outward
    signs: only the family's decryptor itself can prove a match,
    by opening the files.
    """

    entry: DecryptorEntry
    confidence: str  # "looks like" | "may be"
    evidence: list[str] = field(default_factory=list)  # plain language
