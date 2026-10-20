"""Command lines and output text for Windows' own BitLocker tools.

This module builds commands and reads what Windows prints. It
never *runs* anything: every function here is pure, so the exact
words Lazarus would hand to Windows can be checked in a test
without a Windows machine, and without touching a real drive.

The cryptography is Windows' job, not Lazarus's. Lazarus asks
``manage-bde.exe`` — the tool Windows itself ships for BitLocker —
to open a volume with the owner's own key, and Windows does the
rest. Lazarus never sees, derives, or guesses a key.

That is why the recovery-key command is built with a placeholder
where the key belongs. A built command is safe to show, log, and
test: the real key is only ever placed into a copy of the command,
in memory, at the moment the Windows executor runs it (see
:mod:`lazarus.unlock`). A key never appears in a command Lazarus
displays, and :func:`redact_argv` masks the placeholder slot for
any command that does get shown.
"""

from __future__ import annotations

import re

from .decrypt_model import VolumeInfo

#: Marks the slot in a built command where the owner's recovery key
#: belongs. The key itself never appears in a built command; the
#: Windows executor fills this slot, in memory only, at run time.
SECRET_PLACEHOLDER = "{secret}"

_MOUNT_POINT_RE = re.compile(r"^[A-Za-z]:$")


def bitlocker_status_argv(mount: str) -> list[str]:
    """The command that asks Windows how a volume stands.

    ``mount`` is how the owner points at the volume — a drive
    letter such as ``"E:"``. Windows answers with a plain report:
    whether BitLocker protects the volume, and whether it is
    locked.
    """
    return ["manage-bde.exe", "-status", mount]


def bitlocker_unlock_recovery_argv(mount: str) -> list[str]:
    """The command that opens a volume with its recovery key.

    The key itself NEVER appears here. :data:`SECRET_PLACEHOLDER`
    marks the slot, and the Windows executor replaces it with the
    owner's key in a private copy of this list, in memory only,
    at the moment the command runs. This list, exactly as built,
    is safe to display and to keep.
    """
    return [
        "manage-bde.exe",
        "-unlock",
        mount,
        "-RecoveryPassword",
        SECRET_PLACEHOLDER,
    ]


def bitlocker_unlock_password_argv(mount: str) -> list[str]:
    """The command that opens a volume with its password.

    No secret is involved in building this one: with no password
    on the command line, Windows itself prompts the owner for it,
    in its own window.
    """
    return ["manage-bde.exe", "-unlock", mount, "-Password"]


def bitlocker_mount_points_argv() -> list[str]:
    """The command that lists this computer's BitLocker volumes.

    Asks Windows' scripting shell for the mount point of every
    BitLocker volume it knows about, one per line, so Lazarus can
    then ask about each one in turn.
    """
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-BitLockerVolume | Select-Object -ExpandProperty MountPoint",
    ]


def parse_mount_points(text: str) -> list[str]:
    """Read the mount-point list Windows printed, one per line.

    Blank lines and anything that is not a plain drive letter
    (``"E:"`` and the like) are dropped: Lazarus only follows up
    on lines it can point at with confidence. A letter listed
    twice is followed up once.
    """
    points: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        candidate = line.strip()
        if candidate and _MOUNT_POINT_RE.match(candidate):
            key = candidate.upper()
            if key not in seen:
                seen.add(key)
                points.append(candidate)
    return points


def redact_argv(argv: list[str]) -> list[str]:
    """A copy of a command, safe to show.

    The secret slot, if present, is replaced with stars — and so
    is whatever stands in the slot after ``-RecoveryPassword``,
    placeholder or not. A built command should only ever hold
    the placeholder there in the first place, but anything
    Lazarus displays goes through here so a secret can never
    reach a screen or a log by way of a command line.
    """
    redacted: list[str] = []
    mask_next = False
    for item in argv:
        if mask_next or item == SECRET_PLACEHOLDER:
            redacted.append("********")
        else:
            redacted.append(item)
        mask_next = item == "-RecoveryPassword"
    return redacted


def _value_after_label(text: str, label: str) -> str | None:
    """The trimmed value on the first ``label: value`` line, if any.

    The label must stand at the start of the line, followed by its
    colon, with a value after it. A line that merely begins with
    the label's letters — a longer word, a sentence, a label with
    nothing after the colon — is not the field, and reads as
    absent: Lazarus would rather say "unknown" than build a
    reading on a line that only resembles the real thing.
    """
    prefix = f"{label}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip()
            return value if value else None
    return None


def parse_status_output(text: str, ref: str) -> VolumeInfo:
    """Turn ``manage-bde -status`` output into a :class:`VolumeInfo`.

    The reading is deliberately tolerant: Windows' wording varies
    between versions, so a missing field simply stays unknown
    (``None`` or ``""``) rather than failing. This function never
    raises on odd text — the worst reading is an honest "unknown",
    and Lazarus stops at unknown rather than guessing.

    Only English output is understood. The text is only read as
    a BitLocker report when it carries BitLocker's own status
    fields (a conversion status or a BitLocker version) — a stray
    mention of the word, in an error or a sentence, names
    nothing. Without those fields the volume is reported as
    ``kind="unknown"``: whatever lock it may have, this output
    does not name it.
    """
    if (
        _value_after_label(text, "Conversion Status") is None
        and _value_after_label(text, "BitLocker Version") is None
    ):
        return VolumeInfo(
            ref=ref,
            kind="unknown",
            evidence=[
                "Windows did not describe this volume as a BitLocker volume."
            ],
        )

    evidence: list[str] = []

    conversion = _value_after_label(text, "Conversion Status")
    if conversion:
        evidence.append(
            f"Windows reports this volume's conversion status as: {conversion}."
        )

    protection = _value_after_label(text, "Protection Status")
    if protection:
        tokens = protection.lower().split()
        if "off" in tokens:
            evidence.append("Protection is off for this volume.")
        elif "on" in tokens:
            evidence.append("Protection is on for this volume.")
        else:
            evidence.append(
                f"Windows reports this volume's protection status as: "
                f"{protection}."
            )

    locked: bool | None = None
    lock_status = _value_after_label(text, "Lock Status")
    if lock_status:
        # The first whole word decides — "Locked" or "Unlocked".
        # A word that merely starts with those letters ("LockedOut"
        # and the like) is not the status, and stays unknown.
        tokens = lock_status.lower().split()
        first_word = tokens[0].strip(".:") if tokens else ""
        if first_word == "unlocked":
            locked = False
            evidence.append("The volume is unlocked.")
        elif first_word == "locked":
            locked = True
            evidence.append("The volume is locked.")
        # Any other wording stays unknown (None): Lazarus reports
        # what Windows said, and does not read more into it.

    label = _value_after_label(text, "Volume Label") or ""

    return VolumeInfo(
        ref=ref,
        kind="bitlocker",
        label=label,
        locked=locked,
        evidence=evidence,
    )
