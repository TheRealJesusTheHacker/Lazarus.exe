"""The maintained decryptor list, and telling families apart.

Phase 3 has honest limits, and this module is where they start.
Lazarus does not keep its own copies of other publishers'
decryptors, and it does not guess at keys. What it keeps is a
short, hand-checked list of the decryptors that are genuinely
published — who made each one, where it lives, what it can and
cannot do, and the day the entry was last checked at its source.
That list is trusted data, so it is loaded strictly: a hand-edited
or damaged list stops with a plain explanation rather than
half-loading and quietly steering Lazarus wrong.

The other half of the module reads a folder's outward signs —
which extensions its files carry and whether a ransom note is
among them — and says which families those signs point to, and
how strongly. Outward signs can make a family *look like* the
answer, or only a *maybe*. They can never confirm one: only the
family's own decryptor proves a match, by opening the files.
"""

from __future__ import annotations

import datetime
import fnmatch
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .decrypt_model import DecryptorEntry, FamilyMatch
from .errors import DecryptorError, RegistryError

__all__ = [
    "Identification",
    "default_registry_path",
    "find_entry",
    "identify_family",
    "load_registry",
]

# The fields a registry entry may carry — exactly the fields of
# DecryptorEntry. Anything else in an entry is a mistake or a
# plant, and the list is refused rather than quietly patched up.
_ALLOWED_KEYS = frozenset(
    {
        "family",
        "publisher",
        "decryptor_name",
        "source_url",
        "publisher_url",
        "credit",
        "verified_utc",
        "aliases",
        "executable_name",
        "extensions",
        "note_filenames",
        "note_patterns",
        "conditions",
    }
)

_REQUIRED_STRING_FIELDS = (
    "family",
    "publisher",
    "decryptor_name",
    "source_url",
    "publisher_url",
    "credit",
    "verified_utc",
)

_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Size limits on the list itself. The shipped list is short and
# hand-kept; a list far past these sizes is not a bigger list, it
# is a damaged or planted one, and it is refused rather than read
# for minutes.
_MAX_ENTRIES = 500
_MAX_LIST_SIZES = {
    "aliases": 50,
    "extensions": 200,
    "note_filenames": 100,
    "note_patterns": 100,
}
# …and on each string in it. A family name is a name, not a page.
_MAX_NAME_LENGTH = 200
_MAX_ITEM_LENGTH = 260
_MAX_TEXT_LENGTH = 2000
_MAX_URL_LENGTH = 2048


# Characters that steer a terminal or a reader rather than print:
# direction overrides, line/paragraph separators, zero-width marks,
# and the byte-order mark. Invisible in most displays, active in
# all of them — they have no place in a name Lazarus prints.
_FORMAT_CHARACTERS = frozenset(
    "\u200b\u200c\u200d\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2060\ufeff"
)


def _is_unprintable(char: str) -> bool:
    code = ord(char)
    return (
        code < 0x20
        or 0x7F <= code <= 0x9F
        or code
        in (
            0x00AD,  # soft hyphen
            0x034F,  # combining grapheme joiner
            0x061C,  # Arabic letter mark
            0x180E,  # Mongolian vowel separator
            0x2028,  # line separator
            0x2029,  # paragraph separator
            0xFFF9,  # interlinear annotation anchor
            0xE0001,  # language tag
        )
        or 0x2061 <= code <= 0x2064  # invisible operators
        or 0xE0020 <= code <= 0xE007F  # tag characters
        or char in _FORMAT_CHARACTERS
    )


def _has_control_characters(text: str) -> bool:
    """True if ``text`` holds characters that must never be printed.

    Registry strings are shown to the owner as-is — in lists, in
    reports, on the command line. A string carrying control or
    format characters (a newline smuggled into a family name, a
    direction override flipping a sentence around) could fake the
    shape of Lazarus's own output, so none are allowed.
    """
    return any(_is_unprintable(char) for char in text)


def _printable(text: str) -> str:
    """``text`` with every unprintable character replaced by ``?``.

    Used for names that come from a disk, not from the registry:
    a file's name is whatever its author chose, and it is shown
    as evidence — shown safely, never able to steer the terminal
    or fake a line of Lazarus's own output.
    """
    return "".join("?" if _is_unprintable(char) else char for char in text)


def default_registry_path() -> Path:
    """Where the decryptor list Lazarus ships with lives."""
    return Path(__file__).with_name("decryptors_registry.json")


