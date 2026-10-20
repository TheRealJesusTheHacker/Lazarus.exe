"""Read-only access to drives and drive images.

Everything Phase 2 does — scanning for deleted files, cloning a
failing drive — starts here, with a source that can only be read.
The class below opens its file exactly once, with the operating
system told *read-only*, and simply has no method that writes. That
is the enforcement behind the safety law "recovery never writes to
the drive it is recovering from": not a promise in a manual, but a
seam the whole phase is built on, and one the test suite pins down.

A *source* is anything with a size that can hand back bytes by
offset (the :class:`BlockReader` protocol). Real drives and image
files come in through :class:`ReadOnlySource`; tests bring their
own failing, flaky, synthetic sources through the same door, which
is what makes a dying drive rehearsable on a healthy machine.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import SourceError


@runtime_checkable
class BlockReader(Protocol):
    """Anything Lazarus can read bytes from, by offset."""

    size: int

    def read_at(self, offset: int, length: int) -> bytes:
        """Bytes starting at ``offset`` — fewer than asked only at
        the very end of the source. Raises :class:`OSError` (or a
        Lazarus error) when the read itself fails."""
        ...


class ReadOnlySource:
    """A file or device opened read-only, and never any other way.

    ``open_flags`` is kept on the instance so tests can prove the
    mode that was asked for, and :attr:`opened_read_only` states it
    in one checkable line.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise SourceError(
                f"Lazarus could not find {self.path}. "
                "Check the name and try again."
            )
        self.open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            self._fd = os.open(self.path, self.open_flags)
        except OSError as error:
            raise SourceError(
                f"Lazarus could not open {self.path} for reading: "
                f"{error.strerror or 'permission was refused'}."
            ) from error
        info = os.fstat(self._fd)
        if os.path.isdir(self.path) or (info.st_mode & 0o170000) == 0o040000:
            os.close(self._fd)
            raise SourceError(
                f"{self.path} is a folder, not a drive or a drive image. "
                "Point Lazarus at the drive or the image file itself."
            )
        self.size: int = info.st_size
        self.device_id: int = info.st_dev
        self.file_id: int = info.st_ino

    @property
    def opened_read_only(self) -> bool:
        """True — the only way this class ever opens anything."""
        # O_ACCMODE is the mask that picks the access mode out of
        # the open flags. Windows' os module does not name it, but
        # the access mode lives in the same low bits there, so the
        # mask's value stands in where the name is absent.
        return (self.open_flags & getattr(os, "O_ACCMODE", 0o3)) == os.O_RDONLY

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError("a read cannot start before the beginning")
        if length == 0 or offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        try:
            # Read at a position without depending on where the
            # file pointer sits. os.pread does exactly that where
            # it exists; Windows has no os.pread, so there the
            # pointer is moved first and the read follows it. This
            # source is only ever read from one thread at a time,
            # so the pointer cannot be moved under the read.
            pread = getattr(os, "pread", None)
            if pread is not None:
                return pread(self._fd, length, offset)
            os.lseek(self._fd, offset, os.SEEK_SET)
            return os.read(self._fd, length)
        except OSError as error:
            raise SourceError(
                f"Reading {self.path} failed at byte {offset:,}: "
                f"{error.strerror or 'the drive did not answer'}. "
                "If this is a failing drive, rescue it to an image "
                "first (lazarus rescue) and work from the image."
            ) from error

    def close(self) -> None:
        fd = getattr(self, "_fd", None)
        if fd is not None:
            self._fd = None
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "ReadOnlySource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def device_id_of(path: str | Path) -> int:
    """The device a path lives on — how "a different drive" is told
    from "the same drive" before recovery writes anything."""
    return os.stat(path).st_dev
