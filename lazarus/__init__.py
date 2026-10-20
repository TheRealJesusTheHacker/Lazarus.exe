"""LAZARUS.EXE — Sentinel Safeguard LLC backup, restore & file recovery.

Dead isn't gone. Lazarus brings files, folders, and whole systems
back from deletion, drive failure, and lockout — restored to the
day you choose, with honest odds stated before it makes a promise.

Phase 1 is the snapshot store and the restore engine: dated,
versioned snapshots of files and folders, point-in-time restore of a
file, a folder, or everything, a safety snapshot taken before any
restore overwrites anything, and a plain-language report — verified
by hash — at the end of every restore.
"""

from ._version import __version__
from .errors import LazarusError
from .restore import restore
from .snapshot import create_snapshot
from .store import SnapshotStore
from .verify import verify_snapshot, verify_store

__all__ = [
    "LazarusError",
    "SnapshotStore",
    "create_snapshot",
    "restore",
    "verify_snapshot",
    "verify_store",
    "__version__",
]
