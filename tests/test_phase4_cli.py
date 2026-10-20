"""The Phase 4 commands, end to end: setup, run, status, history,
due, prune, register — with the honest exit codes the rest of the
kit uses (0 done, 1 done but needs attention, 2 stopped).

Everything runs through ``cli.main([...])`` against real stores and
settings on a scratch disk, the way a person at a terminal would
drive them — no part of the backup engine is faked here.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from lazarus import cli
from lazarus.backup import load_config, save_config
from lazarus.backup_model import RetentionSpec
from lazarus.store import SnapshotStore


def _source(tmp_path: Path, text: str = "alpha") -> Path:
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    (source / "a.txt").write_text(text, encoding="utf-8")
    return source


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "store", tmp_path / "cfg" / "backup.json"


def _setup(tmp_path: Path, capsys, *extra: str) -> tuple[Path, Path]:
    store_path, config_path = _paths(tmp_path)
    source = _source(tmp_path)
    code = cli.main(
        [
            "backup", "setup",
            "--store", str(store_path),
            "--add", str(source),
            "--config", str(config_path),
            *extra,
        ]
    )
    assert code == 0
    capsys.readouterr()
    return store_path, config_path


def _run_ok(config_path: Path, capsys) -> None:
    assert cli.main(["backup", "run", "--config", str(config_path)]) == 0
    capsys.readouterr()


# -- the full flow, in the order a person would walk it ---------------


def test_cli_backup_full_flow(tmp_path, capsys):
    store_path, config_path = _paths(tmp_path)
    source = _source(tmp_path)

    code = cli.main(
        [
            "backup", "setup",
            "--store", str(store_path),
            "--add", str(source),
            "--config", str(config_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Backup is set up." in out
    assert f"Backup store: {store_path}" in out
    assert str(source) in out
    assert "Settings saved to:" in out

    assert cli.main(["backup", "run", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "Backup complete" in out
    assert "Snapshots created:" in out

    assert cli.main(["backup", "status", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "READY" in out
    assert "Snapshots in the store: 1" in out

    assert cli.main(["backup", "history", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "complete" in out
    assert "Snapshots made: 1" in out

    assert cli.main(["backup", "due", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "No backup is due right now" in out

    code = cli.main(
        ["backup", "prune", "--config", str(config_path), "--dry-run"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Nothing to tidy up" in out


# -- stopping plainly, before any work ----------------------------------


def test_cli_backup_run_before_setup_stops(tmp_path, capsys):
    config_path = tmp_path / "never" / "backup.json"
    code = cli.main(["backup", "run", "--config", str(config_path)])
    assert code == 2
    captured = capsys.readouterr()
    told = captured.err + captured.out
    assert "Lazarus stopped:" in told
    assert "No backup is set up" in told
    assert "Traceback" not in told


def test_cli_backup_setup_with_a_missing_folder_stops(tmp_path, capsys):
    store_path, config_path = _paths(tmp_path)
    missing = tmp_path / "not-there"
    code = cli.main(
        [
            "backup", "setup",
            "--store", str(store_path),
            "--add", str(missing),
            "--config", str(config_path),
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "Lazarus stopped:" in captured.err
    assert "is not there" in captured.err
    assert not config_path.exists()  # no settings were saved


def test_cli_backup_setup_keep_count_and_keep_days_together_is_an_error(
    tmp_path, capsys
):
    store_path, config_path = _paths(tmp_path)
    source = _source(tmp_path)
    with pytest.raises(SystemExit) as caught:
        cli.main(
            [
                "backup", "setup",
                "--store", str(store_path),
                "--add", str(source),
                "--config", str(config_path),
                "--keep-count", "3",
                "--keep-days", "30",
            ]
        )
    assert caught.value.code == 2
    capsys.readouterr()


# -- status and prune when the world has changed -------------------------


def test_cli_backup_status_with_the_store_deleted(tmp_path, capsys):
    store_path, config_path = _setup(tmp_path, capsys)
    _run_ok(config_path, capsys)
    shutil.rmtree(store_path)

    code = cli.main(["backup", "status", "--config", str(config_path)])
    assert code == 2
    out = capsys.readouterr().out
    assert "NOT CONNECTED" in out


def test_cli_backup_prune_removes_and_says_what_it_removed(tmp_path, capsys):
    store_path, config_path = _setup(tmp_path, capsys, "--keep-count", "10")
    source = tmp_path / "src"
    for text in ("alpha", "alpha-two", "alpha-three"):
        (source / "a.txt").write_text(text, encoding="utf-8")
        _run_ok(config_path, capsys)

    store = SnapshotStore.open(store_path)
    before = [info.id for info in store.list_snapshots() if info.has_manifest]
    assert len(before) == 3

    # The owner tightens the keeping rule; the prune obeys the new one.
    config = load_config(config_path)
    save_config(
        replace(config, retention=RetentionSpec(mode="count", count=1, days=30)),
        config_path,
    )

    code = cli.main(["backup", "prune", "--config", str(config_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Backups to remove: 2" in out
    assert "Removed 2 old backup(s)." in out
    for removed_id in before[:2]:
        assert removed_id in out  # each removal was named before it happened

    store = SnapshotStore.open(store_path)
    after = [info.id for info in store.list_snapshots() if info.has_manifest]
    assert after == before[2:]


def test_cli_backup_prune_dry_run_removes_nothing(tmp_path, capsys):
    store_path, config_path = _setup(tmp_path, capsys, "--keep-count", "10")
    source = tmp_path / "src"
    for text in ("alpha", "alpha-two"):
        (source / "a.txt").write_text(text, encoding="utf-8")
        _run_ok(config_path, capsys)
    config = load_config(config_path)
    save_config(
        replace(config, retention=RetentionSpec(mode="count", count=1, days=30)),
        config_path,
    )

    code = cli.main(
        ["backup", "prune", "--config", str(config_path), "--dry-run"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    store = SnapshotStore.open(store_path)
    assert len([i for i in store.list_snapshots() if i.has_manifest]) == 2


# -- registering on a computer that is not Windows -----------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="this checks the non-Windows refusal"
)
def test_cli_backup_register_on_this_platform_stops_with_the_windows_reason(
    tmp_path, capsys
):
    _store_path, config_path = _setup(tmp_path, capsys)
    code = cli.main(["backup", "register", "--config", str(config_path)])
    assert code == 2
    captured = capsys.readouterr()
    assert "Lazarus stopped:" in captured.err
    assert "Windows" in captured.err


# -- history, with a limit --------------------------------------------------


def test_cli_backup_history_limit_shows_only_the_newest_block(tmp_path, capsys):
    _store_path, config_path = _setup(tmp_path, capsys)
    _run_ok(config_path, capsys)
    _run_ok(config_path, capsys)

    code = cli.main(
        ["backup", "history", "--config", str(config_path), "--limit", "1"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("How it was started:") == 1
    assert out.count("Snapshots made:") == 1

    # Without the limit, both runs are there.
    assert cli.main(["backup", "history", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("How it was started:") == 2
