"""The snapshot store: an on-disk home for dated, restorable versions.

Layout (restore-first, and friendly to later phases)::

    <store>/
      store.json            what this store is, and when it was made
      index.json            summary list of snapshots (a cache; the
                            manifests on disk are the real truth)
      objects/
        ab/<sha256>         file contents, named by their hash, kept
                            once no matter how many snapshots use them
      snapshots/
        <snapshot-id>/
          manifest.json     the dated list of what was saved

Everything Lazarus writes here is written atomically: data lands in a
temporary file first and is moved into place in one step, so a crash
or a pulled plug can leave a snapshot unfinished — never half-written
in a way that looks finished. Unfinished snapshots are detected and
reported honestly instead of being trusted.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional

from ._version import __version__
from .errors import (
    SnapshotNotFoundError,
    StoreError,
    StoreExistsError,
)
from .hashing import fsync_dir, fsync_file
from .model import (
    STORE_FORMAT,
    SnapshotInfo,
    SnapshotManifest,
)

STORE_FILE = "store.json"
INDEX_FILE = "index.json"
OBJECTS_DIR = "objects"
SNAPSHOTS_DIR = "snapshots"
MANIFEST_FILE = "manifest.json"

_TMP_PREFIX = ".lazarus-tmp-"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp (or plain date) into an aware UTC time.

    A plain date means the end of that day: asking for "Tuesday's
    version" should find the last snapshot taken on Tuesday.
    """
    cleaned = text.strip()
    try:
        if "T" not in cleaned and " " not in cleaned:
            day = datetime.fromisoformat(cleaned).date()
            return datetime.combine(day, time.max, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        raise StoreError(
            f"'{text}' is not a date Lazarus can read. "
            "Use a date like 2026-10-18, or a full time like 2026-10-18T14:30:00Z."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def new_snapshot_id(moment: Optional[datetime] = None) -> str:
    """Sortable, collision-proof snapshot id: 20261020-073015-1a2b3c4d."""
    moment = moment or utc_now()
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(4)}"


def is_valid_snapshot_id(snapshot_id: object) -> bool:
    """True if ``snapshot_id`` is a plain folder name, nothing more.

    Ids arrive from users (and, in principle, from tampered data) and
    get joined into paths. Anything with a separator, a drive, or a
    dot-segment could aim that join outside the snapshots folder.
    """
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return False
    if "/" in snapshot_id or "\\" in snapshot_id or "\x00" in snapshot_id:
        return False
    if snapshot_id in (".", ".."):
        return False
    return True


_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def safe_instant(text: object) -> datetime:
    """The instant a timestamp names, or the earliest time if unreadable."""
    if not isinstance(text, str) or not text:
        return _EARLIEST
    try:
        return parse_utc(text)
    except StoreError:
        return _EARLIEST


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes to ``path`` in one atomic step."""
    tmp = path.with_name(_TMP_PREFIX + secrets.token_hex(6))
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    fsync_dir(path.parent)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_bytes(path, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))


def read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise StoreError(f"'{path}' is missing.") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise StoreError(
            f"'{path}' could not be read. It may be damaged; "
            "Lazarus will not guess at what it held."
        ) from None
    if not isinstance(data, dict):
        raise StoreError(f"'{path}' does not hold the record Lazarus expected.")
    return data


class SnapshotStore:
    """An opened snapshot store."""

    def __init__(self, root: Path, meta: dict):
        self.root = root
        self.meta = meta

    # -- paths ------------------------------------------------------

    @property
    def objects_dir(self) -> Path:
        return self.root / OBJECTS_DIR

    @property
    def snapshots_dir(self) -> Path:
        return self.root / SNAPSHOTS_DIR

    def object_path(self, sha256: str) -> Path:
        return self.objects_dir / sha256[:2] / sha256

    def snapshot_dir(self, snapshot_id: str) -> Path:
        if not is_valid_snapshot_id(snapshot_id):
            raise SnapshotNotFoundError(
                f"There is no finished snapshot called '{snapshot_id}' in this store."
            )
        resolved = (self.snapshots_dir / snapshot_id).resolve()
        if self.snapshots_dir.resolve() not in resolved.parents:
            raise SnapshotNotFoundError(
                f"There is no finished snapshot called '{snapshot_id}' in this store."
            )
        return self.snapshots_dir / snapshot_id

    def manifest_path(self, snapshot_id: str) -> Path:
        return self.snapshot_dir(snapshot_id) / MANIFEST_FILE

    # -- opening and creating ---------------------------------------

    @classmethod
    def create(cls, root: Path) -> "SnapshotStore":
        root = Path(root)
        if (root / STORE_FILE).exists():
            raise StoreExistsError(
                f"There is already a Lazarus store at '{root}'. "
                "Open it instead of making a new one."
            )
        if root.exists() and not root.is_dir():
            raise StoreExistsError(
                f"'{root}' is a file, not a folder. "
                "Pick a folder for the store."
            )
        if root.exists():
            try:
                has_contents = any(root.iterdir())
            except OSError:
                raise StoreError(
                    f"Lazarus could not look inside '{root}'. "
                    "Check the drive is connected and the folder can be read."
                ) from None
            if has_contents:
                raise StoreExistsError(
                    f"'{root}' already holds other files. "
                    "Pick an empty folder for the store so backups never mix with them."
                )
        try:
            (root / OBJECTS_DIR).mkdir(parents=True, exist_ok=True)
            (root / SNAPSHOTS_DIR).mkdir(parents=True, exist_ok=True)
        except OSError:
            raise StoreError(
                f"Lazarus could not create a store at '{root}'. "
                "Check the drive is connected and the folder can be written to."
            ) from None
        meta = {
            "format": STORE_FORMAT,
            "id": secrets.token_hex(16),
            "created_utc": format_utc(utc_now()),
            "tool_version": __version__,
            "product": "LAZARUS.EXE",
        }
        atomic_write_json(root / STORE_FILE, meta)
        atomic_write_json(root / INDEX_FILE, {"format": STORE_FORMAT, "snapshots": []})
        return cls(root.resolve(), meta)

    @classmethod
    def open(cls, root: Path) -> "SnapshotStore":
        root = Path(root)
        if not root.is_dir():
            raise StoreError(
                f"There is no folder at '{root}'. "
                "If the store lives on an external drive, check it is plugged in."
            )
        if not (root / STORE_FILE).exists():
            raise StoreError(
                f"'{root}' is not a Lazarus store. "
                "Point Lazarus at the folder that holds the store."
            )
        meta = read_json(root / STORE_FILE)
        if meta.get("format") != STORE_FORMAT:
            raise StoreError(
                f"The store at '{root}' was made in a format Lazarus "
                "does not understand. It may belong to a different version."
            )
        for part in (OBJECTS_DIR, SNAPSHOTS_DIR):
            if not (root / part).is_dir():
                raise StoreError(
                    f"Part of the store at '{root}' is missing: the "
                    f"'{part}' folder is gone. The store is damaged; "
                    "Lazarus will not pretend otherwise."
                )
        return cls(root.resolve(), meta)

    # -- manifests and the index ------------------------------------

    def save_manifest(self, manifest: SnapshotManifest) -> None:
        folder = self.snapshot_dir(manifest.id)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise StoreError(
                f"Lazarus could not save the snapshot record in '{self.root}'. "
                "Check the drive is connected and not full."
            ) from None
        atomic_write_json(self.manifest_path(manifest.id), manifest.to_dict())
        fsync_file(self.manifest_path(manifest.id))
        self._refresh_index_entry(manifest)

    def load_manifest(self, snapshot_id: str) -> SnapshotManifest:
        path = self.manifest_path(snapshot_id)
        if not path.exists():
            raise SnapshotNotFoundError(
                f"There is no finished snapshot called '{snapshot_id}' in this store."
            )
        data = read_json(path)
        try:
            return SnapshotManifest.from_dict(data)
        except (KeyError, TypeError, ValueError):
            raise StoreError(
                f"The record for snapshot '{snapshot_id}' is damaged and cannot be read. "
                "Lazarus will not restore from a record it cannot trust."
            ) from None

    def _refresh_index_entry(self, manifest: SnapshotManifest) -> None:
        """Rewrite the index from the manifests on disk.

        The index is only a cache — the manifests are the truth — so
        it is rebuilt wholesale rather than patched. A damaged index
        can therefore never crash a snapshot or quietly mis-describe
        the store; it is simply replaced with what the disk says.
        """
        index_path = self.root / INDEX_FILE
        try:
            entries = [
                info.to_dict()
                for info in self.list_snapshots()
                if info.has_manifest
            ]
        except StoreError:
            entries = [SnapshotInfo.from_manifest(manifest).to_dict()]
        atomic_write_json(index_path, {"format": STORE_FORMAT, "snapshots": entries})

    # -- listing and choosing ---------------------------------------

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Every snapshot on disk, oldest first.

        The manifests are the truth; a snapshot folder without a
        readable manifest is listed as unfinished rather than hidden.
        """
        infos: list[SnapshotInfo] = []
        base = self.snapshots_dir
        if not base.is_dir():
            return infos
        for folder in sorted(base.iterdir()):
            if not folder.is_dir():
                continue
            manifest_file = folder / MANIFEST_FILE
            if not manifest_file.exists():
                infos.append(
                    SnapshotInfo(
                        id=folder.name,
                        kind="unknown",
                        label="",
                        created_utc="",
                        complete=False,
                        file_count=0,
                        total_bytes=0,
                        has_manifest=False,
                    )
                )
                continue
            try:
                manifest = self.load_manifest(folder.name)
            except StoreError:
                infos.append(
                    SnapshotInfo(
                        id=folder.name,
                        kind="unknown",
                        label="",
                        created_utc="",
                        complete=False,
                        file_count=0,
                        total_bytes=0,
                        has_manifest=False,
                    )
                )
                continue
            infos.append(SnapshotInfo.from_manifest(manifest))
        # Order by the instant each snapshot was taken, not by the
        # spelling of its timestamp: two records written in different
        # UTC offsets must still sort into true time order.
        infos.sort(key=lambda i: (safe_instant(i.created_utc), i.id))
        return infos

    def resolve_snapshot(
        self,
        snapshot_id: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> SnapshotManifest:
        """Pick the snapshot to restore from: by id, by date, or latest.

        ``as_of`` chooses the newest finished snapshot taken at or
        before that moment — "how it was on Tuesday" is the last
        snapshot of Tuesday, not the first of Wednesday.
        """
        if snapshot_id is not None:
            manifest = self.load_manifest(snapshot_id)
            return manifest
        if as_of is not None:
            cutoff = parse_utc(as_of)
            candidates = [
                info
                for info in self.list_snapshots()
                if info.has_manifest and info.created_utc
                and safe_instant(info.created_utc) <= cutoff
            ]
            if not candidates:
                raise SnapshotNotFoundError(
                    f"No snapshot in this store was taken on or before {as_of}. "
                    "Pick a later date, or choose a snapshot from the list."
                )
            return self.load_manifest(candidates[-1].id)
        finished = [i for i in self.list_snapshots() if i.has_manifest]
        if not finished:
            raise SnapshotNotFoundError(
                "This store holds no finished snapshots yet. Take one first."
            )
        return self.load_manifest(finished[-1].id)
