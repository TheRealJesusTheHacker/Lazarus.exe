"""PyInstaller entry point for the ONE Lazarus.exe.

Kept outside the package so PyInstaller's analysis starts from a
plain script. Two jobs:

* ``--self-check`` — the release smoke test. It builds the real
  interface offscreen (no window is shown), waits for the
  window's startup work to settle — building the window starts
  an initial volume refresh on a worker thread, so it drains
  the Qt event loop, for up to
  ``_SELF_CHECK_DRAIN_SECONDS``, until no jobs are running —
  then closes it, and answers with its exit code alone: 0 when
  the packaged program can genuinely start, 1 when anything at
  all went wrong, including a startup that never settles within
  the deadline. Closing without that drain would face the
  window's "a job is still running" confirmation, which a
  self-check has no one to answer, so the drain is part of the
  check, not a way around it. The exe is windowed, so it prints
  nothing and no one should expect output — the exit code is
  the signal.
* Everything else is handed, untouched, to
  :func:`lazarus.ui.app.main` — including the
  ``--lazarus-install`` / ``--lazarus-uninstall`` runs the
  self-install flow relaunches elevated.
"""

import os
import sys
import time

#: Names the interface module may give its main-window builder, in
#: the order the self-check tries them: a factory function first,
#: then the window class itself. The self-check exists to prove
#: the packaged program can build its real window, so it must use
#: the same construction the program's own entry point uses.
_WINDOW_FACTORIES = ("create_main_window", "build_main_window", "create_window")
_WINDOW_CLASSES = ("MainWindow", "LazarusMainWindow", "LazarusWindow")

#: How long the self-check waits for a freshly built window's
#: jobs to drain before it calls that startup a failure.
_SELF_CHECK_DRAIN_SECONDS = 15.0

#: Pause between event-loop pumps while draining.
_SELF_CHECK_POLL_SECONDS = 0.02


def _build_main_window(app_module):
    """Build the interface's main window, hidden, and return it."""
    for name in _WINDOW_FACTORIES + _WINDOW_CLASSES:
        builder = getattr(app_module, name, None)
        if callable(builder):
            return builder()
    raise AttributeError(
        "lazarus.ui.app offers no main-window builder the "
        "self-check can construct"
    )


def _self_check() -> int:
    """Prove the packaged program starts. Exit code is the report."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        import lazarus  # noqa: F401 — the package itself must load
        import lazarus.ui.app as app_module

        from PySide6.QtWidgets import QApplication

        application = QApplication([sys.argv[0]])
        window = _build_main_window(app_module)
        if window is not None:
            jobs_running = getattr(window, "jobs_running", None)
            if callable(jobs_running):
                deadline = time.monotonic() + _SELF_CHECK_DRAIN_SECONDS
                while jobs_running():
                    if time.monotonic() >= deadline:
                        # The verdict is already decided (this
                        # startup never settled: fail), the checker
                        # holds no state to clean up, and a graceful
                        # close is precisely what the stuck worker
                        # prevents — returning would let teardown
                        # destroy the still-running thread and abort
                        # instead of exiting 1. Exit immediately.
                        os._exit(1)
                    application.processEvents()
                    time.sleep(_SELF_CHECK_POLL_SECONDS)
            window.close()
        application.quit()
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv[1:]:
        sys.exit(_self_check())
    from lazarus.ui.app import main

    sys.exit(main(sys.argv[1:]))
