"""The decryptor list and family identification.

The shipped list is trusted data: it must load whole, and every
hand-made poison of it must be refused in plain language. The
identification tests use empty files — the content never matters,
only the outward signs: extensions and ransom-note names.
"""

from __future__ import annotations

import datetime
import json
import sys

import pytest

from lazarus.decryptors import (
    default_registry_path,
    find_entry,
    identify_family,
    load_registry,
)
from lazarus.errors import RegistryError


# ------------------------------------------------------------------
# The shipped registry
# ------------------------------------------------------------------

EXPECTED_FAMILIES = {
    "STOP/Djvu",
    "GandCrab",
    "Avaddon",
    "REvil / Sodinokibi",
    "Babuk",
    "Maze / Sekhmet / Egregor",
}


def test_shipped_registry_loads_with_six_entries():
    registry = load_registry()
    assert len(registry) == 6
    assert {entry.family for entry in registry} == EXPECTED_FAMILIES


def test_default_registry_path_points_at_the_shipped_list():
    path = default_registry_path()
    assert path.name == "decryptors_registry.json"
    assert path.is_file()


def test_every_shipped_entry_is_fully_checked():
    for entry in load_registry():
        assert entry.source_url.startswith("https://")
        assert entry.publisher_url.startswith("https://")
        # A real calendar day, in year-month-day form.
        parsed = datetime.date.fromisoformat(entry.verified_utc)
        assert parsed.isoformat() == entry.verified_utc
        assert entry.conditions.strip()
        assert entry.family.strip()
        assert entry.publisher.strip()
        assert entry.decryptor_name.strip()
        assert entry.credit.strip()
        for extension in entry.extensions:
            assert extension.startswith(".")
            assert extension == extension.lower()


def test_find_entry_by_family_without_case():
    registry = load_registry()
    assert find_entry(registry, "GandCrab").family == "GandCrab"
    assert find_entry(registry, "gandcrab").family == "GandCrab"
    assert find_entry(registry, "BABUK").family == "Babuk"
    assert find_entry(registry, "stop/djvu").family == "STOP/Djvu"


def test_find_entry_by_alias_without_case():
    registry = load_registry()
    entry = find_entry(registry, "Sodinokibi")
    assert entry is not None
    assert entry.family == "REvil / Sodinokibi"
    assert find_entry(registry, "sodinokibi") is entry
    assert find_entry(registry, "REvil") is entry
    assert find_entry(registry, "Sekhmet").family == "Maze / Sekhmet / Egregor"


def test_find_entry_for_an_unknown_family_is_none():
    registry = load_registry()
    assert find_entry(registry, "NotARealFamily") is None


# ------------------------------------------------------------------
# Validation: a poisoned list must fail loudly and plainly
# ------------------------------------------------------------------


def _valid_entry(**overrides):
    entry = {
        "family": "TestFamily",
        "publisher": "Test Publisher",
        "decryptor_name": "Test Publisher Decryptor for TestFamily",
        "source_url": "https://example.com/decryptor",
        "publisher_url": "https://example.com",
        "credit": "Published free of charge by Test Publisher.",
        "verified_utc": "2026-10-20",
        "aliases": ["TestFam"],
        "executable_name": None,
        "extensions": [".test"],
        "note_filenames": ["TEST-DECRYPT.txt"],
        "note_patterns": ["*-decrypt.txt"],
        "conditions": "Works only sometimes, and says so.",
    }
    entry.update(overrides)
    return entry


def _write_registry(tmp_path, payload, name="registry.json"):
    path = tmp_path / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _load_poisoned(tmp_path, payload):
    path = _write_registry(tmp_path, payload)
    with pytest.raises(RegistryError):
        load_registry(path)


def test_a_small_valid_registry_loads(tmp_path):
    path = _write_registry(tmp_path, {"entries": [_valid_entry()]})
    registry = load_registry(path)
    assert len(registry) == 1
    assert registry[0].family == "TestFamily"
    assert registry[0].extensions == [".test"]


