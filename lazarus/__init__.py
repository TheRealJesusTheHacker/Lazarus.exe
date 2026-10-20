"""LAZARUS.EXE — Sentinel Safeguard LLC backup, restore & file recovery.

Dead isn't gone. Lazarus brings files, folders, and whole systems
back from deletion, drive failure, and lockout — restored to the
day you choose, with honest odds stated before it makes a promise.

Phase 1 is the snapshot store and the restore engine: dated,
versioned snapshots of files and folders, point-in-time restore of a
file, a folder, or everything, a safety snapshot taken before any
restore overwrites anything, and a plain-language report — verified
by hash — at the end of every restore.

Phase 2 is undelete and drive rescue: scanning FAT and NTFS drives
and images for deleted files and weighing the evidence for each one
in the open (likely whole, partly there, or gone), recovering them
onto a different drive than the one being read, and cloning failing
drives to images gently — bad stretches skipped fast and returned
to later, with a progress map that lets a rescue stop and resume.
"""

from ._version import __version__
from .errors import LazarusError
from .rescue import rescue_to_image
from .restore import restore
from .snapshot import create_snapshot
from .store import SnapshotStore
from .undelete import recover_deleted, scan_for_deleted
from .verify import verify_snapshot, verify_store

__all__ = [
    "LazarusError",
    "SnapshotStore",
    "create_snapshot",
    "restore",
    "verify_snapshot",
    "verify_store",
    "scan_for_deleted",
    "recover_deleted",
    "rescue_to_image",
    "__version__",
]
