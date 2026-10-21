"""The Lazarus window: an animated HUD over the finished engine.

Five pages, in the order a person in trouble needs them — restore
first. Every button does the thing it says, through
:class:`~lazarus.ui.controller.Controller`; every long job runs on
a worker thread with progress taken from the engine's own records;
every failure lands in a designed message panel carrying the
engine's plain words and one clear next step. No raw exception text
ever reaches the window.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .. import installer
from .._version import __version__
from ..errors import LazarusError
from . import theme
from .controller import Controller, display_error

try:
    from PySide6.QtCore import QCoreApplication, Qt, QThread, QTime, QTimer, Signal
    from PySide6.QtGui import QColor, QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLayout,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSpinBox,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QTimeEdit,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    _QT_ERROR = None
except ImportError as error:  # pragma: no cover - exercised by hand
    _QT_ERROR = error

APOLOGY = (
    "Something went wrong inside Lazarus. Nothing was changed by "
    "this step. Try again, and if it keeps happening, restart Lazarus."
)

if _QT_ERROR is None:

    # ------------------------------------------------------------------
    # Small shared pieces

    class Worker(QThread):
        """Run one callable off the window thread; report plainly."""

        succeeded = Signal(object)
        failed = Signal(str)

        def __init__(self, fn, parent=None):
            super().__init__(parent)
            self._fn = fn

        def run(self) -> None:
            # Take the callable, then drop the worker's own
            # reference to it before running. A finished worker
            # stays a child of the window for the window's whole
            # life, and one job's callable closes over the owner's
            # recovery key (the unlock job's key provider): the
            # engine drops the key the moment the attempt ends,
            # and the key must not outlive it here either, kept
            # reachable by a worker nobody looks at again.
            fn, self._fn = self._fn, None
            try:
                result = fn()
            except LazarusError as error:
                self.failed.emit(str(error))
            except Exception:  # the window never shows a traceback
                self.failed.emit(APOLOGY)
            else:
                self.succeeded.emit(result)

    class MessagePanel(QFrame):
        """The one designed place good news and problems are said."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("messagePanel")
            layout = QVBoxLayout(self)
            self.title = QLabel("")
            self.body = QLabel("")
            self.body.setWordWrap(True)
            layout.addWidget(self.title)
            layout.addWidget(self.body)
            self.hide()

        def show_info(self, title: str, body: str = "") -> None:
            self.title.setObjectName("infoTitle")
            self.title.setText(title)
            self.body.setText(body)
            self._repolish(self.title)
            self.show()

        def show_problem(self, title: str, body: str = "") -> None:
            self.title.setObjectName("problemTitle")
            self.title.setText(title)
            self.body.setText(body)
            self._repolish(self.title)
            self.show()

        def show_lines(self, title: str, lines, problem: bool = False) -> None:
            if problem:
                self.show_problem(title, "\n".join(lines))
            else:
                self.show_info(title, "\n".join(lines))

        def clear(self) -> None:
            self.hide()

        @staticmethod
        def _repolish(widget) -> None:
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)

    class ReportPanel(QFrame):
        """A finished job's report, in the engine's own lines."""

        def __init__(self, heading: str, parent=None):
            super().__init__(parent)
            self.setObjectName("panel")
            layout = QVBoxLayout(self)
            self.title = QLabel(heading)
            self.title.setObjectName("sectionTitle")
            self.text = QTextEdit()
            self.text.setReadOnly(True)
            layout.addWidget(self.title)
            layout.addWidget(self.text)
            self.hide()

        def show_lines(self, lines) -> None:
            self.text.setPlainText("\n".join(lines))
            self.show()

        def show_text(self, text: str) -> None:
            self.text.setPlainText(text)
            self.show()

    def _label(text: str, object_name: str = "") -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        if object_name:
            label.setObjectName(object_name)
        return label

    def _button(text: str, slot=None, primary: bool = False, danger: bool = False):
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
        if danger:
            button.setObjectName("danger")
        if slot is not None:
            button.clicked.connect(slot)
        return button

    def _path_row(edit: QLineEdit, button_text: str, slot):
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        row.addWidget(_button(button_text, slot))
        return row

    # ------------------------------------------------------------------
    # Dialogs

    class RestoreConfirmDialog(QDialog):
        """The moment before a restore: what, from when, to where."""

        def __init__(self, date_text, label, items_text, destination, parent=None):
            super().__init__(parent)
            self.setMinimumWidth(540)
            self.setWindowTitle("Restore these files?")
            layout = QVBoxLayout(self)
            # Every Lazarus dialog sizes itself to its contents
            # (here and in the dialogs below): a minimum width on
            # its own can squeeze a word-wrapping label into less
            # height than its text needs, and not every platform
            # grows the dialog afterwards to compensate — the text
            # then overflows its label. Fixed size = the size the
            # contents ask for, everywhere.
            layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
            heading = f"Restore the snapshot taken {date_text}"
            if label:
                heading += f" (“{label}”)"
            layout.addWidget(_label(heading, "pageTitle"))
            layout.addWidget(_label(f"Files: {items_text}"))
            layout.addWidget(_label(f"Restore to: {destination}"))
            layout.addWidget(
                _label(
                    "Before anything is overwritten, Lazarus saves a "
                    "safety copy of how things are now, so this restore "
                    "can itself be undone. If that safety copy cannot "
                    "be saved in full, nothing is changed."
                )
            )
            buttons = QHBoxLayout()
            self.restore_button = _button("Restore", primary=True)
            self.restore_button.clicked.connect(self.accept)
            cancel = _button("Not yet")
            cancel.clicked.connect(self.reject)
            buttons.addWidget(self.restore_button)
            buttons.addWidget(cancel)
            layout.addLayout(buttons)

    class KeyDialog(QDialog):
        """Ask for a recovery key once; keep it nowhere."""

        def __init__(self, ref: str, parent=None):
            super().__init__(parent)
            self.setMinimumWidth(440)
            self.setWindowTitle("Recovery key")
            layout = QVBoxLayout(self)
            # Size to contents — see RestoreConfirmDialog.
            layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
            layout.addWidget(_label(f"Type the recovery key for {ref}.", "pageTitle"))
            layout.addWidget(
                _label(
                    "The key is tried once and never kept: it is not "
                    "saved, not written down, and not shown again."
                )
            )
            self.key_edit = QLineEdit()
            self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.key_edit.setPlaceholderText("Recovery key")
            layout.addWidget(self.key_edit)
            buttons = QHBoxLayout()
            self.ok_button = _button("Try this key", primary=True)
            self.ok_button.clicked.connect(self.accept)
            cancel = _button("Cancel")
            cancel.clicked.connect(self.reject)
            buttons.addWidget(self.ok_button)
            buttons.addWidget(cancel)
            layout.addLayout(buttons)

        def take_key(self) -> str:
            """Read the key once and clear the field immediately."""
            key = self.key_edit.text()
            self.key_edit.clear()
            return key

        read_key_once = take_key

    class ConfirmDialog(QDialog):
        """A final, plain confirmation before a destructive action."""

        def __init__(self, title, body, confirm_text, parent=None, danger=True):
            super().__init__(parent)
            self.setMinimumWidth(540)
            self.setWindowTitle(title)
            layout = QVBoxLayout(self)
            # Size to contents — see RestoreConfirmDialog.
            layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
            layout.addWidget(_label(title, "pageTitle"))
            layout.addWidget(_label(body))
            buttons = QHBoxLayout()
            self.confirm_button = _button(confirm_text, danger=danger, primary=not danger)
            self.confirm_button.clicked.connect(self.accept)
            cancel = _button("Not yet")
            cancel.clicked.connect(self.reject)
            buttons.addWidget(self.confirm_button)
            buttons.addWidget(cancel)
            layout.addLayout(buttons)

    # ------------------------------------------------------------------
    # The window itself

    class MainWindow(QMainWindow):
        PAGE_NAMES = ["Restore", "Snapshots", "Deleted files", "Locked files", "Backup"]

        # How long closeEvent waits, pumping the event loop, for
        # active workers to finish on their own before it asks
        # about them. Long enough for the startup volume refresh
        # and other quick reads to drain; far too short for a
        # real job (restore, rescue, decrypt, backup) to finish,
        # so those still get the confirmation dialog.
        _CLOSE_GRACE_SECONDS = 2.0

        def __init__(self, controller: Controller | None = None, parent=None):
            super().__init__(parent)
            self.controller = controller or Controller()
            self.setWindowTitle(f"LAZARUS.EXE {__version__}")
            self.resize(1060, 760)
            self._workers: list[Worker] = []
            self._manifest = None  # browse data for the chosen snapshot
            self._scan_result = None  # last deleted-files scan view data
            self._identification = None
            self._identification_folder: str | None = None
            self._decrypt_entry = None
            self._decrypt_plan = None  # last family plan, for its folder
            self._decrypt_plan_folder: str | None = None
            self._volumes: list[dict] = []
            self._backup_prefilled = False
            self._prune_view = None
            self._close_when_idle = False
            self._close_grace_active = False
            self._grace_saw_failure = False
            self._job_seq = 0
            self._latest_jobs: dict = {}
            self._install_action: str | None = None

            root = QWidget()
            root.setObjectName("root")
            self.setCentralWidget(root)
            outer = QVBoxLayout(root)

            header = QHBoxLayout()
            title = QLabel("LAZARUS.EXE")
            title.setObjectName("appTitle")
            header.addWidget(title)
            header.addWidget(_label(f"Version {__version__}", "faint"))
            self.store_label = _label("No store is open.", "faint")
            header.addWidget(self.store_label, 1)
            self.install_button = _button("", self._install_button_clicked)
            self.install_button.hide()
            header.addWidget(self.install_button)
            self.working_label = _label("", "faint")
            header.addWidget(self.working_label)
            outer.addLayout(header)
            outer.addWidget(
                _label("Dead isn't gone. Bring files back, restored to the day you choose.", "faint")
            )

            body = QHBoxLayout()
            self.sidebar = QListWidget()
            self.sidebar.setObjectName("sidebar")
            self.sidebar.addItems(self.PAGE_NAMES)
            self.sidebar.setMaximumWidth(190)
            body.addWidget(self.sidebar)
            self.stack = QStackedWidget()
            body.addWidget(self.stack, 1)
            outer.addLayout(body, 1)

            # Timers exist before any page is built: building the
            # locked page already starts a (worker) volume refresh,
            # and run_worker reaches for the shimmer timer.
            self._shimmer = QTimer(self)
            self._shimmer.setInterval(350)
            self._shimmer.timeout.connect(self._tick_shimmer)
            self._shimmer_phase = 0
            self._rescue_timer = QTimer(self)
            self._rescue_timer.setInterval(400)
            self._rescue_timer.timeout.connect(self._poll_rescue)
            self._rescue_map_path: str | None = None

            self.restore_page = self._build_restore_page()
            self.snapshots_page = self._build_snapshots_page()
            self.deleted_page = self._build_deleted_page()
            self.locked_page = self._build_locked_page()
            self.backup_page = self._build_backup_page()
            for page in (
                self.restore_page,
                self.snapshots_page,
                self.deleted_page,
                self.locked_page,
                self.backup_page,
            ):
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setWidget(page)
                self.stack.addWidget(scroll)
            self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
            self.sidebar.setCurrentRow(0)
            for index in range(5):
                shortcut = QShortcut(
                    QKeySequence(f"Ctrl+{index + 1}"), self
                )
                shortcut.activated.connect(
                    lambda i=index: self.sidebar.setCurrentRow(i)
                )

            self.refresh_install_button()
            self.refresh_backup()

        # -- shared helpers ---------------------------------------------

        def _page(self, title_text: str):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addWidget(_label(title_text, "pageTitle"))
            return page, layout

        def _tick_shimmer(self) -> None:
            if self._close_when_idle:
                # While a close is waiting on the job, the label
                # keeps saying so — the plain "Working…" must not
                # overwrite the one fact the owner is waiting on.
                self.working_label.setText(
                    "Working — Lazarus will close when the job finishes."
                )
                return
            self._shimmer_phase = (self._shimmer_phase + 1) % 4
            self.working_label.setText("Working" + "." * (self._shimmer_phase + 1))

        def run_worker(self, fn, controls, on_result, panel: MessagePanel,
                       action=None, on_failure=None):
            """The one funnel every long job starts through.

            One job at a time, whoever asks — a button, an Enter
            key, another page. A second ask while a job is running
            starts nothing and says so in the asking page's own
            message panel. Each job carries a sequence number, and
            a result is applied only while its job is still the
            latest for that action.
            """
            if self._workers:
                panel.show_problem(
                    "Lazarus is already working",
                    "Lazarus is already working on something — let it finish first.",
                )
                return None
            self._job_seq += 1
            seq = self._job_seq
            job_key = action if action is not None else f"worker-{seq}"
            self._latest_jobs[job_key] = seq
            for control in controls:
                control.setEnabled(False)
            self.working_label.setText("Working.")
            self._shimmer.start()
            worker = Worker(fn, self)

            def done(result):
                self._finish_worker(worker, controls)
                if self._latest_jobs.get(job_key) != seq:
                    return  # a newer job for this action owns the page now
                on_result(result)

            def failed(message):
                if self._close_grace_active:
                    # A failure that lands while a close is in
                    # its grace pump must not be buried by that
                    # close: consent to close was given before
                    # this failure existed, so it is not a
                    # waiver of its report (the R-03 principle).
                    # closeEvent checks this flag when the pump
                    # ends and keeps the window open instead.
                    self._grace_saw_failure = True
                if self._close_when_idle:
                    # A job that fails after "finish, then close"
                    # keeps its report: the pending close is
                    # dropped, the window is handed back, and the
                    # failure below stays up to be read. The close
                    # must be dropped BEFORE _finish_worker runs,
                    # or it would schedule the close this failure
                    # has just cancelled.
                    self._drop_pending_close()
                self._finish_worker(worker, controls)
                if self._latest_jobs.get(job_key) != seq:
                    return
                if on_failure is not None:
                    on_failure(message)
                else:
                    panel.show_problem("Lazarus stopped", message)

            worker.succeeded.connect(done)
            worker.failed.connect(failed)
            self._workers.append(worker)
            worker.start()
            return worker

        def _finish_worker(self, worker, controls) -> None:
            for control in controls:
                control.setEnabled(True)
            if worker in self._workers:
                self._workers.remove(worker)
            if not self._workers:
                self._shimmer.stop()
                self.working_label.setText("")
                if self._close_when_idle:
                    QTimer.singleShot(0, self._close_if_idle)

        def _drop_pending_close(self) -> None:
            """Give the window back after a close was called off.

            Clears the close-when-idle flag and re-enables what
            the pending close disabled — the pages, the sidebar,
            and the header install button.
            """
            self._close_when_idle = False
            self.stack.setEnabled(True)
            self.sidebar.setEnabled(True)
            self.install_button.setEnabled(True)

        def _close_if_idle(self) -> None:
            if self._close_when_idle and not self._workers:
                self._close_when_idle = False
                self.close()

        def jobs_running(self) -> bool:
            """True while any funnel worker is active.

            The same state closeEvent consults. The launcher's
            self-check uses this to drain startup work before it
            closes the window it built.
            """
            return bool(self._workers)

        def closeEvent(self, event) -> None:
            """Never destroy a running worker thread.

            Closing while a job runs asks first. Saying yes lets
            the job finish — Lazarus writes its safety records
            as it goes (safety copies, the rescue progress file,
            the backup history), so waiting loses nothing, and
            there is no safe instant-stop inside a single job —
            and the window closes for real when the job is done.

            Before asking, active workers get a short grace to
            finish on their own: the startup volume refresh and
            other quick reads are real workers, but they are not
            jobs a person should be interrogated about, and a
            self-check that closes its window without running an
            event loop must not be left facing a dialog no one
            can answer. Genuinely long jobs outlast the grace
            and get the dialog exactly as before.
            """
            if self._close_when_idle:
                # A close is already waiting on the job: a second
                # attempt changes nothing and asks nothing again.
                event.ignore()
                return
            if self._close_grace_active:
                # A close is already inside its grace pump below:
                # a re-entrant attempt changes nothing either.
                event.ignore()
                return
            if self.jobs_running():
                # Grace drain: pump the event loop for up to
                # _CLOSE_GRACE_SECONDS so a worker's finished
                # signal can land, re-checking as we go. The
                # bound is the bound — if a finished signal can
                # never be delivered, this still ends and the
                # dialog flow below decides.
                self._close_grace_active = True
                self._grace_saw_failure = False
                try:
                    deadline = time.monotonic() + self._CLOSE_GRACE_SECONDS
                    while self.jobs_running() and time.monotonic() < deadline:
                        QCoreApplication.processEvents()
                        if not self.jobs_running():
                            break
                        time.sleep(0.02)
                finally:
                    self._close_grace_active = False
                if self._grace_saw_failure:
                    # A job failed during the grace: its handler
                    # has already put the failure in its panel
                    # and handed the controls back. Keep the
                    # window open so it can be read, whether or
                    # not the workers have all drained.
                    self._grace_saw_failure = False
                    event.ignore()
                    return
                if not self.jobs_running():
                    super().closeEvent(event)
                    return
            if self._workers:
                dialog = ConfirmDialog(
                    "A job is still running",
                    (
                        "A job is still running. It will finish "
                        "first, and Lazarus will close when it is "
                        "done. If the job fails, Lazarus stays open "
                        "so you can read what happened. Lazarus "
                        "writes its safety records as it goes — "
                        "safety copies, the rescue progress file, "
                        "and the backup history — so waiting loses "
                        "nothing. There is no safe way to stop a "
                        "single job in the middle, so Lazarus does "
                        "not offer one."
                    ),
                    "Finish the job, then close",
                    self,
                    danger=False,
                )
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    self._close_when_idle = True
                    self.stack.setEnabled(False)
                    self.sidebar.setEnabled(False)
                    self.install_button.setEnabled(False)
                    self.working_label.setText(
                        "Working — Lazarus will close when the job finishes."
                    )
                    if not self._shimmer.isActive():
                        self._shimmer.start()
                event.ignore()
                return
            super().closeEvent(event)

        def showEvent(self, event) -> None:
            super().showEvent(event)
            self.refresh_install_button()

        # -- the install / remove header button ----------------------

        def install_button_state(self):
            """(visible, text, action) for the header install button.

            One contextual button, driven entirely by the
            installer's own account of the world: hidden when this
            is not the shipped program file on a Windows computer;
            an install offer when the installer says one is due;
            a plain statement when this copy IS the installed one;
            a remove choice when an installed copy exists and this
            is a portable copy.
            """
            try:
                if installer is None or not installer.is_frozen():
                    return (False, "", None)
                if not installer._is_windows():
                    return (False, "", None)
                if installer.needs_install_offer():
                    return (True, "Install Lazarus on this computer…", "install")
                running = installer.current_exe()
                target = installer.installed_exe()
                if installer._same_path(running, target):
                    return (
                        True,
                        "Lazarus is installed — this is the installed copy",
                        "installed",
                    )
                if target.exists():
                    return (True, "Remove the installed copy…", "uninstall")
            except Exception:
                return (False, "", None)
            return (False, "", None)

        def refresh_install_button(self) -> None:
            visible, text, action = self.install_button_state()
            self._install_action = action
            if visible:
                self.install_button.setText(text)
                self.install_button.show()
            else:
                self.install_button.hide()

        def _install_button_clicked(self) -> None:
            _visible, _text, action = self.install_button_state()
            self.refresh_install_button()
            if action == "install":
                dialog = ConfirmDialog(
                    "Install Lazarus?",
                    (
                        "Lazarus can install itself on this "
                        "computer: it copies Lazarus into Program "
                        "Files and adds it to the Start Menu. "
                        "Windows will ask for your approval once."
                    ),
                    "Install Lazarus",
                    self,
                    danger=False,
                )
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                started = installer.elevate_and_run("install")
                if started:
                    QMessageBox.information(
                        self,
                        "Installing Lazarus",
                        "Windows is asking for an administrator's OK. "
                        "When the install report says it is done, "
                        "Lazarus is in the Start Menu, and this window "
                        "can be closed whenever. This window keeps "
                        "running the copy you opened.",
                    )
                else:
                    QMessageBox.information(
                        self,
                        "Installing Lazarus",
                        "The install could not be started. This copy "
                        "of Lazarus will keep running, and you can try "
                        "installing again another time.",
                    )
            elif action == "installed":
                QMessageBox.information(
                    self,
                    "Lazarus is installed",
                    "This copy is the installed one. To remove it, "
                    "run the portable copy and choose “Remove the "
                    "installed copy” there.",
                )
            elif action == "uninstall":
                dialog = ConfirmDialog(
                    "Remove the installed copy?",
                    (
                        "Windows will ask for an administrator's OK, "
                        "and the installed copy of Lazarus will be "
                        "removed. This portable copy is not removed, "
                        "and neither are any files Lazarus has saved "
                        "for you."
                    ),
                    "Remove the installed copy",
                    self,
                    danger=True,
                )
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                started = installer.elevate_and_run("uninstall")
                if started:
                    QMessageBox.information(
                        self,
                        "Removing Lazarus",
                        "Windows is asking for an administrator's OK. "
                        "When the removal report says it is done, the "
                        "installed copy will be gone. This window keeps "
                        "running the portable copy you opened, and it "
                        "can be closed whenever.",
                    )
                else:
                    QMessageBox.information(
                        self,
                        "Removing Lazarus",
                        "The removal could not be started. Nothing "
                        "has been removed, and you can try again "
                        "another time.",
                    )

        def _update_store_label(self) -> None:
            path = self.controller.store_path
            self.store_label.setText(
                f"Store: {path}" if path else "No store is open."
            )

        def refresh_snapshots(self) -> None:
            if self.controller.store_path:
                self.restore_empty.hide()
            try:
                views = self.controller.list_snapshots()
            except LazarusError:
                views = []
            for widget in (self.snapshot_list, self.snap_list_page):
                current = (
                    widget.currentItem().data(Qt.ItemDataRole.UserRole)
                    if widget.currentItem()
                    else None
                )
                widget.blockSignals(True)
                widget.clear()
                for view in views:
                    item = QListWidgetItem(view["row_text"])
                    item.setData(Qt.ItemDataRole.UserRole, view["id"])
                    widget.addItem(item)
                    if view["id"] == current:
                        widget.setCurrentItem(item)
                widget.blockSignals(False)
            self._update_store_label()

        def _pick_folder(self, edit: QLineEdit) -> None:
            chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text())
            if chosen:
                edit.setText(chosen)

        def _pick_file(self, edit: QLineEdit) -> None:
            chosen, _ = QFileDialog.getOpenFileName(self, "Choose a file", edit.text())
            if chosen:
                edit.setText(chosen)

        def _pick_save_file(self, edit: QLineEdit) -> None:
            chosen, _ = QFileDialog.getSaveFileName(self, "Choose where it goes", edit.text())
            if chosen:
                edit.setText(chosen)

        # -- (a) Restore ---------------------------------------------------

        def _build_restore_page(self):
            page, layout = self._page("Restore — bring files back")
            self.restore_panel = MessagePanel()
            bar = QFrame()
            bar.setObjectName("panel")
            bar_layout = QVBoxLayout(bar)
            bar_layout.addWidget(_label("Your snapshot store", "sectionTitle"))
            self.store_path_edit = QLineEdit()
            self.store_path_edit.setPlaceholderText("Folder that holds the store")
            bar_layout.addLayout(
                _path_row(self.store_path_edit, "Choose folder", lambda: self._pick_folder(self.store_path_edit))
            )
            buttons = QHBoxLayout()
            self.open_store_button = _button("Open store", self._open_store)
            self.create_store_button = _button("Make a new store", self._create_store)
            buttons.addWidget(self.open_store_button)
            buttons.addWidget(self.create_store_button)
            buttons.addStretch(1)
            bar_layout.addLayout(buttons)
            layout.addWidget(bar)
            layout.addWidget(self.restore_panel)

            self.restore_empty = _label(
                "No store is open yet. Open the store your snapshots live in, "
                "or make a new one, and what it holds will appear here."
            )
            layout.addWidget(self.restore_empty)
            layout.addWidget(_label("Snapshots — newest at the bottom", "sectionTitle"))
            self.snapshot_list = QListWidget()
            self.snapshot_list.currentItemChanged.connect(self._snapshot_chosen)
            layout.addWidget(self.snapshot_list, 1)

            layout.addWidget(_label("What is in this snapshot — tick what comes back", "sectionTitle"))
            self.browse_tree = QTreeWidget()
            self.browse_tree.setHeaderLabels(["Item", "Kind", "Size"])
            header = self.browse_tree.header()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self.browse_tree.itemChanged.connect(self._tree_item_changed)
            layout.addWidget(self.browse_tree, 1)

            self.restore_dest_edit = QLineEdit()
            self.restore_dest_edit.setPlaceholderText("Folder to restore into")
            layout.addWidget(_label("Restore to", "sectionTitle"))
            layout.addLayout(
                _path_row(self.restore_dest_edit, "Choose folder", lambda: self._pick_folder(self.restore_dest_edit))
            )
            actions = QHBoxLayout()
            self.restore_button = _button("Restore…", self._confirm_restore, primary=True)
            self.verify_button = _button("Verify this store", self._verify_store)
            actions.addWidget(self.restore_button)
            actions.addWidget(self.verify_button)
            actions.addStretch(1)
            layout.addLayout(actions)
            self.restore_report = ReportPanel("Restore report")
            layout.addWidget(self.restore_report)
            self.store_path_edit.returnPressed.connect(self._open_store)
            return page

        def _open_store(self) -> None:
            path = self.store_path_edit.text().strip()
            if not path:
                self.restore_panel.show_problem(
                    "No folder was named",
                    "Type or choose the folder that holds the store, then open it.",
                )
                return
            try:
                result = self.controller.open_store(path)
            except LazarusError as error:
                self.restore_panel.show_problem("The store could not be opened", str(error))
                return
            self.restore_panel.show_info("Store open", result["message"])
            self.restore_empty.hide()
            self.refresh_snapshots()

        def _create_store(self) -> None:
            path = self.store_path_edit.text().strip()
            if not path:
                self.restore_panel.show_problem(
                    "No folder was named",
                    "Type or choose an empty folder for the new store, then make it.",
                )
                return
            try:
                result = self.controller.create_store(path)
            except LazarusError as error:
                self.restore_panel.show_problem("The store could not be made", str(error))
                return
            self.restore_panel.show_info("Store made", result["message"])
            self.restore_empty.hide()
            self.refresh_snapshots()

        def _snapshot_chosen(self, current, _previous=None) -> None:
            if current is None:
                return
            snapshot_id = current.data(Qt.ItemDataRole.UserRole)
            try:
                manifest = self.controller.browse_snapshot(snapshot_id)
            except LazarusError as error:
                self.restore_panel.show_problem("That snapshot could not be read", str(error))
                return
            self._manifest = manifest
            self._fill_browse_tree(manifest)
            if manifest["source_root"]:
                self.restore_dest_edit.setText(manifest["source_root"])
            if manifest["problems"]:
                self.restore_panel.show_problem(
                    "This snapshot is INCOMPLETE",
                    "\n".join(
                        f"{p['path']}: {p['message']}" if p["path"] else p["message"]
                        for p in manifest["problems"]
                    ),
                )

        def _fill_browse_tree(self, manifest) -> None:
            self.browse_tree.blockSignals(True)
            self.browse_tree.clear()
            nodes: dict[str, QTreeWidgetItem] = {}

            def node_for(path: str) -> QTreeWidgetItem:
                if path in nodes:
                    return nodes[path]
                parent_path, _, name = path.rpartition("/")
                item = QTreeWidgetItem([name or path, "", ""])
                item.setData(0, Qt.ItemDataRole.UserRole, path)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked)
                nodes[path] = item
                if parent_path:
                    node_for(parent_path).addChild(item)
                else:
                    self.browse_tree.addTopLevelItem(item)
                return item

            for entry in manifest["entries"]:
                item = node_for(entry["path"])
                kind = {"file": "file", "dir": "folder", "symlink": "shortcut"}.get(
                    entry["type"], entry["type"]
                )
                item.setText(1, kind)
                item.setText(2, entry["size_text"])
            self.browse_tree.expandAll()
            self.browse_tree.blockSignals(False)

        def _tree_item_changed(self, item, _column) -> None:
            state = item.checkState(0)
            self.browse_tree.blockSignals(True)
            stack = [item.child(i) for i in range(item.childCount())]
            while stack:
                child = stack.pop()
                child.setCheckState(0, state)
                stack.extend(child.child(i) for i in range(child.childCount()))
            parent = item.parent()
            while parent is not None:
                states = {
                    parent.child(i).checkState(0) for i in range(parent.childCount())
                }
                if states == {Qt.CheckState.Checked}:
                    parent.setCheckState(0, Qt.CheckState.Checked)
                elif states == {Qt.CheckState.Unchecked}:
                    parent.setCheckState(0, Qt.CheckState.Unchecked)
                else:
                    parent.setCheckState(0, Qt.CheckState.PartiallyChecked)
                parent = parent.parent()
            self.browse_tree.blockSignals(False)

        def _tree_selection(self):
            chosen: list[str] = []

            def walk(item):
                if item.checkState(0) == Qt.CheckState.Checked:
                    chosen.append(item.data(0, Qt.ItemDataRole.UserRole))
                    return
                for i in range(item.childCount()):
                    walk(item.child(i))

            for i in range(self.browse_tree.topLevelItemCount()):
                walk(self.browse_tree.topLevelItem(i))
            return chosen

        def _confirm_restore(self) -> None:
            if self._manifest is None:
                self.restore_panel.show_problem(
                    "No snapshot is chosen",
                    "Open a store and pick a snapshot from the list, then restore.",
                )
                return
            selected = self._tree_selection()
            total = len(self._manifest["entries"])
            if not selected:
                self.restore_panel.show_problem(
                    "Nothing is ticked",
                    "Tick at least one file or folder in the snapshot, then restore.",
                )
                return
            destination = self.restore_dest_edit.text().strip()
            if not destination:
                self.restore_panel.show_problem(
                    "No destination was named",
                    "Say which folder the files should come back into.",
                )
                return
            expanded = list(selected) + self._descendants(selected)
            is_everything = (
                self.controller.normalise_selection(
                    self._manifest["entries"], expanded
                )
                is None
            )
            items_text = (
                "everything in the snapshot"
                if is_everything
                else f"{len(selected)} chosen item(s)"
            )
            dialog = RestoreConfirmDialog(
                self._manifest["date_text"],
                self._manifest["label"],
                items_text,
                destination,
                self,
            )
            self.last_restore_dialog = dialog
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._do_restore(selected, destination)

        def _gather_all_paths(self):
            paths = []

            def walk(item):
                paths.append(item.data(0, Qt.ItemDataRole.UserRole))
                for i in range(item.childCount()):
                    walk(item.child(i))

            for i in range(self.browse_tree.topLevelItemCount()):
                walk(self.browse_tree.topLevelItem(i))
            return paths

        def _descendants(self, roots):
            all_paths = self._gather_all_paths()
            return [
                p for p in all_paths
                if any(p != r and p.startswith(r + "/") for r in roots)
            ]

        def _do_restore(self, selected, destination) -> None:
            snapshot_id = self._manifest["id"]
            if selected is None:
                selection = None
            else:
                expanded = list(selected) + self._descendants(selected)
                selection = (
                    None
                    if self.controller.normalise_selection(
                        self._manifest["entries"], expanded
                    )
                    is None
                    else selected
                )

            def on_result(result):
                self.restore_report.show_lines(result["lines"])
                if result["status"] == "complete":
                    self.restore_panel.show_info("Restore finished", "Everything came back.")
                else:
                    self.restore_panel.show_problem(
                        "Restore finished with problems",
                        "\n".join(
                            f"{p['path']}: {p['message']}" for p in result["problems"]
                        ),
                    )
                self.refresh_snapshots()

            self.run_worker(
                lambda: self.controller.restore_selection(snapshot_id, destination, selection),
                [self.restore_button, self.verify_button],
                on_result,
                self.restore_panel,
                action="restore",
            )

        def _verify_store(self) -> None:
            def on_result(result):
                self.restore_report.show_lines(result["lines"])
                if result["ok"]:
                    self.restore_panel.show_info("Store verified", result["lines"][0])
                else:
                    self.restore_panel.show_problem(
                        "The store has problems", "\n".join(result["lines"])
                    )

            self.run_worker(
                self.controller.verify_store_view,
                [self.verify_button, self.restore_button],
                on_result,
                self.restore_panel,
                action="verify",
            )

        # -- (b) Snapshots ---------------------------------------------------

        def _build_snapshots_page(self):
            page, layout = self._page("Snapshots — save how things are")
            self.snap_panel = MessagePanel()
            layout.addWidget(
                _label(
                    "A snapshot saves a file or folder exactly as it is now, "
                    "dated, so any of it can come back later."
                )
            )
            self.snap_source_edit = QLineEdit()
            self.snap_source_edit.setPlaceholderText("File or folder to save")
            layout.addLayout(
                _path_row(self.snap_source_edit, "Choose folder", lambda: self._pick_folder(self.snap_source_edit))
            )
            pick_file_row = QHBoxLayout()
            pick_file_row.addStretch(1)
            pick_file_row.addWidget(
                _button("Choose a file instead", lambda: self._pick_file(self.snap_source_edit))
            )
            layout.addLayout(pick_file_row)
            self.snap_label_edit = QLineEdit()
            self.snap_label_edit.setPlaceholderText("A name for this moment (not required)")
            layout.addWidget(_label("Label", "sectionTitle"))
            layout.addWidget(self.snap_label_edit)
            self.snap_button = _button("Take the snapshot", self._take_snapshot, primary=True)
            layout.addWidget(self.snap_button)
            layout.addWidget(self.snap_panel)
            self.snap_report = ReportPanel("Snapshot result")
            layout.addWidget(self.snap_report)
            layout.addWidget(_label("Snapshots in the open store", "sectionTitle"))
            self.snap_list_page = QListWidget()
            layout.addWidget(self.snap_list_page, 1)
            self.snap_source_edit.returnPressed.connect(self._take_snapshot)
            return page

        def _take_snapshot(self) -> None:
            source = self.snap_source_edit.text().strip()
            if not source:
                self.snap_panel.show_problem(
                    "Nothing was named",
                    "Choose the file or folder to save, then take the snapshot.",
                )
                return
            label = self.snap_label_edit.text()

            def on_result(result):
                self.snap_report.show_lines(result["lines"])
                if result["complete"]:
                    self.snap_panel.show_info("Snapshot saved", result["lines"][0])
                else:
                    self.snap_panel.show_problem(
                        "Snapshot saved, but INCOMPLETE",
                        "\n".join(
                            f"{p['path']}: {p['message']}" for p in result["problems"]
                        ),
                    )
                self.refresh_snapshots()

            self.run_worker(
                lambda: self.controller.take_snapshot(source, label),
                [self.snap_button],
                on_result,
                self.snap_panel,
                action="snapshot",
            )

        # -- (c) Deleted files ------------------------------------------------

        def _build_deleted_page(self):
            page, layout = self._page("Deleted files — what can still come back")
            self.deleted_panel = MessagePanel()
            self.scan_source_edit = QLineEdit()
            self.scan_source_edit.setPlaceholderText("Drive or drive image to look at")
            layout.addLayout(
                _path_row(self.scan_source_edit, "Choose image file", lambda: self._pick_file(self.scan_source_edit))
            )
            self.scan_button = _button("Look for deleted files", self._scan, primary=True)
            layout.addWidget(self.scan_button)
            layout.addWidget(self.deleted_panel)
            self.scan_table = QTableWidget(0, 4)
            self.scan_table.setHorizontalHeaderLabels(
                ["File", "Size", "Odds", "What the evidence says"]
            )
            self.scan_table.setSelectionBehavior(
                QTableWidget.SelectionBehavior.SelectRows
            )
            self.scan_table.horizontalHeader().setSectionResizeMode(
                3, QHeaderView.ResizeMode.Stretch
            )
            self.scan_table.currentCellChanged.connect(self._scan_row_chosen)
            layout.addWidget(self.scan_table, 1)
            self.evidence_text = QTextEdit()
            self.evidence_text.setReadOnly(True)
            self.evidence_text.setPlaceholderText(
                "Pick a file above and the evidence for its odds appears here."
            )
            layout.addWidget(self.evidence_text)
            self.recover_dest_edit = QLineEdit()
            self.recover_dest_edit.setPlaceholderText(
                "Folder on a DIFFERENT drive to bring files back into"
            )
            layout.addWidget(_label("Bring the ticked files back into", "sectionTitle"))
            layout.addLayout(
                _path_row(self.recover_dest_edit, "Choose folder", lambda: self._pick_folder(self.recover_dest_edit))
            )
            self.recover_button = _button(
                "Bring back the selected files", self._recover, primary=True
            )
            layout.addWidget(self.recover_button)
            self.recover_report = ReportPanel("Recovery report")
            layout.addWidget(self.recover_report)

            layout.addWidget(_label("Drive rescue — copy a failing drive to an image", "sectionTitle"))
            layout.addWidget(
                _label(
                    "A rescue copies a dying drive gently, stretch by stretch, "
                    "and writes down what it copied as it goes, so it can stop "
                    "and pick up where it left off."
                )
            )
            self.rescue_source_edit = QLineEdit()
            self.rescue_source_edit.setPlaceholderText("Drive or image to copy")
            layout.addLayout(
                _path_row(self.rescue_source_edit, "Choose file", lambda: self._pick_file(self.rescue_source_edit))
            )
            self.rescue_dest_edit = QLineEdit()
            self.rescue_dest_edit.setPlaceholderText("Image file to write")
            self.rescue_dest_edit.textChanged.connect(self._rescue_dest_changed)
            layout.addLayout(
                _path_row(self.rescue_dest_edit, "Choose place", lambda: self._pick_save_file(self.rescue_dest_edit))
            )
            self.rescue_map_edit = QLineEdit()
            self.rescue_map_edit.setPlaceholderText("Progress file")
            layout.addWidget(_label("Progress file (written as the copy goes)", "faint"))
            layout.addWidget(self.rescue_map_edit)
            self.rescue_button = _button("Start the rescue", self._rescue, primary=True)
            layout.addWidget(self.rescue_button)
            self.rescue_progress = QProgressBar()
            self.rescue_progress.setRange(0, 0)
            self.rescue_progress.hide()
            layout.addWidget(self.rescue_progress)
            self.rescue_progress_label = _label("", "faint")
            layout.addWidget(self.rescue_progress_label)
            self.rescue_report = ReportPanel("Rescue report")
            layout.addWidget(self.rescue_report)
            self.scan_source_edit.returnPressed.connect(self._scan)
            return page

        def _scan(self) -> None:
            source = self.scan_source_edit.text().strip()
            if not source:
                self.deleted_panel.show_problem(
                    "No drive or image was named",
                    "Choose the drive or drive image to look at, then look for deleted files.",
                )
                return

            def on_result(result):
                self._scan_result = result
                self._fill_scan_table(result)
                if result["files"]:
                    self.deleted_panel.show_info(
                        "Scan finished",
                        f"{len(result['files'])} deleted file(s) found: "
                        f"{result['strong_count']} likely whole, "
                        f"{result['partial_count']} partly there, "
                        f"{result['gone_count']} gone.",
                    )
                else:
                    self.deleted_panel.show_info(
                        "Scan finished", "\n".join(result["lines"])
                    )

            self.run_worker(
                lambda: self.controller.scan_deleted(source),
                [self.scan_button],
                on_result,
                self.deleted_panel,
                action="scan",
            )

        def _fill_scan_table(self, result) -> None:
            verdict_colours = {
                "strong": theme.GREEN,
                "partial": theme.TEXT,
                "gone": theme.RED_TEXT,
            }
            self.scan_table.setRowCount(len(result["files"]))
            for row, entry in enumerate(result["files"]):
                for column, value in enumerate(
                    [entry["name"], entry["size_text"], entry["verdict_words"], entry["summary"]]
                ):
                    item = QTableWidgetItem(value)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if column == 0 and entry["verdict"] != "gone"
                        else Qt.CheckState.Unchecked
                    )
                    if column == 2:
                        item.setForeground(
                            QColor(verdict_colours.get(entry["verdict"], theme.TEXT))
                        )
                    self.scan_table.setItem(row, column, item)
            for column in range(3):
                self.scan_table.resizeColumnToContents(column)

        def _scan_row_chosen(self, row, _column, _prev_row, _prev_column) -> None:
            if self._scan_result is None or row < 0 or row >= len(self._scan_result["files"]):
                return
            entry = self._scan_result["files"][row]
            lines = [entry["summary"], "", "Evidence:"]
            lines.extend(f"  - {piece}" for piece in entry["evidence"])
            self.evidence_text.setPlainText("\n".join(lines))

        def _recover(self) -> None:
            if self._scan_result is None:
                self.deleted_panel.show_problem(
                    "Nothing has been scanned yet",
                    "Look for deleted files first, then bring the ticked ones back.",
                )
                return
            names = []
            for row in range(self.scan_table.rowCount()):
                item = self.scan_table.item(row, 0)
                if item is not None and item.checkState() == Qt.CheckState.Checked:
                    names.append(self._scan_result["files"][row]["name"])
            if not names:
                self.deleted_panel.show_problem(
                    "No files are ticked",
                    "Tick the files to bring back in the list above, then try again.",
                )
                return
            destination = self.recover_dest_edit.text().strip()
            if not destination:
                self.deleted_panel.show_problem(
                    "No destination was named",
                    "Choose a folder on a different drive for the files to come back into.",
                )
                return
            source = self.scan_source_edit.text().strip()

            def on_result(result):
                self.recover_report.show_lines(result["lines"])
                if result["status"] == "complete":
                    self.deleted_panel.show_info("Recovery finished", "Everything asked for came back whole.")
                else:
                    self.deleted_panel.show_problem(
                        "Recovery finished",
                        "Some of what was asked for could not come back. The report says which, and why.",
                    )

            self.run_worker(
                lambda: self.controller.recover(source, destination, names),
                [self.recover_button],
                on_result,
                self.deleted_panel,
                action="recover",
            )

        def _rescue_dest_changed(self, text: str) -> None:
            if text.strip():
                self.rescue_map_edit.setText(
                    self.controller.default_map_path(text.strip())
                )

        def _rescue(self) -> None:
            source = self.rescue_source_edit.text().strip()
            destination = self.rescue_dest_edit.text().strip()
            map_path = self.rescue_map_edit.text().strip()
            if not source or not destination:
                self.deleted_panel.show_problem(
                    "The rescue needs a drive and a place for the image",
                    "Name the drive to copy and the image file to write, then start the rescue.",
                )
                return
            if not map_path:
                map_path = self.controller.default_map_path(destination)
                self.rescue_map_edit.setText(map_path)
            self._rescue_map_path = map_path
            self.rescue_progress.setRange(0, 0)
            self.rescue_progress.setValue(0)
            self.rescue_progress.show()
            self.rescue_progress_label.setText("Starting the copy…")

            def on_result(result):
                self._rescue_timer.stop()
                progress = self.controller.read_rescue_progress(map_path)
                if progress:
                    self._show_rescue_progress(progress)
                else:
                    # No readable progress file at the finish
                    # line: settle the bar anyway — hide it and
                    # let the label carry the outcome, never the
                    # "Starting the copy…" it began with.
                    self.rescue_progress.hide()
                    self.rescue_progress_label.setText(
                        "Everything was copied."
                        if result["status"] == "complete"
                        else "The rescue finished with gaps — the "
                        "report below says what is missing."
                    )
                self.rescue_report.show_lines(result["lines"])
                if result["status"] == "complete":
                    self.deleted_panel.show_info("Rescue finished", "Everything was copied.")
                else:
                    self.deleted_panel.show_problem(
                        "Rescue finished with gaps",
                        "Part of the drive could not be read. The report and the progress file say exactly what is missing.",
                    )

            def on_failure(message):
                self._rescue_timer.stop()
                self.rescue_progress.hide()
                self.rescue_progress_label.setText(message)
                self.deleted_panel.show_problem("Lazarus stopped", message)

            worker = self.run_worker(
                lambda: self.controller.rescue(source, destination, map_path),
                [self.rescue_button],
                on_result,
                self.deleted_panel,
                action="rescue",
                on_failure=on_failure,
            )
            if worker is None:
                # Another job is running (run_worker has said so in
                # the panel): no copy is starting, so no bar.
                self.rescue_progress.hide()
                self.rescue_progress_label.setText("")
                return
            self._rescue_worker_ref = worker
            self._rescue_timer.start()
            # Stopping the timer when the worker ends, whatever happens:
            worker.finished.connect(self._rescue_timer.stop)

        def _poll_rescue(self) -> None:
            if not self._rescue_map_path:
                return
            progress = self.controller.read_rescue_progress(self._rescue_map_path)
            if progress is None:
                return  # no readable map yet — keep the shimmer
            self._show_rescue_progress(progress)

        def _show_rescue_progress(self, progress) -> None:
            total = progress["regions_total"]
            done = progress["regions_recovered"]
            self.rescue_progress.setRange(0, max(total, 1))
            self.rescue_progress.setValue(done)
            self.rescue_progress_label.setText(
                f"{done} of {total} stretch(es) copied — "
                f"{progress['bytes_recovered']:,} of {progress['total_bytes']:,} bytes safe."
            )

        # -- (d) Locked files ---------------------------------------------------

        def _build_locked_page(self):
            page, layout = self._page("Locked files — open what is yours")
            self.locked_panel = MessagePanel()
            layout.addWidget(_label("Locked drives", "sectionTitle"))
            self.volumes_state = _label("")
            layout.addWidget(self.volumes_state)
            self.volumes_table = QTableWidget(0, 3)
            self.volumes_table.setHorizontalHeaderLabels(["Drive", "Kind of lock", "State"])
            self.volumes_table.setSelectionBehavior(
                QTableWidget.SelectionBehavior.SelectRows
            )
            layout.addWidget(self.volumes_table)
            buttons = QHBoxLayout()
            self.volumes_refresh_button = _button("Look again", self.refresh_volumes)
            self.unlock_button = _button("Unlock the chosen drive", self._unlock, primary=True)
            buttons.addWidget(self.volumes_refresh_button)
            buttons.addWidget(self.unlock_button)
            buttons.addStretch(1)
            layout.addLayout(buttons)
            self.unlock_report = ReportPanel("Unlock result")
            layout.addWidget(self.unlock_report)
            layout.addWidget(self.locked_panel)

            layout.addWidget(_label("Files locked by a ransom note", "sectionTitle"))
            self.locked_folder_edit = QLineEdit()
            self.locked_folder_edit.setPlaceholderText("Folder the locked files are in")
            layout.addLayout(
                _path_row(self.locked_folder_edit, "Choose folder", lambda: self._pick_folder(self.locked_folder_edit))
            )
            self.locked_folder_edit.textChanged.connect(self._locked_folder_changed)
            self.identify_button = _button("See which family this looks like", self._identify)
            layout.addWidget(self.identify_button)
            self.identify_text = QTextEdit()
            self.identify_text.setReadOnly(True)
            layout.addWidget(self.identify_text)
            self.family_combo = QComboBox()
            self.family_combo.hide()
            self.family_label = _label("Pick the family to use", "sectionTitle")
            self.family_label.hide()
            layout.addWidget(self.family_label)
            layout.addWidget(self.family_combo)
            self.work_edit = QLineEdit()
            self.work_edit.setPlaceholderText("New work folder — Lazarus copies the files here first")
            layout.addWidget(_label("Work folder (must not exist yet)", "faint"))
            layout.addLayout(
                _path_row(self.work_edit, "Choose folder", lambda: self._pick_folder(self.work_edit))
            )
            self.decryptor_edit = QLineEdit()
            self.decryptor_edit.setPlaceholderText("The published decryptor program, from its publisher")
            layout.addLayout(
                _path_row(self.decryptor_edit, "Choose program", lambda: self._pick_file(self.decryptor_edit))
            )
            self.decrypt_button = _button(
                "Run the decryptor on copies", self._decrypt, danger=True
            )
            layout.addWidget(self.decrypt_button)
            self.decrypt_report = ReportPanel("Decryptor report")
            layout.addWidget(self.decrypt_report)

            layout.addWidget(_label("What can Lazarus do for a drive or folder?", "sectionTitle"))
            self.assess_edit = QLineEdit()
            self.assess_edit.setPlaceholderText("Drive or folder to assess")
            layout.addLayout(
                _path_row(self.assess_edit, "Choose folder", lambda: self._pick_folder(self.assess_edit))
            )
            self.assess_button = _button("Assess my options", self._assess)
            layout.addWidget(self.assess_button)
            self.assess_text = QTextEdit()
            self.assess_text.setReadOnly(True)
            layout.addWidget(self.assess_text)

            layout.addWidget(_label("Published decryptors Lazarus knows", "sectionTitle"))
            self.registry_list = QListWidget()
            for row in self.controller.registry_view():
                self.registry_list.addItem(row["row_text"])
            layout.addWidget(self.registry_list)
            self.refresh_volumes()
            return page

        def refresh_volumes(self) -> None:
            def on_result(result):
                self._volumes = result["volumes"]
                self.volumes_table.setRowCount(len(result["volumes"]))
                for row, volume in enumerate(result["volumes"]):
                    for column, value in enumerate(
                        [volume["ref"], volume["kind_words"], volume["state_words"]]
                    ):
                        self.volumes_table.setItem(row, column, QTableWidgetItem(value))
                self.volumes_table.resizeColumnsToContents()
                if not result["available"]:
                    self.volumes_state.setText(result["message"])
                    self.volumes_state.show()
                elif not result["volumes"]:
                    self.volumes_state.setText("No volumes could be listed.")
                    self.volumes_state.show()
                else:
                    self.volumes_state.hide()

            self.run_worker(
                self.controller.list_volumes,
                [self.volumes_refresh_button],
                on_result,
                self.locked_panel,
                action="refresh_volumes",
            )

        def _unlock(self) -> None:
            row = self.volumes_table.currentRow()
            if row < 0 or row >= len(self._volumes):
                self.locked_panel.show_problem(
                    "No drive is chosen",
                    "Pick a drive in the list above, then unlock it.",
                )
                return
            ref = self._volumes[row]["ref"]
            # The key is asked for here, on the window thread, before
            # any worker starts — a dialog cannot open from inside a
            # worker. The engine still decides whether a key is
            # needed at all: for an already-open drive it never calls
            # the provider, so a key typed for one is simply never
            # used, and it is dropped with this call either way.
            key = ""
            if self._volumes[row]["locked"] is not False:
                # The dialog is a local, never stored on the
                # window: on Accept the key is taken (which clears
                # the field); on Cancel the field is cleared
                # before the dialog is dropped. Nothing on the
                # window keeps the dialog, or a cancelled key,
                # after this attempt.
                dialog = KeyDialog(ref, self)
                try:
                    if dialog.exec() == QDialog.DialogCode.Accepted:
                        key = dialog.take_key()
                    else:
                        dialog.key_edit.clear()
                finally:
                    dialog.deleteLater()

            def key_provider() -> str:
                return key

            def on_result(result):
                self.unlock_report.show_lines(
                    [result["message"], f"Next step: {result['next_step']}"]
                )
                if result["status"] == "not-accepted":
                    self.locked_panel.show_problem("The key was not accepted", result["message"])
                else:
                    self.locked_panel.show_info("Unlock finished", result["message"])
                self.refresh_volumes()

            self.run_worker(
                lambda: self.controller.unlock(ref, key_provider),
                [self.unlock_button],
                on_result,
                self.locked_panel,
                action="unlock",
            )

        def _reset_family_combo(self) -> None:
            self.family_combo.blockSignals(True)
            self.family_combo.clear()
            self.family_combo.blockSignals(False)
            self.family_combo.hide()
            self.family_label.hide()

        def _note_decrypt_plan(self, folder, plan) -> None:
            """Record a fresh family plan and reset the picker.

            Every identify result clears and hides the family
            picker first; only a "choose" plan puts it back, with
            that plan's families and no one else's.
            """
            self._reset_family_combo()
            self._decrypt_plan = plan
            self._decrypt_plan_folder = folder
            if plan["status"] == "choose":
                self._decrypt_entry = None
                self.family_combo.blockSignals(True)
                self.family_combo.addItems(plan["choices"])
                self.family_combo.blockSignals(False)
                self.family_combo.show()
                self.family_label.show()
            elif plan["status"] == "ready":
                self._decrypt_entry = plan["entry"]
            else:
                self._decrypt_entry = None

        def _locked_folder_changed(self, _text: str = "") -> None:
            # A different folder is a different question: what was
            # identified, and any plan made, for the old folder no
            # longer stands, and neither does its picker.
            self._identification = None
            self._identification_folder = None
            self._decrypt_plan = None
            self._decrypt_plan_folder = None
            self._decrypt_entry = None
            self._reset_family_combo()

        def _identify(self) -> None:
            folder = self.locked_folder_edit.text().strip()
            if not folder:
                self.locked_panel.show_problem(
                    "No folder was named",
                    "Choose the folder the locked files are in, then look at its signs.",
                )
                return

            def work():
                identification = self.controller.identify(folder)
                return {
                    "identification": identification,
                    # The plan is settled from the identification
                    # just made — the folder is read once, here on
                    # the worker thread, and never again for this.
                    "plan": self.controller.plan_from_identification(identification),
                }

            def on_result(result):
                identification = result["identification"]
                self._identification = identification
                self._identification_folder = folder
                lines = []
                if identification["matches"]:
                    for match in identification["matches"]:
                        lines.append(
                            f"{match['family']} — this {match['confidence']} the family that locked these files."
                        )
                        lines.extend(f"  - {piece}" for piece in match["evidence"])
                        lines.append(
                            f"  Published decryptor: {match['decryptor_name']} ({match['publisher']}). {match['conditions']}"
                        )
                else:
                    lines.append(identification["no_match_text"])
                lines.extend(identification["notes"])
                self.identify_text.setPlainText("\n".join(lines))
                self._note_decrypt_plan(folder, result["plan"])

            self.run_worker(
                work,
                [self.identify_button],
                on_result,
                self.locked_panel,
                action="identify",
            )

        def _decrypt(self) -> None:
            folder = self.locked_folder_edit.text().strip()
            if not folder:
                self.locked_panel.show_problem(
                    "No folder was named",
                    "Choose the folder the locked files are in first.",
                )
                return
            # The entry comes from the CURRENT plan for this
            # folder, and from nowhere else: a "ready" plan's own
            # entry, or — only while a "choose" plan for this same
            # folder is still standing — the picker's current
            # selection, settled from the identification already
            # made for this folder. Nothing here reads the folder
            # again: identifying is the step that looks at it, and
            # it runs on a worker thread. With no identification
            # standing for this folder, nothing starts. A picker
            # left over from another folder is never read:
            # changing the folder has already cleared it and the
            # old plan.
            pending = (
                self._decrypt_plan
                if self._decrypt_plan_folder == folder
                else None
            )
            if pending is None:
                self.locked_panel.show_problem(
                    "Identify the folder first",
                    "Identify the folder first, so Lazarus can say "
                    "which family it looks like before anything runs.",
                )
                return
            if pending.get("status") == "choose":
                chosen = (
                    self.family_combo.currentText()
                    if self.family_combo.isVisible()
                    else ""
                )
                if not chosen:
                    self.locked_panel.show_problem(
                        "You need to pick the family", pending.get("message", "")
                    )
                    return
                if (
                    self._identification is None
                    or self._identification_folder != folder
                ):
                    self.locked_panel.show_problem(
                        "Identify the folder first",
                        "Identify the folder first, so Lazarus can say "
                        "which family it looks like before anything runs.",
                    )
                    return
                try:
                    plan = self.controller.plan_from_identification(
                        self._identification, chosen
                    )
                except LazarusError as error:
                    self.locked_panel.show_problem("Lazarus stopped", str(error))
                    return
                if plan["status"] != "ready":
                    self.locked_panel.show_problem(
                        "No decryptor will be run", plan["message"]
                    )
                    return
                self._note_decrypt_plan(folder, plan)
                entry = plan["entry"]
            elif pending.get("status") == "ready":
                self._reset_family_combo()
                entry = pending["entry"]
            else:
                # The standing plan already refused, in the words
                # identifying produced: say them again, as they are.
                self.locked_panel.show_problem(
                    "No decryptor will be run", pending.get("message", "")
                )
                return
            work = self.work_edit.text().strip()
            program = self.decryptor_edit.text().strip()
            if not work or not program:
                self.locked_panel.show_problem(
                    "The work folder and the decryptor program are both needed",
                    "Choose a new work folder and the published decryptor program, then run it.",
                )
                return
            dialog = ConfirmDialog(
                "Run this decryptor on copies?",
                (
                    f"Lazarus will run {entry.decryptor_name} (published by "
                    f"{entry.publisher}, for {entry.family}) on COPIES of the "
                    f"files in {folder}, inside the work folder {work}. The "
                    "program you chose is the one that runs. Your original "
                    "files are never handed to it, and they are checked "
                    "before and after so the report can say they did not change."
                ),
                "Run it on copies",
                self,
                danger=True,
            )
            self.last_decrypt_dialog = dialog
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return

            def on_result(result):
                self.decrypt_report.show_text(result["text"])
                if result["status"] == "complete":
                    self.locked_panel.show_info("Decryptor finished", "The report below says what it made.")
                else:
                    self.locked_panel.show_problem(
                        "Decryptor finished — read the report",
                        "The report below says what happened, in plain terms.",
                    )

            self.run_worker(
                lambda: self.controller.run_decrypt_with_entry(entry, folder, work, program),
                [self.decrypt_button],
                on_result,
                self.locked_panel,
                action="decrypt",
            )

        def _assess(self) -> None:
            path = self.assess_edit.text().strip()
            if not path:
                self.locked_panel.show_problem(
                    "Nothing was named",
                    "Name the drive or folder to assess, then assess it.",
                )
                return

            def on_result(result):
                sections = []
                if result["findings"]:
                    sections.append("What Lazarus found:")
                    sections.extend(f"  - {line}" for line in result["findings"])
                if result["options"]:
                    sections.append("Your options:")
                    sections.extend(f"  - {line}" for line in result["options"])
                if result["notes"]:
                    sections.append("Worth knowing:")
                    sections.extend(f"  - {line}" for line in result["notes"])
                self.assess_text.setPlainText("\n".join(sections))

            self.run_worker(
                lambda: self.controller.assess(path),
                [self.assess_button],
                on_result,
                self.locked_panel,
                action="assess",
            )

        # -- (e) Backup ---------------------------------------------------------

        def _build_backup_page(self):
            page, layout = self._page("Backup — kept safe on a schedule")
            self.backup_panel = MessagePanel()
            self.backup_none = _label(
                "No backup is set up yet. Fill in the form below — where "
                "backups live, and what they keep safe — and Lazarus will "
                "save on the schedule you choose."
            )
            layout.addWidget(self.backup_none)
            banner = QFrame()
            banner.setObjectName("banner")
            banner_layout = QVBoxLayout(banner)
            self.backup_state_word = QLabel("")
            self.backup_state_word.setObjectName("stateWord")
            self.backup_headline = _label("")
            self.backup_message = _label("")
            self.backup_numbers = _label("", "faint")
            self.backup_next_action = _label("")
            for widget in (
                self.backup_state_word,
                self.backup_headline,
                self.backup_message,
                self.backup_numbers,
                self.backup_next_action,
            ):
                banner_layout.addWidget(widget)
            self.backup_banner = banner
            layout.addWidget(banner)
            banner.hide()
            layout.addWidget(self.backup_panel)

            layout.addWidget(_label("Set a backup up", "sectionTitle"))
            self.backup_store_edit = QLineEdit()
            self.backup_store_edit.setPlaceholderText("Folder the backups live in (on the backup drive)")
            layout.addLayout(
                _path_row(self.backup_store_edit, "Choose folder", lambda: self._pick_folder(self.backup_store_edit))
            )
            layout.addWidget(_label("What to keep safe", "faint"))
            self.backup_sources_list = QListWidget()
            layout.addWidget(self.backup_sources_list)
            source_buttons = QHBoxLayout()
            source_buttons.addWidget(_button("Add a folder", self._backup_add_source))
            source_buttons.addWidget(_button("Add a file", self._backup_add_file))
            source_buttons.addWidget(_button("Take the chosen one out", self._backup_remove_source))
            source_buttons.addStretch(1)
            layout.addLayout(source_buttons)

            schedule_row = QHBoxLayout()
            schedule_row.addWidget(_label("Run"))
            self.backup_every_combo = QComboBox()
            self.backup_every_combo.addItems(["every day", "every week"])
            schedule_row.addWidget(self.backup_every_combo)
            self.backup_weekday_combo = QComboBox()
            self.backup_weekday_combo.addItems(
                ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            )
            schedule_row.addWidget(self.backup_weekday_combo)
            schedule_row.addWidget(_label("at"))
            self.backup_time_edit = QTimeEdit()
            self.backup_time_edit.setDisplayFormat("HH:mm")
            self.backup_time_edit.setTime(QTime(2, 0))
            schedule_row.addWidget(self.backup_time_edit)
            schedule_row.addStretch(1)
            layout.addLayout(schedule_row)

            keep_row = QHBoxLayout()
            self.backup_keep_combo = QComboBox()
            self.backup_keep_combo.addItems(["keep the newest", "keep the last"])
            keep_row.addWidget(self.backup_keep_combo)
            self.backup_keep_spin = QSpinBox()
            self.backup_keep_spin.setRange(1, 10000)
            self.backup_keep_spin.setValue(10)
            keep_row.addWidget(self.backup_keep_spin)
            self.backup_keep_unit = _label("backups")
            keep_row.addWidget(self.backup_keep_unit)
            keep_row.addStretch(1)
            layout.addLayout(keep_row)
            self.backup_keep_combo.currentTextChanged.connect(self._keep_unit_changed)

            verify_row = QHBoxLayout()
            verify_row.addWidget(_label("Spot-check the store every"))
            self.backup_verify_spin = QSpinBox()
            self.backup_verify_spin.setRange(1, 365)
            self.backup_verify_spin.setValue(7)
            verify_row.addWidget(self.backup_verify_spin)
            verify_row.addWidget(_label("runs"))
            verify_row.addStretch(1)
            layout.addLayout(verify_row)
            self.backup_register_check = QCheckBox(
                "Also set up the Windows schedule, so backups run on their own"
            )
            layout.addWidget(self.backup_register_check)
            self.backup_setup_button = _button("Save these backup settings", self._backup_setup, primary=True)
            layout.addWidget(self.backup_setup_button)

            actions = QHBoxLayout()
            self.backup_run_button = _button("Run a backup now", self._backup_run, primary=True)
            self.backup_refresh_button = _button("Check the status again", self.refresh_backup)
            actions.addWidget(self.backup_run_button)
            actions.addWidget(self.backup_refresh_button)
            actions.addStretch(1)
            layout.addLayout(actions)
            self.backup_run_report = ReportPanel("Backup run")
            layout.addWidget(self.backup_run_report)

            layout.addWidget(_label("Past backup runs", "sectionTitle"))
            self.backup_history_list = QListWidget()
            layout.addWidget(self.backup_history_list, 1)

            layout.addWidget(_label("Tidy up old backups", "sectionTitle"))
            layout.addWidget(
                _label(
                    "Tidying removes the backups outside the keeping rule "
                    "above. Lazarus shows the plan first, and removes "
                    "nothing until you say so."
                )
            )
            tidy_buttons = QHBoxLayout()
            self.prune_plan_button = _button("Show what tidying would do", self._prune_plan)
            self.prune_apply_button = _button("Tidy them away", self._prune_apply, danger=True)
            self.prune_apply_button.setEnabled(False)
            tidy_buttons.addWidget(self.prune_plan_button)
            tidy_buttons.addWidget(self.prune_apply_button)
            tidy_buttons.addStretch(1)
            layout.addLayout(tidy_buttons)
            self.prune_report = ReportPanel("Tidying plan")
            layout.addWidget(self.prune_report)

            schedule_buttons = QHBoxLayout()
            self.register_button = _button("Set up the Windows schedule", self._register)
            self.unregister_button = _button("Remove the Windows schedule", self._unregister)
            schedule_buttons.addWidget(self.register_button)
            schedule_buttons.addWidget(self.unregister_button)
            schedule_buttons.addStretch(1)
            layout.addLayout(schedule_buttons)
            return page

        def _keep_unit_changed(self, text: str) -> None:
            self.backup_keep_unit.setText("days" if "last" in text else "backups")
            self.backup_keep_spin.setRange(1, 36500 if "last" in text else 10000)

        def _backup_add_source(self) -> None:
            chosen = QFileDialog.getExistingDirectory(self, "Choose a folder to keep safe")
            if chosen:
                self.backup_sources_list.addItem(chosen)

        def _backup_add_file(self) -> None:
            chosen, _ = QFileDialog.getOpenFileName(self, "Choose a file to keep safe")
            if chosen:
                self.backup_sources_list.addItem(chosen)

        def _backup_remove_source(self) -> None:
            row = self.backup_sources_list.currentRow()
            if row >= 0:
                self.backup_sources_list.takeItem(row)

        def refresh_backup(self) -> None:
            if not self.controller.backup_configured():
                self.backup_banner.hide()
                self.backup_none.show()
                self.backup_history_list.clear()
                return
            try:
                view = self.controller.backup_status_view()
                config = self.controller.load_backup_config()
            except LazarusError as error:
                self.backup_banner.hide()
                self.backup_none.hide()
                self.backup_panel.show_problem("The backup settings could not be read", str(error))
                return
            self.backup_none.hide()
            state_words = {
                "ready": "READY",
                "target-missing": "BACKUP DRIVE NOT CONNECTED",
                "target-full": "BACKUP DRIVE FULL",
                "store-damaged": "BACKUP STORE DAMAGED",
                "never-run": "NO BACKUP HAS RUN YET",
                "overdue": "BACKUP OVERDUE",
                "unfinished-backup": "AN EARLIER BACKUP NEVER FINISHED SAVING",
            }
            self.backup_state_word.setText(state_words.get(view["state"], view["state"].upper()))
            self.backup_headline.setText(view["headline"])
            self.backup_message.setText(view["message"])
            numbers = []
            if view["free_text"]:
                numbers.append(f"Free space on the backup drive: {view['free_text']}")
            if view["estimate_text"]:
                numbers.append(f"Next backup needs {view['estimate_text']}")
            if view["next_due_local"]:
                numbers.append(f"Next backup due: {view['next_due_local']}")
            self.backup_numbers.setText("\n".join(numbers))
            if view["next_action"]:
                self.backup_next_action.setText(f"What to do: {view['next_action']}")
                self.backup_next_action.show()
            else:
                self.backup_next_action.hide()
            self.backup_banner.show()
            if not self._backup_prefilled:
                self._backup_prefilled = True
                self.backup_store_edit.setText(config.store)
                self.backup_sources_list.clear()
                self.backup_sources_list.addItems(config.sources)
                self.backup_every_combo.setCurrentText(
                    "every week" if config.schedule.every == "weekly" else "every day"
                )
                self.backup_weekday_combo.setCurrentText(config.schedule.weekday)
                hour, _, minute = config.schedule.at.partition(":")
                self.backup_time_edit.setTime(QTime(int(hour), int(minute)))
                if config.retention.mode == "days":
                    self.backup_keep_combo.setCurrentText("keep the last")
                    self.backup_keep_spin.setValue(config.retention.days)
                else:
                    self.backup_keep_combo.setCurrentText("keep the newest")
                    self.backup_keep_spin.setValue(config.retention.count)
                self.backup_verify_spin.setValue(config.verify_every_runs)
            history = self.controller.backup_history_view()
            self.backup_history_list.clear()
            if not history["entries"]:
                self.backup_history_list.addItem(
                    "No backups have run yet. Run one now to take the first."
                )
            for entry in history["entries"]:
                self.backup_history_list.addItem(
                    f"{entry['started_utc']} — {entry['outcome_words']} "
                    f"({entry['trigger_words']}), {entry['files_saved']} file(s), "
                    f"{entry['size_text']}"
                )
            if history["damaged_lines"]:
                self.backup_history_list.addItem(
                    f"Warning: {history['damaged_lines']} line(s) in the backup "
                    "history could not be read and were left out."
                )

        def _backup_setup(self) -> None:
            store = self.backup_store_edit.text().strip()
            sources = [
                self.backup_sources_list.item(i).text()
                for i in range(self.backup_sources_list.count())
            ]
            if not store:
                self.backup_panel.show_problem(
                    "No backup store was named",
                    "Choose the folder the backups will live in, then save the settings.",
                )
                return
            if not sources:
                self.backup_panel.show_problem(
                    "Nothing is being kept safe",
                    "Add at least one file or folder to keep safe, then save the settings.",
                )
                return
            # Every form value is read here, on the window thread,
            # into plain values. The worker callable below receives
            # only those values — it touches no widget, ever.
            every = "weekly" if self.backup_every_combo.currentText() == "every week" else "daily"
            weekday = self.backup_weekday_combo.currentText()
            keep_mode = "days" if "last" in self.backup_keep_combo.currentText() else "count"
            keep_spin_value = self.backup_keep_spin.value()
            keep_count = keep_spin_value if keep_mode == "count" else 10
            keep_days = keep_spin_value if keep_mode == "days" else 30
            verify_every_runs = self.backup_verify_spin.value()
            at = self.backup_time_edit.time().toString("HH:mm")
            register = self.backup_register_check.isChecked()

            def do_setup():
                return self.controller.backup_setup(
                    store,
                    sources,
                    every=every,
                    weekday=weekday,
                    at=at,
                    keep_mode=keep_mode,
                    keep_count=keep_count,
                    keep_days=keep_days,
                    verify_every_runs=verify_every_runs,
                    register=register,
                )

            def on_result(result):
                self.backup_panel.show_info("Backup is set up", "\n".join(result["lines"]))
                self._backup_prefilled = False
                # Saving settings spends any tidying plan on show.
                self._prune_view = None
                self.prune_apply_button.setEnabled(False)
                self.refresh_backup()

            self.run_worker(
                do_setup,
                [self.backup_setup_button],
                on_result,
                self.backup_panel,
                action="backup_setup",
            )

        def _backup_run(self) -> None:
            def on_result(result):
                self.backup_run_report.show_lines(result["lines"])
                if result["outcome"] == "complete":
                    self.backup_panel.show_info("Backup complete", result["message"] or result["lines"][0])
                else:
                    self.backup_panel.show_problem("Backup finished — read the report", "\n".join(result["lines"]))
                # A finished run spends any tidying plan on show.
                self._prune_view = None
                self.prune_apply_button.setEnabled(False)
                self.refresh_backup()

            self.run_worker(
                self.controller.run_backup_now,
                [self.backup_run_button],
                on_result,
                self.backup_panel,
                action="backup_run",
            )

        def _prune_plan(self) -> None:
            def on_result(view):
                self._prune_view = view
                lines = []
                if view["remove"]:
                    lines.append("These backups are outside the keeping rule you chose:")
                    for item in view["remove"]:
                        when = item["created_utc"] or "unknown date"
                        lines.append(f"  - {item['id']} (made {when}): {item['reason']}")
                    lines.append(f"Backups to remove: {len(view['remove'])}")
                else:
                    lines.append(
                        "Nothing to tidy up: every backup in the store is inside the keeping rule you chose."
                    )
                if view["held"]:
                    lines.append("Left alone:")
                    for held in view["held"]:
                        lines.append(f"  - {held['id']}: {held['reason']}")
                self.prune_report.show_lines(lines)
                self.prune_apply_button.setEnabled(bool(view["remove"]))

            self.run_worker(
                self.controller.prune_plan_view,
                [self.prune_plan_button],
                on_result,
                self.backup_panel,
                action="prune_plan",
            )

        def _prune_apply(self) -> None:
            dialog = ConfirmDialog(
                "Tidy these old backups away?",
                (
                    "The backups in the plan will be removed for good. The "
                    "plan on this page lists exactly which ones, and why. "
                    "Backups inside your keeping rule are never touched."
                ),
                "Tidy them away",
                self,
                danger=True,
            )
            self.last_prune_dialog = dialog
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return

            def on_result(result):
                self.prune_report.show_lines(result["lines"])
                self.backup_panel.show_info("Tidying finished", "\n".join(result["lines"]))
                self._prune_view = None
                self.prune_apply_button.setEnabled(False)
                self.refresh_backup()

            self.run_worker(
                self.controller.prune_apply,
                [self.prune_apply_button, self.prune_plan_button],
                on_result,
                self.backup_panel,
                action="prune_apply",
            )

        def _register(self) -> None:
            try:
                message = self.controller.register()
            except LazarusError as error:
                self.backup_panel.show_problem("The schedule could not be set up", str(error))
                return
            self.backup_panel.show_info("Schedule set up", message)

        def _unregister(self) -> None:
            try:
                message = self.controller.unregister()
            except LazarusError as error:
                self.backup_panel.show_problem("The schedule could not be removed", str(error))
                return
            self.backup_panel.show_info("Schedule removed", message)


