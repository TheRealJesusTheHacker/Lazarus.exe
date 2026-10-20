"""The UI controller, driven without Qt against real engine fixtures.

Every test here talks to the engine through the controller exactly
as the window does, on temporary folders, and checks the plain view
data the window would show — including the refusals, whose text is
the engine's own.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from lazarus.backup import load_config, save_config
from lazarus.backup_model import RetentionSpec
from lazarus.decrypt_model import VolumeInfo
from lazarus.errors import LazarusError, RestoreRefusedError
from lazarus.rescue import RegionMap
from lazarus.store import SnapshotStore
from lazarus.ui import controller as ctrl_mod
from lazarus.ui.controller import Controller, display_error
from lazarus.unlock import UnlockAttempt

try:
    import fat_image
except ImportError:  # pragma: no cover
    fat_image = None


def _tree(root: Path) -> Path:
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "notes.txt").write_text("remember the milk", encoding="utf-8")
    (root / "docs" / "report.txt").write_text("quarterly numbers", encoding="utf-8")
    (root / "photo.bin").write_bytes(bytes(range(256)))
    return root


@pytest.fixture()
def controller(tmp_path):
    return Controller(config_path=tmp_path / "cfg" / "backup.json")


@pytest.fixture()
def stocked(controller, tmp_path):
    """A controller with an open store holding one snapshot."""
    controller.create_store(tmp_path / "store")
    source = _tree(tmp_path / "source")
    controller.take_snapshot(source, label="before the trip")
    return controller, source


# -- the store -----------------------------------------------------------


def test_open_a_folder_that_is_not_a_store_is_the_engines_words(controller, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(LazarusError) as caught:
        controller.open_store(plain)
    assert "is not a Lazarus store" in str(caught.value)
    assert display_error(caught.value) == str(caught.value)


def test_open_a_missing_folder_is_refused(controller, tmp_path):
    with pytest.raises(LazarusError) as caught:
        controller.open_store(tmp_path / "nope")
    assert "no folder" in str(caught.value)


def test_create_then_create_again_is_refused(controller, tmp_path):
    result = controller.create_store(tmp_path / "store")
    assert result["store_path"] == str((tmp_path / "store").resolve())
    with pytest.raises(LazarusError) as caught:
        Controller().create_store(tmp_path / "store")
    assert "already a Lazarus store" in str(caught.value)


def test_actions_without_a_store_say_so(controller):
    with pytest.raises(LazarusError) as caught:
        controller.list_snapshots()
    assert "No store is open yet" in str(caught.value)


def test_unexpected_errors_become_one_plain_apology():
    message = display_error(RuntimeError("boom-traceback-material"))
    assert "Something went wrong inside Lazarus" in message
    assert "boom" not in message


# -- snapshots, browsing, restore ------------------------------------------


def test_take_snapshot_list_and_browse(stocked, tmp_path):
    controller, _source = stocked
    views = controller.list_snapshots()
    assert len(views) == 1
    view = views[0]
    assert view["label"] == "before the trip"
    assert view["complete"] is True
    assert view["file_count"] == 3
    assert "safety copy" not in view["row_text"]
    manifest = controller.browse_snapshot(view["id"])
    paths = {entry["path"] for entry in manifest["entries"]}
    assert "docs/notes.txt" in paths
    assert "photo.bin" in paths
    assert manifest["source_root"].endswith("source")


def test_snapshot_result_lines_name_what_was_saved(stocked):
    controller, _ = stocked
    views = controller.list_snapshots()
    assert views[0]["date_text"] != "date unknown"


def test_restore_everything_to_a_new_home(stocked, tmp_path):
    controller, _source = stocked
    snapshot_id = controller.list_snapshots()[0]["id"]
    dest = tmp_path / "brought-back"
    result = controller.restore_selection(snapshot_id, dest, None)
    assert result["status"] == "complete"
    assert (dest / "docs" / "notes.txt").read_text(encoding="utf-8") == "remember the milk"
    assert any("Everything came back" in line for line in result["lines"])


def test_restore_one_file_only(stocked, tmp_path):
    controller, _source = stocked
    snapshot_id = controller.list_snapshots()[0]["id"]
    dest = tmp_path / "one-file"
    result = controller.restore_selection(snapshot_id, dest, ["docs/notes.txt"])
    assert result["status"] == "complete"
    assert (dest / "docs" / "notes.txt").exists()
    assert not (dest / "photo.bin").exists()


def test_restore_one_folder_only(stocked, tmp_path):
    controller, _source = stocked
    snapshot_id = controller.list_snapshots()[0]["id"]
    dest = tmp_path / "one-folder"
    result = controller.restore_selection(snapshot_id, dest, ["docs"])
    assert result["status"] == "complete"
    assert (dest / "docs" / "report.txt").exists()
    assert not (dest / "photo.bin").exists()


def test_restore_two_files_in_one_selection(stocked, tmp_path):
    controller, _source = stocked
    snapshot_id = controller.list_snapshots()[0]["id"]
    dest = tmp_path / "two-files"
    result = controller.restore_selection(
        snapshot_id, dest, ["docs/notes.txt", "photo.bin"]
    )
    assert result["status"] == "complete"
    assert (dest / "docs" / "notes.txt").exists()
    assert (dest / "photo.bin").exists()
    assert not (dest / "docs" / "report.txt").exists()


def test_restore_into_the_store_itself_is_refused_as_display_text(stocked, tmp_path):
    controller, _source = stocked
    snapshot_id = controller.list_snapshots()[0]["id"]
    with pytest.raises(LazarusError) as caught:
        controller.restore_selection(snapshot_id, tmp_path / "store" / "inside", None)
    assert "inside the Lazarus store" in display_error(caught.value)


def test_restore_takes_a_safety_copy_before_overwriting(stocked, tmp_path):
    controller, source = stocked
    snapshot_id = controller.list_snapshots()[0]["id"]
    (source / "docs" / "notes.txt").write_text("changed since", encoding="utf-8")
    result = controller.restore_selection(snapshot_id, source, ["docs/notes.txt"])
    assert result["status"] == "complete"
    assert result["safety_snapshot_ids"], "a safety copy should have been taken"
    kinds = {view["id"]: view["kind_words"] for view in controller.list_snapshots()}
    assert kinds[result["safety_snapshot_ids"][0]] == "safety copy"
    assert (source / "docs" / "notes.txt").read_text(encoding="utf-8") == "remember the milk"


def test_verify_store_view(stocked):
    controller, _ = stocked
    result = controller.verify_store_view()
    assert result["ok"] is True
    assert result["snapshots_checked"] == 1
    assert "checked out" in result["lines"][0]


# -- deleted files ------------------------------------------------------------


@pytest.fixture()
def fat_drive(tmp_path):
    if fat_image is None:
        pytest.skip("FAT image builder not present")
    image = fat_image.create_fat16()
    content = b"the quarterly numbers, in full" * 30
    record = image.add_file("", "REPORT.TXT", content)
    image.delete(record)
    path = tmp_path / "drive.img"
    path.write_bytes(bytes(image.data))
    return path, content


def test_scan_view_carries_the_engines_odds_words(controller, fat_drive):
    path, _content = fat_drive
    result = controller.scan_deleted(path)
    assert result["files"], "the deleted file should be found"
    found = result["files"][0]
    assert found["verdict"] == "strong"
    assert found["verdict_words"] == "likely whole"
    assert found["summary"]
    assert result["strong_count"] == 1


def test_recover_onto_the_same_drive_is_refused_in_plain_words(controller, fat_drive, tmp_path):
    path, _content = fat_drive
    with pytest.raises(LazarusError) as caught:
        controller.recover(path, tmp_path / "out", names=["_EPORT.TXT"])
    assert "same drive" in str(caught.value)


def test_recover_onto_a_different_drive(controller, fat_drive):
    path, content = fat_drive
    source_copy = Path("/tmp") / "lazarus-ui-drive.img"
    try:
        source_copy.write_bytes(path.read_bytes())
    except OSError:
        pytest.skip("no room on the second filesystem")
    dest = Path(tempfile.mkdtemp(prefix="lazarus-ui-dest-", dir=Path.home()))
    try:
        if os.stat(dest).st_dev == os.stat(source_copy).st_dev:
            pytest.skip("no second device available")
        result = controller.recover(source_copy, dest, names=["_EPORT.TXT"])
        assert result["status"] == "complete"
        assert (dest / "_EPORT.TXT").read_bytes() == content
    finally:
        source_copy.unlink(missing_ok=True)
        for child in dest.iterdir():
            child.unlink()
        dest.rmdir()


# -- rescue progress -------------------------------------------------------------


def test_rescue_a_small_file_and_read_its_finished_map(controller, tmp_path):
    source = tmp_path / "drive.bin"
    source.write_bytes(bytes((i % 255) + 1 for i in range(8192)))
    dest = tmp_path / "image.bin"
    result = controller.rescue(source, dest)
    assert result["status"] == "complete"
    assert dest.read_bytes() == source.read_bytes()
    progress = controller.read_rescue_progress(controller.default_map_path(dest))
    assert progress is not None
    assert progress["regions_total"] == 1
    assert progress["regions_recovered"] == 1
    assert progress["bytes_missing"] == 0


def test_rescue_progress_reads_a_half_done_map(controller, tmp_path):
    region_map = RegionMap(
        source_size=8192,
        block_size=4096,
        regions=[
            {"state": "recovered", "holes": []},
            {"state": "unrecovered", "holes": [[0, 4096]]},
        ],
    )
    map_path = tmp_path / "image.bin.map.json"
    region_map.save(map_path)
    progress = controller.read_rescue_progress(map_path)
    assert progress["regions_total"] == 2
    assert progress["regions_recovered"] == 1
    assert progress["bytes_recovered"] == 4096
    assert progress["bytes_missing"] == 4096


def test_rescue_progress_before_any_map_is_none(controller, tmp_path):
    assert controller.read_rescue_progress(tmp_path / "absent.map.json") is None


# -- unlock, behind a fake platform -----------------------------------------------


class FakePlatform:
    def __init__(self, volumes, attempt=None):
        self._volumes = {volume.ref: volume for volume in volumes}
        self._attempt = attempt if attempt is not None else UnlockAttempt(ok=True)
        self.run_secrets: list[str] = []

    def describe_volume(self, ref):
        return self._volumes.get(ref)

    def list_volumes(self):
        return list(self._volumes.values())

    def run_unlock(self, argv, secret):
        self.run_secrets.append(secret)
        return self._attempt


class CountingProvider:
    def __init__(self, key="FAKE-KEY-1"):
        self.key = key
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.key


def _volume(ref, locked, kind="bitlocker"):
    return VolumeInfo(ref=ref, kind=kind, label="", locked=locked, evidence=[])


def test_volumes_view_words(controller):
    controller.platform = FakePlatform(
        [_volume("E:", True), _volume("F:", False), _volume("G:", None, kind="unknown")]
    )
    result = controller.list_volumes()
    assert result["available"] is True
    states = {row["ref"]: row["state_words"] for row in result["volumes"]}
    assert states == {"E:": "locked", "F:": "open", "G:": "lock state unknown"}


def test_unlock_open_volume_never_asks_for_a_key(controller):
    platform = FakePlatform([_volume("F:", False)])
    controller.platform = platform
    provider = CountingProvider()
    result = controller.unlock("F:", provider)
    assert result["status"] == "already-unlocked"
    assert provider.calls == 0
    assert platform.run_secrets == []


def test_unlock_locked_volume_asks_once(controller):
    platform = FakePlatform([_volume("E:", True)])
    controller.platform = platform
    provider = CountingProvider()
    result = controller.unlock("E:", provider)
    assert result["status"] == "unlocked"
    assert provider.calls == 1
    assert platform.run_secrets == ["FAKE-KEY-1"]


def test_unlock_wrong_key_is_not_accepted(controller):
    platform = FakePlatform([_volume("E:", True)], attempt=UnlockAttempt(ok=False))
    controller.platform = platform
    result = controller.unlock("E:", CountingProvider(key="WRONG-KEY"))
    assert result["status"] == "not-accepted"
    assert "did not accept" in result["message"]
    assert result["next_step"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="pins the non-Windows seam answer; on Windows the seam "
    "is live Windows tooling",
)
def test_volumes_on_a_non_windows_computer_say_so_plainly(controller):
    # This machine is not Windows, so the real seam is unavailable.
    result = controller.list_volumes()
    assert result["available"] is False
    assert "not running Windows" in result["message"]
    assert result["volumes"] == []


# -- the family-choice rule ---------------------------------------------------------


def _locked_folder(root: Path, filenames: dict[str, bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in filenames.items():
        (root / name).write_bytes(data)
    return root


def test_single_looks_like_family_is_chosen_for_the_owner(controller, tmp_path):
    folder = _locked_folder(tmp_path / "locked", {"_readme.txt": b"note", "a.djvu": b"x"})
    plan = controller.resolve_decrypt_family(folder)
    assert plan["status"] == "ready"
    assert plan["entry"].family == "STOP/Djvu"


def test_may_be_only_needs_the_owner_to_choose(controller, tmp_path):
    folder = _locked_folder(tmp_path / "locked", {"a.djvu": b"x"})
    plan = controller.resolve_decrypt_family(folder)
    assert plan["status"] == "choose"
    assert "STOP/Djvu" in plan["choices"]


def test_two_looks_like_families_need_the_owner_to_choose(controller, tmp_path):
    folder = _locked_folder(
        tmp_path / "locked",
        {"_readme.txt": b"n", "Help Restore Your Files.txt": b"n", "a.babuk": b"x"},
    )
    plan = controller.resolve_decrypt_family(folder)
    assert plan["status"] == "choose"
    assert "STOP/Djvu" in plan["choices"]
    assert "Babuk" in plan["choices"]


def test_no_signs_refuses_in_the_clis_words(controller, tmp_path):
    folder = _locked_folder(tmp_path / "locked", {"plain.txt": b"hello"})
    plan = controller.resolve_decrypt_family(folder)
    assert plan["status"] == "refused"
    assert "will not run a decryptor blind" in plan["message"]
    with pytest.raises(LazarusError):
        controller.run_decrypt(folder, tmp_path / "work", tmp_path / "prog.bin")


def test_naming_the_family_directly_works_and_a_bad_name_refuses(controller, tmp_path):
    folder = _locked_folder(tmp_path / "locked", {"plain.txt": b"hello"})
    plan = controller.resolve_decrypt_family(folder, "Babuk")
    assert plan["status"] == "ready"
    assert plan["entry"].family == "Babuk"
    bad = controller.resolve_decrypt_family(folder, "NoSuchFamily")
    assert bad["status"] == "refused"
    assert "no decryptor listed" in bad["message"]


def test_identify_view_uses_only_the_engines_confidence_words(controller, tmp_path):
    folder = _locked_folder(tmp_path / "locked", {"_readme.txt": b"n", "a.djvu": b"x"})
    result = controller.identify(folder)
    assert result["matches"]
    assert {m["confidence"] for m in result["matches"]} <= {"looks like", "may be"}


def test_registry_view_lists_six_published_decryptors(controller):
    rows = controller.registry_view()
    assert len(rows) == 6
    assert all(row["publisher"] and row["verified_utc"] for row in rows)


def test_assess_view_for_a_plain_folder(controller, tmp_path):
    folder = _locked_folder(tmp_path / "plain", {"a.txt": b"hi"})
    result = controller.assess(folder)
    assert result["findings"]
    assert result["options"]


# -- backup: setup mirror, status, history, pruning -----------------------------------


def test_backup_setup_with_a_bad_time_creates_nothing(controller, tmp_path):
    source = _tree(tmp_path / "source")
    store = tmp_path / "backup-store"
    with pytest.raises(LazarusError) as caught:
        controller.backup_setup(store, [source], at="tea-time")
    assert "cannot read" in str(caught.value)
    assert not store.exists(), "a refused setup must not create the store"
    assert not controller.backup_configured()


def test_backup_setup_with_a_missing_source_refuses_in_engine_words(controller, tmp_path):
    store = tmp_path / "backup-store"
    with pytest.raises(LazarusError) as caught:
        controller.backup_setup(store, [tmp_path / "missing"])
    assert "is not there, so it cannot be backed up" in str(caught.value)
    assert not store.exists()


def _set_up(controller, tmp_path, **kwargs):
    source = _tree(tmp_path / "source")
    store = tmp_path / "backup-store"
    result = controller.backup_setup(store, [source], **kwargs)
    return result, source, store


def test_backup_setup_run_status_ready_and_history(controller, tmp_path):
    _set_up(controller, tmp_path)
    status = controller.backup_status_view()
    assert status["state"] == "never-run"
    assert status["next_due_local"]
    run = controller.run_backup_now()
    assert run["outcome"] == "complete"
    status = controller.backup_status_view()
    assert status["state"] == "ready"
    history = controller.backup_history_view()
    assert len(history["entries"]) == 1
    assert history["entries"][0]["trigger_words"] == "by hand"
    assert history["entries"][0]["outcome_words"] == "complete"


def test_backup_status_target_missing(controller, tmp_path):
    import shutil

    _result, _source, store = _set_up(controller, tmp_path)
    controller.run_backup_now()
    shutil.rmtree(store)
    status = controller.backup_status_view()
    assert status["state"] == "target-missing"
    assert status["next_action"]


def test_backup_setup_into_a_folder_with_other_files_is_refused(controller, tmp_path):
    source = _tree(tmp_path / "source")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "other.txt").write_text("not a store", encoding="utf-8")
    with pytest.raises(LazarusError) as caught:
        controller.backup_setup(busy, [source])
    assert "already holds other files" in str(caught.value)


def test_prune_plan_and_apply(controller, tmp_path):
    _set_up(controller, tmp_path)
    controller.run_backup_now()
    source = tmp_path / "source"
    (source / "docs" / "notes.txt").write_text("second version", encoding="utf-8")
    controller.run_backup_now()
    # Tighten the keeping rule to the newest one backup, saved
    # through the engine's own writer, then plan through the
    # controller.
    config = load_config(controller.config_path)
    config.retention = RetentionSpec(mode="count", count=1, days=30)
    save_config(config, controller.config_path)
    plan = controller.prune_plan_view()
    assert len(plan["remove"]) == 1
    result = controller.prune_apply()
    assert len(result["removed_ids"]) == 1
    store_views = controller.backup_status_view()
    assert store_views["snapshots_in_store"] == 1


# -- multi-root restore: refusals merge, they do not swallow (F-02/F-10) ------


def test_second_root_refusal_does_not_swallow_first_root(stocked, tmp_path, monkeypatch):
    controller, _source = stocked
    sid = controller.list_snapshots()[0]["id"]
    real_restore = ctrl_mod.restore
    calls = {"n": 0}

    def flaky(store, destination=None, snapshot_id=None, path=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RestoreRefusedError(
                "Lazarus could not save a complete safety copy of what "
                "this restore would overwrite, so nothing was changed."
            )
        return real_restore(
            store, destination=destination, snapshot_id=snapshot_id, path=path
        )

    monkeypatch.setattr(ctrl_mod, "restore", flaky)
    dest = tmp_path / "dest"
    result = controller.restore_selection(
        sid, dest, ["docs/notes.txt", "photo.bin"]
    )
    # Root 1's report AND root 2's refusal are both in the merged
    # output, and root 1's file is on disk.
    assert (dest / "docs" / "notes.txt").read_text(encoding="utf-8") == (
        "remember the milk"
    )
    joined = "\n".join(result["lines"])
    assert "Everything came back" in joined
    assert "photo.bin" in joined
    assert "could not save a complete safety copy" in joined
    assert result["refusals"] == [
        {"path": "photo.bin", "message": result["refusals"][0]["message"]}
    ]
    assert any(p["path"] == "photo.bin" for p in result["problems"])
    assert result["status"] == "partial"


def test_all_roots_refusing_merges_to_failed_with_every_refusal(
    stocked, tmp_path, monkeypatch
):
    controller, _source = stocked
    sid = controller.list_snapshots()[0]["id"]

    def always_refuse(store, destination=None, snapshot_id=None, path=None):
        raise RestoreRefusedError(f"refused for {path} in this test")

    monkeypatch.setattr(ctrl_mod, "restore", always_refuse)
    dest = tmp_path / "dest"
    result = controller.restore_selection(
        sid, dest, ["docs/notes.txt", "photo.bin"]
    )
    assert result["status"] == "failed"
    joined = "\n".join(result["lines"])
    assert "docs/notes.txt: refused for docs/notes.txt in this test" in joined
    assert "photo.bin: refused for photo.bin in this test" in joined
    assert result["reports"] == []
    assert not (dest / "docs" / "notes.txt").exists()


def test_one_complete_and_one_partial_root_merges_to_partial(stocked, tmp_path):
    controller, _source = stocked
    sid = controller.list_snapshots()[0]["id"]
    store = SnapshotStore.open(tmp_path / "store")
    manifest = store.load_manifest(sid)
    photo = next(e for e in manifest.entries if e.path == "photo.bin")
    store.object_path(photo.sha256).write_bytes(b"corrupted")
    dest = tmp_path / "dest"
    result = controller.restore_selection(
        sid, dest, ["docs/notes.txt", "photo.bin"]
    )
    assert result["status"] == "partial"  # was "failed" before the fix
    assert (dest / "docs" / "notes.txt").exists()
    assert not (dest / "photo.bin").exists()
    assert any(p["path"] == "photo.bin" for p in result["problems"])


def test_folder_blocked_root_merges_to_partial_not_failed(stocked, tmp_path):
    controller, _source = stocked
    sid = controller.list_snapshots()[0]["id"]
    dest = tmp_path / "dest"
    (dest / "photo.bin").mkdir(parents=True)  # a folder blocks the file
    result = controller.restore_selection(
        sid, dest, ["docs/notes.txt", "photo.bin"]
    )
    assert result["status"] == "partial"
    assert (dest / "docs" / "notes.txt").exists()


# -- prune: apply the shown plan, and only that plan (F-03) --------------------


def _three_backups(controller, tmp_path):
    _set_up(controller, tmp_path)
    source = tmp_path / "source"
    for version in ("v1", "v2", "v3"):
        (source / "docs" / "notes.txt").write_text(version, encoding="utf-8")
        controller.run_backup_now()
    config = load_config(controller.config_path)
    config.retention = RetentionSpec(mode="count", count=2, days=30)
    save_config(config, controller.config_path)
    return source


def test_prune_apply_refuses_after_settings_are_resaved(controller, tmp_path):
    source = _three_backups(controller, tmp_path)
    shown = controller.prune_plan_view()
    assert len(shown["remove"]) == 1
    keep_ids = list(shown["keep_ids"])
    # Re-saving the backup settings (here tightened to keep-1)
    # spends the shown plan.
    controller.backup_setup(
        tmp_path / "backup-store", [source], keep_count=1
    )
    with pytest.raises(LazarusError) as caught:
        controller.prune_apply()
    assert "Look at what would be tidied up first" in str(caught.value)
    # Nothing was removed: every snapshot, keep-set included, stands.
    store = SnapshotStore.open(tmp_path / "backup-store")
    remaining = {info.id for info in store.list_snapshots()}
    assert set(keep_ids) <= remaining
    assert len(remaining) == 3


def test_prune_apply_refuses_after_a_backup_run(controller, tmp_path):
    source = _three_backups(controller, tmp_path)
    shown = controller.prune_plan_view()
    assert len(shown["remove"]) == 1
    (source / "docs" / "notes.txt").write_text("v4", encoding="utf-8")
    controller.run_backup_now()
    with pytest.raises(LazarusError) as caught:
        controller.prune_apply()
    assert "Look at what would be tidied up first" in str(caught.value)
    # The run itself is the only thing that changed the store
    # (a clean run applies the keeping rule, keep-2); the stale
    # plan removed nothing.
    assert controller.backup_status_view()["snapshots_in_store"] == 2


def test_prune_apply_applies_exactly_the_shown_plan_once(controller, tmp_path):
    _three_backups(controller, tmp_path)
    shown = controller.prune_plan_view()
    planned_remove = [item["id"] for item in shown["remove"]]
    assert len(planned_remove) == 1
    result = controller.prune_apply()
    assert result["removed_ids"] == planned_remove
    store = SnapshotStore.open(tmp_path / "backup-store")
    remaining = {info.id for info in store.list_snapshots()}
    assert remaining == set(shown["keep_ids"])
    # A plan is single-use: a second apply refuses plainly.
    with pytest.raises(LazarusError) as caught:
        controller.prune_apply()
    assert "Look at what would be tidied up first" in str(caught.value)


def test_prune_apply_without_any_plan_refuses_plainly(controller, tmp_path):
    _set_up(controller, tmp_path)
    with pytest.raises(LazarusError) as caught:
        controller.prune_apply()
    assert str(caught.value) == (
        "Look at what would be tidied up first, so you can see "
        "exactly what will be removed."
    )


# -- pipes and sockets never reach the engine (F-05) ----------------------------

def _mkfifo(path):
    """Make a pipe, or stand the test down where pipes cannot be made.

    os.mkfifo is POSIX-only; on Windows there is no pipe file to
    refuse, so these pins stand down there instead of erroring.
    """
    if not hasattr(os, "mkfifo"):
        pytest.skip("this computer cannot make a pipe (no os.mkfifo)")
    os.mkfifo(path)



def test_fifo_sources_are_refused_plainly(controller, tmp_path):
    fifo = tmp_path / "the-pipe"
    _mkfifo(fifo)
    for call in (
        lambda: controller.scan_deleted(fifo),
        lambda: controller.rescue(fifo, tmp_path / "img.bin"),
        lambda: controller.recover(fifo, tmp_path / "out"),
    ):
        with pytest.raises(LazarusError) as caught:
            call()
        assert "is a pipe" in str(caught.value)
        assert "not a drive, an image, a file, or a folder" in str(caught.value)
    controller.create_store(tmp_path / "store")
    with pytest.raises(LazarusError) as caught:
        controller.take_snapshot(fifo)
    assert "is a pipe" in str(caught.value)


def test_fifo_rescue_destination_is_refused_plainly(controller, tmp_path):
    source = tmp_path / "drive.bin"
    source.write_bytes(b"\x01" * 8192)
    fifo = tmp_path / "dest-pipe"
    _mkfifo(fifo)
    with pytest.raises(LazarusError) as caught:
        controller.rescue(source, fifo)
    assert "is a pipe" in str(caught.value)
    assert "write the rescue image" in str(caught.value)


def test_socket_source_is_refused_plainly(controller, tmp_path):
    import socket

    sock_path = tmp_path / "the-socket"
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(sock_path))
        with pytest.raises(LazarusError) as caught:
            controller.scan_deleted(sock_path)
        assert "is a socket" in str(caught.value)
        assert "not a drive, an image, a file, or a folder" in str(caught.value)
    finally:
        sock.close()


def test_normal_file_still_scans_with_the_fifo_guard_in_place(controller, fat_drive):
    path, _content = fat_drive
    result = controller.scan_deleted(path)
    assert result["files"], "a normal image file must still scan"


# -- shortcuts to pipes and sockets are refused too (R-01) --------------------
#
# The guard must judge a shortcut by what it points at for the
# calls whose engine opens the target (scan, recover, rescue —
# source and destination alike): a shortcut to a pipe would
# otherwise block the engine's open forever, exactly as a direct
# pipe does. A snapshot is the one exception — the engine stores
# a shortcut as a shortcut and never opens its target.


def test_symlink_to_fifo_sources_are_refused_plainly(controller, tmp_path):
    fifo = tmp_path / "the-pipe"
    _mkfifo(fifo)
    link = tmp_path / "the-shortcut"
    link.symlink_to(fifo)
    for call in (
        lambda: controller.scan_deleted(link),
        lambda: controller.rescue(link, tmp_path / "img.bin"),
        lambda: controller.recover(link, tmp_path / "out"),
    ):
        with pytest.raises(LazarusError) as caught:
            call()
        assert "is a pipe" in str(caught.value)
        assert "not a drive, an image, a file, or a folder" in str(caught.value)


def test_symlink_to_fifo_rescue_destination_is_refused_plainly(controller, tmp_path):
    source = tmp_path / "drive.bin"
    source.write_bytes(b"\x01" * 8192)
    fifo = tmp_path / "dest-pipe"
    _mkfifo(fifo)
    link = tmp_path / "dest-shortcut"
    link.symlink_to(fifo)
    with pytest.raises(LazarusError) as caught:
        controller.rescue(source, link)
    assert "is a pipe" in str(caught.value)
    assert "write the rescue image" in str(caught.value)


def test_symlink_to_socket_source_is_refused_plainly(controller, tmp_path):
    import socket

    sock_path = tmp_path / "the-socket"
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(sock_path))
        link = tmp_path / "socket-shortcut"
        link.symlink_to(sock_path)
        with pytest.raises(LazarusError) as caught:
            controller.scan_deleted(link)
        assert "is a socket" in str(caught.value)
        assert "not a drive, an image, a file, or a folder" in str(caught.value)
    finally:
        sock.close()


def test_symlink_to_a_regular_file_still_scans(controller, fat_drive, tmp_path):
    path, _content = fat_drive
    link = tmp_path / "image-shortcut"
    link.symlink_to(path)
    result = controller.scan_deleted(link)
    assert result["files"], "a shortcut to a normal image file must still scan"


def test_broken_symlink_source_passes_the_guard_to_the_engine(controller, tmp_path):
    link = tmp_path / "broken-shortcut"
    link.symlink_to(tmp_path / "missing-target")
    with pytest.raises(LazarusError) as caught:
        controller.scan_deleted(link)
    # The guard does not speak for a broken shortcut: the refusal
    # is the engine's own plain words, not the pipe message.
    assert "is a pipe" not in str(caught.value)
    assert "is a socket" not in str(caught.value)
    assert "could not find" in str(caught.value)


def test_snapshot_of_a_symlink_to_a_fifo_is_still_stored_as_a_link(
    controller, tmp_path
):
    fifo = tmp_path / "the-pipe"
    _mkfifo(fifo)
    link = tmp_path / "the-shortcut"
    link.symlink_to(fifo)
    controller.create_store(tmp_path / "store")
    result = controller.take_snapshot(link)
    assert result["complete"] is True
    assert result["file_count"] == 0


# -- a plan settled from an identification already made (R-04) -----------------


def test_plan_from_identification_settles_without_reading_the_folder(
    controller, tmp_path
):
    import shutil

    folder = _locked_folder(tmp_path / "locked", {"_readme.txt": b"n", "a.djvu": b"x"})
    identification = controller.identify(folder)
    # The folder is gone by the time the plan is settled: the
    # settle can only have used the identification already made.
    shutil.rmtree(folder)
    plan = controller.plan_from_identification(identification)
    assert plan["status"] == "ready"
    assert plan["entry"].family == "STOP/Djvu"


def test_plan_from_identification_choose_settle_and_refusals(controller, tmp_path):
    folder = _locked_folder(tmp_path / "locked", {"a.djvu": b"x"})
    identification = controller.identify(folder)
    plan = controller.plan_from_identification(identification)
    assert plan["status"] == "choose"
    assert plan["choices"] == ["STOP/Djvu"]
    settled = controller.plan_from_identification(identification, "STOP/Djvu")
    assert settled["status"] == "ready"
    assert settled["entry"].family == "STOP/Djvu"
    bad = controller.plan_from_identification(identification, "NoSuchFamily")
    assert bad["status"] == "refused"
    assert "no decryptor listed" in bad["message"]
    plain = _locked_folder(tmp_path / "plain", {"plain.txt": b"hello"})
    no_signs = controller.plan_from_identification(controller.identify(plain))
    assert no_signs["status"] == "refused"
    assert "will not run a decryptor blind" in no_signs["message"]
    # And it still agrees with the resolving entry point, word
    # for word in status and choices, on a folder both can read.
    resolved = controller.resolve_decrypt_family(folder)
    assert resolved["status"] == plan["status"]
    assert resolved["choices"] == plan["choices"]
    assert resolved["message"] == plan["message"]
