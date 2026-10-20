"""The Phase 2 commands, end to end: scan, recover, rescue —
with the honest exit codes the rest of the kit uses."""

import os
from pathlib import Path

import pytest

from lazarus.cli import main

try:
    import fat_image
except ImportError:  # pragma: no cover
    fat_image = None


@pytest.fixture()
def image_with_deleted(tmp_path):
    if fat_image is None:
        pytest.skip("FAT image builder not present")
    image = fat_image.create_fat16()
    record = image.add_file("", "NOTES.TXT", b"remember the milk")
    image.delete(record)
    path = tmp_path / "drive.img"
    path.write_bytes(bytes(image.data))
    return path


def test_cli_scan_lists_the_deleted_file(image_with_deleted, capsys):
    assert main(["scan", str(image_with_deleted)]) == 0
    out = capsys.readouterr().out
    # Stored as an 8.3 name with no long-name entry: the first
    # letter honestly did not survive deletion.
    assert "_OTES.TXT" in out


def test_cli_scan_of_garbage_stops_cleanly(tmp_path, capsys):
    junk = tmp_path / "junk.img"
    junk.write_bytes(os.urandom(2048))
    assert main(["scan", str(junk)]) == 2
    err = capsys.readouterr().err
    assert "Lazarus stopped:" in err
    assert "Traceback" not in err


def test_cli_recover_to_the_same_drive_is_refused(image_with_deleted, capsys):
    code = main(
        ["recover", str(image_with_deleted), "--to", str(image_with_deleted.parent / "out")]
    )
    assert code == 2
    assert "same drive" in capsys.readouterr().err


def test_cli_rescue_clones_a_file(tmp_path, capsys):
    source = tmp_path / "drive.img"
    payload = bytes(range(256)) * 64
    source.write_bytes(payload)
    image = tmp_path / "clone.img"
    assert main(["rescue", str(source), str(image)]) == 0
    assert image.read_bytes() == payload
    assert Path(str(image) + ".map.json").exists()
    out = capsys.readouterr().out
    assert "copied" in out.lower() or "rescu" in out.lower()
