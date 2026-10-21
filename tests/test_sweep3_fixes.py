"""Fleet sweep #3 (2026-10-21) — the fixes, pinned.

- The engine refuses a pipe or a socket BEFORE opening it: the
  open used to come first, and opening a pipe to read waits for
  a writer — the command line could be made to hang forever on
  a source the window already refused in its own layer.
- A path that points back on itself (a shortcut loop) gets a
  plain refusal everywhere the engines follow shortcuts —
  rescue, recovery, snapshot, restore — instead of a raw
  RuntimeError out of Path.resolve().
- Recovery streams: a deleted file is written a chunk at a
  time and hashed as it lands, never held whole in memory, and
  a copy that cannot be written whole is not left behind under
  the recovered name.
- A backup run whose history line cannot be written still
  reports its own true outcome instead of raising over it.
- The size estimate follows the same future-dating rule as
  every other history derivation.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lazarus import backup as backup_mod
from lazarus.backup import estimate_backup_bytes, run_backup
from lazarus.backup_model import (
    OUTCOME_COMPLETE,
    RETENTION_COUNT,
    BackupConfig,
    History,
    HistoryEntry,
    RetentionSpec,
    ScheduleSpec,
)
from lazarus.errors import (
    BackupError,
    RecoveryRefusedError,
    RescueError,
    RestoreRefusedError,
    SourceError,
    SourceNotFoundError,
    StoreError,
)
from lazarus.hashing import hash_bytes
from lazarus.recovery_model import DeletedFile, Extent
from lazarus.rescue import rescue_to_image
from lazarus.restore import _check_destination
from lazarus.snapshot import create_snapshot
from lazarus.source import ReadOnlySource
from lazarus.store import SnapshotStore
from lazarus.undelete import _RECOVERY_CHUNK, _recover_one, assess, recover_deleted

UTC = timezone.utc


# ---------------------------------------------------------------------
# Pipes and sockets are refused before any open can block


def _mkfifo(path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("this computer cannot make a pipe (no os.mkfifo)")
    os.mkfifo(path)


def test_pipe_source_is_refused_before_it_can_block(tmp_path):
    fifo = tmp_path / "a-pipe"
    _mkfifo(fifo)
    # On the unpatched engine this line never returns: opening a
    # pipe to read waits for a writer that is never coming.
    with pytest.raises(SourceError) as caught:
        ReadOnlySource(fifo)
    assert "is a pipe" in str(caught.value)


def test_socket_source_is_refused_before_open(tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("this Python has no filesystem sockets (AF_UNIX)")
    sock_path = tmp_path / "a-socket"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        with pytest.raises(SourceError) as caught:
            ReadOnlySource(sock_path)
        assert "is a socket" in str(caught.value)
    finally:
        server.close()


def test_shortcut_to_a_pipe_is_refused_too(tmp_path):
    fifo = tmp_path / "a-pipe"
    _mkfifo(fifo)
    link = tmp_path / "a-shortcut"
    try:
        link.symlink_to(fifo)
    except (OSError, NotImplementedError):
        pytest.skip("shortcuts not available here")
    with pytest.raises(SourceError) as caught:
        ReadOnlySource(link)
    assert "is a pipe" in str(caught.value)


def test_plain_file_source_still_opens(tmp_path):
    image = tmp_path / "drive.img"
    image.write_bytes(b"\x00" * 4096)
    with ReadOnlySource(image) as source:
        assert source.size == 4096
        assert source.opened_read_only


# ---------------------------------------------------------------------
# Shortcut loops get a plain refusal, never a raw RuntimeError


def _make_loop(tmp_path: Path) -> Path:
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(tmp_path / "loop", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("shortcuts not available here")
    return loop


# The cross-platform contract for a looping destination is a
# DESIGNED refusal before anything is written — never a raw
# RuntimeError, never a traceback. Where the platform's path
# resolver reports the loop (POSIX does; Windows' does not, for
# a loop in a parent folder), the refusal names the loop
# itself; on Windows the refusal lands one designed check
# later, in that check's own plain words (the folder cannot be
# created there; the folder is on the same drive). Both shapes
# are pinned for what they are.


def test_rescue_into_a_shortcut_loop_is_a_plain_refusal(tmp_path):
    source = tmp_path / "drive.img"
    source.write_bytes(b"\x00" * 8192)
    loop = _make_loop(tmp_path)
    with pytest.raises(RescueError) as caught:
        rescue_to_image(source, loop / "image.img")
    if os.name != "nt":
        assert "points back on itself" in str(caught.value)


def test_recover_into_a_shortcut_loop_is_a_plain_refusal(tmp_path):
    source = tmp_path / "drive.img"
    source.write_bytes(b"\x00" * 8192)
    loop = _make_loop(tmp_path)
    with pytest.raises(RecoveryRefusedError) as caught:
        recover_deleted(source, loop / "out")
    if os.name != "nt":
        assert "points back on itself" in str(caught.value)


def test_snapshot_of_a_shortcut_loop_is_a_plain_refusal(tmp_path):
    store = SnapshotStore.create(tmp_path / "store")
    loop = _make_loop(tmp_path)
    with pytest.raises(SourceNotFoundError) as caught:
        create_snapshot(store, loop)
    assert "points back on itself" in str(caught.value)


def test_restore_into_a_shortcut_loop_is_a_plain_refusal(tmp_path):
    store = SimpleNamespace(root=tmp_path / "store")
    loop = _make_loop(tmp_path)
    # On Windows the resolver stays silent about a parent-folder
    # loop, so the refusal arrives as the destination check's own
    # "could not use" StoreError instead — still designed, still
    # plain, still before anything is written.
    with pytest.raises((RestoreRefusedError, StoreError)) as caught:
        _check_destination(store, loop / "out")
    if os.name != "nt":
        assert isinstance(caught.value, RestoreRefusedError)
        assert "points back on itself" in str(caught.value)


# ---------------------------------------------------------------------
# Recovery streams, in chunks, with the same honest accounting


class RecordingSource:
    """A source that remembers the largest read it was asked for."""

    def __init__(self, data: bytes):
        self._data = bytes(data)
        self.size = len(self._data)
        self.max_read = 0

    def read_at(self, offset, length):
        self.max_read = max(self.max_read, length)
        if offset >= self.size:
            return b""
        return self._data[offset : offset + length]


def test_recovery_streams_a_large_file_in_chunks(tmp_path):
    content = bytes(range(256)) * 20000  # ~5 MiB, several chunks
    record = DeletedFile(
        name="BIG.BIN",
        fs_type="NTFS",
        size=len(content),
        extents=[Extent(0, len(content), False)],
    )
    source = RecordingSource(content)
    out = tmp_path / "BIG.BIN"
    item = _recover_one(source, assess(record), out)
    assert item.outcome == "recovered"
    assert item.bytes_written == len(content)
    assert item.sha256 == hash_bytes(content)
    assert out.read_bytes() == content
    assert source.max_read <= _RECOVERY_CHUNK


def test_recovery_short_reads_still_count_missing_and_pad(tmp_path):
    class ShortSource:
        size = 100

        def read_at(self, offset, length):
            return b"x" * 40  # answers 40 of the first 100, then nothing

    record = DeletedFile(
        name="SHORT.BIN",
        fs_type="NTFS",
        size=100,
        extents=[Extent(0, 100, False)],
    )
    out = tmp_path / "SHORT.BIN"
    item = _recover_one(ShortSource(), assess(record), out)
    assert item.outcome == "partial"
    assert item.missing_bytes == 60
    assert out.read_bytes() == b"x" * 40 + b"\x00" * 60


# ---------------------------------------------------------------------
# A backup run reports its own truth even when history cannot be written


def _config(store_root, sources) -> BackupConfig:
    return BackupConfig(
        store=str(store_root),
        sources=[str(source) for source in sources],
        schedule=ScheduleSpec(every="daily", at="02:00"),
        retention=RetentionSpec(mode=RETENTION_COUNT, count=10, days=30),
        verify_every_runs=7,
        created_utc="2026-10-20T00:00:00.000000Z",
        tool_version="1.0.2",
    )


def _no_history(monkeypatch):
    def broken_append(path, entry):
        raise BackupError("the history cannot be written (told-you-so)")

    monkeypatch.setattr(backup_mod, "append_history", broken_append)


def test_completed_backup_still_reports_complete_when_history_fails(
    store, tree, tmp_path, monkeypatch
):
    _no_history(monkeypatch)
    config = _config(store.root, [tree])
    result = run_backup(
        config,
        tmp_path / "cfg" / "backup.json",
        now=datetime(2026, 10, 21, 12, tzinfo=UTC),
        free_space_fn=lambda path: 10**12,
    )
    assert result.outcome == OUTCOME_COMPLETE
    assert "could not be written into the backup history" in result.message
    assert any(
        "could not be written" in note for note in result.history_entry.notes
    )
    # The backup itself really happened; only its history line is missing.
    assert result.history_entry.snapshots


def test_refused_backup_still_reports_refusal_when_history_fails(
    tmp_path, monkeypatch
):
    _no_history(monkeypatch)
    config = _config(tmp_path / "no-such-store", [])
    result = run_backup(
        config,
        tmp_path / "cfg" / "backup.json",
        now=datetime(2026, 10, 21, 12, tzinfo=UTC),
        free_space_fn=lambda path: 10**12,
    )
    assert result.outcome == "refused"
    assert "could not be written into the backup history" in result.message


# ---------------------------------------------------------------------
# The estimate ignores runs that claim to be from the future


def _entry(run_id: str, **overrides) -> HistoryEntry:
    fields = dict(
        run_id=run_id,
        started_utc="2026-10-20T02:00:00.000000Z",
        finished_utc="2026-10-20T02:00:01.000000Z",
        trigger="manual",
        outcome=OUTCOME_COMPLETE,
        snapshots=["snap-one"],
        sources=["/somewhere"],
        files_saved=3,
        bytes_saved=30,
        bytes_copied=30,
        seconds=1.0,
    )
    fields.update(overrides)
    return HistoryEntry(**fields)


def test_estimate_ignores_a_future_dated_last_run(tmp_path):
    source = tmp_path / "docs"
    source.mkdir()
    (source / "a.txt").write_text("hello", encoding="utf-8")
    config = _config(tmp_path / "store", [source])
    history = History(
        entries=[
            _entry("real-last", bytes_copied=10),
            _entry(
                "from-the-future",
                started_utc="2030-01-01T02:00:00.000000Z",
                finished_utc="2030-01-01T02:00:01.000000Z",
                bytes_copied=999_999,
            ),
        ]
    )
    estimate, basis = estimate_backup_bytes(config, history)
    assert estimate == 10
    assert "last time" in basis
