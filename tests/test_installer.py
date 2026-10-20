"""The self-install module: every path through it, off Windows.

A recording shell stands in for the real filesystem seam, doing
its work in scratch folders so existence and size checks behave
for real while no test ever touches an actual Program Files.
Frozen-ness, Windows-ness, and the running program file are all
monkeypatched — the three facts the module reads from the machine.
"""

import shutil
import sys
from pathlib import Path

import pytest

from lazarus import installer
from lazarus.installer import (
    InstallReport,
    canonical_exe,
    current_exe,
    elevate_and_run,
    install_folder,
    is_frozen,
    needs_install_offer,
    perform_install,
    perform_uninstall,
    shortcut_argv,
    start_menu_shortcut,
)

SOURCE_BYTES = b"LAZARUS-PROGRAM" * 64


class RecordingShell:
    """The seam, recorded, working on real scratch files.

    ``fail_on`` names methods that raise instead of working;
    ``truncate_copy`` makes copy_file write half the bytes, the
    way an interrupted copy would.
    """

    def __init__(self, fail_on=(), truncate_copy=False):
        self.calls = []
        self.fail_on = set(fail_on)
        self.truncate_copy = truncate_copy

    def _record(self, name, *args):
        self.calls.append((name, args))
        if name in self.fail_on:
            raise OSError(f"{name} was told to fail by the test")

    def make_dirs(self, path):
        self._record("make_dirs", Path(path))
        Path(path).mkdir(parents=True, exist_ok=True)

    def copy_file(self, source, destination):
        self._record("copy_file", Path(source), Path(destination))
        data = Path(source).read_bytes()
        if self.truncate_copy:
            data = data[: len(data) // 2]
        Path(destination).write_bytes(data)

    def replace(self, source, destination):
        self._record("replace", Path(source), Path(destination))
        Path(source).replace(destination)

    def file_size(self, path):
        self._record("file_size", Path(path))
        return Path(path).stat().st_size

    def exists(self, path):
        self._record("exists", Path(path))
        return Path(path).exists()

    def remove_file(self, path):
        self._record("remove_file", Path(path))
        Path(path).unlink()

    def remove_tree(self, path):
        self._record("remove_tree", Path(path))
        shutil.rmtree(path)

    def write_shortcut(self, target, link_path, description):
        self._record(
            "write_shortcut", Path(target), Path(link_path), description
        )
        Path(link_path).write_text(
            f"shortcut to {target}", encoding="utf-8"
        )

    def call_names(self):
        return [name for name, _args in self.calls]


@pytest.fixture()
def bases(tmp_path):
    return tmp_path / "Program Files", tmp_path / "ProgramData"


@pytest.fixture()
def running_exe(tmp_path):
    exe = tmp_path / "Downloads" / "Lazarus.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(SOURCE_BYTES)
    return exe


@pytest.fixture()
def shipped(monkeypatch, running_exe):
    """Pretend to be the shipped program file, running on Windows."""
    monkeypatch.setattr(installer, "is_frozen", lambda: True)
    monkeypatch.setattr(installer, "_is_windows", lambda: True)
    monkeypatch.setattr(installer, "current_exe", lambda: running_exe)
    monkeypatch.delenv("LAZARUS_NO_INSTALL_OFFER", raising=False)
    return running_exe


# ------------------------------------------------------------------
# Basics and path layout
# ------------------------------------------------------------------


def test_not_frozen_and_current_exe_in_a_source_checkout():
    assert is_frozen() is False
    assert current_exe() == Path(sys.executable).resolve()


def test_canonical_layout(bases):
    program_files, program_data = bases
    folder = install_folder(program_files)
    assert folder == program_files / "Sentinel Safeguard" / "Lazarus"
    assert canonical_exe(program_files) == folder / "Lazarus.exe"
    assert start_menu_shortcut(program_data) == (
        program_data
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Lazarus.lnk"
    )


def test_path_builders_default_to_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "PD"))
    assert install_folder() == (
        tmp_path / "PF" / "Sentinel Safeguard" / "Lazarus"
    )
    assert start_menu_shortcut() == (
        tmp_path / "PD"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Lazarus.lnk"
    )


