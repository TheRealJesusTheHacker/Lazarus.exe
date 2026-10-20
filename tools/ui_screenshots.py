"""Render every Lazarus window state, offscreen, for the audit.

    python tools/ui_screenshots.py

Fixtures are all synthetic and built with the real engine: a store
holding a normal snapshot, an INCOMPLETE one, and a safety copy; a
FAT image with deleted files at each verdict; a fake unlock platform
with a locked, an open, and an unknown volume; backup settings
written by the engine for READY / TARGET MISSING / STORE DAMAGED /
OVERDUE. The PNGs land in ``phase5-audit/`` beside a CONTRAST.md
with every palette pairing computed and a pixel audit of each PNG.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import fat_image  # noqa: E402
from lazarus.backup import append_history, history_path_for, load_history  # noqa: E402
from lazarus.decrypt_model import VolumeInfo  # noqa: E402
from lazarus.rescue import rescue_to_image  # noqa: E402
from lazarus.snapshot import create_snapshot  # noqa: E402
from lazarus.store import SnapshotStore, format_utc, utc_now  # noqa: E402
from lazarus.ui import theme  # noqa: E402
from lazarus.ui.app import (  # noqa: E402
    ConfirmDialog,
    KeyDialog,
    MainWindow,
    RestoreConfirmDialog,
)
from lazarus.ui.controller import Controller  # noqa: E402
from lazarus.unlock import UnlockAttempt  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "phase5-audit"
ROOT = Path(tempfile.mkdtemp(prefix="lazarus-phase5-shots-"))
RENDERED: list[str] = []


def wait_until(app, predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return predicate()


def shoot(app, window: MainWindow, name: str, page: int | None = None, scroll: int = 0):
    if page is not None:
        window.sidebar.setCurrentRow(page)
        app.processEvents()
        bar = window.stack.currentWidget().verticalScrollBar()
        bar.setValue(scroll)
        app.processEvents()
    window.grab().save(str(OUT / f"{name}.png"))
    RENDERED.append(name)
    print(f"rendered {name}")


def shoot_widget(app, widget, name: str):
    # Dialogs are grabbed inside a real exec() loop: the offscreen
    # platform cannot settle a dialog's size hints outside one, and
    # a grab taken too early shows wrapped text at the wrong height.
    from PySide6.QtCore import QTimer

    def grab():
        widget.grab().save(str(OUT / f"{name}.png"))
        widget.reject()

    QTimer.singleShot(350, grab)
    widget.exec()
    RENDERED.append(name)
    print(f"rendered {name}")
    widget.close()


def make_window(app, controller) -> MainWindow:
    window = MainWindow(controller)
    window.resize(1120, 860)
    window.show()
    app.processEvents()
    return window


def build_tree(root: Path, notes: str = "remember the milk") -> Path:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "notes.txt").write_text(notes, encoding="utf-8")
    (root / "docs" / "report.txt").write_text("quarterly numbers", encoding="utf-8")
    (root / "photo.bin").write_bytes(bytes(range(256)) * 4)
    return root


class FakePlatform:
    def __init__(self, volumes):
        self._volumes = {v.ref: v for v in volumes}

    def describe_volume(self, ref):
        return self._volumes.get(ref)

    def list_volumes(self):
        return list(self._volumes.values())

    def run_unlock(self, argv, secret):
        return UnlockAttempt(ok=True)


class SlowReader:
    """A drive that answers slowly, so a rescue can be caught mid-run."""

    def __init__(self, data: bytes, delay: float = 0.22):
        self._data = data
        self.size = len(data)
        self._delay = delay

    def read_at(self, offset: int, length: int) -> bytes:
        time.sleep(self._delay)
        return self._data[offset : offset + length]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyleSheet(theme.STYLESHEET)

    # -- the store fixture: normal + INCOMPLETE + safety copy ---------
    work = ROOT / "main"
    work.mkdir()
    controller = Controller(config_path=work / "cfg" / "backup.json")
    window = make_window(app, controller)

    shoot(app, window, "restore-no-store", page=0)

    # Error state: opening a folder that is not a store.
    plain = work / "plain-folder"
    plain.mkdir()
    (plain / "hello.txt").write_text("just a folder", encoding="utf-8")
    window.store_path_edit.setText(str(plain))
    window.open_store_button.click()
    app.processEvents()
    shoot(app, window, "error-state", page=0)

    store_path = work / "store"
    controller.create_store(store_path)
    source = build_tree(work / "source")
    controller.take_snapshot(source, label="before the trip")
    pipe_tree = build_tree(work / "pipe-source")
    os.mkfifo(pipe_tree / "pipe")
    store = SnapshotStore.open(store_path)
    window.store_path_edit.setText(str(store_path))
    window.restore_panel.clear()
    create_snapshot(store, pipe_tree, label="the day of the pipe")
    # A safety copy: change a file, restore over it.
    snapshot_id = controller.list_snapshots()[0]["id"]
    (source / "docs" / "notes.txt").write_text("changed since", encoding="utf-8")
    controller.restore_selection(snapshot_id, source, ["docs/notes.txt"])
    window.refresh_snapshots()
    shoot(app, window, "restore-snapshot-list", page=0)

    item = window.snapshot_list.item(0)
    window.snapshot_list.setCurrentItem(item)
    app.processEvents()
    shoot(app, window, "restore-browse", page=0, scroll=450)

    dialog = RestoreConfirmDialog(
        window._manifest["date_text"],
        window._manifest["label"],
        "everything in the snapshot",
        str(source),
    )
    shoot_widget(app, dialog, "restore-confirm-dialog")

    dest = work / "brought-back"
    window._do_restore(None, str(dest))
    wait_until(app, lambda: window.restore_report.isVisible())
    shoot(app, window, "restore-report-complete", page=0, scroll=10**9)

    # Partial: damage one saved copy, restore again.
    manifest = controller.browse_snapshot(snapshot_id)
    photo = next(e for e in manifest["entries"] if e["path"] == "photo.bin")
    from lazarus.store import SnapshotStore as _Store  # objects live by hash

    blob = store.object_path(
        store.load_manifest(snapshot_id).entries[
            [e.path for e in store.load_manifest(snapshot_id).entries].index("photo.bin")
        ].sha256
    )
    blob.write_bytes(b"damaged beyond recognition")
    dest2 = work / "brought-back-again"
    window._do_restore(None, str(dest2))
    wait_until(
        app,
        lambda: window.restore_panel.isVisible()
        and window.restore_panel.title.text() == "Restore finished with problems",
    )
    shoot(app, window, "restore-report-partial", page=0, scroll=10**9)

    # Snapshots page, with the result of a fresh snapshot showing.
    window.snap_source_edit.setText(str(source))
    window.snap_label_edit.setText("after the trip")
    window.snap_button.click()
    wait_until(app, lambda: window.snap_report.isVisible())
    shoot(app, window, "snapshots-page", page=1)

    # -- deleted files: one of each verdict ---------------------------
    image = fat_image.create_fat16()
    strong = image.add_file("", "STRONG.TXT", b"whole and sound " * 20)
    image.delete(strong)
    partial_content = bytes((i % 250) + 1 for i in range(1000))
    partial = image.add_file("", "PARTLY.TXT", partial_content)
    partial_clusters = list(partial.clusters)
    image.delete(partial)
    filler = image.add_file("", "FILLER.TXT", b"f" * 100, forced_clusters=[partial_clusters[1]])
    gone = image.add_file("", "GONE.TXT", b"g" * 300)
    gone_clusters = list(gone.clusters)
    image.delete(gone)
    image.add_file("", "TAKER.TXT", b"t" * 300, forced_clusters=gone_clusters)
    drive_path = work / "drive.img"
    drive_path.write_bytes(bytes(image.data))
    window.scan_source_edit.setText(str(drive_path))
    window.scan_button.click()
    wait_until(app, lambda: window._scan_result is not None and len(window._scan_result["files"]) >= 3)
    window.scan_table.setCurrentCell(0, 0)
    app.processEvents()
    shoot(app, window, "undelete-results", page=2)

    # -- rescue, caught mid-run ----------------------------------------
    data = bytes((i % 255) + 1 for i in range(4096 * 14))
    reader = SlowReader(data)
    image_dest = work / "rescued.bin"
    map_path = Path(str(image_dest) + ".map.json")
    window.rescue_source_edit.setText("the failing drive (a rehearsal drive)")
    window.rescue_dest_edit.setText(str(image_dest))
    window.rescue_map_edit.setText(str(map_path))
    window._rescue_map_path = str(map_path)
    window.rescue_progress.show()
    window.run_worker(
        lambda: rescue_to_image(reader, image_dest, map_path=map_path, block_size=4096),
        [window.rescue_button],
        lambda result: None,
        window.deleted_panel,
    )
    window._rescue_timer.start()
    wait_until(
        app,
        lambda: "of 14 stretch" in window.rescue_progress_label.text()
        and not window.rescue_progress_label.text().startswith("14 of"),
        timeout=20,
    )
    shoot(app, window, "rescue-progress", page=2, scroll=10**9)
    wait_until(app, lambda: not window._workers, timeout=30)
    window._rescue_timer.stop()

    # -- locked files -----------------------------------------------------
    volumes = [
        VolumeInfo(ref="E:", kind="bitlocker", label="Backup Drive", locked=True, evidence=[]),
        VolumeInfo(ref="F:", kind="bitlocker", label="Photos", locked=False, evidence=[]),
        VolumeInfo(ref="G:", kind="unknown", label="", locked=None, evidence=[]),
    ]
    controller.platform = FakePlatform(volumes)
    window.refresh_volumes()
    wait_until(app, lambda: window.volumes_table.rowCount() == 3)
    shoot(app, window, "locked-volumes", page=3)

    key_dialog = KeyDialog("E:")
    shoot_widget(app, key_dialog, "unlock-key-dialog")

    locked_folder = work / "locked-files"
    locked_folder.mkdir()
    (locked_folder / "_readme.txt").write_text("your files are locked", encoding="utf-8")
    (locked_folder / "letter.djvu").write_bytes(b"locked")
    (locked_folder / "photo.djvu").write_bytes(b"locked")
    window.locked_folder_edit.setText(str(locked_folder))
    window.identify_button.click()
    wait_until(app, lambda: window.identify_text.toPlainText() != "")
    shoot(app, window, "identify-results", page=3, scroll=350)

    entry = controller.resolve_decrypt_family(locked_folder)["entry"]
    work_root = work / "decrypt-work"
    program = work / "decryptor-program.bin"
    program.write_bytes(b"the published program")
    confirm = ConfirmDialog(
        "Run this decryptor on copies?",
        (
            f"Lazarus will run {entry.decryptor_name} (published by "
            f"{entry.publisher}, for {entry.family}) on COPIES of the "
            f"files in {locked_folder}, inside the work folder {work_root}. "
            f"The program you chose is the one that runs. Your original "
            "files are never handed to it, and they are checked before "
            "and after so the report can say they did not change."
        ),
        "Run it on copies",
        danger=True,
    )
    shoot_widget(app, confirm, "decrypt-confirm")

    window.assess_edit.setText(str(locked_folder))
    window.assess_button.click()
    wait_until(app, lambda: window.assess_text.toPlainText() != "")
    shoot(app, window, "assess-results", page=3, scroll=10**9)
    wait_until(app, lambda: not window._workers)
    window.close()

    # -- backup states ------------------------------------------------------
    def backup_window(name: str, mutate=None, run: bool = True) -> MainWindow:
        folder = ROOT / name
        folder.mkdir()
        ctl = Controller(config_path=folder / "cfg" / "backup.json")
        src = build_tree(folder / "source")
        ctl.backup_setup(folder / "backup-store", [src])
        if run:
            ctl.run_backup_now()
        if mutate is not None:
            mutate(ctl, folder)
        win = make_window(app, ctl)
        win.refresh_backup()
        app.processEvents()
        return win

    win = backup_window("ready")
    shoot(app, win, "backup-status-ready", page=4)
    shoot(app, win, "backup-history", page=4, scroll=10**9)
    wait_until(app, lambda: not win._workers)
    win.close()

    win = backup_window(
        "missing", mutate=lambda ctl, folder: shutil.rmtree(folder / "backup-store")
    )
    shoot(app, win, "backup-status-target-missing", page=4)
    wait_until(app, lambda: not win._workers)
    win.close()

    def damage_store(ctl, folder):
        shutil.rmtree(folder / "backup-store")
        (folder / "backup-store").mkdir()
        (folder / "backup-store" / "junk.txt").write_text("not a store", encoding="utf-8")

    win = backup_window("damaged", mutate=damage_store)
    shoot(app, win, "backup-status-store-damaged", page=4)
    wait_until(app, lambda: not win._workers)
    win.close()

    def age_history(ctl, folder):
        history_file = history_path_for(ctl.config_path)
        history = load_history(history_file)
        history_file.unlink()
        old = format_utc(utc_now() - timedelta(days=3))
        for entry in history.entries:
            append_history(history_file, replace(entry, started_utc=old, finished_utc=old))

    win = backup_window("overdue", mutate=age_history)
    shoot(app, win, "backup-status-overdue", page=4)
    wait_until(app, lambda: not win._workers)
    win.close()

    folder = ROOT / "setup-form"
    folder.mkdir()
    ctl = Controller(config_path=folder / "cfg" / "backup.json")
    win = make_window(app, ctl)
    win.backup_store_edit.setText(str(folder / "backup-store"))
    win.backup_sources_list.addItem(str(build_tree(folder / "source")))
    shoot(app, win, "backup-setup-form", page=4)
    wait_until(app, lambda: not win._workers)
    win.close()

    write_contrast_report()
    print(f"\n{len(RENDERED)} states rendered into {OUT}")
    return 0


def _is_purple(red: int, green: int, blue: int) -> bool:
    return blue > red + 16 and blue > green + 16 and red > green


def pixel_audit(path: Path) -> tuple[int, int, list[tuple[str, int]]]:
    image = QImage(str(path))
    counts: dict[tuple[int, int, int], int] = {}
    purple = 0
    total = 0
    step = 2
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            colour = image.pixelColor(x, y)
            red, green, blue = colour.red(), colour.green(), colour.blue()
            total += 1
            if _is_purple(red, green, blue):
                purple += 1
            key = (red // 32 * 32, green // 32 * 32, blue // 32 * 32)
            counts[key] = counts.get(key, 0) + 1
    dominant = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    dominant_text = [
        (f"#{r:02x}{g:02x}{b:02x}", round(100 * n / max(total, 1), 1))
        for (r, g, b), n in dominant
    ]
    return purple, total, dominant_text


def write_contrast_report() -> None:
    lines = [
        "# Lazarus Phase 5 — contrast and pixel audit",
        "",
        "Every text/background pairing the window uses, computed with",
        "the WCAG contrast function in `lazarus/ui/theme.py`. The AA",
        "floor for ordinary text is 4.5:1.",
        "",
        "| Foreground | Background | Where | Ratio |",
        "|---|---|---|---|",
    ]
    for foreground, background, where in theme.PAIRINGS:
        ratio = theme.contrast_ratio(foreground, background)
        lines.append(f"| {foreground} | {background} | {where} | {ratio:.2f}:1 |")
    lines += [
        "",
        "## Pixel audit",
        "",
        "Each PNG sampled every second pixel. A pixel counts as",
        "near-purple when blue beats red and green by more than 16",
        "and red beats green (the purple/magenta family). Counts are",
        "reported exactly as measured, anti-aliasing fringes included.",
        "",
        "| State | Near-purple pixels | Sampled | Dominant colours (quantised, %) |",
        "|---|---|---|---|",
    ]
    for name in RENDERED:
        purple, total, dominant = pixel_audit(OUT / f"{name}.png")
        dominant_text = ", ".join(f"{colour} {pct}%" for colour, pct in dominant)
        lines.append(f"| {name} | {purple} | {total} | {dominant_text} |")
    (OUT / "CONTRAST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote CONTRAST.md")


if __name__ == "__main__":
    sys.exit(main())
