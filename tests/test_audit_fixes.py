"""Regression tests for the independent audit of Phase 1.

Each test is named for the attack it answers. The audit's job was to
break the safety promises; these tests keep them unbroken.
"""

import json
import os
from pathlib import Path

import pytest

import lazarus.snapshot as snapshot_module
from lazarus.cli import main
from lazarus.errors import LazarusError, RestoreRefusedError, StoreError
from lazarus.model import SnapshotManifest, is_safe_relative_path
from lazarus.restore import restore
from lazarus.snapshot import create_snapshot
from lazarus.store import SnapshotStore
from lazarus.verify import verify_snapshot


def _rewrite_manifest(store, manifest, entries=None, mutate=None):
    path = store.root / "snapshots" / manifest.id / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if entries is not None:
        data["entries"] = entries
    if mutate is not None:
        mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    return store.load_manifest(manifest.id)


def _file_entry(manifest, path):
    return next(
        {"path": e.path, "type": e.type, "size": e.size, "sha256": e.sha256}
        for e in manifest.entries
        if e.path == path
    )


# F1 — restore must never write through a shortcut.

def test_crafted_symlink_cannot_be_written_through(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pwned.txt").write_text("do not touch", encoding="utf-8")
    notes = _file_entry(manifest, "docs/notes.txt")
    crafted = _rewrite_manifest(
        store,
        manifest,
        entries=[
            {"path": "link", "type": "symlink", "target": str(outside)},
            {**notes, "path": "link/pwned.txt"},
        ],
    )
    out = tmp_path / "dest"
    report = restore(store, destination=out, snapshot_id=crafted.id)
    assert (outside / "pwned.txt").read_text() == "do not touch"
    outcomes = {i.path: i.outcome for i in report.items}
    assert outcomes["link/pwned.txt"] == "failed"


def test_preexisting_symlink_folder_blocks_restore(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    out = tmp_path / "dest"
    out.mkdir()
    os.symlink(outside, out / "docs")
    report = restore(store, destination=out, snapshot_id=manifest.id)
    outcomes = {i.path: i.outcome for i in report.items}
    assert outcomes["docs/notes.txt"] == "failed"
    assert not (outside / "notes.txt").exists()


# F2 — an incomplete safety copy stops the restore cold.

def test_unreadable_overwrite_target_aborts_restore(store, tree, monkeypatch):
    first = create_snapshot(store, tree)
    notes = tree / "docs" / "notes.txt"
    notes.write_text("version two, precious", encoding="utf-8")

    real_copy = snapshot_module._copy_object

    def flaky_copy(store_, source_file):
        if source_file.name == "notes.txt":
            raise OSError("simulated: cannot read the current file")
        return real_copy(store_, source_file)

    monkeypatch.setattr(snapshot_module, "_copy_object", flaky_copy)
    with pytest.raises(RestoreRefusedError):
        restore(store, snapshot_id=first.id)
    # Nothing was overwritten: the precious current version survives.
    assert notes.read_text() == "version two, precious"


# F3 — the store never snapshots itself.

def test_store_refuses_to_snapshot_itself(store):
    with pytest.raises(StoreError):
        create_snapshot(store, store.root)
    with pytest.raises(StoreError):
        create_snapshot(store, store.root / "objects")
    with pytest.raises(StoreError):
        create_snapshot(store, store.root / "store.json")


# F4 — relative source paths work like a person would type them.

def test_snapshot_from_a_relative_path(store, tree, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manifest = create_snapshot(store, Path("docs-root"))
    paths = [e.path for e in manifest.entries]
    assert "docs/notes.txt" in paths


# F5 — special files are recorded, never opened.

def test_a_pipe_is_a_problem_not_a_hang(store, tree):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo here")
    os.mkfifo(tree / "a-pipe")
    manifest = create_snapshot(store, tree)
    assert manifest.complete is False
    assert "a-pipe" not in [e.path for e in manifest.entries]
    assert any(p.path == "a-pipe" for p in manifest.problems)


# F6 — duplicate entries stop the restore before anything is written.

def test_duplicate_entries_are_refused(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    notes = _file_entry(manifest, "docs/notes.txt")
    report_entry = _file_entry(manifest, "docs/report.txt")
    crafted = _rewrite_manifest(
        store,
        manifest,
        entries=[notes, {**report_entry, "path": "docs/notes.txt"}],
    )
    with pytest.raises(RestoreRefusedError):
        restore(store, destination=tmp_path / "out", snapshot_id=crafted.id)
    assert not (tmp_path / "out" / "docs" / "notes.txt").exists()


# F7/F14 — wrong-typed records are "damaged", never tracebacks.

def test_wrong_typed_manifest_fields_fail_as_damaged(store, tree):
    manifest = create_snapshot(store, tree)
    path = store.root / "snapshots" / manifest.id / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"][0]["mtime_ns"] = "yesterday"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StoreError):
        store.load_manifest(manifest.id)


def test_cli_init_on_an_existing_file(tmp_path):
    a_file = tmp_path / "not-a-folder"
    a_file.write_text("x", encoding="utf-8")
    assert main(["init", str(a_file)]) == 2


# F8 — backslash paths are traversal too (Windows is the target).

def test_backslash_paths_are_not_safe():
    assert not is_safe_relative_path("..\\escape.txt")
    assert not is_safe_relative_path("a\\..\\b.txt")
    assert not is_safe_relative_path("a/\x00b")


def test_backslash_manifest_entry_is_refused(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    notes = _file_entry(manifest, "docs/notes.txt")
    crafted = _rewrite_manifest(
        store, manifest, entries=[{**notes, "path": "..\\escape.txt"}]
    )
    with pytest.raises(RestoreRefusedError):
        restore(store, destination=tmp_path / "out", snapshot_id=crafted.id)


# F9 — a folder that will not open is a recorded gap, not a silent one.

def test_unopenable_folder_is_recorded(store, tree, monkeypatch):
    real_walk = os.walk

    def fake_walk(*args, **kwargs):
        onerror = kwargs.pop("onerror", None)
        if onerror is not None:
            error = OSError("simulated: permission denied")
            error.filename = str(tree / "docs")
            onerror(error)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", fake_walk)
    manifest = create_snapshot(store, tree)
    assert manifest.complete is False
    assert any("could not be opened" in p.message for p in manifest.problems)


# F10 — a damaged stored object is healed by the next snapshot.

def test_damaged_object_is_healed(store, tree):
    manifest = create_snapshot(store, tree)
    entry = next(e for e in manifest.entries if e.path == "docs/notes.txt")
    store.object_path(entry.sha256).write_bytes(b"torn by a crash")
    assert not verify_snapshot(store, manifest.id).ok
    healed = create_snapshot(store, tree)
    assert verify_snapshot(store, healed.id).ok
    assert store.object_path(entry.sha256).read_bytes() == b"remember the milk"


# F11 — snapshot ids cannot climb out of the store.

def test_snapshot_id_traversal_is_refused(store):
    for bad in ("../other", "a/b", "..\\up", "/abs/path", ".."):
        with pytest.raises(LazarusError):
            store.load_manifest(bad)


# F12 — a wrecked index is a cache miss, never a crash or a lie.

def test_corrupt_index_is_rebuilt_from_manifests(store, tree):
    first = create_snapshot(store, tree, label="one")
    (store.root / "index.json").write_text('{"snapshots": 123}', encoding="utf-8")
    second = create_snapshot(store, tree, label="two")
    index = json.loads((store.root / "index.json").read_text(encoding="utf-8"))
    assert {e["id"] for e in index["snapshots"]} == {first.id, second.id}


# F13 — a gutted store is not pronounced healthy.

def test_open_rejects_a_store_missing_its_parts(tmp_path):
    root = tmp_path / "gutted"
    store = SnapshotStore.create(root)
    import shutil

    shutil.rmtree(store.root / "objects")
    with pytest.raises(StoreError):
        SnapshotStore.open(root)


def test_open_rejects_a_foreign_format(tmp_path):
    root = tmp_path / "foreign"
    SnapshotStore.create(root)
    meta_path = root / "store.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["format"] = 999
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(StoreError):
        SnapshotStore.open(root)


# F15 — point-in-time thinks in instants, not spellings.

def test_as_of_compares_instants_not_strings(store):
    def bare(snapshot_id, created):
        return SnapshotManifest(
            id=snapshot_id,
            kind="snapshot",
            label="",
            created_utc=created,
            tool_version="0.2.0",
            source_root=None,
            root_name="x",
            complete=True,
        )

    # 06:30 at +02:00 is 04:30 UTC — EARLIER than 05:00 UTC, despite
    # the bigger-looking string.
    store.save_manifest(bare("20261020-043000-aaaa", "2026-10-20T06:30:00+02:00"))
    store.save_manifest(bare("20261020-050000-bbbb", "2026-10-20T05:00:00Z"))
    assert store.resolve_snapshot().id == "20261020-050000-bbbb"
    assert store.resolve_snapshot(as_of="2026-10-20T05:30:00Z").id == "20261020-050000-bbbb"
    assert store.resolve_snapshot(as_of="2026-10-20T04:45:00Z").id == "20261020-043000-aaaa"
