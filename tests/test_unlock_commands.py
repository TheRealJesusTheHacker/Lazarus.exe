"""Tests for the pure BitLocker command builders and parsers.

Nothing here executes a command: these tests pin down the exact
words Lazarus would hand to Windows, and how it reads what Windows
prints back, using synthetic output written for the purpose.
"""

from __future__ import annotations

import re

from lazarus.unlock_commands import (
    SECRET_PLACEHOLDER,
    bitlocker_mount_points_argv,
    bitlocker_status_argv,
    bitlocker_unlock_password_argv,
    bitlocker_unlock_recovery_argv,
    parse_mount_points,
    parse_status_output,
    redact_argv,
)

LOCKED_STATUS = """\
BitLocker Drive Encryption: Configuration Tool version 10.0.22621
Copyright (C) 2013 Microsoft Corporation. All rights reserved.

Volume E: [Backup Drive]
[Data Volume]

    Size:                 931.51 GB
    BitLocker Version:    2.0
    Conversion Status:    Used Space Only Encrypted
    Percentage Encrypted: 100.0%
    Encryption Method:    XTS-AES 128
    Protection Status:    Protection On
    Lock Status:          Locked
    Volume Label:         Backup Drive
    Key Protectors:
        Numerical Password
"""

UNLOCKED_STATUS = """\
BitLocker Drive Encryption: Configuration Tool version 10.0.22621

Volume F:
[Data Volume]

    BitLocker Version:    2.0
    Conversion Status:    Fully Encrypted
    Protection Status:    Protection On
    Lock Status:          Unlocked
"""

NO_LOCK_STATUS = """\
BitLocker Drive Encryption: Configuration Tool version 10.0.22621

Volume G:
[Data Volume]

    BitLocker Version:    2.0
    Protection Status:    Protection Off
"""

NOT_BITLOCKER = """\
Volume E: is an ordinary drive.
No encryption tool reported anything about it.
"""


# --- argv builders -------------------------------------------------------


def test_status_argv_is_exact():
    assert bitlocker_status_argv("E:") == ["manage-bde.exe", "-status", "E:"]


def test_unlock_recovery_argv_is_exact_and_holds_only_the_placeholder():
    argv = bitlocker_unlock_recovery_argv("E:")
    assert argv == [
        "manage-bde.exe",
        "-unlock",
        "E:",
        "-RecoveryPassword",
        SECRET_PLACEHOLDER,
    ]
    assert SECRET_PLACEHOLDER in argv


def test_unlock_recovery_argv_contains_no_key_shaped_material():
    argv = bitlocker_unlock_recovery_argv("E:")
    joined = " ".join(argv)
    # A BitLocker recovery key is groups of six digits joined by
    # dashes. Nothing like that may appear in a built command.
    assert not re.search(r"\d{6}(?:-\d{6})+", joined)
    assert not re.search(r"\d", argv[-1])


def test_unlock_password_argv_is_exact_and_prompts_in_windows():
    assert bitlocker_unlock_password_argv("E:") == [
        "manage-bde.exe",
        "-unlock",
        "E:",
        "-Password",
    ]


def test_mount_points_argv_is_exact():
    assert bitlocker_mount_points_argv() == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-BitLockerVolume | Select-Object -ExpandProperty MountPoint",
    ]


# --- redact_argv ----------------------------------------------------------


def test_redact_argv_masks_the_secret_slot():
    argv = bitlocker_unlock_recovery_argv("E:")
    redacted = redact_argv(argv)
    assert redacted == [
        "manage-bde.exe",
        "-unlock",
        "E:",
        "-RecoveryPassword",
        "********",
    ]
    # The original is untouched, and a copy was returned.
    assert argv[-1] == SECRET_PLACEHOLDER
    assert redacted is not argv


def test_redact_argv_leaves_other_commands_unchanged():
    argv = bitlocker_status_argv("E:")
    assert redact_argv(argv) == argv


# --- parse_mount_points ---------------------------------------------------


def test_parse_mount_points_keeps_only_drive_letters():
    text = "\n".join(
        [
            "E:",
            "",
            "   ",
            "F:",
            "not a drive",
            "E",  # missing colon
            "E:\\",  # a path, not a mount point
            "EE:",  # too long
            "1:",  # not a letter
            "g:",
            "",
        ]
    )
    assert parse_mount_points(text) == ["E:", "F:", "g:"]


