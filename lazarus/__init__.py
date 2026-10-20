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

Phase 3 is decrypt and unlock. A locked volume is opened with the
owner's own recovery key, through Windows' own unlock mechanisms —
Windows does the cryptography; Lazarus asks it once, with the one
key the owner hands it, and never retries keys. For files locked
by ransomware, Lazarus keeps a short, sourced list of the
decryptors researchers have published, reads a folder's outward
signs to say which family it looks like (never "confirmed" —
signs are not proof), and runs a published decryptor only on
copies inside a work area it prepared, hashing the originals
before and after so the report can say they never changed.

Phase 4 is backup automation: the folders that matter, saved into
the same versioned store on the owner's own schedule — by hand
with one click, or by the computer's own scheduler when a backup
comes due, with a missed moment owed exactly once as a catch-up.
Status is loud and honest, said before trouble matters, and the
tidying rule the owner chose is applied only after a run that
finished cleanly — a bad day is never when history gets thrown
out.

Phase 5 is the window and the wrapping: an animated PySide6
interface over the whole engine — restore leading, odds and
status as first-class screens — the self-installing one-file
Lazarus.exe, and the portable no-install edition, shipped
together as the first public release.
"""

from ._version import __version__
from .assess import assess_options
from .backup import backup_status, load_config, run_backup
from .decrypt_run import prepare_work_area, run_decryptor
from .decryptors import identify_family, load_registry
from .errors import LazarusError
from .rescue import rescue_to_image
from .restore import restore
from .retention import apply_retention, plan_retention
from .schedule import run_due_backup
from .snapshot import create_snapshot
from .store import SnapshotStore
from .undelete import recover_deleted, scan_for_deleted
from .unlock import unlock_volume
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
    "unlock_volume",
    "load_registry",
    "identify_family",
    "prepare_work_area",
    "run_decryptor",
    "assess_options",
    "run_backup",
    "backup_status",
    "load_config",
    "plan_retention",
    "apply_retention",
    "run_due_backup",
    "__version__",
]