def load_registry(
    path: Optional[Union[str, Path]] = None,
) -> list[DecryptorEntry]:
    """Load and strictly check the maintained decryptor list.

    ``path`` defaults to the list shipped with Lazarus. Every
    problem — a missing file, unreadable or damaged contents, a
    field that does not hold up — raises :class:`RegistryError`
    in plain language naming what is at fault. The list either
    loads whole and trustworthy, or not at all.
    """
    registry_path = Path(path) if path is not None else default_registry_path()
    try:
        raw_text = registry_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        raise RegistryError(
            "The decryptor list could not be read. It may be missing "
            "or damaged, so Lazarus cannot say which decryptors are "
            "available. Restore the list that came with Lazarus and "
            "try again."
        ) from None
    try:
        data = json.loads(raw_text)
    except ValueError:
        raise RegistryError(
            "The decryptor list is damaged and cannot be read as a "
            "list at all, so Lazarus cannot say which decryptors are "
            "available. Restore the list that came with Lazarus and "
            "try again."
        ) from None

    if not isinstance(data, dict):
        raise RegistryError(
            "The decryptor list is not in the shape Lazarus expects: "
            "it should be a record holding the list of entries. "
            "Restore the list that came with Lazarus and try again."
        )
    if "entries" not in data:
        raise RegistryError(
            "The decryptor list does not hold its entries: the "
            "'entries' part is missing. Restore the list that came "
            "with Lazarus and try again."
        )
    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise RegistryError(
            "The decryptor list does not hold its entries as a list. "
            "Restore the list that came with Lazarus and try again."
        )

    if len(raw_entries) > _MAX_ENTRIES:
        raise RegistryError(
            f"The decryptor list holds {len(raw_entries)} entries — "
            "far more than the short, hand-kept list Lazarus ships. "
            "It cannot be trusted as it stands. Restore the list "
            "that came with Lazarus and try again."
        )

    entries: list[DecryptorEntry] = []
    seen_families: dict[str, str] = {}
    for index, raw_entry in enumerate(raw_entries):
        entry = _load_entry(raw_entry, index)
        family_key = entry.family.casefold()
        if family_key in seen_families:
            raise RegistryError(
                f"The decryptor list names the family "
                f"'{entry.family}' more than once. Each family may "
                f"appear only once, so the list cannot be trusted "
                f"as it stands."
            )
        seen_families[family_key] = entry.family
        entries.append(entry)

    # A family's other names get the same protection as the names
    # themselves: an alias that repeats a family name (its own or
    # another's) or another alias would steer a lookup to the
    # wrong entry, so the whole list is refused.
    seen_aliases: dict[str, str] = {}
    for entry in entries:
        for alias in entry.aliases:
            alias_key = alias.strip().casefold()
            if not alias_key:
                raise RegistryError(
                    f"The decryptor list gives the family "
                    f"'{entry.family}' an empty other-name. The "
                    f"list cannot be trusted as it stands."
                )
            if alias_key in seen_families:
                raise RegistryError(
                    f"The decryptor list gives '{alias}' as another "
                    f"name for '{entry.family}', but that name "
                    f"already belongs to a family on the list. The "
                    f"list cannot be trusted as it stands."
                )
            if alias_key in seen_aliases:
                raise RegistryError(
                    f"The decryptor list gives '{alias}' as another "
                    f"name for both '{seen_aliases[alias_key]}' and "
                    f"'{entry.family}'. The list cannot be trusted "
                    f"as it stands."
                )
            seen_aliases[alias_key] = entry.family
    return entries


