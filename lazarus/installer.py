"""Lazarus installing itself — a folder and a shortcut, nothing more.

The shipped ``Lazarus.exe`` is the whole product in one file. When
a person runs it on a Windows computer, Lazarus may offer to put
itself in its proper place so it is there next time without hunting
for the download. This module is that offer, kept honest and small:

* The install is exactly two things — the program file copied into
  ``<ProgramFiles>\\Sentinel Safeguard\\Lazarus`` and one Start
  Menu shortcut pointing at it.
* There are NO registry writes, NO PATH changes, and NO services.
  Removing Lazarus removes that folder and that shortcut, and
  that is the whole footprint of an install.
* Nothing here installs in secret or on its own. The interface
  asks; this module only does what the person said yes to.

Everything is plain standard library, and nothing here imports
the interface, so the whole install can be tested on any machine.
The filesystem work goes through a small platform seam (a *shell*):
on Windows :class:`WindowsShell` does the real copying, and tests
hand in a recording stand-in, so no test ever touches a real
Program Files folder. Off Windows — or when the code running is
not the shipped, frozen program file — installing and removing
refuse plainly before the seam is touched at all.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

__all__ = [
    "InstallReport",
    "WindowsShell",
    "canonical_exe",
    "current_exe",
    "elevate_and_run",
    "install_folder",
    "installed_exe",
    "is_frozen",
    "needs_install_offer",
    "perform_install",
    "perform_uninstall",
    "shortcut_argv",
    "start_menu_shortcut",
]

#: Set this environment variable (to anything) and the shipped
#: program never offers to install itself on this computer.
NO_INSTALL_OFFER_ENV = "LAZARUS_NO_INSTALL_OFFER"

_PROGRAM_FOLDER = "Sentinel Safeguard"
_APP_FOLDER = "Lazarus"
_EXE_NAME = "Lazarus.exe"
_SHORTCUT_NAME = "Lazarus.lnk"
_SHORTCUT_DESCRIPTION = "Lazarus — backup, restore, and file recovery"

#: ``SW_SHOWNORMAL`` for ShellExecute: open the window normally.
_SW_SHOWNORMAL = 1


# ------------------------------------------------------------------
# Where things live
# ------------------------------------------------------------------


def _env_base(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else Path(fallback)


def install_folder(program_files: Optional[Path] = None) -> Path:
    """The folder Lazarus installs into.

    ``program_files`` is the base the folder hangs off (the
    ``ProgramFiles`` folder on Windows). Tests pass a scratch
    folder; real runs leave it out and get the computer's own.
    """
    base = (
        Path(program_files)
        if program_files is not None
        else _env_base("ProgramFiles", "C:\\Program Files")
    )
    return base / _PROGRAM_FOLDER / _APP_FOLDER


def installed_exe(program_files: Optional[Path] = None) -> Path:
    """The installed program file — Lazarus.exe in its folder."""
    return install_folder(program_files) / _EXE_NAME


#: The canonical name for the installed program file.
canonical_exe = installed_exe


def start_menu_shortcut(program_data: Optional[Path] = None) -> Path:
    """The one Start Menu shortcut an install writes."""
    base = (
        Path(program_data)
        if program_data is not None
        else _env_base("ProgramData", "C:\\ProgramData")
    )
    return (
        base
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / _SHORTCUT_NAME
    )


# ------------------------------------------------------------------
# What is running, and where
# ------------------------------------------------------------------


def is_frozen() -> bool:
    """True when running as the shipped, packaged program file."""
    return bool(getattr(sys, "frozen", False))


def current_exe() -> Path:
    """The program file that is running right now."""
    return Path(sys.executable).resolve()


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _same_path(first: Path, second: Path) -> bool:
    """True when two paths point at the same place.

    Windows does not care about letter case in a path, so the
    comparison does not either.
    """
    try:
        first_text = str(Path(first).resolve()).lower()
        second_text = str(Path(second).resolve()).lower()
    except OSError:
        first_text = str(first).lower()
        second_text = str(second).lower()
    return first_text == second_text


def needs_install_offer(program_files: Optional[Path] = None) -> bool:
    """True when Lazarus should offer to install itself.

    The offer is made only when every one of these holds:

    * this is the shipped program file (frozen), running on
      Windows — a source checkout never offers;
    * the file running is not already the installed copy;
    * there is no installed copy, or the installed copy is a
      different size from the one running — a stale or interrupted
      install heals by being offered again, never by being trusted;
    * the person has not turned the offer off with the
      ``LAZARUS_NO_INSTALL_OFFER`` environment variable.
    """
    if not is_frozen():
        return False
    if not _is_windows():
        return False
    if os.environ.get(NO_INSTALL_OFFER_ENV):
        return False
    running = current_exe()
    target = installed_exe(program_files)
    if _same_path(running, target):
        return False
    if not target.exists():
        return True
    try:
        return target.stat().st_size != running.stat().st_size
    except OSError:
        # An installed copy that cannot even be looked at is not
        # one to trust. Offer again and let the install heal it.
        return True


# ------------------------------------------------------------------
# The report
# ------------------------------------------------------------------


@dataclass
class InstallReport:
    """What an install or a removal did, step by step, in the open.

    ``steps`` holds one ``(step name, did it work, plain-language
    message)`` entry per step, in the order they were tried. An
    install or removal stops at the first step that fails, so the
    steps listed are exactly the work that was attempted — no more
    was done, and the summary says so.
    """

    steps: list = field(default_factory=list)
    action: str = "install"
    target: str = ""
    shortcut: str = ""

    @property
    def ok(self) -> bool:
        """True when every step that was tried worked."""
        return bool(self.steps) and all(
            worked for _name, worked, _message in self.steps
        )

    def summary_lines(self) -> list[str]:
        """The report as plain lines a person can read as-is."""
        lines: list[str] = []
        if self.ok:
            if self.action == "uninstall":
                lines.append("Lazarus has been removed from this computer.")
                for name, _worked, message in self.steps:
                    lines.append(f"{name}: {message}")
                lines.append(
                    "Copies you kept elsewhere — for example on a USB "
                    "stick — are untouched and still work."
                )
            else:
                lines.append("Lazarus is installed on this computer.")
                if self.target:
                    lines.append(f"Program: {self.target}")
                if self.shortcut:
                    lines.append(f"Start Menu shortcut: {self.shortcut}")
                for name, _worked, message in self.steps:
                    lines.append(f"{name}: {message}")
                lines.append(
                    "To run it, open the Start Menu and choose Lazarus."
                )
                lines.append(
                    "The copy you ran to install it can be kept or "
                    "deleted — the installed copy works on its own."
                )
            return lines
        installing = self.action != "uninstall"
        lines.append(
            "Lazarus is not fully installed."
            if installing
            else "Lazarus could not be fully removed."
        )
        for name, worked, message in self.steps:
            if worked:
                lines.append(f"Done — {name}: {message}")
            else:
                lines.append(f"Stopped here — {name}: {message}")
        lines.append(
            "Nothing after the step that stopped was done. "
            "What is listed as done above is exactly what is on "
            "this computer now — no more, no less."
        )
        return lines


def _refused(action: str, title: str, message: str) -> InstallReport:
    """A report for a refusal: one step, not done, saying why."""
    return InstallReport(steps=[(title, False, message)], action=action)


# ------------------------------------------------------------------
# The platform seam
# ------------------------------------------------------------------


class Shell(Protocol):
    """The handful of filesystem acts an install needs.

    Keeping them behind this seam is what makes the whole module
    testable anywhere: tests record these calls against a scratch
    folder instead of touching a real computer.
    """

    def make_dirs(self, path: Path) -> None: ...

    def copy_file(self, source: Path, destination: Path) -> None: ...

    def replace(self, source: Path, destination: Path) -> None: ...

    def file_size(self, path: Path) -> int: ...

    def exists(self, path: Path) -> bool: ...

    def remove_file(self, path: Path) -> None: ...

    def remove_tree(self, path: Path) -> None: ...

    def write_shortcut(
        self, target: Path, link_path: Path, description: str
    ) -> None: ...


def _powershell_quote(text: str) -> str:
    """``text`` as a single-quoted PowerShell string."""
    return "'" + str(text).replace("'", "''") + "'"


def shortcut_argv(target: Path, link_path: Path, description: str) -> list[str]:
    """The PowerShell command that writes one shortcut.

    Pure, so a test can pin the exact words. The shortcut is made
    through Windows' own ``WScript.Shell`` — the same mechanism
    Windows itself uses — and it is run as an argument list, never
    through a shell string, so a path can never turn into a second
    command.
    """
    script = "; ".join(
        [
            "$shell = New-Object -ComObject WScript.Shell",
            f"$link = $shell.CreateShortcut({_powershell_quote(link_path)})",
            f"$link.TargetPath = {_powershell_quote(target)}",
            f"$link.WorkingDirectory = {_powershell_quote(Path(target).parent)}",
            f"$link.Description = {_powershell_quote(description)}",
            "$link.Save()",
        ]
    )
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]


class WindowsShell:
    """The seam on a real Windows computer: os, shutil, PowerShell."""

    def make_dirs(self, path: Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def copy_file(self, source: Path, destination: Path) -> None:
        shutil.copyfile(source, destination)

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def file_size(self, path: Path) -> int:
        return Path(path).stat().st_size

    def exists(self, path: Path) -> bool:
        return Path(path).exists()

    def remove_file(self, path: Path) -> None:
        Path(path).unlink()

    def remove_tree(self, path: Path) -> None:
        shutil.rmtree(path)

    def write_shortcut(
        self, target: Path, link_path: Path, description: str
    ) -> None:
        subprocess.run(
            shortcut_argv(target, link_path, description),
            check=True,
            capture_output=True,
            timeout=30,
        )


# ------------------------------------------------------------------
# Installing
# ------------------------------------------------------------------

_NOT_SHIPPED_MESSAGE = (
    "Installing is something the shipped Lazarus.exe does on a "
    "Windows computer. This copy is not the shipped program file, "
    "so there is nothing here that can install itself. Run the "
    "Lazarus.exe that came from Sentinel Safeguard instead."
)

_NOT_WINDOWS_MESSAGE = (
    "Installing is something the shipped Lazarus.exe does on a "
    "Windows computer. This computer is not running Windows, so "
    "Lazarus cannot be installed here."
)

_NOT_SHIPPED_UNINSTALL_MESSAGE = (
    "Removing Lazarus is something the shipped Lazarus.exe does "
    "on a Windows computer. This copy is not the shipped program "
    "file, so it cannot remove Lazarus. Run the Lazarus.exe that "
    "came from Sentinel Safeguard instead."
)

_NOT_WINDOWS_UNINSTALL_MESSAGE = (
    "Removing Lazarus is something the shipped Lazarus.exe does "
    "on a Windows computer. This computer is not running Windows, "
    "so Lazarus cannot be removed here."
)


def _guard(action: str, title: str) -> Optional[InstallReport]:
    """The refusal both directions share, or None to go ahead.

    The words speak to the action that was asked for: a refusal
    to remove says removing, never installing.
    """
    removing = action == "uninstall"
    if not is_frozen():
        return _refused(
            action,
            title,
            _NOT_SHIPPED_UNINSTALL_MESSAGE if removing else _NOT_SHIPPED_MESSAGE,
        )
    if not _is_windows():
        return _refused(
            action,
            title,
            _NOT_WINDOWS_UNINSTALL_MESSAGE if removing else _NOT_WINDOWS_MESSAGE,
        )
    return None


def perform_install(
    shell: Optional[Shell] = None,
    program_files: Optional[Path] = None,
    program_data: Optional[Path] = None,
) -> InstallReport:
    """Install the running program file, step by verified step.

    Steps, each checked before the next begins: make the install
    folder; copy this program into it (to a temporary name first,
    then move it into place, so an interrupted copy never leaves
    half a program at the real name); check the copy is whole;
    write the Start Menu shortcut; check the shortcut is there.
    The first step that fails stops the install, and the report
    says exactly what was and was not done.
    """
    refused = _guard("install", "Install Lazarus")
    if refused is not None:
        return refused

    folder = install_folder(program_files)
    target = installed_exe(program_files)
    link = start_menu_shortcut(program_data)
    source = current_exe()
    if _same_path(source, target):
        # The copy running already IS the installed copy, in its
        # place: there is nothing to do, and the report says so
        # truthfully instead of trying (and, on Windows, failing)
        # to copy the running file over itself.
        return InstallReport(
            steps=[
                (
                    "Check this copy",
                    True,
                    "This is the installed copy of Lazarus, "
                    f"already in its place at {target} — there "
                    "was nothing to do.",
                )
            ],
            action="install",
            target=str(target),
            shortcut=str(link),
        )
    if shell is None:
        shell = WindowsShell()
    # The copy lands under a temporary name in the same folder and
    # is only moved over the real name once it is fully written.
    staging = folder / (_EXE_NAME + ".installing")
    report = InstallReport(
        action="install", target=str(target), shortcut=str(link)
    )
    steps = report.steps

    try:
        shell.make_dirs(folder)
    except OSError:
        steps.append(
            (
                "Make the install folder",
                False,
                f"The folder {folder} could not be created, so "
                "nothing was installed. You may need an "
                "administrator's permission to install programs "
                "on this computer.",
            )
        )
        return report
    steps.append(
        ("Make the install folder", True, f"Ready at {folder}.")
    )

    try:
        shell.copy_file(source, staging)
        shell.replace(staging, target)
    except OSError:
        # Pick up the half-written staging copy so nothing partial
        # is left behind under any name.
        try:
            if shell.exists(staging):
                shell.remove_file(staging)
        except OSError:
            pass
        steps.append(
            (
                "Copy Lazarus into place",
                False,
                "Lazarus could not be copied into the install "
                "folder, so no new copy was put in place. The "
                "install folder is there, but it does not hold a "
                "working Lazarus.",
            )
        )
        return report
    steps.append(
        (
            "Copy Lazarus into place",
            True,
            f"Lazarus.exe is in place at {target}.",
        )
    )

    try:
        whole = shell.exists(target) and shell.file_size(
            target
        ) == shell.file_size(source)
    except OSError:
        whole = False
    if not whole:
        steps.append(
            (
                "Check the copy",
                False,
                "The copy in the install folder is not the same "
                "size as the program you ran, so it should not be "
                "used. Run Lazarus again and accept the install "
                "offer — it will be offered again precisely "
                "because this copy is not whole — or copy "
                "Lazarus.exe there by hand.",
            )
        )
        return report
    steps.append(
        (
            "Check the copy",
            True,
            "The installed copy is the same size as the program "
            "you ran.",
        )
    )

    try:
        shell.make_dirs(link.parent)
        shell.write_shortcut(target, link, _SHORTCUT_DESCRIPTION)
    except (OSError, subprocess.SubprocessError):
        steps.append(
            (
                "Add the Start Menu shortcut",
                False,
                "The Start Menu shortcut could not be created. "
                "Lazarus itself IS installed — you can open it "
                f"from {target} — it just has no Start Menu entry.",
            )
        )
        return report
    steps.append(
        (
            "Add the Start Menu shortcut",
            True,
            f"Start Menu entry created at {link}.",
        )
    )

    if not shell.exists(link):
        steps.append(
            (
                "Check the shortcut",
                False,
                "The Start Menu shortcut is not there after being "
                "written, so the install cannot promise it. "
                "Lazarus itself IS installed — you can open it "
                f"from {target}.",
            )
        )
        return report
    steps.append(
        ("Check the shortcut", True, "The Start Menu entry is there.")
    )
    return report


# ------------------------------------------------------------------
# Removing
# ------------------------------------------------------------------


def perform_uninstall(
    shell: Optional[Shell] = None,
    program_files: Optional[Path] = None,
    program_data: Optional[Path] = None,
) -> InstallReport:
    """Remove an installed Lazarus: the shortcut, then the folder.

    The installed copy cannot remove itself — a program cannot
    delete the file Windows is running. Run the portable copy
    (the one on your USB stick or in your downloads) and ask it to
    remove the installed one instead. A computer with nothing
    installed is a clean report, not an error: there was simply
    nothing to do.
    """
    refused = _guard("uninstall", "Remove Lazarus")
    if refused is not None:
        return refused

    folder = install_folder(program_files)
    target = installed_exe(program_files)
    link = start_menu_shortcut(program_data)
    if _same_path(current_exe(), target):
        return _refused(
            "uninstall",
            "Remove Lazarus",
            "This is the installed copy of Lazarus, and a program "
            "cannot remove the very file that is running. To "
            "remove the installed copy, run Lazarus from somewhere "
            "else — the portable copy on a USB stick or in your "
            "downloads — and choose to remove Lazarus there. "
            "Nothing has been removed yet.",
        )
    if shell is None:
        shell = WindowsShell()

    report = InstallReport(
        action="uninstall", target=str(target), shortcut=str(link)
    )
    steps = report.steps

    try:
        if shell.exists(link):
            shell.remove_file(link)
            if shell.exists(link):
                raise OSError("shortcut still there after removal")
            steps.append(
                (
                    "Remove the Start Menu shortcut",
                    True,
                    "The Start Menu entry is gone.",
                )
            )
        else:
            steps.append(
                (
                    "Remove the Start Menu shortcut",
                    True,
                    "There was no Start Menu entry to remove.",
                )
            )
    except OSError:
        steps.append(
            (
                "Remove the Start Menu shortcut",
                False,
                "The Start Menu entry at "
                f"{link} could not be removed, so nothing else "
                "was removed either. You may need an "
                "administrator's permission.",
            )
        )
        return report

    try:
        if shell.exists(folder):
            shell.remove_tree(folder)
            if shell.exists(folder):
                raise OSError("folder still there after removal")
            steps.append(
                (
                    "Remove the install folder",
                    True,
                    f"The folder {folder} and everything in it "
                    "are gone.",
                )
            )
        else:
            steps.append(
                (
                    "Remove the install folder",
                    True,
                    "There was no install folder to remove.",
                )
            )
    except OSError:
        steps.append(
            (
                "Remove the install folder",
                False,
                f"The install folder at {folder} could not be "
                "fully removed. The Start Menu entry is already "
                "gone; whatever is left is in that folder, and "
                "you can delete it by hand once no Lazarus "
                "window is open.",
            )
        )
        return report
    return report


# ------------------------------------------------------------------
# Asking Windows for an administrator's permission
# ------------------------------------------------------------------


def elevate_and_run(mode: str) -> bool:
    """Relaunch this program as administrator to install or remove.

    Installing into Program Files needs an administrator's say-so.
    This asks Windows to start the same program file again,
    elevated, with ``--lazarus-install`` or
    ``--lazarus-uninstall`` — the interface in that elevated copy
    does the work and shows the report. Returns True only when
    Windows agreed to start it; False everywhere else, including
    off Windows and when the person declines the prompt.
    """
    if mode not in ("install", "uninstall"):
        raise ValueError(
            f"Unknown install mode {mode!r}: use 'install' or 'uninstall'."
        )
    if not _is_windows():
        return False
    try:
        result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None,
            "runas",
            str(current_exe()),
            f"--lazarus-{mode}",
            None,
            _SW_SHOWNORMAL,
        )
    except (AttributeError, OSError):
        return False
    return int(result) > 32
