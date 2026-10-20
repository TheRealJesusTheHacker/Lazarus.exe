"""Tests for unlock orchestration, behind a fake platform.

The fake platform records what Lazarus asked it to do and answers
from a script, so every rule of the module is exercised without
Windows and without a real drive: one key, one attempt, no key
asked for when none is needed, and the key appearing nowhere it
could be kept or shown.
"""

from __future__ import annotations

import pytest

from lazarus.decrypt_model import VolumeInfo
from lazarus.errors import UnlockError, UnlockRefusedError
from lazarus.unlock import (
    UnavailablePlatform,
    UnlockAttempt,
    WindowsUnlockPlatform,
    identify_volume,
    unlock_volume,
)
from lazarus.unlock_commands import SECRET_PLACEHOLDER

# A distinctive fake key: if these characters show up anywhere they
# should not, the string is unmistakable in a failure message.
FAKE_KEY = "DISTINCTIVE-FAKE-KEY-97531"

LOCKED_STATUS = """\
BitLocker Drive Encryption: Configuration Tool version 10.0.22621

Volume E:
[Data Volume]

    BitLocker Version:    2.0
    Conversion Status:    Fully Encrypted
    Protection Status:    Protection On
    Lock Status:          Locked
    Volume Label:         Backup Drive
"""


def locked_volume(ref: str = "E:") -> VolumeInfo:
    return VolumeInfo(
        ref=ref,
        kind="bitlocker",
        label="Backup Drive",
        locked=True,
        evidence=["The volume is locked."],
    )


class FakePlatform:
    """A scripted UnlockPlatform that records every call it gets.

    The secret handed to ``run_unlock`` is kept in its own list,
    apart from the recorded argv — mirroring the real contract,
    where the secret travels as a separate argument and the argv
    holds only the placeholder.
    """

    def __init__(self, volume, attempt=None):
        self._volume = volume
        self._attempt = (
            attempt if attempt is not None else UnlockAttempt(ok=True)
        )
        self.describe_calls: list[str] = []
        self.run_argv: list[list[str]] = []
        self.run_secrets: list[str] = []

    def describe_volume(self, ref):
        self.describe_calls.append(ref)
        return self._volume

    def list_volumes(self):
        return [self._volume] if self._volume is not None else []

    def run_unlock(self, argv, secret):
        self.run_argv.append(list(argv))
        self.run_secrets.append(secret)
        return self._attempt


class CountingKeyProvider:
    """A key_provider that counts its calls and returns a set key."""

    def __init__(self, key=FAKE_KEY):
        self.key = key
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.key


# --- happy path ------------------------------------------------------------


def test_unlock_happy_path_tries_once_with_the_one_key():
    platform = FakePlatform(locked_volume())
    provider = CountingKeyProvider()

    outcome = unlock_volume(platform, "E:", provider)

    assert outcome.status == "unlocked"
    assert outcome.ref == "E:"
    assert "E: is open." in outcome.message
    assert provider.calls == 1
    assert len(platform.run_argv) == 1
    # The platform was handed the key as its own argument, and the
    # argv it saw carries the placeholder, not the key.
    assert platform.run_secrets == [FAKE_KEY]
    assert platform.run_argv[0][-1] == SECRET_PLACEHOLDER
    assert outcome.volume.kind == "bitlocker"


def test_unlock_outcome_text_is_plain_and_shows_the_next_step():
    platform = FakePlatform(locked_volume())
    outcome = unlock_volume(platform, "E:", CountingKeyProvider())
    text = outcome.to_text()
    assert outcome.message in text
    assert "Next step:" in text
    assert outcome.next_step in text


# --- already unlocked -------------------------------------------------------


def test_already_unlocked_never_asks_for_a_key_or_tries():
    volume = locked_volume()
    volume.locked = False
    platform = FakePlatform(volume)
    provider = CountingKeyProvider()

    outcome = unlock_volume(platform, "E:", provider)

    assert outcome.status == "already-unlocked"
    assert provider.calls == 0
    assert platform.run_argv == []


# --- wrong key ---------------------------------------------------------------


def test_wrong_key_is_not_accepted_and_stops_after_one_attempt():
    platform = FakePlatform(
        locked_volume(),
        attempt=UnlockAttempt(
            ok=False, message="Windows reported that the key was not accepted."
        ),
    )
    provider = CountingKeyProvider()

    outcome = unlock_volume(platform, "E:", provider)

    assert outcome.status == "not-accepted"
    assert provider.calls == 1
    assert len(platform.run_argv) == 1  # exactly one attempt, never a retry
    assert "tried once" in outcome.message
    assert "does not keep trying keys" in outcome.message
    assert "different key" in outcome.next_step


