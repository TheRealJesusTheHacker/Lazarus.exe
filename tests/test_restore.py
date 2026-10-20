"""The restore engine: point-in-time, subsets, undo, and the guards."""

import json
import os

import pytest

from lazarus.errors import (
    NothingToRestoreError,
    RestoreRefusedError,
)
from lazarus.report import (
    OUTCOME_ALREADY_CURRENT,
    OUTCOME_NOT_RESTORED,
    OUTCOME_RESTORED,
)
from lazarus.restore import restore
from lazarus.snapshot import create_snapshot


def _outcomes(report):
    return {item.path: item.outcome for item in report.items}


def _plain_target(value: str) -> str:
    # Windows reports absolute link targets in extended-length
    # form (\\?\C:\...); it is the same file as the plain path,
    # so strip that prefix from both sides before comparing.
    prefix = "\\" + "\\" + "?" + "\\"
    return value[len(prefix) :] if value.startswith(prefix) else value


def test_restore_everything_to_a_fresh_folder(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    out = tmp_path / "brought-back"
    report = restore(store, destination=out, snapshot_id=manifest.id)
    assert report.status == "complete"
    assert (out / "docs" / "notes.txt").read_text() == "remember the milk"
    assert (out / "docs" / "report.txt").read_text() == "quarterly numbers"
    assert (out / "photo.bin").read_bytes() == bytes(range(256)) * 4
    assert (out / "docs" / "empty-dir").is_dir()
    # Nothing was overwritten in a fresh folder, so no safety copy.
    assert report.safety_snapshot_id is None


def test_restore_a_single_file(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    out = tmp_path / "one-file"
    report = restore(
        store, destination=out, snapshot_id=manifest.id, path="docs/notes.txt"
    )
    assert report.status == "complete"
    assert (out / "docs" / "notes.txt").read_text() == "remember the milk"
    assert not (out / "photo.bin").exists()
    assert not (out / "docs" / "report.txt").exists()


def test_restore_a_folder(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    out = tmp_path / "folder"
    restore(store, destination=out, snapshot_id=manifest.id, path="docs")
    assert (out / "docs" / "notes.txt").exists()
    assert (out / "docs" / "empty-dir").is_dir()
    assert not (out / "photo.bin").exists()


def test_restore_a_name_the_snapshot_does_not_hold(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    with pytest.raises(NothingToRestoreError):
        restore(
            store,
            destination=tmp_path / "out",
            snapshot_id=manifest.id,
            path="not-in-here.txt",
        )


def test_point_in_time_restore(store, tree, tmp_path):
    first = create_snapshot(store, tree, label="morning")
    (tree / "docs" / "notes.txt").write_text("buy oat milk instead", encoding="utf-8")
    second = create_snapshot(store, tree, label="evening")

    out_old = tmp_path / "as-of-morning"
    restore(store, destination=out_old, as_of=first.created_utc)
    assert (out_old / "docs" / "notes.txt").read_text() == "remember the milk"

    out_new = tmp_path / "as-of-evening"
    restore(store, destination=out_new, snapshot_id=second.id)
    assert (out_new / "docs" / "notes.txt").read_text() == "buy oat milk instead"


def test_restore_is_reversible(store, tree):
    """The flagship promise: an undo for the undo."""
    first = create_snapshot(store, tree, label="version one")
    notes = tree / "docs" / "notes.txt"
    notes.write_text("version two", encoding="utf-8")
    create_snapshot(store, tree, label="version two")

    report = restore(store, snapshot_id=first.id)  # back to the original home
    assert notes.read_text() == "remember the milk"
    assert report.safety_snapshot_id is not None

    undo = restore(store, snapshot_id=report.safety_snapshot_id)
    assert notes.read_text() == "version two"
    assert undo.status == "complete"

    # The safety copy is a real, listed snapshot with an honest label.
    infos = {i.id: i for i in store.list_snapshots()}
    safety = infos[report.safety_snapshot_id]
    assert safety.kind == "pre-restore"
    assert "Safety copy" in safety.label


def test_restore_reports_what_it_did(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    (tree / "docs" / "notes.txt").write_text("meanwhile, changed", encoding="utf-8")
    report = restore(store, snapshot_id=manifest.id)
    text = report.to_text()
    assert report.status == "complete"
    assert "Everything came back" in text
    assert report.safety_snapshot_id in text  # the undo is named
    outcomes = _outcomes(report)
    assert outcomes["docs/notes.txt"] == OUTCOME_RESTORED


def test_second_restore_finds_everything_already_current(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    out = tmp_path / "twice"
    restore(store, destination=out, snapshot_id=manifest.id)
    again = restore(store, destination=out, snapshot_id=manifest.id)
    assert again.status == "complete"
    assert again.restored_count == 0
    assert all(item.outcome == OUTCOME_ALREADY_CURRENT for item in again.items)
    # Nothing was overwritten, so no safety copy was taken.
    assert again.safety_snapshot_id is None


def test_restore_brings_back_the_original_mtime(store, tree, tmp_path):
    notes = tree / "docs" / "notes.txt"
    known_ns = 1_600_000_000_000_000_000
    os.utime(notes, ns=(known_ns, known_ns))
    manifest = create_snapshot(store, tree)
    out = tmp_path / "dated"
    restore(store, destination=out, snapshot_id=manifest.id)
    assert (out / "docs" / "notes.txt").stat().st_mtime_ns == known_ns


def test_restore_recreates_symlinks(store, tree, tmp_path):
    link = tree / "quick-notes"
    try:
        os.symlink(tree / "docs" / "notes.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    manifest = create_snapshot(store, tree)
    out = tmp_path / "with-links"
    report = restore(store, destination=out, snapshot_id=manifest.id)
    assert report.status == "complete"
    restored_link = out / "quick-notes"
    assert restored_link.is_symlink()
    assert _plain_target(os.readlink(restored_link)) == _plain_target(
        str(tree / "docs" / "notes.txt")
    )


def test_restore_never_deletes_newer_files(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    out = tmp_path / "growing"
    restore(store, destination=out, snapshot_id=manifest.id)
    (out / "brand-new.txt").write_text("made later", encoding="utf-8")
    restore(store, destination=out, snapshot_id=manifest.id)
    assert (out / "brand-new.txt").read_text() == "made later"


def test_restore_into_the_store_is_refused(store, tree):
    manifest = create_snapshot(store, tree)
    with pytest.raises(RestoreRefusedError):
        restore(store, destination=store.root, snapshot_id=manifest.id)
    with pytest.raises(RestoreRefusedError):
        restore(store, destination=store.root / "objects", snapshot_id=manifest.id)


def test_restore_over_the_store_is_refused(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    with pytest.raises(RestoreRefusedError):
        restore(store, destination=tmp_path, snapshot_id=manifest.id)


def test_tampered_manifest_cannot_aim_a_restore_outside(store, tree, tmp_path):
    """A doctored record pointing at ../escape must stop the restore."""
    manifest = create_snapshot(store, tree)
    manifest_path = store.root / "snapshots" / manifest.id / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    notes_entry = next(e for e in data["entries"] if e["path"] == "docs/notes.txt")
    data["entries"].append(
        {
            "path": "../escape.txt",
            "type": "file",
            "size": notes_entry["size"],
            "sha256": notes_entry["sha256"],
        }
    )
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    out = tmp_path / "victim"
    with pytest.raises(RestoreRefusedError):
        restore(store, destination=out, snapshot_id=manifest.id)
    assert not (tmp_path / "escape.txt").exists()


def test_restore_from_a_partial_snapshot_says_so(store, tree, tmp_path, monkeypatch):
    import lazarus.snapshot as snapshot_module

    real_copy = snapshot_module._copy_object

    def flaky_copy(store_, source_file):
        if source_file.name == "report.txt":
            raise OSError("simulated read failure")
        return real_copy(store_, source_file)

    monkeypatch.setattr(snapshot_module, "_copy_object", flaky_copy)
    manifest = create_snapshot(store, tree)
    assert manifest.complete is False

    out = tmp_path / "from-partial"
    report = restore(store, destination=out, snapshot_id=manifest.id)
    assert report.snapshot_complete is False
    assert "incomplete" in report.to_text()
    assert (out / "docs" / "notes.txt").exists()
