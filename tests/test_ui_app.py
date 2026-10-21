"""The Lazarus window, offscreen: it builds, switches, and answers.

These tests keep to construction, navigation, designed empty and
problem states, the key dialog's handling, and one full pass through
the store wiring (make a store, take a snapshot, browse it, restore
it) — fast and deterministic, no dialogs left open.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QLineEdit,
)

from lazarus.decrypt_model import VolumeInfo  # noqa: E402
from lazarus.errors import LazarusError  # noqa: E402
from lazarus.ui import app as app_mod  # noqa: E402
from lazarus.ui.app import KeyDialog, MainWindow  # noqa: E402
from lazarus.ui.controller import Controller  # noqa: E402
from lazarus.unlock import UnlockAttempt  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _fixture_platform():
    """The platform a fixture window talks to.

    Off Windows this is the computer's own seam (None), whose
    honest "not running Windows" answer the locked-page test
    pins. On Windows the computer's own seam shells out to
    PowerShell and manage-bde with no timeout, and a window
    fixture must never wait on real Windows tooling — or leave
    it running when the fixture tears the window down — so the
    window gets an empty fake there instead. The unlock flows
    keep their own fake-platform tests on every system.
    """
    if sys.platform == "win32":
        return _FakePlatform([])
    return None


@pytest.fixture()
def window(qapp, tmp_path):
    controller = Controller(
        config_path=tmp_path / "cfg" / "backup.json",
        platform=_fixture_platform(),
    )
    win = MainWindow(controller)
    win.show()
    qapp.processEvents()
    yield win
    win.close()
    qapp.processEvents()


def wait_until(qapp, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    qapp.processEvents()
    return predicate()


def test_window_constructs_with_all_five_pages_restore_first(window):
    assert window.stack.count() == 5
    names = [window.sidebar.item(i).text() for i in range(window.sidebar.count())]
    assert names == ["Restore", "Snapshots", "Deleted files", "Locked files", "Backup"]
    assert window.sidebar.currentRow() == 0
    assert "LAZARUS.EXE" in window.windowTitle()


def test_sidebar_switches_pages(window, qapp):
    for row in range(5):
        window.sidebar.setCurrentRow(row)
        qapp.processEvents()
        assert window.stack.currentIndex() == row


def test_restore_page_empty_state_with_no_store(window):
    assert window.restore_empty.isVisible()
    assert window.snapshot_list.count() == 0
    assert "No store is open" in window.store_label.text()


def test_every_page_main_action_without_input_shows_a_designed_message(window, qapp):
    # Restore with nothing chosen.
    window.restore_button.click()
    assert window.restore_panel.isVisible()
    assert window.restore_panel.title.text() == "No snapshot is chosen"
    # Snapshots with no source named.
    window.snap_button.click()
    assert window.snap_panel.title.text() == "Nothing was named"
    # Scan with no source named.
    window.scan_button.click()
    assert window.deleted_panel.title.text() == "No drive or image was named"
    # Unlock with no drive chosen.
    window.unlock_button.click()
    assert window.locked_panel.title.text() == "No drive is chosen"
    # Backup setup with nothing filled in.
    window.backup_setup_button.click()
    qapp.processEvents()
    assert window.backup_panel.title.text() == "No backup store was named"


def test_key_dialog_is_password_echo_and_clears_after_reading(qapp):
    dialog = KeyDialog("E:")
    assert dialog.key_edit.echoMode() == QLineEdit.EchoMode.Password
    dialog.key_edit.setText("1234-5678-FAKE")
    assert dialog.take_key() == "1234-5678-FAKE"
    assert dialog.key_edit.text() == ""


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="pins the off-Windows explanation; window fixtures use "
    "a fake platform on Windows",
)
def test_locked_page_shows_the_engines_plain_explanation_off_windows(window):
    # This machine is not Windows: the volumes area must carry the
    # engine's explanation, not an empty void.
    assert window.volumes_table.rowCount() == 0
    assert "not running Windows" in window.volumes_state.text()


def test_backup_page_leads_with_the_no_setup_state(window, qapp):
    window.sidebar.setCurrentRow(4)
    qapp.processEvents()
    assert window.backup_none.isVisible()
    assert not window.backup_banner.isVisible()


def test_store_snapshot_browse_restore_through_the_window(window, qapp, tmp_path):
    source = tmp_path / "source"
    (source / "docs").mkdir(parents=True)
    (source / "docs" / "notes.txt").write_text("remember the milk", encoding="utf-8")

    window.store_path_edit.setText(str(tmp_path / "store"))
    window.create_store_button.click()
    qapp.processEvents()
    assert window.controller.store_path is not None
    assert not window.restore_empty.isVisible()

    window.snap_source_edit.setText(str(source))
    window.snap_label_edit.setText("first save")
    window.snap_button.click()
    assert wait_until(qapp, lambda: window.snapshot_list.count() == 1)
    assert window.snap_list_page.count() == 1

    item = window.snapshot_list.item(0)
    window.snapshot_list.setCurrentItem(item)
    qapp.processEvents()
    assert window._manifest is not None
    assert window.browse_tree.topLevelItemCount() >= 1
    assert window.restore_dest_edit.text() == str(source.resolve()) or window.restore_dest_edit.text()

    dest = tmp_path / "brought-back"
    window._do_restore(None, str(dest))
    assert wait_until(qapp, lambda: window.restore_report.isVisible())
    assert (dest / "docs" / "notes.txt").read_text(encoding="utf-8") == "remember the milk"


def test_backup_page_shows_the_loud_status_after_setup(window, qapp, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("safe", encoding="utf-8")
    window.controller.backup_setup(tmp_path / "backup-store", [source])
    window.refresh_backup()
    window.sidebar.setCurrentRow(4)
    qapp.processEvents()
    assert window.backup_banner.isVisible()
    assert not window.backup_none.isVisible()
    assert window.backup_state_word.text() == "NO BACKUP HAS RUN YET"


def test_dialogs_size_to_their_contents(qapp):
    """A dialog's wrapping labels must fit inside the dialog.

    Regression pin: the dialogs once carried only a minimum width,
    which could squeeze a word-wrapping label into less height
    than its text needs — the text overflowed its label wherever
    the platform did not grow the dialog afterwards. The dialogs
    now size to their contents; this test measures it.
    """
    from PySide6.QtWidgets import QLabel

    from lazarus.ui import theme
    from lazarus.ui.app import ConfirmDialog, RestoreConfirmDialog

    # The squeeze only bites under the app's own stylesheet — its
    # font metrics are what outgrow the dialog's first size — so
    # the test wears the stylesheet too, and puts it back after.
    previous_stylesheet = qapp.styleSheet()
    qapp.setStyleSheet(theme.STYLESHEET)
    long_body = (
        "Lazarus will run Emsisoft Decryptor for STOP Djvu "
        "(published by Emsisoft, for STOP/Djvu) on COPIES of "
        "the files in /tmp/a-long-folder-name/locked-files, "
        "inside the work folder /tmp/a-long-folder-name/"
        "decrypt-work. The program you chose is the one that "
        "runs. Your original files are never handed to it, and "
        "they are checked before and after so the report can "
        "say they did not change."
    )
    dialogs = [
        ConfirmDialog("Run this decryptor on copies?", long_body,
                      "Run it on copies", danger=True),
        RestoreConfirmDialog("2026-10-20", "a label", "3 files",
                             "/a/long/destination/path"),
        KeyDialog("E:"),
    ]
    for dialog in dialogs:
        dialog.show()
        qapp.processEvents()
        for label in dialog.findChildren(QLabel):
            if label.wordWrap():
                needed = label.heightForWidth(label.width())
                assert label.height() >= needed, (
                    f"{type(dialog).__name__}: a wrapping label is "
                    f"squeezed ({label.height()} < {needed})"
                )
        dialog.close()
    qapp.setStyleSheet(previous_stylesheet)


# -- Phase 5 audit fixes -------------------------------------------------------


class _FakePlatform:
    def __init__(self, volumes, attempt=None):
        self._volumes = {v.ref: v for v in volumes}
        self._attempt = attempt if attempt is not None else UnlockAttempt(ok=True)
        self.run_secrets = []

    def describe_volume(self, ref):
        return self._volumes.get(ref)

    def list_volumes(self):
        return list(self._volumes.values())

    def run_unlock(self, argv, secret):
        self.run_secrets.append(secret)
        return self._attempt


def _volume(ref, locked, kind="bitlocker"):
    return VolumeInfo(ref=ref, kind=kind, label="", locked=locked, evidence=[])


def _locked_dir(root: Path, files: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (root / name).write_bytes(data)
    return root


def _drain(qapp, window):
    assert wait_until(qapp, lambda: not window._workers)


def test_stale_family_picker_cannot_offer_the_wrong_decryptor(
    window, qapp, tmp_path, monkeypatch
):
    _drain(qapp, window)
    folder_a = _locked_dir(tmp_path / "locked-a", {"a.djvu": b"x"})
    window.locked_folder_edit.setText(str(folder_a))
    window.identify_button.click()
    _drain(qapp, window)
    assert not window.family_combo.isHidden()
    assert [window.family_combo.itemText(i) for i in range(window.family_combo.count())] == [
        "STOP/Djvu"
    ]
    folder_b = _locked_dir(
        tmp_path / "locked-b",
        {"Help Restore Your Files.txt": b"n", "a.babuk": b"x"},
    )
    window.locked_folder_edit.setText(str(folder_b))
    # Changing the folder has already cleared the stale picker.
    assert window.family_combo.isHidden()
    assert window.family_combo.count() == 0
    # And with no identification standing for B, Run starts
    # nothing (R-04): it asks for an identification first.
    window.decrypt_button.click()
    qapp.processEvents()
    assert window.locked_panel.title.text() == "Identify the folder first"
    # Identifying B settles a ready plan for Babuk, which is what
    # the confirm below must be about.
    window.identify_button.click()
    _drain(qapp, window)
    assert window._decrypt_plan["status"] == "ready"
    assert window._decrypt_plan["entry"].family == "Babuk"
    window.work_edit.setText(str(tmp_path / "work"))
    window.decryptor_edit.setText(str(tmp_path / "prog"))
    captured = {}

    def fake_exec(self):
        captured["labels"] = [
            label.text() for label in self.findChildren(QLabel)
        ]
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", fake_exec)
    window.decrypt_button.click()
    qapp.processEvents()
    body = "\n".join(captured.get("labels", []))
    assert "Babuk" in body
    assert "STOP/Djvu" not in body and "STOP Djvu" not in body
    assert window.family_combo.isHidden()
    assert window.family_combo.count() == 0


def test_identify_resets_picker_and_its_ready_plan_drives_decrypt(
    window, qapp, tmp_path, monkeypatch
):
    _drain(qapp, window)
    folder_a = _locked_dir(tmp_path / "locked-a", {"a.djvu": b"x"})
    window.locked_folder_edit.setText(str(folder_a))
    window.identify_button.click()
    _drain(qapp, window)
    assert not window.family_combo.isHidden()
    assert window.family_combo.count() == 1
    folder_b = _locked_dir(
        tmp_path / "locked-b",
        {"Help Restore Your Files.txt": b"n", "a.babuk": b"x"},
    )
    window.locked_folder_edit.setText(str(folder_b))
    window.identify_button.click()
    _drain(qapp, window)
    # The ready plan for B reset the picker: hidden and empty.
    assert window.family_combo.isHidden()
    assert window.family_combo.count() == 0
    assert window._decrypt_plan["status"] == "ready"
    assert window._decrypt_plan["entry"].family == "Babuk"
    window.work_edit.setText(str(tmp_path / "work"))
    window.decryptor_edit.setText(str(tmp_path / "prog"))
    captured = {}

    def fake_exec(self):
        captured["labels"] = [
            label.text() for label in self.findChildren(QLabel)
        ]
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", fake_exec)
    window.decrypt_button.click()
    qapp.processEvents()
    body = "\n".join(captured.get("labels", []))
    assert "Babuk" in body
    assert "STOP/Djvu" not in body


def test_cancelled_key_is_not_kept_anywhere_on_the_window(
    window, qapp, tmp_path, monkeypatch
):
    _drain(qapp, window)
    platform = _FakePlatform([_volume("E:", True)])
    window.controller.platform = platform
    window.refresh_volumes()
    _drain(qapp, window)
    window.volumes_table.setCurrentCell(0, 0)

    def cancel_exec(self):
        self.key_edit.setText("TYPED-BUT-CANCELLED-KEY")
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(KeyDialog, "exec", cancel_exec)
    window.unlock_button.click()
    _drain(qapp, window)
    assert platform.run_secrets == []
    assert "No recovery key was given" in window.locked_panel.body.text()
    # The dialog is gone from the window, and so is the typed key.
    assert not hasattr(window, "last_key_dialog")
    for value in vars(window).values():
        assert not isinstance(value, KeyDialog)
        if isinstance(value, str):
            assert "TYPED-BUT-CANCELLED-KEY" not in value

    # The Accept path still works exactly as before.
    def accept_exec(self):
        self.key_edit.setText("REAL-KEY-42")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(KeyDialog, "exec", accept_exec)
    window.unlock_button.click()
    _drain(qapp, window)
    assert platform.run_secrets == ["REAL-KEY-42"]
    assert not hasattr(window, "last_key_dialog")


def test_close_while_a_job_runs_waits_for_it_then_closes(
    window, qapp, monkeypatch
):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    monkeypatch.setattr(
        app_mod.ConfirmDialog,
        "exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    try:
        worker = window.run_worker(
            lambda: release.wait(10),
            [window.scan_button],
            lambda result: None,
            window.deleted_panel,
        )
        assert worker is not None
        window.close()
        qapp.processEvents()
        assert window.isVisible()  # still open: the job comes first
        assert window._close_when_idle is True
        assert not window.stack.isEnabled()
        assert not window.sidebar.isEnabled()
    finally:
        release.set()
    assert wait_until(qapp, lambda: not window.isVisible())
    _drain(qapp, window)


def test_close_cancelled_while_a_job_runs_leaves_everything_open(
    window, qapp, monkeypatch
):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    monkeypatch.setattr(
        app_mod.ConfirmDialog,
        "exec",
        lambda self: QDialog.DialogCode.Rejected,
    )
    try:
        worker = window.run_worker(
            lambda: release.wait(10),
            [window.scan_button],
            lambda result: None,
            window.deleted_panel,
        )
        assert worker is not None
        window.close()
        qapp.processEvents()
        assert window.isVisible()
        assert window._close_when_idle is False
    finally:
        release.set()
    _drain(qapp, window)
    assert window.isVisible()  # a finished job does not close it either


def test_return_pressed_cannot_start_a_second_job_and_other_pages_wait(
    window, qapp, tmp_path
):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    calls = []

    def slow_scan(source):
        calls.append(("scan", source))
        release.wait(10)
        return {
            "source_description": "img", "fs_type": "FAT", "notes": [],
            "lines": ["img"], "strong_count": 0, "partial_count": 0,
            "gone_count": 0, "files": [],
        }

    def rescue_spy(source, destination, map_path=None):
        calls.append(("rescue", source))
        return {}

    window.controller.scan_deleted = slow_scan
    window.controller.rescue = rescue_spy
    try:
        window.scan_source_edit.setText("img")
        window.scan_button.click()
        assert wait_until(qapp, lambda: len(calls) == 1)
        # Enter in the source edit is the same ask, and is refused.
        window.scan_source_edit.returnPressed.emit()
        qapp.processEvents()
        assert len(window._workers) == 1
        assert calls == [("scan", "img")]
        assert window.deleted_panel.body.text() == (
            "Lazarus is already working on something — let it finish first."
        )
        # A different long job on another part of the page waits too.
        window.rescue_source_edit.setText("img")
        window.rescue_dest_edit.setText(str(tmp_path / "out.bin"))
        window.rescue_button.click()
        qapp.processEvents()
        assert calls == [("scan", "img")]
        assert len(window._workers) == 1
        assert window.rescue_progress.isHidden()
    finally:
        release.set()
    _drain(qapp, window)
    # The one scan's result was applied, once.
    assert window._scan_result is not None
    assert window._scan_result["source_description"] == "img"


def test_identify_is_refused_while_another_job_runs(window, qapp, tmp_path):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    identify_calls = []
    real_identify = window.controller.identify
    window.controller.identify = lambda folder: (
        identify_calls.append(folder), real_identify(folder)
    )[1]
    try:
        worker = window.run_worker(
            lambda: release.wait(10),
            [window.scan_button],
            lambda result: None,
            window.locked_panel,
        )
        assert worker is not None
        folder = _locked_dir(tmp_path / "locked", {"a.djvu": b"x"})
        window.locked_folder_edit.setText(str(folder))
        window.identify_button.click()
        qapp.processEvents()
        assert identify_calls == []
        assert window.locked_panel.body.text() == (
            "Lazarus is already working on something — let it finish first."
        )
    finally:
        release.set()
    _drain(qapp, window)


def test_backup_setup_saves_the_values_the_form_held_at_click(
    window, qapp, tmp_path
):
    _drain(qapp, window)
    recorded = {}

    def fake_setup(store, sources, **kwargs):
        recorded["store"] = store
        recorded["sources"] = sources
        recorded.update(kwargs)
        time.sleep(0.4)
        return {"lines": ["Backup is set up."], "store_path": str(store)}

    window.controller.backup_setup = fake_setup
    source = tmp_path / "source"
    source.mkdir()
    window.backup_store_edit.setText(str(tmp_path / "backup-store"))
    window.backup_sources_list.addItem(str(source))
    window.backup_weekday_combo.setCurrentText("tuesday")
    window.backup_keep_spin.setValue(5)
    window.backup_verify_spin.setValue(3)
    window.backup_setup_button.click()
    # Change the form while the setup is running: none of this may
    # reach the saved settings, which froze at the click.
    window.backup_weekday_combo.setCurrentText("friday")
    window.backup_keep_spin.setValue(9)
    window.backup_verify_spin.setValue(11)
    _drain(qapp, window)
    assert recorded["weekday"] == "tuesday"
    assert recorded["keep_count"] == 5
    assert recorded["verify_every_runs"] == 3


def test_failed_rescue_hides_the_bar_and_names_the_outcome(
    window, qapp, tmp_path
):
    _drain(qapp, window)

    def boom(source, destination, map_path=None):
        raise LazarusError("The rescue could not start (test).")

    window.controller.rescue = boom
    window.rescue_source_edit.setText(str(tmp_path / "src.bin"))
    window.rescue_dest_edit.setText(str(tmp_path / "img.bin"))
    window.rescue_button.click()
    _drain(qapp, window)
    assert not window._rescue_timer.isActive()
    assert window.rescue_progress.isHidden()
    assert "could not start" in window.rescue_progress_label.text()
    assert window.rescue_progress_label.text() != "Starting the copy…"


def test_decrypt_without_identifying_first_resolves_nothing_and_starts_nothing(
    window, qapp, tmp_path, monkeypatch
):
    _drain(qapp, window)
    calls = {"identify": 0, "resolve": 0, "plan": 0, "dialogs": 0}
    real_identify = window.controller.identify
    real_resolve = window.controller.resolve_decrypt_family
    real_plan = window.controller.plan_from_identification

    def counting_identify(folder):
        calls["identify"] += 1
        return real_identify(folder)

    def counting_resolve(folder, family_name=None):
        calls["resolve"] += 1
        return real_resolve(folder, family_name)

    def counting_plan(identification, family_name=None):
        calls["plan"] += 1
        return real_plan(identification, family_name)

    def counting_exec(self):
        calls["dialogs"] += 1
        return QDialog.DialogCode.Accepted

    window.controller.identify = counting_identify
    window.controller.resolve_decrypt_family = counting_resolve
    window.controller.plan_from_identification = counting_plan
    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", counting_exec)
    folder = _locked_dir(tmp_path / "locked", {"a.djvu": b"x"})
    window.locked_folder_edit.setText(str(folder))
    window.work_edit.setText(str(tmp_path / "work"))
    window.decryptor_edit.setText(str(tmp_path / "prog"))
    window.decrypt_button.click()
    qapp.processEvents()
    # No folder was read, no plan was settled, no confirm was
    # shown, and no job started: the panel asks for an
    # identification first, in plain words.
    assert calls == {"identify": 0, "resolve": 0, "plan": 0, "dialogs": 0}
    assert window._workers == []
    assert window._decrypt_plan is None
    assert window.locked_panel.title.text() == "Identify the folder first"
    assert "which family it looks like" in window.locked_panel.body.text()


def test_decrypt_settles_a_chosen_family_from_the_stored_identification(
    window, qapp, tmp_path, monkeypatch
):
    import shutil

    _drain(qapp, window)
    folder = _locked_dir(tmp_path / "locked", {"a.djvu": b"x"})
    window.locked_folder_edit.setText(str(folder))
    window.identify_button.click()
    _drain(qapp, window)
    assert not window.family_combo.isHidden()
    assert window.family_combo.currentText() == "STOP/Djvu"
    # Settling the pick must never go back through the resolving
    # entry point — and never needs the folder, which is gone.
    def forbidden_resolve(*args, **kwargs):
        raise AssertionError("decrypt resolved the family afresh")

    window.controller.resolve_decrypt_family = forbidden_resolve
    plan_calls = []
    real_plan = window.controller.plan_from_identification

    def counting_plan(identification, family_name=None):
        plan_calls.append(family_name)
        return real_plan(identification, family_name)

    window.controller.plan_from_identification = counting_plan
    shutil.rmtree(folder)
    # The owner is standing on this page when they click Run —
    # the picker is only read while it is truly on show.
    window.sidebar.setCurrentRow(3)
    qapp.processEvents()
    assert window.family_combo.isVisible()
    window.work_edit.setText(str(tmp_path / "work"))
    window.decryptor_edit.setText(str(tmp_path / "prog"))
    captured = {}

    def fake_exec(self):
        captured["labels"] = [
            label.text() for label in self.findChildren(QLabel)
        ]
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", fake_exec)
    window.decrypt_button.click()
    qapp.processEvents()
    assert plan_calls == ["STOP/Djvu"]
    body = "\n".join(captured.get("labels", []))
    assert "STOP/Djvu" in body
    assert window._decrypt_plan["status"] == "ready"
    assert window._decrypt_plan["entry"].family == "STOP/Djvu"


def test_second_close_while_a_close_is_pending_asks_nothing_again(
    window, qapp, monkeypatch
):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    calls = []

    def counting_exec(self):
        calls.append(1)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", counting_exec)
    try:
        worker = window.run_worker(
            lambda: release.wait(10),
            [window.scan_button],
            lambda result: None,
            window.deleted_panel,
        )
        assert worker is not None
        window.close()
        qapp.processEvents()
        assert calls == [1]
        assert window.isVisible() and window._close_when_idle is True
        # A second close attempt while the first is still pending:
        # no second dialog, and the window stays open and pending.
        window.close()
        qapp.processEvents()
        assert calls == [1]
        assert window.isVisible() and window._close_when_idle is True
    finally:
        release.set()
    assert wait_until(qapp, lambda: not window.isVisible())


def test_close_pending_message_survives_the_shimmer(window, qapp, monkeypatch):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    monkeypatch.setattr(
        app_mod.ConfirmDialog,
        "exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    try:
        worker = window.run_worker(
            lambda: release.wait(10),
            [window.scan_button],
            lambda result: None,
            window.deleted_panel,
        )
        assert worker is not None
        window.close()
        qapp.processEvents()
        assert window._close_when_idle is True
        closing = "Working — Lazarus will close when the job finishes."
        assert window.working_label.text() == closing
        for _ in range(5):
            window._tick_shimmer()
        assert window.working_label.text() == closing
    finally:
        release.set()
    assert wait_until(qapp, lambda: not window.isVisible())


def test_install_button_is_disabled_while_a_close_is_pending(
    window, qapp, tmp_path, monkeypatch
):
    import threading

    _drain(qapp, window)
    mod = app_mod.installer
    monkeypatch.setattr(mod, "is_frozen", lambda: True)
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "needs_install_offer", lambda *a, **k: True)
    window.refresh_install_button()
    assert not window.install_button.isHidden()
    assert window.install_button.isEnabled()
    release = threading.Event()
    monkeypatch.setattr(
        app_mod.ConfirmDialog,
        "exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    try:
        worker = window.run_worker(
            lambda: release.wait(10),
            [window.scan_button],
            lambda result: None,
            window.deleted_panel,
        )
        assert worker is not None
        window.close()
        qapp.processEvents()
        assert window._close_when_idle is True
        assert not window.install_button.isEnabled()
        assert not window.stack.isEnabled()
        assert not window.sidebar.isEnabled()
    finally:
        release.set()
    assert wait_until(qapp, lambda: not window.isVisible())


def test_a_job_that_fails_after_finish_then_close_stays_open_to_be_read(
    window, qapp, monkeypatch
):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    monkeypatch.setattr(
        app_mod.ConfirmDialog,
        "exec",
        lambda self: QDialog.DialogCode.Accepted,
    )

    def slow_boom():
        release.wait(10)
        raise LazarusError("The thing could not be done (test).")

    try:
        worker = window.run_worker(
            slow_boom,
            [window.scan_button],
            lambda result: None,
            window.deleted_panel,
        )
        assert worker is not None
        window.close()
        qapp.processEvents()
        assert window.isVisible() and window._close_when_idle is True
        assert not window.stack.isEnabled()
        assert not window.install_button.isEnabled()
    finally:
        release.set()
    _drain(qapp, window)
    qapp.processEvents()
    # The failure cancelled the pending close: the window is open,
    # handed back, and the failure is still there to read.
    assert window.isVisible()
    assert window._close_when_idle is False
    assert window.stack.isEnabled()
    assert window.sidebar.isEnabled()
    assert window.install_button.isEnabled()
    assert window.scan_button.isEnabled()
    assert "could not be done" in window.deleted_panel.body.text()
    assert window.working_label.text() == ""


def test_close_dialog_says_lazarus_stays_open_if_the_job_fails(
    window, qapp, monkeypatch
):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    captured = {}

    def fake_exec(self):
        captured["labels"] = [
            label.text() for label in self.findChildren(QLabel)
        ]
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", fake_exec)
    try:
        worker = window.run_worker(
            lambda: release.wait(10),
            [window.scan_button],
            lambda result: None,
            window.deleted_panel,
        )
        assert worker is not None
        window.close()
        qapp.processEvents()
        body = "\n".join(captured.get("labels", []))
        assert (
            "If the job fails, Lazarus stays open so you can "
            "read what happened."
        ) in body
        assert window._close_when_idle is False
    finally:
        release.set()
    _drain(qapp, window)
    assert window.isVisible()


def test_rescue_success_without_a_readable_map_settles_the_bar(
    window, qapp, tmp_path
):
    _drain(qapp, window)

    def fake_rescue(source, destination, map_path=None):
        return {
            "status": "complete", "lines": ["copied"], "regions_total": 1,
            "regions_recovered": 1, "bytes_recovered": 1,
            "bytes_missing": 0, "total_bytes": 1, "report": None,
        }

    window.controller.rescue = fake_rescue
    window.rescue_source_edit.setText(str(tmp_path / "src.bin"))
    window.rescue_dest_edit.setText(str(tmp_path / "img.bin"))
    # The fake rescue writes no progress file, so none can be
    # read at the finish line — the bar must still settle.
    window.rescue_button.click()
    _drain(qapp, window)
    assert window.rescue_progress.isHidden()
    assert window.rescue_progress_label.text() == "Everything was copied."
    assert not window.rescue_report.isHidden()


def test_install_button_state_matrix(window, qapp, tmp_path, monkeypatch):
    _drain(qapp, window)
    mod = app_mod.installer
    target = tmp_path / "PF" / "Sentinel Safeguard" / "Lazarus" / "Lazarus.exe"
    portable = tmp_path / "Downloads" / "Lazarus.exe"

    # Not the shipped program file: no button at all.
    monkeypatch.setattr(mod, "is_frozen", lambda: False)
    assert window.install_button_state() == (False, "", None)
    window.refresh_install_button()
    assert window.install_button.isHidden()

    monkeypatch.setattr(mod, "is_frozen", lambda: True)
    monkeypatch.setattr(mod, "_is_windows", lambda: False)
    assert window.install_button_state() == (False, "", None)

    # An install is due.
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "needs_install_offer", lambda *a, **k: True)
    assert window.install_button_state() == (
        True, "Install Lazarus on this computer…", "install",
    )
    window.refresh_install_button()
    assert not window.install_button.isHidden()
    assert window.install_button.text() == "Install Lazarus on this computer…"

    # This copy IS the installed copy.
    monkeypatch.setattr(mod, "needs_install_offer", lambda *a, **k: False)
    monkeypatch.setattr(mod, "current_exe", lambda: target)
    monkeypatch.setattr(mod, "installed_exe", lambda *a, **k: target)
    visible, text, action = window.install_button_state()
    assert visible and action == "installed"
    assert text == "Lazarus is installed — this is the installed copy"

    # A portable copy, and an installed copy exists to remove.
    target.parent.mkdir(parents=True)
    target.write_bytes(b"program")
    monkeypatch.setattr(mod, "current_exe", lambda: portable)
    assert window.install_button_state() == (
        True, "Remove the installed copy…", "uninstall",
    )


def test_install_button_flows_elevate_and_say_what_actually_happens(
    window, qapp, tmp_path, monkeypatch
):
    _drain(qapp, window)
    mod = app_mod.installer
    target = tmp_path / "PF" / "Sentinel Safeguard" / "Lazarus" / "Lazarus.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"program")
    portable = tmp_path / "Downloads" / "Lazarus.exe"
    monkeypatch.setattr(mod, "is_frozen", lambda: True)
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "installed_exe", lambda *a, **k: target)
    monkeypatch.setattr(mod, "current_exe", lambda: portable)
    elevated = []
    infos = []

    class FakeBox:
        @staticmethod
        def information(*args):
            infos.append(args)

    monkeypatch.setattr(app_mod, "QMessageBox", FakeBox)
    monkeypatch.setattr(
        app_mod.ConfirmDialog,
        "exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        mod, "elevate_and_run", lambda mode: elevated.append(mode) or True
    )

    # Remove flow.
    monkeypatch.setattr(mod, "needs_install_offer", lambda *a, **k: False)
    window.refresh_install_button()
    window.install_button.click()
    qapp.processEvents()
    assert elevated == ["uninstall"]
    assert infos and "administrator's OK" in infos[-1][2]

    # Install flow, with the corrected follow-up wording.
    monkeypatch.setattr(mod, "needs_install_offer", lambda *a, **k: True)
    window.refresh_install_button()
    window.install_button.click()
    qapp.processEvents()
    assert elevated == ["uninstall", "install"]
    follow_up = infos[-1][2]
    assert "Windows is asking for an administrator's OK" in follow_up
    assert "Lazarus is in the Start Menu" in follow_up
    assert "this window can be closed whenever" in follow_up
    assert "will open when the install finishes" not in follow_up

    # The installed copy explains itself, and elevates nothing.
    monkeypatch.setattr(mod, "needs_install_offer", lambda *a, **k: False)
    monkeypatch.setattr(mod, "current_exe", lambda: target)
    window.refresh_install_button()
    window.install_button.click()
    qapp.processEvents()
    assert elevated == ["uninstall", "install"]
    assert "run the portable copy" in infos[-1][2]
    assert "Remove the installed copy" in infos[-1][2]


# -- F-17: close during the startup refresh (grace drain) ---------------------


def test_close_during_startup_refresh_drains_without_a_dialog(
    qapp, tmp_path, monkeypatch
):
    """Closing while the startup volume refresh is still running.

    Pre-fix this test hangs at the modal dialog: building the
    window starts the Locked page's initial volume-refresh
    worker, and close() with no event loop running opened the
    "A job is still running" ConfirmDialog, with no one there
    to answer it (the frozen self-check's hang, F-17).
    """
    import threading

    release = threading.Event()
    dialog_calls = []

    def counting_exec(self):
        dialog_calls.append(1)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", counting_exec)

    class BlockingPlatform(_FakePlatform):
        def list_volumes(self):
            release.wait(10)
            return super().list_volumes()

    platform = BlockingPlatform([_volume("E:", True)])
    controller = Controller(
        config_path=tmp_path / "cfg" / "backup.json", platform=platform
    )
    win = MainWindow(controller)
    try:
        win.show()
        qapp.processEvents()
        # Construction started the startup refresh, and it is
        # still blocked inside list_volumes.
        assert win.jobs_running() is True
        # Release the refresh ~0.3s from now, from a timer that
        # can only fire if the close's grace pump runs the event
        # loop — then close immediately, without releasing first.
        QTimer.singleShot(300, release.set)
        win.close()
        # close() returned, no dialog was shown, and the window
        # actually closed once the refresh drained.
        assert dialog_calls == []
        assert win.jobs_running() is False
        assert not win.isVisible()
    finally:
        release.set()
        qapp.processEvents()


def test_close_with_a_long_job_still_gets_the_dialog_after_the_grace(
    window, qapp, monkeypatch
):
    import threading

    _drain(qapp, window)
    release = threading.Event()
    dialog_calls = []

    def counting_exec(self):
        dialog_calls.append(1)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", counting_exec)
    try:
        worker = window.run_worker(
            lambda: release.wait(10),
            [window.scan_button],
            lambda result: None,
            window.deleted_panel,
        )
        assert worker is not None
        started = time.monotonic()
        window.close()
        elapsed = time.monotonic() - started
        qapp.processEvents()
        # The worker outlasted the grace, so the dialog happened
        # exactly once — and only after roughly the grace, not
        # instantly (loose bound on purpose: no tight timing).
        assert dialog_calls == [1]
        assert elapsed >= 1.5
        assert window.isVisible()
        assert window._close_when_idle is False
    finally:
        release.set()
    _drain(qapp, window)
    assert window.isVisible()


def test_failure_during_close_grace_stays_open_to_be_read(
    window, qapp, monkeypatch
):
    """N3-01: a job failing mid-grace must not be buried by the close.

    The worker is blocked, then raises ~0.4s after the close
    starts — inside the 2.0s grace pump, so its failure handler
    runs during the pump, populates the panel, and drains the
    worker list. Pre-fix, the pump's drained branch then closed
    the window in the same closeEvent call and the failure
    report was silently lost. Consent to close was given before
    this failure existed, so it is not a waiver of its report
    (the R-03 principle, inside the grace).
    """
    _drain(qapp, window)
    dialog_calls = []

    def counting_exec(self):
        dialog_calls.append(1)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(app_mod.ConfirmDialog, "exec", counting_exec)

    def blocked_then_failing():
        time.sleep(0.4)
        raise LazarusError("mid-grace failure marker")

    worker = window.run_worker(
        blocked_then_failing,
        [window.scan_button],
        lambda result: None,
        window.deleted_panel,
    )
    assert worker is not None
    window.close()
    qapp.processEvents()
    # close() returned, but the window stayed open on the
    # failure: no dialog, the workers drained, the failure text
    # is in the panel, and the controls are as the failure
    # handler left them.
    assert dialog_calls == []
    assert window.isVisible()
    assert window.jobs_running() is False
    assert window.deleted_panel.title.text() == "Lazarus stopped"
    assert "mid-grace failure marker" in window.deleted_panel.body.text()
    assert window.scan_button.isEnabled()
    # The grace-failure flag was consumed by that close attempt:
    # a second close, with no jobs running, closes normally.
    assert window._grace_saw_failure is False
    window.close()
    qapp.processEvents()
    assert not window.isVisible()


def test_worker_drops_its_callable_after_running(qapp):
    """Sweep #3: a finished worker must not keep its callable —
    and whatever the callable closed over — alive. The unlock
    job's callable closes over the owner's recovery key; the
    worker stays a child of the window after the job, so the
    closure must not stay with it."""
    from lazarus.ui.app import Worker

    seen = {}
    worker = Worker(lambda: "done")
    worker.succeeded.connect(lambda result: seen.update(result=result))
    worker.start()
    assert worker.wait(10000)
    qapp.processEvents()
    assert seen.get("result") == "done"
    assert worker._fn is None
