"""Data model for the snapshot store.

A snapshot is a manifest: a dated list of everything a folder (or a
single file) held at one moment. File *contents* live once in the
store's object area, named by their SHA-256 hash; the manifest only
records which hash belongs at which path. That split is what lets
Phase 4 backups add new restore points cheaply, and what a later
encryption layer (Aegis) can seal without reshaping the store.

All paths inside a manifest are relative and use forward slashes on
every platform, so a store written on one machine reads the same on
another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MANIFEST_FORMAT = 1
STORE_FORMAT = 1

KIND_SNAPSHOT = "snapshot"
KIND_PRE_RESTORE = "pre-restore"

TYPE_FILE = "file"
TYPE_DIR = "dir"
TYPE_SYMLINK = "symlink"


@dataclass
class FileEntry:
    """One item inside a snapshot."""

    path: str  # relative, forward slashes, no leading slash, never ".."
    type: str  # TYPE_FILE | TYPE_DIR | TYPE_SYMLINK
    size: int = 0  # files only
    sha256: Optional[str] = None  # files only
    mtime_ns: Optional[int] = None
    target: Optional[str] = None  # symlinks only: the link target as recorded

    def to_dict(self) -> dict:
        data: dict = {"path": self.path, "type": self.type}
        if self.type == TYPE_FILE:
            data["size"] = self.size
            data["sha256"] = self.sha256
        if self.mtime_ns is not None:
            data["mtime_ns"] = self.mtime_ns
        if self.type == TYPE_SYMLINK:
            data["target"] = self.target
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "FileEntry":
        # A manifest is data, and data can be damaged or tampered
        # with. Wrong-typed fields are rejected here, at load, so a
        # bad record fails as "damaged" instead of exploding later in
        # some other module's hands.
        if not isinstance(data, dict):
            raise TypeError("a snapshot entry is not a record")
        path = data.get("path")
        entry_type = data.get("type")
        if not isinstance(path, str):
            raise TypeError("a snapshot entry has no usable path")
        if entry_type not in (TYPE_FILE, TYPE_DIR, TYPE_SYMLINK):
            raise ValueError(f"unknown snapshot entry type: {entry_type!r}")
        size = data.get("size", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise TypeError("a snapshot entry has an unusable size")
        sha256 = data.get("sha256")
        if sha256 is not None and not isinstance(sha256, str):
            raise TypeError("a snapshot entry has an unusable content hash")
        if entry_type == TYPE_FILE and sha256 is None:
            raise ValueError("a file entry is missing its content hash")
        mtime_ns = data.get("mtime_ns")
        if mtime_ns is not None and (
            not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool)
        ):
            raise TypeError("a snapshot entry has an unusable timestamp")
        target = data.get("target")
        if target is not None and not isinstance(target, str):
            raise TypeError("a shortcut entry has an unusable target")
        if entry_type == TYPE_SYMLINK and target is None:
            raise ValueError("a shortcut entry is missing its target")
        return cls(
            path=path,
            type=entry_type,
            size=size,
            sha256=sha256,
            mtime_ns=mtime_ns,
            target=target,
        )


@dataclass
class Problem:
    """Something that did not go perfectly, in plain language."""

    path: str  # manifest-relative path, or "" for the snapshot as a whole
    message: str

    def to_dict(self) -> dict:
        return {"path": self.path, "message": self.message}

    @classmethod
    def from_dict(cls, data: dict) -> "Problem":
        return cls(path=data.get("path", ""), message=data.get("message", ""))


@dataclass
class SnapshotManifest:
    """The full record of one snapshot."""

    id: str
    kind: str  # KIND_SNAPSHOT | KIND_PRE_RESTORE
    label: str
    created_utc: str  # ISO-8601, UTC, e.g. 2026-10-20T07:30:00.123456Z
    tool_version: str
    source_root: Optional[str]  # absolute path the snapshot was taken from
    root_name: str  # display name of the snapshotted file/folder
    complete: bool  # False when some items could not be copied in
    entries: list[FileEntry] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    protects: Optional[dict] = None  # pre-restore snapshots: what they guard

    @property
    def file_count(self) -> int:
        return sum(1 for e in self.entries if e.type == TYPE_FILE)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries if e.type == TYPE_FILE)

    def to_dict(self) -> dict:
        return {
            "format": MANIFEST_FORMAT,
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "created_utc": self.created_utc,
            "tool_version": self.tool_version,
            "source_root": self.source_root,
            "root_name": self.root_name,
            "complete": self.complete,
            "entries": [e.to_dict() for e in self.entries],
            "problems": [p.to_dict() for p in self.problems],
            "protects": self.protects,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SnapshotManifest":
        if not isinstance(data, dict):
            raise TypeError("a snapshot record is not a record")
        snapshot_id = data.get("id")
        created_utc = data.get("created_utc")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise TypeError("a snapshot record has no usable id")
        if not isinstance(created_utc, str) or not created_utc:
            raise TypeError("a snapshot record has no usable date")
        label = data.get("label", "")
        if not isinstance(label, str):
            raise TypeError("a snapshot record has an unusable label")
        kind = data.get("kind", KIND_SNAPSHOT)
        if not isinstance(kind, str):
            raise TypeError("a snapshot record has an unusable kind")
        source_root = data.get("source_root")
        if source_root is not None and not isinstance(source_root, str):
            raise TypeError("a snapshot record has an unusable source")
        root_name = data.get("root_name", "")
        if not isinstance(root_name, str):
            raise TypeError("a snapshot record has an unusable name")
        raw_entries = data.get("entries", [])
        raw_problems = data.get("problems", [])
        if not isinstance(raw_entries, list) or not isinstance(raw_problems, list):
            raise TypeError("a snapshot record has unusable contents")
        protects = data.get("protects")
        if protects is not None and not isinstance(protects, dict):
            raise TypeError("a snapshot record has an unusable guard note")
        return cls(
            id=snapshot_id,
            kind=kind,
            label=label,
            created_utc=created_utc,
            tool_version=data.get("tool_version", "unknown"),
            source_root=source_root,
            root_name=root_name,
            complete=bool(data.get("complete", True)),
            entries=[FileEntry.from_dict(e) for e in raw_entries],
            problems=[Problem.from_dict(p) for p in raw_problems],
            protects=protects,
        )


@dataclass
class SnapshotInfo:
    """A snapshot as the index describes it — what listing shows."""

    id: str
    kind: str
    label: str
    created_utc: str
    complete: bool
    file_count: int
    total_bytes: int
    has_manifest: bool = True  # False = the save never finished

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "created_utc": self.created_utc,
            "complete": self.complete,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }

    @classmethod
    def from_manifest(cls, manifest: SnapshotManifest) -> "SnapshotInfo":
        return cls(
            id=manifest.id,
            kind=manifest.kind,
            label=manifest.label,
            created_utc=manifest.created_utc,
            complete=manifest.complete,
            file_count=manifest.file_count,
            total_bytes=manifest.total_bytes,
            has_manifest=True,
        )


def is_safe_relative_path(path: object) -> bool:
    """True if ``path`` is a manifest path that cannot escape its root.

    This is the store's path-traversal guard: a manifest (which is
    data, and could in principle be tampered with) must never be able
    to aim a restore at files outside the folder the user chose.
    """
    if not isinstance(path, str) or not path:
        return False
    if "\\" in path or "\x00" in path:
        # Backslashes are path separators on Windows (the target
        # platform) even though Linux treats them as ordinary
        # characters; a NUL byte is never legitimate in a path.
        return False
    if path.startswith("/") or ":" in path.split("/")[0]:
        return False  # absolute path or drive-qualified
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return False
    return True