def test_extensions_are_normalised_to_lowercase(tmp_path):
    path = _write_registry(
        tmp_path, {"entries": [_valid_entry(extensions=[".TEST", ".DjVu"])]}
    )
    registry = load_registry(path)
    assert registry[0].extensions == [".test", ".djvu"]


def test_unknown_key_is_refused(tmp_path):
    _load_poisoned(tmp_path, {"entries": [_valid_entry(surprise="planted")]})


def test_duplicate_family_differing_only_by_case_is_refused(tmp_path):
    entries = [_valid_entry(), _valid_entry(family="testfamily")]
    _load_poisoned(tmp_path, {"entries": entries})


def test_http_source_url_is_refused(tmp_path):
    _load_poisoned(
        tmp_path,
        {"entries": [_valid_entry(source_url="http://example.com/decryptor")]},
    )


def test_http_publisher_url_is_refused(tmp_path):
    _load_poisoned(
        tmp_path,
        {"entries": [_valid_entry(publisher_url="http://example.com")]},
    )


@pytest.mark.parametrize("bad_day", ["2026-13-40", "yesterday"])
def test_unreal_verified_day_is_refused(tmp_path, bad_day):
    _load_poisoned(tmp_path, {"entries": [_valid_entry(verified_utc=bad_day)]})


def test_extension_without_a_dot_is_refused(tmp_path):
    _load_poisoned(tmp_path, {"entries": [_valid_entry(extensions=["djvu"])]})


def test_note_filename_with_a_slash_is_refused(tmp_path):
    _load_poisoned(
        tmp_path,
        {"entries": [_valid_entry(note_filenames=["../TEST-DECRYPT.txt"])]},
    )


def test_note_pattern_with_a_backslash_is_refused(tmp_path):
    _load_poisoned(
        tmp_path,
        {"entries": [_valid_entry(note_patterns=["folder\\*-decrypt.txt"])]},
    )


def test_missing_publisher_is_refused(tmp_path):
    entry = _valid_entry()
    del entry["publisher"]
    _load_poisoned(tmp_path, {"entries": [entry]})


def test_top_level_list_instead_of_a_record_is_refused(tmp_path):
    _load_poisoned(tmp_path, [_valid_entry()])


def test_malformed_json_is_refused(tmp_path):
    _load_poisoned(tmp_path, "{this is not a list")


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(tmp_path / "no-such-list.json")


# ------------------------------------------------------------------
# Identification: outward signs, honestly weighed
# ------------------------------------------------------------------


@pytest.fixture()
def registry():
    return load_registry()


def _make_files(folder, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"")
    return folder


def _only_match(identification):
    assert len(identification.matches) == 1
    return identification.matches[0]


def _assert_never_confirmed(identification):
    for match in identification.matches:
        assert "confirmed" not in match.confidence
        for line in match.evidence:
            assert "confirmed" not in line


def test_stop_djvu_looks_like_with_both_kinds_of_evidence(tmp_path, registry):
    folder = _make_files(tmp_path / "victim", "photo.jpg.djvu", "_readme.txt")
    result = identify_family(folder, registry)
    assert result.files_seen == 2
    match = _only_match(result)
    assert match.entry.family == "STOP/Djvu"
    assert match.confidence == "looks like"
    assert any("end in .djvu" in line for line in match.evidence)
    assert any("_readme.txt" in line for line in match.evidence)
    _assert_never_confirmed(result)


def test_gandcrab_looks_like(tmp_path, registry):
    folder = _make_files(
        tmp_path / "victim", "report.docx.gdcb", "notes.txt.gdcb", "KRAB-DECRYPT.txt"
    )
    result = identify_family(folder, registry)
    match = _only_match(result)
    assert match.entry.family == "GandCrab"
    assert match.confidence == "looks like"
    assert any("end in .gdcb" in line for line in match.evidence)
    assert any("KRAB-DECRYPT.txt" in line for line in match.evidence)
    _assert_never_confirmed(result)


