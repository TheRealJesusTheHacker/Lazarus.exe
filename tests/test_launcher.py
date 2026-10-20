"""The launcher's self-check: it drains startup work, then closes.

``packaging/launch_lazarus.py`` is loaded by file path (it lives
outside the package, as PyInstaller's entry point). It is
import-safe: loading it runs nothing. These tests stub its
window builder and its QApplication so the drain itself — the
F-17 fix — is what is measured, offscreen and fast.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_LAUNCHER_PATH = (
    Path(__file__).resolve().parent.parent / "packaging" / "launch_lazarus.py"
)


def _load_launcher():
    spec = importlib.util.spec_from_file_location("launch_lazarus", _LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeApplication:
    def __init__(self, argv):
        self.argv = argv
        self.process_calls = 0
        self.quit_called = False

    def processEvents(self):
        self.process_calls += 1

    def quit(self):
        self.quit_called = True


@pytest.fixture()
def launcher(monkeypatch):
    module = _load_launcher()
    monkeypatch.setattr("PySide6.QtWidgets.QApplication", _FakeApplication)
    return module


class _DrainingWindow:
    """jobs_running() flips to False after a few polls."""

    def __init__(self):
        self.polls = 0
        self.closed = False

    def jobs_running(self):
        self.polls += 1
        return self.polls <= 3

    def close(self):
        self.closed = True


class _StuckWindow:
    """jobs_running() never clears."""

    def __init__(self):
        self.closed = False

    def jobs_running(self):
        return True

    def close(self):
        self.closed = True


class _PlainWindow:
    """No jobs_running at all — the duck-type skip."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_self_check_drains_startup_jobs_then_closes_and_returns_0(
    launcher, monkeypatch
):
    stub = _DrainingWindow()
    monkeypatch.setattr(launcher, "_build_main_window", lambda module: stub)
    assert launcher._self_check() == 0
    assert stub.closed is True
    # The drain really polled until the stub settled.
    assert stub.polls >= 4


class _ExitSentinel(BaseException):
    """Raised by the fake os._exit so the test process survives.

    BaseException, not Exception, so _self_check's own
    ``except Exception`` cannot swallow it and turn the exit
    back into an ordinary return.
    """


def test_self_check_returns_1_when_startup_jobs_never_settle(
    launcher, monkeypatch
):
    stub = _StuckWindow()
    monkeypatch.setattr(launcher, "_build_main_window", lambda module: stub)
    # Shrink the 15s deadline so the failure is quick to prove.
    monkeypatch.setattr(launcher, "_SELF_CHECK_DRAIN_SECONDS", 0.2)
    # N3-02: on the timeout path the verdict (fail) is already
    # decided, so the launcher exits the process immediately
    # with os._exit(1) instead of returning 1 and letting
    # teardown destroy the still-running thread (SIGABRT).
    # Patch os._exit in the launcher module's namespace to
    # record the call and raise, so this process survives.
    exit_calls = []

    def fake_exit(code):
        exit_calls.append(code)
        raise _ExitSentinel

    monkeypatch.setattr(launcher.os, "_exit", fake_exit)
    with pytest.raises(_ExitSentinel):
        launcher._self_check()
    # os._exit was invoked with exactly 1, close() was never
    # called, and the ordinary return-1 path was not reached
    # (the sentinel propagated out of _self_check instead).
    assert exit_calls == [1]
    assert stub.closed is False


def test_self_check_skips_the_wait_when_the_window_has_no_jobs_running(
    launcher, monkeypatch
):
    stub = _PlainWindow()
    monkeypatch.setattr(launcher, "_build_main_window", lambda module: stub)
    assert launcher._self_check() == 0
    assert stub.closed is True
