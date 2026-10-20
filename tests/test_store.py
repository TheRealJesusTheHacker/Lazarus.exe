"""The store itself: creating, opening, listing, choosing."""

import json

import pytest

from lazarus.errors import (
    SnapshotNotFoundError,
    StoreError,
    StoreExistsError,
)
from lazarus.snapshot import create_snapshot
from lazarus.store import SnapshotStore


def test_create_and_reopen(tmp_path):
    root = tmp_path / "store"
    store = SnapshotStore.create(root)
    assert (root / "store.json").exists()
    assert (root / "objects").is_dir()
    assert (root / "snapshots").is_dir()
    again = SnapshotStore.open(root)
    assert again.meta["id"] == store.meta["id"]


def test_create_refuses_a_folder_with_other_files(tmp_path):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "someone-elses-file.txt").write_text("not a store")
    with pytest.raises(StoreExistsError):
        SnapshotStore.create(busy)


def test_create_refuses_an_existing_store(store):
    with pytest.raises(StoreExistsError):
        SnapshotStore.create(store.root)


def test_open_missing_folder(tmp_path):
    with pytest.raises(StoreError):
        SnapshotStore.open(tmp_path / "nope")


def test_open_a_folder_that_is_not_a_store(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(StoreError):
        SnapshotStore.open(plain)


def test_list_empty_store(store):
    assert store.list_snapshots() == []


def test_unfinished_snapshot_is_listed_honestly(store, tmp_path):
    """A snapshot folder with no manifest is an interrupted save.

    It must show up as unusable, never silently trusted or hidden.
    """
    ghost = store.root / "snapshots" / "20260101-000000-deadbeef"
    ghost.mkdir(parents=True)
    infos = store.list_snapshots()
    assert len(infos) == 1
    assert infos[0].has_manifest is False
    assert infos[0].complete is False
    with pytest.raises(SnapshotNotFoundError):
        store.load_manifest("20260101-000000-deadbeef")


def test_corrupt_manifest_is_listed_as_unfinished(store, tree):
    manifest = create_snapshot(store, tree)
    (store.root / "snapshots" / manifest.id / "manifest.json").write_text(
        "{ this is not json", encoding="utf-8"
    )
    infos = store.list_snapshots()
    assert infos[0].has_manifest is False
    with pytest.raises(StoreError):
        store.load_manifest(manifest.id)


def test_resolve_latest_and_as_of(store, tree):
    first = create_snapshot(store, tree, label="first")
    (tree / "docs" / "notes.txt").write_text("changed", encoding="utf-8")
    second = create_snapshot(store, tree, label="second")

    assert store.resolve_snapshot().id == second.id
    assert store.resolve_snapshot(snapshot_id=first.id).id == first.id
    # "How it was when the first snapshot was taken" picks the first.
    assert store.resolve_snapshot(as_of=first.created_utc).id == first.id
    # A plain date means the end of that day: the day's last snapshot.
    day = second.created_utc[:10]
    assert store.resolve_snapshot(as_of=day).id == second.id


def test_resolve_as_of_before_any_snapshot(store, tree):
    create_snapshot(store, tree)
    with pytest.raises(SnapshotNotFoundError):
        store.resolve_snapshot(as_of="2000-01-01")


def test_resolve_in_empty_store(store):
    with pytest.raises(SnapshotNotFoundError):
        store.resolve_snapshot()


def test_index_file_tracks_snapshots(store, tree):
    manifest = create_snapshot(store, tree, label="indexed")
    index = json.loads((store.root / "index.json").read_text(encoding="utf-8"))
    assert [entry["id"] for entry in index["snapshots"]] == [manifest.id]
