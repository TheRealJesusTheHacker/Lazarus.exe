"""Shared fixtures: a scratch store and a small sample tree."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from lazarus.store import SnapshotStore


@pytest.fixture()
def store(tmp_path):
    return SnapshotStore.create(tmp_path / "store")


@pytest.fixture()
def tree(tmp_path):
    """A small folder worth saving:

    docs/
      notes.txt   "remember the milk"
      report.txt  "quarterly numbers"
    photo.bin     (binary bytes)
    empty-dir/
    """
    root = tmp_path / "docs-root"
    (root / "docs" / "empty-dir").mkdir(parents=True)
    (root / "docs" / "notes.txt").write_text("remember the milk", encoding="utf-8")
    (root / "docs" / "report.txt").write_text("quarterly numbers", encoding="utf-8")
    (root / "photo.bin").write_bytes(bytes(range(256)) * 4)
    return root


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
