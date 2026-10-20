"""Regression pins from the 2026-10-20 kit-wide Windows bug hunt.

Each test names the defect it answers. Every one was checked to
fail against the code as shipped in v1.0.0, for the reason stated.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import lazarus.backup as backup_module
import lazarus.schedule as schedule_module
import lazarus.unlock as unlock_module
from lazarus.errors import RescueError, RestoreRefusedError
from lazarus.installer import WindowsShell
from lazarus.model import is_safe_relative_path
from lazarus.rescue import rescue_to_image
from lazarus.restore import restore
from lazarus.snapshot import create_snapshot
from lazarus.source import device_id_of
from lazarus.undelete import _safe_output_name


def _rewrite_manifest(store, manifest, entries):
    path = store.root / "snapshots" / manifest.id / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"] = entries
    path.write_text(json.dumps(data), encoding="utf-8")
    return store.load_manifest(manifest.id)


def _file_entry(manifest, path):
    return next(
        {"path": e.path, "type": e.type, "size": e.size, "sha256": e.sha256}
        for e in manifest.entries
        if e.path == path
    )


# 1 — os.kill(pid, 0) is not a liveness probe on Windows: Python
# hands the signal to TerminateProcess, so checking whether the
# lock-holding backup was still running would have KILLED it (and,
# on a reused pid, an unrelated program). The Windows verdict must
# come from the kernel, never from os.kill.

def test_windows_pid_liveness_never_calls_os_kill(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(backup_module, "_pid_alive_windows", lambda pid: True)

    def forbidden_kill(pid, sig):  # pragma: no cover - the bug itself
        raise AssertionError("os.kill was used as a Windows liveness probe")

    monkeypatch.setattr(os, "kill", forbidden_kill)
    assert backup_module._default_pid_alive(1234) is True


def test_posix_pid_liveness_still_works():
    assert backup_module._default_pid_alive(os.getpid()) is True
    assert backup_module._default_pid_alive(0) is False
    assert backup_module._default_pid_alive(-3) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only verdict")
def test_windows_pid_probe_leaves_the_other_process_alive():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        assert backup_module._default_pid_alive(child.pid) is True
        assert child.poll() is None  # the probe must not kill it
    finally:
        child.terminate()
        child.wait(timeout=10)


# 2 — Windows tool shell-outs had no timeout and decoded tool
# output with the locale code page: a hung manage-bde / PowerShell
# / schtasks hung Lazarus forever, and non-ASCII output (a volume
# label, a task path) could crash the decode outright.

def _capture_run(monkeypatch, module):
    captured = {}

    class _Done:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return _Done()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return captured


def test_unlock_executor_is_bounded_and_decode_safe(monkeypatch):
    captured = _capture_run(monkeypatch, unlock_module)
    unlock_module._subprocess_execute(["manage-bde.exe", "-status", "E:"])
    assert captured.get("timeout")
    assert captured.get("errors") == "replace"


def test_schedule_executor_is_bounded_and_decode_safe(monkeypatch):
    captured = _capture_run(monkeypatch, schedule_module)
    schedule_module._subprocess_execute(["schtasks.exe", "/Query"])
    assert captured.get("timeout")
    assert captured.get("errors") == "replace"


def test_shortcut_powershell_is_bounded(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr("lazarus.installer.subprocess.run", fake_run)
    WindowsShell().write_shortcut(
        tmp_path / "Lazarus.exe", tmp_path / "Lazarus.lnk", "desc"
    )
    assert captured.get("timeout")


# 3 — Manifest paths a Windows restore must refuse: an ADS colon
# in ANY segment (the bytes would land in a hidden stream), a
# reserved device name, and a trailing dot or space (Windows
# strips it, so the file lands on top of a different name).

@pytest.mark.parametrize(
    "path",
    [
        "docs/file.txt:hidden-stream",
        "file.txt:stream",
        "CON.txt",
        "docs/NUL",
        "com1.txt",
        "docs/LPT3",
        "docs/file.",
        "docs/file ",
        "trailing.",
        "trailing ",
    ],
)
def test_windows_unsafe_manifest_paths_are_not_safe(path):
    assert not is_safe_relative_path(path)


@pytest.mark.parametrize(
    "path", ["docs/notes.txt", "photo.bin", "a.b/c-d_e.txt", "console.txt"]
)
def test_ordinary_manifest_paths_stay_safe(path):
    assert is_safe_relative_path(path)


def test_ads_manifest_entry_is_refused_before_any_write(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    notes = _file_entry(manifest, "docs/notes.txt")
    crafted = _rewrite_manifest(
        store, manifest, entries=[{**notes, "path": "docs/notes.txt:secret"}]
    )
    with pytest.raises(RestoreRefusedError):
        restore(store, destination=tmp_path / "out", snapshot_id=crafted.id)
    assert not (tmp_path / "out" / "docs" / "notes.txt").exists()


# 4 — Two manifest entries differing only by letter case are ONE
# name on NTFS: restoring both would silently overwrite the first
# with the second while the report claimed both came back.

def test_case_only_duplicate_manifest_is_refused(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    notes = _file_entry(manifest, "docs/notes.txt")
    report_entry = _file_entry(manifest, "docs/report.txt")
    crafted = _rewrite_manifest(
        store,
        manifest,
        entries=[notes, {**report_entry, "path": "docs/NOTES.txt"}],
    )
    with pytest.raises(RestoreRefusedError):
        restore(store, destination=tmp_path / "out", snapshot_id=crafted.id)


# 5 — A restore must not write THROUGH a directory junction: the
# islink check alone misses junctions on Windows builds.

def test_junction_in_restore_path_blocks_the_write(store, tree, tmp_path, monkeypatch):
    manifest = create_snapshot(store, tree)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    out = tmp_path / "dest"
    (out / "docs").mkdir(parents=True)

    real_is_junction = Path.is_junction

    def fake_is_junction(self):
        if self == out / "docs":
            return True
        return real_is_junction(self)

    monkeypatch.setattr(Path, "is_junction", fake_is_junction)
    report = restore(store, destination=out, snapshot_id=manifest.id)
    outcomes = {i.path: i.outcome for i in report.items}
    assert outcomes["docs/notes.txt"] == "failed"
    assert not (outside / "notes.txt").exists()
    assert not (out / "docs" / "notes.txt").exists()


# 6 — Recovered-file names come from a dead drive's records: the
# same Windows name traps apply to them (device names, ADS
# colons, trailing dots/spaces Windows would strip onto a
# collision).

@pytest.mark.parametrize(
    "raw,expected_prefix",
    [("CON", "_CON"), ("aux.txt", "_aux.txt"), ("NUL.bin", "_NUL.bin")],
)
def test_recovered_device_names_are_defused(raw, expected_prefix):
    assert _safe_output_name(raw, set()) == expected_prefix


def test_recovered_ads_and_trailing_names_are_defused():
    taken: set[str] = set()
    out = _safe_output_name("file.txt:hidden", taken)
    assert ":" not in out
    assert _safe_output_name("name.", set()) == "name"
    assert _safe_output_name("name ", set()) == "name"


# 7 — A rescue of a real drive must refuse, before a byte is
# written, when the image (or its progress file) would land on
# that same drive. (An image FILE is exempt: copying a file to
# another file on the same volume overwrites nothing of it.)

class _DeviceDrive:
    """A drive-shaped source: a device id, a size, and bytes."""

    def __init__(self, data: bytes, device_id: int):
        self._data = data
        self.size = len(data)
        self.device_id = device_id

    def read_at(self, offset, length):
        return self._data[offset : offset + length]


def test_rescue_refuses_an_image_on_the_source_drive(tmp_path):
    drive = _DeviceDrive(b"\x22" * 8192, device_id_of(tmp_path))
    dest = tmp_path / "rescue.img"
    with pytest.raises(RescueError):
        rescue_to_image(drive, dest, block_size=4096)
    assert not dest.exists()