def _load_entry(raw_entry: object, index: int) -> DecryptorEntry:
    """Check one raw registry entry and build its record."""
    where = f"entry {index + 1}"
    if not isinstance(raw_entry, dict):
        raise RegistryError(
            f"The decryptor list is not in the shape Lazarus expects: "
            f"{where} is not a record. Restore the list that came "
            f"with Lazarus and try again."
        )

    raw_family = raw_entry.get("family")
    if isinstance(raw_family, str) and raw_family.strip():
        where = f"the entry for '{raw_family.strip()}'"

    for key in raw_entry:
        if key not in _ALLOWED_KEYS:
            raise RegistryError(
                f"The decryptor list has a part Lazarus does not "
                f"recognise in {where}: '{key}'. The list may have "
                f"been changed by hand, so it cannot be trusted as "
                f"it stands."
            )

    values: dict[str, str] = {}
    for field_name in _REQUIRED_STRING_FIELDS:
        value = raw_entry.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise RegistryError(
                f"The decryptor list is missing something it needs "
                f"in {where}: the '{field_name}' part is empty or "
                f"not there. The list cannot be trusted as it stands."
            )
        values[field_name] = value

    # Names are compared exactly — for duplicates, for collisions,
    # for lookups — so they are stored exactly as they will be
    # compared: no padding. A name padded with spaces would
    # otherwise slip past the duplicate checks and then be
    # impossible to look up, or worse, leave a lookup to land on
    # another family's other-name.
    values["family"] = values["family"].strip()
    for field_name, value in values.items():
        if _has_control_characters(value):
            raise RegistryError(
                f"The decryptor list gives the '{field_name}' part "
                f"in {where} with characters Lazarus will not "
                f"print. The list cannot be trusted as it stands."
            )
    for field_name in ("family", "publisher", "decryptor_name"):
        if len(values[field_name]) > _MAX_NAME_LENGTH:
            raise RegistryError(
                f"The decryptor list gives the '{field_name}' part "
                f"in {where} at a length no real name reaches. "
                f"The list cannot be trusted as it stands."
            )
    if len(values["credit"]) > _MAX_TEXT_LENGTH:
        raise RegistryError(
            f"The decryptor list gives the credit in {where} at a "
            f"length no credit reaches. The list cannot be "
            f"trusted as it stands."
        )

    for url_field in ("source_url", "publisher_url"):
        url = values[url_field]
        parsed = urllib.parse.urlparse(url)
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or "." not in parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port == -1
            or len(url) > _MAX_URL_LENGTH
            or any(char.isspace() for char in url)
        ):
            raise RegistryError(
                f"The decryptor list gives an address in {where} "
                f"that Lazarus will not trust: the '{url_field}' "
                f"part must be a secure web address beginning with "
                f"'https://', naming the site it points at."
            )

    verified = values["verified_utc"]
    if not _DATE_SHAPE.match(verified):
        raise RegistryError(
            f"The decryptor list gives a checked-on day in {where} "
            f"that is not a day Lazarus can read: '{verified}'. It "
            f"should be written as year-month-day, like 2026-10-20."
        )
    try:
        datetime.date.fromisoformat(verified)
    except ValueError:
        raise RegistryError(
            f"The decryptor list gives a checked-on day in {where} "
            f"that is not a real day: '{verified}'. Check the day "
            f"and correct the list."
        ) from None

    aliases = [name.strip() for name in _string_list(raw_entry, "aliases", where)]
    extensions = [
        name.strip() for name in _string_list(raw_entry, "extensions", where)
    ]
    for list_name, names in (
        ("aliases", aliases),
        ("extensions", extensions),
    ):
        if len(names) > _MAX_LIST_SIZES[list_name]:
            raise RegistryError(
                f"The decryptor list gives {where} more "
                f"'{list_name}' than the list Lazarus ships ever "
                f"carries. The list cannot be trusted as it stands."
            )
        for name in names:
            if len(name) > _MAX_ITEM_LENGTH or _has_control_characters(name):
                raise RegistryError(
                    f"The decryptor list gives a name under "
                    f"'{list_name}' in {where} that Lazarus will "
                    f"not print. The list cannot be trusted as it "
                    f"stands."
                )
    normalised_extensions: list[str] = []
    for extension in extensions:
        lowered = extension.lower()
        if not lowered.startswith(".") or len(lowered) <= 1:
            raise RegistryError(
                f"The decryptor list gives a file extension in "
                f"{where} that is not an extension: '{extension}'. "
                f"Extensions begin with a dot, like '.djvu'."
            )
        normalised_extensions.append(lowered)

    note_filenames = [
        name.strip() for name in _string_list(raw_entry, "note_filenames", where)
    ]
    note_patterns = [
        name.strip() for name in _string_list(raw_entry, "note_patterns", where)
    ]
    for list_name, names in (
        ("note_filenames", note_filenames),
        ("note_patterns", note_patterns),
    ):
        if len(names) > _MAX_LIST_SIZES[list_name]:
            raise RegistryError(
                f"The decryptor list gives {where} more "
                f"'{list_name}' than the list Lazarus ships ever "
                f"carries. The list cannot be trusted as it stands."
            )
        for name in names:
            if len(name) > _MAX_ITEM_LENGTH or _has_control_characters(name):
                raise RegistryError(
                    f"The decryptor list gives a ransom-note name "
                    f"in {where}, under '{list_name}', that Lazarus "
                    f"will not print. The list cannot be trusted "
                    f"as it stands."
                )
            if "/" in name or "\\" in name:
                raise RegistryError(
                    f"The decryptor list gives a ransom-note name in "
                    f"{where}, under '{list_name}', that is not a "
                    f"plain file name: '{name}'. A note name is a "
                    f"file name only — it must never point somewhere "
                    f"else."
                )

    executable_name = raw_entry.get("executable_name")
    if executable_name is not None:
        if (
            not isinstance(executable_name, str)
            or not executable_name.strip()
            or executable_name.strip() in (".", "..")
            or "/" in executable_name
            or "\\" in executable_name
            or len(executable_name) > _MAX_ITEM_LENGTH
            or _has_control_characters(executable_name)
        ):
            raise RegistryError(
                f"The decryptor list gives a tool file name in "
                f"{where} that is not a plain file name. A tool "
                f"name is a file name only — it must never point "
                f"somewhere else."
            )
        executable_name = executable_name.strip()

    conditions = raw_entry.get("conditions", "")
    if not isinstance(conditions, str):
        raise RegistryError(
            f"The decryptor list gives the limits in {where} in a "
            f"shape Lazarus cannot read: the 'conditions' part "
            f"should be plain text."
        )
    if _has_control_characters(conditions):
        raise RegistryError(
            f"The decryptor list gives the limits in {where} with "
            f"characters Lazarus will not print. The list cannot "
            f"be trusted as it stands."
        )
    if len(conditions) > _MAX_TEXT_LENGTH:
        raise RegistryError(
            f"The decryptor list gives the limits in {where} at a "
            f"length no real limits reach. The list cannot be "
            f"trusted as it stands."
        )

    return DecryptorEntry(
        family=values["family"],
        publisher=values["publisher"],
        decryptor_name=values["decryptor_name"],
        source_url=values["source_url"],
        publisher_url=values["publisher_url"],
        credit=values["credit"],
        verified_utc=values["verified_utc"],
        aliases=aliases,
        executable_name=executable_name,
        extensions=normalised_extensions,
        note_filenames=note_filenames,
        note_patterns=note_patterns,
        conditions=conditions,
    )