# ----------------------------------------------------------------------
# Entry point


def _installer_module():
    """lazarus.installer, if the sibling worker's module is present."""
    try:
        from .. import installer
    except Exception:
        return None
    return installer


def _show_report_box(title: str, report) -> None:
    box = QMessageBox()
    box.setWindowTitle(title)
    box.setText("\n".join(report.summary_lines()))
    box.setIcon(
        QMessageBox.Icon.Information if report.ok else QMessageBox.Icon.Warning
    )
    box.exec()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if _QT_ERROR is not None:
        print(
            "Lazarus needs PySide6 to open its window, and PySide6 is "
            "not installed. Install PySide6 and start Lazarus again."
        )
        return 2
    installer = _installer_module()
    app = QApplication([sys.argv[0]] + [a for a in args if not a.startswith("--lazarus-")])
    app.setStyleSheet(theme.STYLESHEET)
    if "--lazarus-install" in args or "--lazarus-uninstall" in args:
        if installer is None:
            print("Installing Lazarus is not available in this copy.")
            return 1
        if "--lazarus-install" in args:
            report = installer.perform_install()
            _show_report_box("Install Lazarus", report)
        else:
            report = installer.perform_uninstall()
            _show_report_box("Remove Lazarus", report)
        return 0 if report.ok else 1
    if installer is not None:
        try:
            offer = installer.needs_install_offer()
        except Exception:
            offer = False
        if offer:
            box = QMessageBox()
            box.setWindowTitle("Install Lazarus?")
            box.setText(
                "Lazarus can install itself on this computer: it copies "
                "Lazarus into Program Files and adds it to the Start "
                "Menu. Windows will ask for your approval once. Choose "
                "“Not now” to run this copy as it is."
            )
            install_button = box.addButton(
                "Install Lazarus", QMessageBox.ButtonRole.AcceptRole
            )
            box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is install_button:
                started = installer.elevate_and_run("install")
                QMessageBox.information(
                    None,
                    "Installing Lazarus",
                    "Windows is asking for an administrator's OK. "
                    "When the install report says it is done, "
                    "Lazarus is in the Start Menu, and this window "
                    "can be closed whenever. This window keeps "
                    "running the copy you opened."
                    if started
                    else "The install could not be started. This copy "
                    "of Lazarus will open instead, and you can try "
                    "installing again another time.",
                )
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