# ------------------------------------------------------------------
# needs_install_offer: the truth table
# ------------------------------------------------------------------


def test_offer_not_frozen(monkeypatch, bases, running_exe):
    monkeypatch.setattr(installer, "is_frozen", lambda: False)
    monkeypatch.setattr(installer, "_is_windows", lambda: True)
    monkeypatch.setattr(installer, "current_exe", lambda: running_exe)
    assert needs_install_offer(bases[0]) is False


def test_offer_off_windows_blocked(monkeypatch, shipped, bases):
    assert needs_install_offer(bases[0]) is True  # sanity: shipped setup
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    assert needs_install_offer(bases[0]) is False


def test_offer_when_canonical_absent(shipped, bases):
    assert needs_install_offer(bases[0]) is True


def test_offer_when_already_the_installed_copy(
    monkeypatch, shipped, bases
):
    target = canonical_exe(bases[0])
    target.parent.mkdir(parents=True)
    target.write_bytes(SOURCE_BYTES)
    monkeypatch.setattr(installer, "current_exe", lambda: target)
    assert needs_install_offer(bases[0]) is False


def test_no_offer_when_installed_copy_matches(shipped, bases):
    target = canonical_exe(bases[0])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"X" * len(SOURCE_BYTES))
    assert needs_install_offer(bases[0]) is False


def test_offer_when_installed_copy_is_stale_size(shipped, bases):
    target = canonical_exe(bases[0])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old and shorter")
    assert needs_install_offer(bases[0]) is True


def test_offer_suppressed_by_environment(monkeypatch, shipped, bases):
    monkeypatch.setenv("LAZARUS_NO_INSTALL_OFFER", "1")
    assert needs_install_offer(bases[0]) is False


# ------------------------------------------------------------------
# perform_install
# ------------------------------------------------------------------


def test_install_happy_path_step_order_and_verification(shipped, bases):
    program_files, program_data = bases
    shell = RecordingShell()
    report = perform_install(
        shell=shell, program_files=program_files, program_data=program_data
    )
    assert report.ok is True
    assert [name for name, _ok, _msg in report.steps] == [
        "Make the install folder",
        "Copy Lazarus into place",
        "Check the copy",
        "Add the Start Menu shortcut",
        "Check the shortcut",
    ]
    assert all(worked for _n, worked, _m in report.steps)
    target = canonical_exe(program_files)
    assert target.read_bytes() == SOURCE_BYTES
    assert start_menu_shortcut(program_data).exists()
    # The copy went through a staging name, then a replace — never
    # a direct write at the canonical path.
    copies = [c for c in shell.calls if c[0] == "copy_file"]
    assert copies[0][1][1] != target
    assert ("replace", (copies[0][1][1], target)) in shell.calls
    summary = "\n".join(report.summary_lines())
    assert "installed" in summary
    assert str(target) in summary
    assert "Start Menu" in summary


def test_install_copy_failure_is_truthful_and_leaves_no_exe(
    shipped, bases
):
    program_files, program_data = bases
    shell = RecordingShell(fail_on={"copy_file"})
    report = perform_install(
        shell=shell, program_files=program_files, program_data=program_data
    )
    assert report.ok is False
    assert [name for name, _ok, _m in report.steps] == [
        "Make the install folder",
        "Copy Lazarus into place",
    ]
    target = canonical_exe(program_files)
    assert not target.exists()
    # No staging leftover under any name in the install folder.
    assert list(install_folder(program_files).iterdir()) == []
    failed = report.steps[-1]
    assert failed[1] is False
    assert "no new copy was put in place" in failed[2]
    summary = "\n".join(report.summary_lines())
    assert "not fully installed" in summary
    assert "Copy Lazarus into place" in summary