def test_parse_mount_points_on_empty_output():
    assert parse_mount_points("") == []
    assert parse_mount_points("\n\n") == []


# --- parse_status_output --------------------------------------------------


def test_parse_status_locked_volume():
    info = parse_status_output(LOCKED_STATUS, "E:")
    assert info.ref == "E:"
    assert info.kind == "bitlocker"
    assert info.locked is True
    assert info.label == "Backup Drive"
    assert "Protection is on for this volume." in info.evidence
    assert "The volume is locked." in info.evidence


def test_parse_status_unlocked_volume():
    info = parse_status_output(UNLOCKED_STATUS, "F:")
    assert info.ref == "F:"
    assert info.kind == "bitlocker"
    assert info.locked is False
    assert info.label == ""
    assert "The volume is unlocked." in info.evidence


def test_parse_status_non_bitlocker_text_is_unknown():
    info = parse_status_output(NOT_BITLOCKER, "E:")
    assert info.ref == "E:"
    assert info.kind == "unknown"
    assert info.locked is None
    assert info.label == ""
    assert info.evidence == [
        "Windows did not describe this volume as a BitLocker volume."
    ]


def test_parse_status_missing_lock_status_means_locked_is_none():
    info = parse_status_output(NO_LOCK_STATUS, "G:")
    assert info.kind == "bitlocker"
    assert info.locked is None


def test_parse_status_never_raises_on_odd_text():
    for text in ["", "\n\n", "BitLocker", "Lock Status:", "::: :::"]:
        info = parse_status_output(text, "E:")
        assert info.ref == "E:"


# -- audit fixes: the parser must not be fooled by its own text -----


def test_parse_status_merely_mentioning_bitlocker_is_unknown():
    # An error sentence that names BitLocker is not a status
    # report: without BitLocker's own fields, the reading is an
    # honest unknown, lock words and all.
    info = parse_status_output(
        "BitLocker is NOT installed on this computer.\nLock Status: Locked",
        "E:",
    )
    assert info.kind == "unknown"
    assert info.locked is None


def test_parse_status_protection_off_is_not_read_as_on():
    # "Protection" contains "on" as letters; the reading must be
    # by whole words, or an unprotected volume reads as protected.
    text = (
        "BitLocker Drive Encryption: Configuration Tool version 10.0\n"
        "Volume E: [Data]\n"
        "Conversion Status:    Fully Encrypted\n"
        "Percentage Encrypted: 100.0%\n"
        "Protection Status:    Protection Off\n"
        "Lock Status:          Locked\n"
    )
    info = parse_status_output(text, "E:")
    assert info.kind == "bitlocker"
    assert "Protection is off for this volume." in info.evidence
    assert "Protection is on for this volume." not in info.evidence


def test_redact_argv_masks_whatever_stands_in_the_secret_slot():
    argv = ["manage-bde.exe", "-unlock", "E:", "-RecoveryPassword", "A-REAL-KEY"]
    redacted = redact_argv(argv)
    assert "A-REAL-KEY" not in redacted
    assert redacted[-1] == "********"
    assert argv[-1] == "A-REAL-KEY"  # the caller's list is untouched


def test_parse_mount_points_lists_each_letter_once():
    assert parse_mount_points("E:\ne:\nF:\nE:\n") == ["E:", "F:"]


# -- second audit round: labels must be labels, values values ------


def test_parse_status_empty_conversion_value_is_unknown():
    info = parse_status_output(
        "Conversion Status:\nLock Status: Locked\n", "E:"
    )
    assert info.kind == "unknown"
    assert info.locked is None


def test_parse_status_label_in_a_sentence_is_unknown():
    info = parse_status_output(
        "Conversion Status is not a field here\nLock Status: Locked",
        "E:",
    )
    assert info.kind == "unknown"
    assert info.locked is None


def test_parse_status_suffixed_label_is_unknown():
    info = parse_status_output(
        "Conversion StatusXYZ: Fully Encrypted\nLock Status: Locked",
        "E:",
    )
    assert info.kind == "unknown"
    assert info.locked is None


def test_parse_status_lockedout_is_not_locked():
    text = (
        "Volume E: [Data]\n"
        "Conversion Status:    Fully Encrypted\n"
        "Lock Status:          LockedOut\n"
    )
    info = parse_status_output(text, "E:")
    assert info.kind == "bitlocker"
    assert info.locked is None
