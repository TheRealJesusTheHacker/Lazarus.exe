"""Fleet Sweep #5 (2026-10-24): duplicate-content hashing fixes.

A store holds ONE copy of any content, however many entries or
snapshots point at it. Before this sweep, verify hashed that copy
once per pointing entry (and once per snapshot in a whole-store
verify), restore's pre-write check hashed it once per entry, and
taking a snapshot copied every unchanged file into a temporary
file only to throw the copy away when the stored copy checked
out. The behaviour — every verdict, count, and message — is
unchanged; only the repeated work is gone. These tests pin both
halves: the same answers, and the work done once.
"""

from pathlib import Path

import sys

from lazarus.restore import restore
from lazarus.snapshot import create_snapshot
from lazarus.store import SnapshotStore
from lazarus.verify import verify_snapshot, verify_store

# ``import lazarus.restore as x`` can bind the restore *function*
# (the package attribute shadows the submodule), so take the real
# modules from sys.modules for monkeypatching.
restore_module = sys.modules["lazarus.restore"]
snapshot_module = sys.modules["lazarus.snapshot"]
verify_module = sys.modules["lazarus.verify"]


def _counting_hash_file(module, calls):
    real = module.hash_file

    def wrapper(path):
        calls.append(Path(path))
        return real(path)

    return wrapper


def _identical_tree(tmp_path, files=3, payload=b"same contents" * 64):
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(files):
        (root / f"copy{i}.bin").write_bytes(payload)
    return root


def test_verify_snapshot_counts_duplicate_entries_but_hashes_blob_once(
    store, tmp_path, monkeypatch
):
    tree = _identical_tree(tmp_path)
    manifest = create_snapshot(store, tree)
    assert manifest.file_count == 3

    calls = []
    monkeypatch.setattr(
        verify_module, "hash_file", _counting_hash_file(verify_module, calls)
    )
    result = verify_snapshot(store, manifest.id)
    assert result.ok
    assert result.checked_files == 3
    blob = store.object_path(manifest.entries[0].sha256)
    assert calls.count(blob) == 1


def test_verify_store_hashes_shared_blob_once_across_snapshots(
    store, tmp_path, monkeypatch
):
    tree = _identical_tree(tmp_path)
    first = create_snapshot(store, tree, label="one")
    create_snapshot(store, tree, label="two")
    create_snapshot(store, tree, label="three")

    calls = []
    monkeypatch.setattr(
        verify_module, "hash_file", _counting_hash_file(verify_module, calls)
    )
    result = verify_store(store)
    assert result.ok
    assert result.snapshots_checked == 3
    blob = store.object_path(first.entries[0].sha256)
    assert calls.count(blob) == 1


def test_verify_store_still_reports_shared_corruption_per_snapshot(
    store, tmp_path
):
    tree = _identical_tree(tmp_path)
    manifest = create_snapshot(store, tree, label="one")
    create_snapshot(store, tree, label="two")
    blob = store.object_path(manifest.entries[0].sha256)
    blob.write_bytes(b"torn copy")

    result = verify_store(store)
    assert not result.ok
    # Both snapshots name the damage: caching the verdict must not
    # swallow any snapshot's problems.
    assert len(result.problems) == 6


def test_restore_checks_shared_blob_once_and_restores_every_entry(
    store, tmp_path, monkeypatch
):
    tree = _identical_tree(tmp_path)
    manifest = create_snapshot(store, tree)

    calls = []
    monkeypatch.setattr(
        restore_module,
        "hash_file",
        _counting_hash_file(restore_module, calls),
    )
    dest = tmp_path / "out"
    report = restore(store, dest, snapshot_id=manifest.id)
    assert [item.outcome for item in report.items].count("restored") == 3
    for i in range(3):
        assert (dest / f"copy{i}.bin").read_bytes() == b"same contents" * 64
    blob = store.object_path(manifest.entries[0].sha256)
    # Once for the pre-write proof; the per-file writes that follow
    # hash the temporary files they wrote, never the blob again.
    assert calls.count(blob) == 1


def test_snapshot_of_unchanged_tree_copies_nothing(store, tree, monkeypatch):
    first = create_snapshot(store, tree)
    calls = []
    real = snapshot_module.hash_file_into

    def wrapper(src, dst):
        calls.append(Path(src))
        return real(src, dst)

    monkeypatch.setattr(snapshot_module, "hash_file_into", wrapper)
    second = create_snapshot(store, tree)
    assert calls == []
    assert [e.sha256 for e in second.entries] == [e.sha256 for e in first.entries]
    assert verify_store(store).ok


def test_snapshot_still_copies_new_content_and_heals_torn_object(
    store, tree, monkeypatch
):
    first = create_snapshot(store, tree)
    calls = []
    real = snapshot_module.hash_file_into

    def wrapper(src, dst):
        calls.append(Path(src))
        return real(src, dst)

    monkeypatch.setattr(snapshot_module, "hash_file_into", wrapper)

    (tree / "docs" / "notes.txt").write_text("changed", encoding="utf-8")
    second = create_snapshot(store, tree)
    assert len(calls) == 1  # only the changed file is copied
    changed = [e for e in second.entries if e.path == "docs/notes.txt"][0]
    assert changed.sha256 != [e for e in first.entries if e.path == "docs/notes.txt"][0].sha256

    # A torn stored copy is healed by the next snapshot, as before:
    # the copy pass runs because the stored bytes no longer match.
    blob = store.object_path(changed.sha256)
    blob.write_bytes(b"torn")
    calls.clear()
    create_snapshot(store, tree)
    assert len(calls) == 1
    from lazarus.hashing import hash_file

    assert hash_file(blob) == changed.sha256