def test_avaddon_extension_only_may_be(tmp_path, registry):
    folder = _make_files(tmp_path / "victim", "photo.jpg.avdn", "letter.docx.avdn")
    result = identify_family(folder, registry)
    match = _only_match(result)
    assert match.entry.family == "Avaddon"
    assert match.confidence == "may be"
    assert any("2 file(s) end in .avdn" in line for line in match.evidence)
    _assert_never_confirmed(result)


def test_readme_shaped_name_only_may_be_revil(tmp_path, registry):
    folder = _make_files(tmp_path / "victim", "abcd-readme.txt")
    result = identify_family(folder, registry)
    match = _only_match(result)
    assert match.entry.family == "REvil / Sodinokibi"
    assert match.confidence == "may be"
    assert any("abcd-readme.txt" in line for line in match.evidence)
    # The pattern wording carries the weaker weight.
    assert any("similar names" in line for line in match.evidence)
    _assert_never_confirmed(result)


def test_ordinary_files_match_nothing(tmp_path, registry):
    folder = _make_files(tmp_path / "ordinary", "notes.txt", "photo.jpg")
    result = identify_family(folder, registry)
    assert result.files_seen == 2
    assert result.matches == []
    assert result.notes == []


def test_mixed_folder_ranks_looks_like_before_may_be(tmp_path, registry):
    folder = _make_files(
        tmp_path / "mixed", "_readme.txt", "document.pdf.babuk"
    )
    result = identify_family(folder, registry)
    assert len(result.matches) == 2
    assert result.matches[0].entry.family == "STOP/Djvu"
    assert result.matches[0].confidence == "looks like"
    assert result.matches[1].entry.family == "Babuk"
    assert result.matches[1].confidence == "may be"
    _assert_never_confirmed(result)


def test_identification_looks_in_subfolders(tmp_path, registry):
    folder = _make_files(tmp_path / "victim" / "deep" / "deeper", "photo.jpg.djvu")
    result = identify_family(folder.parent.parent, registry)
    match = _only_match(result)
    assert match.entry.family == "STOP/Djvu"
    assert match.confidence == "may be"


def test_missing_folder_is_a_plain_error(tmp_path, registry):
    from lazarus.errors import DecryptorError

    with pytest.raises(DecryptorError):
        identify_family(tmp_path / "not-there", registry)


def test_file_cap_stops_and_says_so(tmp_path, registry):
    names = [f"file{i}.jpg.djvu" for i in range(6)]
    folder = _make_files(tmp_path / "large", *names)
    result = identify_family(folder, registry, max_files=3)
    assert result.files_seen == 3
    partial_note = (
        "Lazarus looked at the first 3 files and stopped there, "
        "so this is based on part of the folder."
    )
    assert partial_note in result.notes
    _assert_never_confirmed(result)


# ------------------------------------------------------------------
# Audit fixes: aliases are names too, patterns are weak evidence,
# and printed strings are checked before they are trusted
# ------------------------------------------------------------------


def test_alias_repeating_another_family_is_refused(tmp_path):
    payload = {
        "entries": [
            _valid_entry(family="FamA", aliases=["FamB"]),
            _valid_entry(family="FamB", aliases=[]),
        ]
    }
    _load_poisoned(tmp_path, payload)


def test_alias_repeating_its_own_family_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(family="FamA", aliases=["fama"])]}
    _load_poisoned(tmp_path, payload)


def test_the_same_alias_on_two_entries_is_refused(tmp_path):
    payload = {
        "entries": [
            _valid_entry(family="FamA", aliases=["Shared"]),
            _valid_entry(family="FamB", aliases=["shared"]),
        ]
    }
    _load_poisoned(tmp_path, payload)


def test_an_empty_alias_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(aliases=["  "])]}
    _load_poisoned(tmp_path, payload)


def test_a_bare_https_address_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(source_url="https://")]}
    _load_poisoned(tmp_path, payload)


def test_an_address_with_a_space_is_refused(tmp_path):
    payload = {
        "entries": [_valid_entry(source_url="https://example .com/x")]
    }
    _load_poisoned(tmp_path, payload)


def test_a_name_with_a_hidden_line_break_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(family="Test\nFamily")]}
    _load_poisoned(tmp_path, payload)