def _string_list(
    raw_entry: dict, field_name: str, where: str
) -> list[str]:
    """Read one entry field that must be a list of strings."""
    value = raw_entry.get(field_name, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise RegistryError(
            f"The decryptor list gives the '{field_name}' part in "
            f"{where} in a shape Lazarus cannot read: it should be "
            f"a list of names."
        )
    return list(value)


def find_entry(
    registry: list[DecryptorEntry], family_name: str
) -> Optional[DecryptorEntry]:
    """Find one entry by family name or by any name it also goes by.

    The match does not depend on capital letters, so "REvil",
    "revil", and "Sodinokibi" all find the same entry.
    """
    if not isinstance(family_name, str):
        return None
    wanted = family_name.strip().casefold()
    if not wanted:
        return None
    for entry in registry:
        if entry.family.casefold() == wanted:
            return entry
        for alias in entry.aliases:
            if alias.casefold() == wanted:
                return entry
    return None


@dataclass
class Identification:
    """What a folder's outward signs say, gathered in one place.

    ``matches`` holds every family the signs point to, strongest
    first. ``files_seen`` is how many files Lazarus actually
    looked at. ``notes`` holds plain-language notes about the
    look itself — for example, that only part of a large folder
    was looked at.
    """

    matches: list[FamilyMatch] = field(default_factory=list)
    files_seen: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class _Signs:
    """The signs pointing to one family, before they are weighed."""

    entry: DecryptorEntry
    extension_hits: dict[str, int] = field(default_factory=dict)
    exact_notes: list[str] = field(default_factory=list)
    pattern_notes: list[str] = field(default_factory=list)


def _extension_of(name: str) -> Optional[str]:
    """A file name's extension: after the last dot, dot included."""
    dot = name.rfind(".")
    if dot < 0:
        return None
    extension = name[dot:].lower()
    if len(extension) <= 1:
        return None
    return extension


def identify_family(
    folder: Union[str, Path],
    registry: list[DecryptorEntry],
    *,
    max_files: int = 20000,
) -> Identification:
    """Weigh a folder's outward signs against each known family.

    The folder is read, never changed. A family's own ransom note,
    or its note together with its extensions, makes it *look
    like* the answer; extensions alone or a note-shaped name
    alone make it only a *maybe*. Lazarus never calls a match
    confirmed from signs like these.
    """
    root = Path(folder)
    if not root.is_dir():
        raise DecryptorError(
            "Lazarus could not look for the signs of a ransomware "
            "family, because that folder is not there or is not a "
            "folder. Check the folder and try again."
        )

    signs: list[_Signs] = [_Signs(entry=entry) for entry in registry]
    # Pre-fold the note names and patterns once, so each file is
    # matched the same way against every family.
    exact_names: list[set[str]] = [
        {name.casefold() for name in entry.note_filenames}
        for entry in registry
    ]
    patterns: list[list[str]] = [
        [pattern.casefold() for pattern in entry.note_patterns]
        for entry in registry
    ]
    extensions_by_entry: list[set[str]] = [
        set(entry.extensions) for entry in registry
    ]
    exact_seen: list[set[str]] = [set() for _ in registry]
    pattern_seen: list[set[str]] = [set() for _ in registry]

    result = Identification()
    cut_short = False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Never follow a linked folder elsewhere: the signs
        # Lazarus weighs are the ones inside the folder asked for.
        dirnames[:] = sorted(
            (
                name
                for name in dirnames
                if not os.path.islink(os.path.join(dirpath, name))
            ),
            key=str.casefold,
        )
        for name in sorted(filenames, key=str.casefold):
            full_path = os.path.join(dirpath, name)
            if os.path.islink(full_path) or not os.path.isfile(full_path):
                continue
            if result.files_seen >= max_files:
                cut_short = True
                break
            result.files_seen += 1

            extension = _extension_of(name)
            folded = name.casefold()
            for position, entry_signs in enumerate(signs):
                if (
                    extension is not None
                    and extension in extensions_by_entry[position]
                ):
                    entry_signs.extension_hits[extension] = (
                        entry_signs.extension_hits.get(extension, 0) + 1
                    )
                if folded in exact_names[position]:
                    if folded not in exact_seen[position]:
                        exact_seen[position].add(folded)
                        entry_signs.exact_notes.append(name)
                    # A name already counted as the family's own
                    # note is not counted again as a mere shape.
                    continue
                if any(
                    fnmatch.fnmatchcase(folded, pattern)
                    for pattern in patterns[position]
                ):
                    if folded not in pattern_seen[position]:
                        pattern_seen[position].add(folded)
                        entry_signs.pattern_notes.append(name)
        if cut_short:
            break

    if cut_short:
        result.notes.append(
            f"Lazarus looked at the first {max_files} files and "
            f"stopped there, so this is based on part of the folder."
        )

    for entry_signs in signs:
        has_extensions = bool(entry_signs.extension_hits)
        has_exact = bool(entry_signs.exact_notes)
        has_pattern = bool(entry_signs.pattern_notes)
        if not (has_extensions or has_exact or has_pattern):
            continue
        if has_exact:
            confidence = "looks like"
        else:
            confidence = "may be"

        family = entry_signs.entry.family
        evidence: list[str] = []
        # The names and extensions below come from the disk, not
        # from Lazarus: they are shown through _printable, so a
        # crafted file name can never steer the terminal or fake
        # a line of Lazarus's own output.
        for extension in sorted(entry_signs.extension_hits):
            count = entry_signs.extension_hits[extension]
            evidence.append(
                f"{count} file(s) end in {_printable(extension)} — an "
                f"extension {family} uses."
            )
        for name in entry_signs.exact_notes:
            evidence.append(
                f"A ransom note named {_printable(name)} is here — "
                f"that is the note {family} leaves."
            )
        for name in entry_signs.pattern_notes:
            evidence.append(
                f"A file named {_printable(name)} is here — the "
                f"shape of note {family} leaves, though other "
                f"families use similar names."
            )
        result.matches.append(
            FamilyMatch(
                entry=entry_signs.entry,
                confidence=confidence,
                evidence=evidence,
            )
        )

    def _sort_key(match: FamilyMatch) -> tuple[int, int, str]:
        strength = 0 if match.confidence == "looks like" else 1
        return (strength, -len(match.evidence), match.entry.family.casefold())

    result.matches.sort(key=_sort_key)
    return result
