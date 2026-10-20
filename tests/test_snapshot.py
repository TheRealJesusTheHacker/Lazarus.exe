"""Taking snapshots: contents, dedupe, partial saves, self-exclusion."""

import os

import pytest

import lazarus.snapshot as snapshot_module
from lazarus.errors import SourceNotFoundError
from lazarus.model import TYPE_DIR, TYPE_FILE, TYPE_SYMLINK
from lazarus.snapshot import create_snapshot
from lazarus.store import SnapshotStore


def _entries_by_path(manifest):
    return {e.path: e for e in manifest.entries}


def test_snapshot_of_folder_tree(store, tree):
    manifest = create_snapshot(store, tree, label="first save")
    entries = _entries_by_path(manifest)
    assert manifest.complete is True
    assert manifest.label == "first save"
    assert entries["docs"].type == TYPE_DIR
    assert entries["docs/empty-dir"].type == TYPE_DIR
    notes = entries["docs/notes.txt"]
    assert notes.type == TYPE_FILE
    assert notes.size == len("remember the milk")
    assert notes.sha256  # recorded by hash
    photo = entries["photo.bin"]
    assert photo.size == 1024
    # The promised contents are physically in the object area.
    assert store.object_path(notes.sha256).exists()
    assert store.object_path(photo.sha256).exists()


def test_snapshot_of_a_single_file(store, tmp_path):
    solo = tmp_path / "solo.txt"
    solo.write_text("just me", encoding="utf-8")
    manifest = create_snapshot(store, solo)
    entries = _entries_by_path(manifest)
    assert list(entries) == ["solo.txt"]
    assert entries["solo.txt"].size == len("just me")
    assert manifest.source_root == str(tmp_path.resolve())


def test_snapshot_missing_source(store, tmp_path):
    with pytest.raises(SourceNotFoundError):
        create_snapshot(store, tmp_path / "ghost")


def test_identical_contents_are_stored_once(store, tree):
    (tree / "copy-of-notes.txt").write_text("remember the milk", encoding="utf-8")
    create_snapshot(store, tree)
    create_snapshot(store, tree)  # same tree again: nothing new to store
    objects = [p for p in store.objects_dir.rglob("*") if p.is_file()]
    # notes + its copy share one object; report and photo are the others.
    assert len(objects) == 3


def test_unreadable_file_makes_an_honest_partial_snapshot(store, tree, monkeypatch):
    real_copy = snapshot_module._copy_object

    def flaky_copy(store_, source_file):
        if source_file.name == "report.txt":
            raise OSError("simulated read failure")
        return real_copy(store_, source_file)

    monkeypatch.setattr(snapshot_module, "_copy_object", flaky_copy)
    manifest = create_snapshot(store, tree)
    entries = _entries_by_path(manifest)
    assert manifest.complete is False
    assert "docs/report.txt" not in entries  # never promised what wasn't saved
    assert "docs/notes.txt" in entries
    problems = [p for p in manifest.problems if p.path == "docs/report.txt"]
    assert problems and "could not be read" in problems[0].message


def test_store_inside_the_source_is_never_snapshotted(tmp_path):
    source = tmp_path / "everything"
    source.mkdir()
    (source / "keep.txt").write_text("precious", encoding="utf-8")
    store = SnapshotStore.create(source / "lazarus-store")
    manifest = create_snapshot(store, source)
    paths = [e.path for e in manifest.entries]
    assert "keep.txt" in paths
    assert not any(p.startswith("lazarus-store") for p in paths)
    # By design, and said out loud in the record.
    assert any("store folder" in p.message for p in manifest.problems)
    assert manifest.complete is True


def test_symlinks_are_recorded_as_links(store, tree):
    target = tree / "docs" / "notes.txt"
    link = tree / "quick-notes"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    manifest = create_snapshot(store, tree)
    entry = _entries_by_path(manifest)["quick-notes"]
    assert entry.type == TYPE_SYMLINK
    assert entry.target == str(target)


def test_snapshot_records_mtime(store, tree):
    notes = tree / "docs" / "notes.txt"
    known_ns = 1_700_000_000_123_456_789
    os.utime(notes, ns=(known_ns, known_ns))
    manifest = create_snapshot(store, tree)
    entry = _entries_by_path(manifest)["docs/notes.txt"]
    assert entry.mtime_ns == known_ns
