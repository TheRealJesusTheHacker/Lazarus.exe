"""Opening a locked volume — with the owner's own key, and only that.

Phase 3's first job is opening an encrypted volume so Lazarus can
recover files from it like any other drive. BitLocker comes first,
and the rule of the whole module is simple: **the owner opens
their own volume with their own key, through Windows' own unlock
mechanisms.** Windows does the cryptography. Lazarus never derives
a key, never cracks one, and never guesses.

Lazarus NEVER brute-forces: one key, one attempt, then it stops.
If Windows does not accept the key the owner gave, the answer is
"not accepted" and a plain explanation of what to check — not
another try. There is no retry loop anywhere in this module, on
purpose.

Keys are handled like they are radioactive:

* a key is asked for only at the moment it is needed, through a
  caller-supplied ``key_provider`` — never earlier, and never at
  all if the volume turns out to be open already;
* a key is passed to the platform as its own separate argument,
  never inside a command line Lazarus builds, shows, or keeps;
* the local reference to the key is dropped the instant the single
  attempt returns;
* no record defined here has a field that could hold a key, and
  nothing in this module logs one. An :class:`UnlockOutcome` can
  be printed, stored, or shown anywhere, safely, forever.

The work sits behind a platform seam (:class:`UnlockPlatform`) so
the orchestration — identify, ask once, try once, report plainly —
can be tested end to end without Windows, while the real
:class:`WindowsUnlockPlatform` runs Windows' own tools on a real
machine. On anything that is not Windows,
:func:`default_platform` returns :class:`UnavailablePlatform`,
which says so plainly instead of pretending.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Protocol

from .decrypt_model import VolumeInfo
from .errors import UnlockError, UnlockRefusedError
from .unlock_commands import (
    bitlocker_mount_points_argv,
    bitlocker_status_argv,
    bitlocker_unlock_recovery_argv,
    parse_mount_points,
    parse_status_output,
)

_NOT_WINDOWS_MESSAGE = (
    "Opening a locked volume uses Windows' own unlock tools, and this "
    "computer is not running Windows. The Windows unlock steps are "
    "built and ready; they can only run on Windows."
)


@dataclass
class UnlockAttempt:
    """The result of one unlock attempt, in plain language.

    ``message`` is words Lazarus wrote for the owner — never raw
    tool output, which can be cryptic, can name things the owner
    does not need to see, and is never needed to explain a result.
    """

    ok: bool
    message: str = ""


@dataclass
class UnlockOutcome:
    """How an unlock request ended, ready to show or to keep.

    ``status`` is one of ``"unlocked"`` (Windows opened the
    volume), ``"already-unlocked"`` (it was open; no key was
    asked for), or ``"not-accepted"`` (Windows did not accept the
    one key it was given, and Lazarus stopped there).

    This record is physically incapable of holding the key: there
    is no field for one, and the default ``__repr__`` shows only
    the fields declared here.
    """

    ref: str
    status: str  # "unlocked" | "already-unlocked" | "not-accepted"
    volume: VolumeInfo
    message: str
    next_step: str

    def to_text(self) -> str:
        """This outcome as plain lines: what happened, then what next."""
        return "\n".join([self.message, f"Next step: {self.next_step}"])


class UnlockPlatform(Protocol):
    """The seam between Lazarus and the machine's own unlock tools.

    An implementation describes volumes and runs exactly the one
    unlock it is handed. It receives the key as a separate
    argument — never inside ``argv``, which carries only the
    placeholder — so nothing it records about the command can
    contain the key.
    """

    def describe_volume(self, ref: str) -> VolumeInfo | None:
        """What the platform says about one volume, or None if absent."""
        ...

    def list_volumes(self) -> list[VolumeInfo]:
        """Every volume the platform can describe."""
        ...

    def run_unlock(self, argv: list[str], secret: str) -> UnlockAttempt:
        """Run one unlock command, filling its secret slot in memory."""
        ...


def identify_volume(platform: UnlockPlatform, ref: str) -> VolumeInfo:
    """Find ``ref`` and confirm Lazarus may try to open it.

    Raises :class:`UnlockRefusedError`, in plain language, when
    the volume cannot be found, or when its lock is anything other
    than BitLocker. An unnamed lock is a stop, never a guess:
    Lazarus opens the lock it knows, with the owner's key, and
    leaves every other lock alone.
    """
    volume = platform.describe_volume(ref)
    if volume is None:
        raise UnlockRefusedError(
            f"Lazarus could not find a volume called {ref}. "
            "Check the drive letter and try again."
        )
    if volume.kind != "bitlocker":
        if volume.kind == "unknown":
            raise UnlockRefusedError(
                f"Lazarus could not tell what kind of lock {ref} has, "
                "and it will not guess at one. Lazarus can open a "
                "BitLocker volume with your own recovery key; it "
                "cannot open a lock it cannot name."
            )
        raise UnlockRefusedError(
            f"Lazarus can tell that {ref} has a {volume.kind} lock, "
            "and this version of Lazarus cannot open that kind. "
            "Lazarus can open a BitLocker volume with your own "
            "recovery key; it will not guess at a lock it was not "
            "built to open."
        )
    return volume


def unlock_volume(
    platform: UnlockPlatform,
    ref: str,
    key_provider: Callable[[], str],
) -> UnlockOutcome:
    """Open ``ref`` with the owner's own recovery key — once.

    ``key_provider`` is called exactly once, and only if the
    volume is actually locked: an already-open volume never
    triggers a request for a key. The key it returns is used for
    the single attempt and then dropped.
    """
    volume = identify_volume(platform, ref)

    if volume.locked is False:
        return UnlockOutcome(
            ref=volume.ref,
            status="already-unlocked",
            volume=volume,
            message=(
                f"{volume.ref} is already open. No key was needed, "
                "and none was asked for."
            ),
            next_step=(
                f"Scan, recover, or restore files from {volume.ref} "
                "now, like any other drive."
            ),
        )

    key = key_provider()
    if key is None or not str(key).strip():
        raise UnlockRefusedError(
            "No recovery key was given, so nothing was tried."
        )

    argv = bitlocker_unlock_recovery_argv(volume.ref)
    # One key, one attempt, then Lazarus stops. This is the only
    # run_unlock call in the module, and it is never retried and
    # never looped: Lazarus does not brute-force, does not cycle
    # through keys, and does not "try variations". If Windows says
    # no, the outcome below says so, plainly, and that is the end.
    attempt = platform.run_unlock(argv, secret=key)
    key = None  # drop the only reference before building any record

    if attempt.ok:
        return UnlockOutcome(
            ref=volume.ref,
            status="unlocked",
            volume=volume,
            message=(
                f"{volume.ref} is open. Lazarus can now recover files "
                "from it like any other drive."
            ),
            next_step=(
                f"Scan, recover, or restore files from {volume.ref} — "
                "Lazarus treats it like any other drive now."
            ),
        )

    return UnlockOutcome(
        ref=volume.ref,
        status="not-accepted",
        volume=volume,
        message=(
            f"Windows did not accept that recovery key for {volume.ref}. "
            "Lazarus tried once, with the one key you gave it, and "
            "stopped — it does not keep trying keys."
        ),
        next_step=(
            "Check the recovery key saved for this drive — each drive "
            "has its own key — and run this again only when you have "
            "a different key in hand."
        ),
    )


#: No Windows tool gets forever. A hung manage-bde, PowerShell, or
#: schtasks must become a designed, plain-language failure (the
#: callers translate every executor failure), never a Lazarus that
#: hangs on a spinning page with no way back.
_EXECUTE_TIMEOUT_SECONDS = 30


def _subprocess_execute(argv: list[str]) -> tuple[int, str]:
    """The real run: one command, no shell, words captured not shown."""
    result = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_EXECUTE_TIMEOUT_SECONDS,
    )
    return result.returncode, result.stdout


class WindowsUnlockPlatform:
    """The platform seam, backed by Windows' own BitLocker tools.

    ``execute`` performs the real run and returns
    ``(returncode, stdout)``; it is injectable so tests can stand
    in for Windows. Tool output is read only to be parsed (for a
    status report) or discarded (for an unlock) — it never becomes
    user-facing text.
    """

    def __init__(
        self,
        execute: Callable[[list[str]], tuple[int, str]] | None = None,
    ) -> None:
        self._execute = execute if execute is not None else _subprocess_execute

    _EXECUTE_MESSAGE = (
        "Lazarus could not run Windows' own unlock tools "
        "on this computer, so it could not ask about this "
        "volume. The tools it drives — manage-bde and "
        "Windows PowerShell — may be missing or blocked."
    )

    def _run(self, argv: list[str]) -> tuple[int, str]:
        """Run one command, translating any failure to run it.

        The executor is a seam, and whatever crosses it gets
        checked: a tool that cannot be started, an executor that
        fails in any way, or an answer in the wrong shape all
        become the same designed Lazarus error in plain language
        — never a raw complaint from underneath.
        """
        try:
            returncode, output = self._execute(argv)
        except Exception:
            raise UnlockError(self._EXECUTE_MESSAGE) from None
        if not isinstance(returncode, int) or not isinstance(output, str):
            raise UnlockError(self._EXECUTE_MESSAGE)
        return returncode, output

    def describe_volume(self, ref: str) -> VolumeInfo | None:
        returncode, output = self._run(bitlocker_status_argv(ref))
        if returncode != 0:
            return None
        return parse_status_output(output, ref)

    def list_volumes(self) -> list[VolumeInfo]:
        returncode, output = self._run(bitlocker_mount_points_argv())
        if returncode != 0:
            return []
        volumes: list[VolumeInfo] = []
        for mount in parse_mount_points(output):
            volume = self.describe_volume(mount)
            if volume is not None:
                volumes.append(volume)
        return volumes

    def run_unlock(self, argv: list[str], secret: str) -> UnlockAttempt:
        # Fill the secret slot in a COPY, in memory only — the
        # slot after "-RecoveryPassword", and only that slot, so
        # nothing else in the command can be overwritten by the
        # key. The caller's argv keeps the placeholder, so
        # anything built from it — a display, a log, a record —
        # can never contain the key. The tool's own words are
        # never carried into the message: the owner gets
        # Lazarus's plain sentence instead.
        real_argv = list(argv)
        for position, item in enumerate(real_argv[:-1]):
            if item == "-RecoveryPassword":
                real_argv[position + 1] = secret
                break
        returncode, _output = self._run(real_argv)
        if returncode == 0:
            return UnlockAttempt(ok=True, message="")
        return UnlockAttempt(
            ok=False,
            message="Windows reported that the key was not accepted.",
        )


class UnavailablePlatform:
    """The platform on any computer that is not running Windows.

    Every method raises the same plain :class:`UnlockError`: the
    unlock steps are built and ready, but they drive Windows' own
    tools, and there is nothing honest to do on a machine without
    them.
    """

    def describe_volume(self, ref: str) -> VolumeInfo | None:
        raise UnlockError(_NOT_WINDOWS_MESSAGE)

    def list_volumes(self) -> list[VolumeInfo]:
        raise UnlockError(_NOT_WINDOWS_MESSAGE)

    def run_unlock(self, argv: list[str], secret: str) -> UnlockAttempt:
        raise UnlockError(_NOT_WINDOWS_MESSAGE)


def default_platform() -> UnlockPlatform:
    """The platform for this computer: Windows tools, or an honest no."""
    if sys.platform == "win32":
        return WindowsUnlockPlatform()
    return UnavailablePlatform()