# --- refusals ------------------------------------------------------------------


def test_volume_not_found_is_refused():
    platform = FakePlatform(None)
    provider = CountingKeyProvider()
    with pytest.raises(UnlockRefusedError) as excinfo:
        unlock_volume(platform, "Z:", provider)
    assert "could not find a volume called Z:" in str(excinfo.value)
    assert provider.calls == 0
    assert platform.run_argv == []


def test_unknown_kind_is_refused_without_guessing():
    platform = FakePlatform(VolumeInfo(ref="E:", kind="unknown"))
    provider = CountingKeyProvider()
    with pytest.raises(UnlockRefusedError) as excinfo:
        unlock_volume(platform, "E:", provider)
    assert "will not guess" in str(excinfo.value)
    assert provider.calls == 0
    assert platform.run_argv == []


def test_other_named_kind_is_refused_and_named():
    platform = FakePlatform(VolumeInfo(ref="E:", kind="veracrypt"))
    with pytest.raises(UnlockRefusedError) as excinfo:
        identify_volume(platform, "E:")
    assert "veracrypt" in str(excinfo.value)


def test_empty_key_is_refused_before_any_attempt():
    platform = FakePlatform(locked_volume())
    provider = CountingKeyProvider(key="   ")
    with pytest.raises(UnlockRefusedError) as excinfo:
        unlock_volume(platform, "E:", provider)
    assert "No recovery key was given, so nothing was tried." in str(
        excinfo.value
    )
    assert provider.calls == 1
    assert platform.run_argv == []


# --- key hygiene -----------------------------------------------------------------


def test_key_appears_nowhere_in_the_outcome_or_the_recorded_argv():
    platform = FakePlatform(locked_volume())
    outcome = unlock_volume(platform, "E:", CountingKeyProvider())

    assert FAKE_KEY not in repr(outcome)
    assert FAKE_KEY not in outcome.to_text()
    assert FAKE_KEY not in outcome.message
    assert FAKE_KEY not in outcome.next_step
    for argv in platform.run_argv:
        assert FAKE_KEY not in argv
        assert FAKE_KEY not in " ".join(argv)
    # The outcome has no field that could hold a key at all.
    assert not hasattr(outcome, "key")
    assert not hasattr(outcome, "secret")


def test_key_appears_nowhere_when_the_key_is_not_accepted():
    platform = FakePlatform(
        locked_volume(), attempt=UnlockAttempt(ok=False, message="no")
    )
    outcome = unlock_volume(platform, "E:", CountingKeyProvider())
    assert FAKE_KEY not in repr(outcome)
    assert FAKE_KEY not in outcome.to_text()
    for argv in platform.run_argv:
        assert FAKE_KEY not in " ".join(argv)


# --- WindowsUnlockPlatform with an injected executor ------------------------------


class FakeExecute:
    """Stands in for subprocess: records runs, answers from a script."""

    def __init__(self, handler):
        self._handler = handler
        self.executed: list[list[str]] = []

    def __call__(self, argv):
        self.executed.append(list(argv))
        return self._handler(argv)


def test_windows_platform_describe_parses_status_output():
    execute = FakeExecute(lambda argv: (0, LOCKED_STATUS))
    platform = WindowsUnlockPlatform(execute=execute)

    info = platform.describe_volume("E:")

    assert execute.executed == [["manage-bde.exe", "-status", "E:"]]
    assert info is not None
    assert info.kind == "bitlocker"
    assert info.locked is True
    assert info.label == "Backup Drive"


def test_windows_platform_describe_missing_volume_returns_none():
    execute = FakeExecute(lambda argv: (1, "No such volume."))
    platform = WindowsUnlockPlatform(execute=execute)
    assert platform.describe_volume("Z:") is None


def test_windows_platform_run_unlock_substitutes_secret_in_the_executed_copy():
    execute = FakeExecute(lambda argv: (0, ""))
    platform = WindowsUnlockPlatform(execute=execute)
    argv = [
        "manage-bde.exe",
        "-unlock",
        "E:",
        "-RecoveryPassword",
        SECRET_PLACEHOLDER,
    ]

    attempt = platform.run_unlock(argv, secret=FAKE_KEY)

    assert attempt.ok is True
    assert attempt.message == ""
    # The copy Windows ran holds the key in the placeholder's slot...
    assert execute.executed[0][-1] == FAKE_KEY
    # ...while the caller's argv still holds only the placeholder.
    assert argv[-1] == SECRET_PLACEHOLDER
    assert FAKE_KEY not in argv


