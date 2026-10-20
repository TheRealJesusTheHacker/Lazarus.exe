"""Assessments: the honest menu for a drive or a folder.

A stub platform stands in for the volume seam, so each case —
locked BitLocker, a folder carrying a known family's signs, a
folder with nothing Lazarus recognizes, a target that is not there
at all — is weighed without Windows and without guessing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lazarus.assess import assess_options
from lazarus.decrypt_model import DecryptorEntry, VolumeInfo
from lazarus.errors import DecryptorError, UnlockError

HONESTY = "Lazarus cannot break encryption"


def stop_entry() -> DecryptorEntry:
    return DecryptorEntry(
        family="STOP/Djvu",
        publisher="Emsisoft",
        decryptor_name="Emsisoft Decryptor for STOP Djvu",
        source_url="https://www.emsisoft.com/ransomware-decryption-tools/stop-djvu",
        publisher_url="https://www.emsisoft.com/ransomware-decryption-tools/",
        credit="Published free of charge by Emsisoft.",
        verified_utc="2026-10-20",
        aliases=["STOP", "Djvu"],
        executable_name=None,
        extensions=[".djvu"],
        note_filenames=["_readme.txt"],
        note_patterns=[],
        conditions="Works only when the files were locked with an offline key.",
    )


class StubPlatform:
    """The volume seam, answering only what it is told to answer."""

    def __init__(self, volume: VolumeInfo | None = None, error: Exception | None = None):
        self._volume = volume
        self._error = error

    def describe_volume(self, ref: str):
        if self._error is not None:
            raise self._error
        return self._volume


def stop_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "locked-files"
    folder.mkdir()
    (folder / "_readme.txt").write_text("your files are locked", encoding="utf-8")
    (folder / "photo.jpg.djvu").write_bytes(b"locked bytes")
    return folder


def test_locked_bitlocker_volume_offers_the_unlock(tmp_path):
    volume = VolumeInfo(ref="D:", kind="bitlocker", locked=True)
    assessment = assess_options(
        Path("D:"), platform=StubPlatform(volume=volume), registry=[]
    )
    assert "This volume is locked with BitLocker." in assessment.findings
    unlock_options = [o for o in assessment.options if "lazarus unlock D:" in o]
    assert unlock_options, assessment.options
    assert "your own recovery key" in unlock_options[0]
    assert assessment.notes[-1].startswith(HONESTY)
    assert assessment.to_text().startswith("What Lazarus can do for D:")


def test_open_bitlocker_volume_offers_normal_recovery():
    volume = VolumeInfo(ref="D:", kind="bitlocker", locked=False)
    assessment = assess_options(
        Path("D:"), platform=StubPlatform(volume=volume), registry=[]
    )
    assert "This volume is open." in assessment.findings
    assert any("lazarus scan" in o for o in assessment.options)


def test_unknown_lock_is_named_as_a_stop_not_a_guess():
    volume = VolumeInfo(ref="D:", kind="unknown", locked=None)
    assessment = assess_options(
        Path("D:"), platform=StubPlatform(volume=volume), registry=[]
    )
    assert any(
        "does not guess at locks" in finding for finding in assessment.findings
    )


def test_folder_with_stop_signs_offers_the_decryptor_and_its_limits(tmp_path):
    folder = stop_folder(tmp_path)
    assessment = assess_options(folder, registry=[stop_entry()])
    assert any("STOP/Djvu" in finding for finding in assessment.findings)
    decryptor_options = [
        o for o in assessment.options if "Emsisoft Decryptor for STOP Djvu" in o
    ]
    assert decryptor_options, assessment.options
    assert "offline key" in decryptor_options[0]
    assert "never on the only copies" in decryptor_options[0]
    assert assessment.notes[-1].startswith(HONESTY)


def test_folder_with_unknown_files_gets_the_plain_statement(tmp_path):
    folder = tmp_path / "mystery"
    folder.mkdir()
    (folder / "notes.xyzq").write_bytes(b"unknown")
    (folder / "photo.qqq").write_bytes(b"also unknown")

    assessment = assess_options(folder, registry=[stop_entry()])

    assert any(
        "Nothing here carries signs" in finding
        for finding in assessment.findings
    )
    combined = assessment.findings + assessment.options
    assert any(
        "no published decryptor Lazarus can offer" in line for line in combined
    )
    assert any("lazarus restore" in o for o in assessment.options)
    assert any("lazarus scan" in o for o in assessment.options)
    assert any("lazarus rescue" in o for o in assessment.options)
    assert assessment.notes[-1].startswith(HONESTY)


def test_missing_target_is_a_plain_error(tmp_path):
    with pytest.raises(DecryptorError):
        assess_options(
            tmp_path / "not-there",
            platform=StubPlatform(volume=None),
            registry=[],
        )


def test_a_seam_that_cannot_answer_becomes_a_note_not_a_crash(tmp_path):
    folder = stop_folder(tmp_path)
    platform = StubPlatform(
        error=UnlockError(
            "Opening a locked volume uses Windows' own unlock tools, "
            "and this computer is not running Windows."
        )
    )
    assessment = assess_options(folder, platform=platform, registry=[stop_entry()])
    assert any("not running Windows" in note for note in assessment.notes)
    # The folder evidence was still weighed, seam failure or not.
    assert any("STOP/Djvu" in o for o in assessment.options)
    assert assessment.notes[-1].startswith(HONESTY)
