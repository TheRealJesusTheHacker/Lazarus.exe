"""Errors Lazarus raises on purpose.

Every message here is written in plain language. These are the words a
user (or a later interface) can show as-is: what happened, and where
possible what to do next. Raw operating-system errors are translated
at the module boundaries and never leak through as user-facing text.
"""


class LazarusError(Exception):
    """Base class for every error Lazarus raises deliberately."""


class StoreError(LazarusError):
    """The snapshot store is missing, unreadable, or not a store."""


class StoreExistsError(StoreError):
    """A store (or something else) already sits at the target path."""


class SnapshotNotFoundError(LazarusError):
    """The requested snapshot does not exist in the store."""


class SnapshotIncompleteError(LazarusError):
    """The snapshot has no readable manifest; it never finished saving."""


class SourceNotFoundError(LazarusError):
    """The file or folder asked for is not there."""


class RestoreRefusedError(LazarusError):
    """A safety rule stopped the restore before anything was written."""


class SourceError(LazarusError):
    """A drive or drive image could not be opened or read."""


class UnsupportedFilesystemError(LazarusError):
    """The image holds a filesystem Lazarus cannot read yet."""


class UndeleteError(LazarusError):
    """A scan for deleted files could not be carried out."""


class RecoveryRefusedError(LazarusError):
    """A safety rule stopped a recovery before anything was written."""


class RescueError(LazarusError):
    """A drive rescue could not start, or had to stop."""


class NothingToRestoreError(LazarusError):
    """The chosen snapshot holds nothing matching what was asked for."""