def test_windows_platform_run_unlock_failure_message_is_plain():
    raw_output = "manage-bde: error 0x80310012 raw tool words"
    execute = FakeExecute(lambda argv: (1, raw_output))
    platform = WindowsUnlockPlatform(execute=execute)

    attempt = platform.run_unlock(
        ["manage-bde.exe", "-unlock", "E:", "-RecoveryPassword", SECRET_PLACEHOLDER],
        secret=FAKE_KEY,
    )

    assert attempt.ok is False
    assert attempt.message == "Windows reported that the key was not accepted."
    assert raw_output not in attempt.message


def test_windows_platform_list_volumes_describes_each_mount_point():
    def handler(argv):
        if argv[0] == "powershell.exe":
            return (0, "E:\nF:\nnot-a-drive\n")
        return (0, LOCKED_STATUS)

    execute = FakeExecute(handler)
    platform = WindowsUnlockPlatform(execute=execute)

    volumes = platform.list_volumes()

    assert [volume.ref for volume in volumes] == ["E:", "F:"]


# --- UnavailablePlatform -------------------------------------------------------


def test_unavailable_platform_raises_plainly_on_every_method():
    platform = UnavailablePlatform()
    calls = [
        lambda: platform.describe_volume("E:"),
        lambda: platform.list_volumes(),
        lambda: platform.run_unlock(["manage-bde.exe"], secret=FAKE_KEY),
    ]
    for call in calls:
        with pytest.raises(UnlockError) as excinfo:
            call()
        message = str(excinfo.value)
        assert "not running Windows" in message
        assert "can only run on Windows" in message
        assert FAKE_KEY not in message


# -- audit fixes: a tool that cannot start is a designed error, and
# the key fills only its own slot -----------------------------------


def test_windows_platform_translates_a_tool_that_cannot_start():
    from lazarus.errors import UnlockError
    from lazarus.unlock import WindowsUnlockPlatform

    def cannot_start(argv):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    platform = WindowsUnlockPlatform(execute=cannot_start)
    with pytest.raises(UnlockError):
        platform.describe_volume("E:")
    with pytest.raises(UnlockError):
        platform.list_volumes()
    with pytest.raises(UnlockError):
        platform.run_unlock(
            ["manage-bde.exe", "-unlock", "E:", "-RecoveryPassword", "{secret}"],
            secret="a key",
        )


def test_windows_platform_fills_only_the_recovery_slot():
    from lazarus.unlock import WindowsUnlockPlatform
    from lazarus.unlock_commands import bitlocker_unlock_recovery_argv

    executed = []

    def record(argv):
        executed.append(list(argv))
        return 0, ""

    platform = WindowsUnlockPlatform(execute=record)
    # A mount point that happens to read like the placeholder
    # must not be overwritten by the key: only the slot after
    # -RecoveryPassword is the key's.
    attempt = platform.run_unlock(
        bitlocker_unlock_recovery_argv("{secret}"), secret="THE-KEY"
    )
    assert attempt.ok
    assert executed[0] == [
        "manage-bde.exe",
        "-unlock",
        "{secret}",
        "-RecoveryPassword",
        "THE-KEY",
    ]
    assert executed[0].count("THE-KEY") == 1


# -- second audit round: whatever crosses the seam is checked -------


def test_windows_platform_translates_any_executor_failure():
    from lazarus.errors import UnlockError
    from lazarus.unlock import WindowsUnlockPlatform

    def broken(argv):
        raise RuntimeError("the seam itself broke")

    platform = WindowsUnlockPlatform(execute=broken)
    with pytest.raises(UnlockError):
        platform.describe_volume("E:")


def test_windows_platform_rejects_a_malformed_executor_answer():
    from lazarus.errors import UnlockError
    from lazarus.unlock import WindowsUnlockPlatform

    platform = WindowsUnlockPlatform(execute=lambda argv: None)
    with pytest.raises(UnlockError):
        platform.describe_volume("E:")

    platform = WindowsUnlockPlatform(execute=lambda argv: ("zero", 42))
    with pytest.raises(UnlockError):
        platform.list_volumes()