def test_install_shortcut_failure_keeps_the_exe_and_says_so(
    shipped, bases
):
    program_files, program_data = bases
    shell = RecordingShell(fail_on={"write_shortcut"})
    report = perform_install(
        shell=shell, program_files=program_files, program_data=program_data
    )
    assert report.ok is False
    target = canonical_exe(program_files)
    assert target.read_bytes() == SOURCE_BYTES  # the exe IS installed
    assert not start_menu_shortcut(program_data).exists()
    failed = report.steps[-1]
    assert failed[0] == "Add the Start Menu shortcut"
    assert "IS installed" in failed[2]


def test_install_truncated_copy_fails_the_check(shipped, bases):
    program_files, program_data = bases
    shell = RecordingShell(truncate_copy=True)
    report = perform_install(
        shell=shell, program_files=program_files, program_data=program_data
    )
    assert report.ok is False
    assert report.steps[-1][0] == "Check the copy"
    # ...and the offer heals it: the bad copy differs in size.
    assert needs_install_offer(program_files) is True


def test_install_folder_failure_stops_at_step_one(shipped, bases):
    shell = RecordingShell(fail_on={"make_dirs"})
    report = perform_install(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert len(report.steps) == 1
    assert report.steps[0][0] == "Make the install folder"


def test_install_refused_when_not_frozen(monkeypatch, bases):
    monkeypatch.setattr(installer, "is_frozen", lambda: False)
    monkeypatch.setattr(installer, "_is_windows", lambda: True)
    shell = RecordingShell()
    report = perform_install(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert "shipped Lazarus.exe" in report.steps[0][2]
    assert shell.calls == []


def test_install_refused_off_windows(monkeypatch, shipped, bases):
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    shell = RecordingShell()
    report = perform_install(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert "not running Windows" in report.steps[0][2]
    assert shell.calls == []


# ------------------------------------------------------------------
# perform_uninstall
# ------------------------------------------------------------------


def _install_first(shipped, bases):
    shell = RecordingShell()
    report = perform_install(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is True


def test_uninstall_clean(shipped, bases):
    _install_first(shipped, bases)
    shell = RecordingShell()
    report = perform_uninstall(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is True
    assert not start_menu_shortcut(bases[1]).exists()
    assert not install_folder(bases[0]).exists()
    summary = "\n".join(report.summary_lines())
    assert "removed" in summary


def test_uninstall_nothing_installed_is_clean(shipped, bases):
    shell = RecordingShell()
    report = perform_uninstall(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is True
    assert all("no" in message.lower() for _n, _o, message in report.steps)


def test_uninstall_partial_shortcut_only(shipped, bases):
    link = start_menu_shortcut(bases[1])
    link.parent.mkdir(parents=True)
    link.write_text("shortcut", encoding="utf-8")
    report = perform_uninstall(
        shell=RecordingShell(),
        program_files=bases[0],
        program_data=bases[1],
    )
    assert report.ok is True
    assert not link.exists()
    assert report.steps[1][2].startswith("There was no install folder")


def test_uninstall_partial_folder_only(shipped, bases):
    target = canonical_exe(bases[0])
    target.parent.mkdir(parents=True)
    target.write_bytes(SOURCE_BYTES)
    report = perform_uninstall(
        shell=RecordingShell(),
        program_files=bases[0],
        program_data=bases[1],
    )
    assert report.ok is True
    assert not install_folder(bases[0]).exists()
    assert report.steps[0][2].startswith("There was no Start Menu")


def test_uninstall_from_the_installed_copy_refuses(
    monkeypatch, shipped, bases
):
    _install_first(shipped, bases)
    target = canonical_exe(bases[0])
    monkeypatch.setattr(installer, "current_exe", lambda: target)
    shell = RecordingShell()
    report = perform_uninstall(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert "portable copy" in report.steps[0][2]
    assert shell.calls == []
    assert target.exists()  # untouched


def test_uninstall_refused_off_windows(monkeypatch, shipped, bases):
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    shell = RecordingShell()
    report = perform_uninstall(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert shell.calls == []


def test_uninstall_shortcut_removal_failure_stops(shipped, bases):
    _install_first(shipped, bases)
    shell = RecordingShell(fail_on={"remove_file"})
    report = perform_uninstall(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert len(report.steps) == 1
    assert install_folder(bases[0]).exists()  # folder step never ran


# ------------------------------------------------------------------
# The shortcut command and elevation
# ------------------------------------------------------------------


def test_shortcut_argv_shape(tmp_path):
    target = tmp_path / "Sentinel Safeguard" / "Lazarus" / "Lazarus.exe"
    link = tmp_path / "Programs" / "Lazarus.lnk"
    argv = shortcut_argv(target, link, "Lazarus — backup and recovery")
    assert argv[:4] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    script = argv[4]
    assert "WScript.Shell" in script
    assert "CreateShortcut" in script
    assert str(link) in script
    assert str(target) in script
    assert "Lazarus — backup and recovery" in script


def test_shortcut_argv_escapes_single_quotes(tmp_path):
    target = tmp_path / "Bob's" / "Lazarus.exe"
    argv = shortcut_argv(target, tmp_path / "Lazarus.lnk", "it's Lazarus")
    assert "Bob''s" in argv[4]
    assert "it''s Lazarus" in argv[4]


def test_elevate_off_windows_is_false(monkeypatch):
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    assert elevate_and_run("install") is False
    assert elevate_and_run("uninstall") is False


def test_elevate_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        elevate_and_run("sideways")


def test_empty_report_is_not_ok():
    assert InstallReport().ok is False


# ------------------------------------------------------------------
# Same-path install and per-action refusal words (F-12 / F-13)
# ------------------------------------------------------------------


def test_install_from_the_installed_copy_reports_nothing_to_do(
    monkeypatch, shipped, bases
):
    target = canonical_exe(bases[0])
    target.parent.mkdir(parents=True)
    target.write_bytes(SOURCE_BYTES)
    monkeypatch.setattr(installer, "current_exe", lambda: target)
    shell = RecordingShell()
    report = perform_install(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is True
    assert len(report.steps) == 1
    name, worked, message = report.steps[0]
    assert name == "Check this copy"
    assert worked is True
    assert "This is the installed copy of Lazarus" in message
    assert f"already in its place at {target}" in message
    assert "there was nothing to do" in message
    assert shell.calls == []  # the seam was never touched
    assert target.read_bytes() == SOURCE_BYTES


def test_uninstall_refusal_when_not_shipped_speaks_of_removing(
    monkeypatch, bases
):
    monkeypatch.setattr(installer, "is_frozen", lambda: False)
    monkeypatch.setattr(installer, "_is_windows", lambda: True)
    shell = RecordingShell()
    report = perform_uninstall(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert shell.calls == []
    text = report.steps[0][2]
    assert "Removing Lazarus is something the shipped Lazarus.exe does" in text
    assert "can install itself" not in text
    assert "install itself" not in text


def test_uninstall_refusal_off_windows_speaks_of_removing(
    monkeypatch, shipped, bases
):
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    shell = RecordingShell()
    report = perform_uninstall(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert shell.calls == []
    text = report.steps[0][2]
    assert "Removing Lazarus is something" in text
    assert "cannot be removed here" in text
    assert "cannot be installed here" not in text


def test_install_refusals_stay_install_flavoured(monkeypatch, shipped, bases):
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    shell = RecordingShell()
    report = perform_install(
        shell=shell, program_files=bases[0], program_data=bases[1]
    )
    assert report.ok is False
    assert shell.calls == []
    text = report.steps[0][2]
    assert "Installing is something the shipped Lazarus.exe does" in text
    assert "cannot be installed here" in text