def test_an_executable_name_of_dotdot_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(executable_name="..")]}
    _load_poisoned(tmp_path, payload)


def test_a_list_far_larger_than_the_shipped_one_is_refused(tmp_path):
    entries = [
        _valid_entry(family=f"Family{i}", aliases=[]) for i in range(501)
    ]
    _load_poisoned(tmp_path, {"entries": entries})


def test_a_pattern_shaped_note_and_one_extension_is_only_a_may_be(
    tmp_path, registry
):
    # "loot-DECRYPT.txt" matches GandCrab's *-DECRYPT.txt pattern
    # but is not one of its own note names; with a single .gdcb
    # file that is two weak signs, and weak signs do not add up
    # to "looks like".
    folder = _make_files(tmp_path / "victim", "a.gdcb", "loot-DECRYPT.txt")
    result = identify_family(folder, registry)
    match = _only_match(result)
    assert match.entry.family == "GandCrab"
    assert match.confidence == "may be"
    _assert_never_confirmed(result)


# ------------------------------------------------------------------
# Second audit round: padding is not a disguise, addresses name a
# site, and disk-born names are printed safely
# ------------------------------------------------------------------


def test_a_padded_duplicate_family_is_refused(tmp_path):
    payload = {
        "entries": [
            _valid_entry(family="FamA", aliases=[]),
            _valid_entry(family=" FamA ", aliases=[]),
        ]
    }
    _load_poisoned(tmp_path, payload)


def test_a_padded_family_loads_under_its_plain_name(tmp_path):
    path = _write_registry(
        tmp_path, {"entries": [_valid_entry(family=" FamA ", aliases=[])]}
    )
    registry = load_registry(path)
    assert registry[0].family == "FamA"
    assert find_entry(registry, "FamA") is registry[0]


def test_an_alias_cannot_steer_a_lookup_away_from_a_family(tmp_path):
    payload = {
        "entries": [
            _valid_entry(family=" FamB ", aliases=[]),
            _valid_entry(family="Other", aliases=["FamB"]),
        ]
    }
    _load_poisoned(tmp_path, payload)


def test_a_c1_control_character_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(family="Test\u0085Family")]}
    _load_poisoned(tmp_path, payload)


def test_an_address_with_a_hidden_account_part_is_refused(tmp_path):
    payload = {
        "entries": [
            _valid_entry(source_url="https://example.com@evil.example.net/x")
        ]
    }
    _load_poisoned(tmp_path, payload)


def test_an_address_naming_no_site_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(source_url="https://localhost/x")]}
    _load_poisoned(tmp_path, payload)


def test_an_address_with_a_broken_port_is_refused(tmp_path):
    payload = {
        "entries": [_valid_entry(source_url="https://example.com:nope/x")]
    }
    _load_poisoned(tmp_path, payload)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the crafted name uses characters Windows filenames cannot contain",
)
def test_a_crafted_file_name_cannot_reach_the_terminal_raw(
    tmp_path, registry
):
    folder = _make_files(
        tmp_path / "victim", "loot\u001b[31m-DECRYPT.txt", "a.gdcb"
    )
    result = identify_family(folder, registry)
    match = _only_match(result)
    for line in match.evidence:
        assert "\u001b" not in line
        assert "\n" not in line
    assert any("loot?[31m-DECRYPT.txt" in line for line in match.evidence)


# ------------------------------------------------------------------
# Third audit round: the last invisible characters, and a tool
# name stored as it will be compared
# ------------------------------------------------------------------


def test_an_invisible_operator_in_a_name_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(family="Al\u2061pha")]}
    _load_poisoned(tmp_path, payload)


def test_a_tag_character_in_an_alias_is_refused(tmp_path):
    payload = {"entries": [_valid_entry(aliases=["Test\U000e0020Fam"])]}
    _load_poisoned(tmp_path, payload)


def test_a_padded_tool_name_is_stored_plain(tmp_path):
    path = _write_registry(
        tmp_path,
        {"entries": [_valid_entry(executable_name=" tool.exe ")]},
    )
    registry = load_registry(path)
    assert registry[0].executable_name == "tool.exe"
