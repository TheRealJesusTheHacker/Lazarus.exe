"""Verification: the store must prove its contents, byte for byte."""

from lazarus.restore import restore
from lazarus.snapshot import create_snapshot
from lazarus.verify import verify_snapshot, verify_store


def _entry(manifest, path):
    return next(e for e in manifest.entries if e.path == path)


def test_a_sound_snapshot_verifies(store, tree):
    manifest = create_snapshot(store, tree)
    result = verify_snapshot(store, manifest.id)
    assert result.ok
    assert result.checked_files == 3


def test_tampered_saved_copy_is_caught(store, tree):
    manifest = create_snapshot(store, tree)
    blob = store.object_path(_entry(manifest, "docs/notes.txt").sha256)
    blob.write_bytes(b"someone changed the saved copy")
    result = verify_snapshot(store, manifest.id)
    assert not result.ok
    problems = [p for p in result.problems if p.path == "docs/notes.txt"]
    assert problems and "changed" in problems[0].message


def test_missing_saved_copy_is_caught(store, tree):
    manifest = create_snapshot(store, tree)
    blob = store.object_path(_entry(manifest, "photo.bin").sha256)
    blob.unlink()
    result = verify_snapshot(store, manifest)
    assert not result.ok
    problems = [p for p in result.problems if p.path == "photo.bin"]
    assert problems and "missing" in problems[0].message


def test_restore_refuses_to_write_a_tampered_copy(store, tree, tmp_path):
    """Damaged goods are named and left in the store, never written out
    as if they were the real thing — and the rest still comes back."""
    manifest = create_snapshot(store, tree)
    blob = store.object_path(_entry(manifest, "docs/notes.txt").sha256)
    blob.write_bytes(b"tampered")
    out = tmp_path / "careful"
    report = restore(store, destination=out, snapshot_id=manifest.id)
    assert report.status == "partial"
    outcomes = {i.path: i.outcome for i in report.items}
    assert outcomes["docs/notes.txt"] == "not_restored"
    assert outcomes["docs/report.txt"] == "restored"
    assert not (out / "docs" / "notes.txt").exists()
    # The report names it, in plain language.
    assert "docs/notes.txt" in report.to_text()


def test_restore_with_only_damaged_goods_fails_honestly(store, tree, tmp_path):
    manifest = create_snapshot(store, tree)
    blob = store.object_path(_entry(manifest, "docs/notes.txt").sha256)
    blob.write_bytes(b"tampered")
    out = tmp_path / "hopeless"
    report = restore(
        store, destination=out, snapshot_id=manifest.id, path="docs/notes.txt"
    )
    assert report.status == "failed"
    assert not (out / "docs" / "notes.txt").exists()


def test_verify_store_covers_every_snapshot(store, tree):
    create_snapshot(store, tree, label="one")
    (tree / "docs" / "notes.txt").write_text("second version", encoding="utf-8")
    second = create_snapshot(store, tree, label="two")
    blob = store.object_path(_entry(second, "docs/notes.txt").sha256)
    blob.write_bytes(b"tampered")
    result = verify_store(store)
    assert not result.ok
    assert result.snapshots_checked == 2
    assert any(p.path.startswith(second.id) for p in result.problems)
