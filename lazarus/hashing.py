"""SHA-256 content hashing.

Content hashes are Lazarus's proof. A file's hash names its copy in
the store, decides whether a restore even needs to write, and is how
restored data is verified against the snapshot it came from.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def hash_bytes(data: bytes) -> str:
    """SHA-256 hex digest of an in-memory bytes object."""
    return hashlib.sha256(data).hexdigest()


def hash_stream(stream) -> str:
    """SHA-256 hex digest of everything readable from a binary stream."""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def hash_file(path: Path) -> str:
    """SHA-256 hex digest of a file's contents."""
    with open(path, "rb") as handle:
        return hash_stream(handle)


def hash_file_into(path: Path, out_path: Path) -> tuple[str, int]:
    """Copy a file to ``out_path`` while hashing it.

    Returns ``(sha256_hex, size_in_bytes)``. The destination is written
    exactly once, front to back, so the hash always describes the
    bytes that actually landed.
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as src, open(out_path, "wb") as dst:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            dst.write(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def is_valid_hash(value: object) -> bool:
    """True if ``value`` looks like a SHA-256 hex digest."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def fsync_file(path: Path) -> None:
    """Flush a file's bytes to disk (best effort, cross-platform)."""
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Flush a directory entry to disk where the platform allows it."""
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
