"""Reading deleted files from FAT drives.

FAT is the filesystem of memory cards, USB sticks, and older
drives, in three sizes that share one shape: a short opening record
(the BIOS parameter block) says how big each piece of the drive is,
one or more file allocation tables map which pieces follow which,
and folders are simply lists of 32-byte entries naming each file,
saying when it was last changed, how large it is, and where its
first piece sits.

Deleting a file on a FAT drive is a light touch, and that is what
makes recovery possible at all. The folder entry is marked with a
single erased byte, and the file's chain in the table is wiped —
but the data itself is left where it lay until something new is
handed those same pieces. So a scan walks every folder, gathers
the marked entries, and reports each one honestly: where its data
began, how much of that stretch the table now says is back in
use, and the plain fact that the stretch in between is an
assumption, because deletion erased the map that would prove it.

Nothing here writes to the source, guesses at a layout the
opening record does not support, or follows damaged records
forever. Loops, runaway chains, and absurd depths are stopped
by fixed limits, and every stop is reported in the scan's notes
in plain language.
"""

from __future__ import annotations

import struct

from .errors import UndeleteError
from .recovery_model import DeletedFile, Extent, FsScan
from .source import BlockReader

FS_FAMILY = "FAT"

_MAX_CHAIN_LENGTH = 100_000
_MAX_DEPTH = 16
_MAX_ENTRIES = 200_000

_CHAIN_EVIDENCE = (
    "When a file is deleted from a FAT drive, the map of which "
    "pieces it used is erased. Lazarus assumes it lay in one "
    "unbroken stretch starting at its first piece \u2014 the "
    "layout such files almost always have."
)
_FIRST_LETTER_EVIDENCE = (
    "the first letter of its name was erased when it was deleted"
)
_NO_START_EVIDENCE = (
    "where its data began is no longer recorded \u2014 the "
    "starting point was erased when it was deleted"
)
_PAST_END_EVIDENCE = (
    "The folder record says this file ran past the end of the "
    "drive, so Lazarus cut its stretch short at the drive's end."
)


def _safe_read(source: BlockReader, offset: int, length: int) -> bytes:
    """Bytes from the source, or nothing when the read fails.

    A short read, a failed read, and a read past the end all come
    back as fewer (or no) bytes; callers pad or stop as suits
    them. This helper never raises, which is what lets
    :func:`detect` keep its promise on any input at all.
    """
    if length <= 0 or offset < 0:
        return b""
    try:
        data = source.read_at(offset, length)
    except Exception:
        return b""
    if not data:
        return b""
    return bytes(data)


def _read_padded(source: BlockReader, offset: int, length: int) -> bytes:
    """Exactly ``length`` bytes, with missing bytes read as zeros.

    Structure parsing stays safe on a truncated image this way:
    zeros are the filesystem's own "nothing here" marker, so a
    folder cut short simply looks like a folder that ends.
    """
    data = _safe_read(source, offset, length)
    if len(data) < length:
        data = data + b"\x00" * (length - len(data))
    return data


def _u16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        return 0
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        return 0
    return struct.unpack_from("<I", data, offset)[0]


class _Layout:
    """The drive's geometry, worked out from its opening record."""

    def __init__(self, boot: bytes):
        self.bytes_per_sector = _u16(boot, 11)
        self.sectors_per_cluster = boot[13] if len(boot) > 13 else 0
        self.reserved_sectors = _u16(boot, 14)
        self.fat_count = boot[16] if len(boot) > 16 else 0
        self.root_entry_count = _u16(boot, 17)
        total16 = _u16(boot, 19)
        self.total_sectors = total16 if total16 else _u32(boot, 32)
        fat16 = _u16(boot, 22)
        self.fat_size = fat16 if fat16 else _u32(boot, 36)
        self.root_cluster = _u32(boot, 44)

    @property
    def cluster_bytes(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def root_sectors(self) -> int:
        if self.bytes_per_sector == 0:
            return 0
        raw = self.root_entry_count * 32
        return (raw + self.bytes_per_sector - 1) // self.bytes_per_sector

    @property
    def data_start_sector(self) -> int:
        return (
            self.reserved_sectors
            + self.fat_count * self.fat_size
            + self.root_sectors
        )

    @property
    def data_sectors(self) -> int:
        return self.total_sectors - self.data_start_sector

    @property
    def cluster_count(self) -> int:
        if self.sectors_per_cluster == 0:
            return 0
        return self.data_sectors // self.sectors_per_cluster

    @property
    def fat_type(self) -> str:
        count = self.cluster_count
        if count < 4085:
            return "FAT12"
        if count < 65525:
            return "FAT16"
        return "FAT32"

    @property
    def data_start(self) -> int:
        return self.data_start_sector * self.bytes_per_sector

    @property
    def data_end(self) -> int:
        return self.data_start + self.cluster_count * self.cluster_bytes


def _plausible_layout(boot: bytes) -> _Layout | None:
    """The layout, if the opening record plausibly describes FAT."""
    if len(boot) < 512:
        return None
    if boot[510] != 0x55 or boot[511] != 0xAA:
        return None
    layout = _Layout(boot)
    if layout.bytes_per_sector not in (512, 1024, 2048, 4096):
        return None
    spc = layout.sectors_per_cluster
    if spc == 0 or (spc & (spc - 1)) != 0:
        return None
    if layout.reserved_sectors < 1:
        return None
    if layout.fat_count < 1:
        return None
    if layout.fat_size < 1:
        return None
    if layout.total_sectors < 1:
        return None
    if layout.data_sectors < 1:
        return None
    if layout.cluster_count < 1:
        return None
    return layout


def detect(source: BlockReader) -> bool:
    """True when this source plausibly holds a FAT filesystem.

    Only the opening record is consulted, and any trouble at
    all — garbage, an empty source, a truncated read — simply
    means "not FAT" rather than an error.
    """
    try:
        boot = _safe_read(source, 0, 512)
    except Exception:
        return False
    try:
        return _plausible_layout(boot) is not None
    except Exception:
        return False


def _modified_utc(entry: bytes) -> str | None:
    """The entry's last-changed moment, or None when it is nonsense."""
    time_word = _u16(entry, 22)
    date_word = _u16(entry, 24)
    if date_word == 0:
        return None
    year = ((date_word >> 9) & 0x7F) + 1980
    month = (date_word >> 5) & 0x0F
    day = date_word & 0x1F
    hour = (time_word >> 11) & 0x1F
    minute = (time_word >> 5) & 0x3F
    second = (time_word & 0x1F) * 2
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    if month in (4, 6, 9, 11) and day > 30:
        return None
    if month == 2:
        leap = (year % 4 == 0 and year % 100 != 0) or year % 400 == 0
        if day > (29 if leap else 28):
            return None
    return (
        f"{year:04d}-{month:02d}-{day:02d}"
        f"T{hour:02d}:{minute:02d}:{second:02d}Z"
    )


_LFN_OFFSETS = (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30)


def _lfn_chars(entry: bytes) -> list[int]:
    chars = []
    for off in _LFN_OFFSETS:
        if off + 2 <= len(entry):
            chars.append(struct.unpack_from("<H", entry, off)[0])
        else:
            chars.append(0xFFFF)
    return chars


def _reconstruct_lfn(pending: list[bytes]) -> str | None:
    """A long name from its gathered entries, in segment order.

    The entries sit in the folder in reverse segment order, each
    carrying a sequence number. When those numbers form a clean
    1..n run they decide the order; when deletion has overwritten
    every number with the same erased byte (as it does), the
    order falls back to simply undoing the physical order.
    """
    if not pending:
        return None
    sequences = [entry[0] & 0x1F for entry in pending]
    count = len(pending)
    if sorted(sequences) == list(range(1, count + 1)):
        ordered = [
            entry
            for _, entry in sorted(
                zip(sequences, pending), key=lambda pair: pair[0]
            )
        ]
    else:
        ordered = list(reversed(pending))
    chars: list[str] = []
    stopped = False
    for entry in ordered:
        for code in _lfn_chars(entry):
            if code == 0x0000:
                stopped = True
                break
            if code == 0xFFFF:
                continue
            chars.append(chr(code))
        if stopped:
            break
    name = "".join(chars)
    return name if name else None


def _short_name(entry: bytes, deleted: bool) -> str:
    """The 8.3 name from a short entry, first letter honest.

    A deleted entry's first character is gone — the erased byte
    sits where it was — so it is shown as an underscore rather
    than guessed at.
    """
    raw = bytearray(entry[0:8])
    if deleted and raw:
        raw[0] = 0x5F  # "_"
    name_part = bytes(raw).decode("ascii", errors="replace").strip()
    ext_part = bytes(entry[8:11]).decode("ascii", errors="replace").strip()
    if ext_part:
        return f"{name_part}.{ext_part}"
    return name_part


class _Scanner:
    def __init__(self, source: BlockReader, layout: _Layout):
        self.source = source
        self.layout = layout
        self.fat_type = layout.fat_type
        fat_offset = layout.reserved_sectors * layout.bytes_per_sector
        fat_length = layout.fat_size * layout.bytes_per_sector
        self.fat = _read_padded(source, fat_offset, fat_length)
        self.files: list[DeletedFile] = []
        self.notes: list[str] = []
        self.entries_examined = 0
        self.stopped = False
        self.visited_dir_clusters: set[int] = set()

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def fat_entry(self, cluster: int) -> int:
        fat = self.fat
        if cluster < 0:
            return 0
        if self.fat_type == "FAT12":
            offset = cluster + cluster // 2
            if offset >= len(fat):
                return 0
            if offset + 1 < len(fat):
                word = struct.unpack_from("<H", fat, offset)[0]
            else:
                word = fat[offset]
            if cluster % 2 == 0:
                return word & 0x0FFF
            return (word >> 4) & 0x0FFF
        if self.fat_type == "FAT16":
            offset = cluster * 2
            if offset + 2 > len(fat):
                return 0
            return struct.unpack_from("<H", fat, offset)[0]
        offset = cluster * 4
        if offset + 4 > len(fat):
            return 0
        return struct.unpack_from("<I", fat, offset)[0] & 0x0FFFFFFF

    def _is_eoc(self, value: int) -> bool:
        if self.fat_type == "FAT12":
            return value >= 0x0FF8
        if self.fat_type == "FAT16":
            return value >= 0xFFF8
        return value >= 0x0FFFFFF8

    def _is_bad(self, value: int) -> bool:
        if self.fat_type == "FAT12":
            return value == 0x0FF7
        if self.fat_type == "FAT16":
            return value == 0xFFF7
        return value == 0x0FFFFFF7

    def cluster_offset(self, cluster: int) -> int:
        return (
            self.layout.data_start
            + (cluster - 2) * self.layout.cluster_bytes
        )

    def read_cluster(self, cluster: int) -> bytes:
        return _read_padded(
            self.source,
            self.cluster_offset(cluster),
            self.layout.cluster_bytes,
        )

    def follow_chain(self, first_cluster: int) -> list[int]:
        """The chain from a starting cluster, guarded hard."""
        chain: list[int] = []
        seen: set[int] = set()
        cluster = first_cluster
        max_cluster = 2 + self.layout.cluster_count
        while 2 <= cluster < max_cluster:
            if cluster in seen:
                self.note(
                    "A folder's chain of pieces loops back on "
                    "itself, so Lazarus stopped following it "
                    "there rather than go round forever."
                )
                break
            seen.add(cluster)
            chain.append(cluster)
            if len(chain) >= _MAX_CHAIN_LENGTH:
                self.note(
                    "A folder's chain of pieces ran past 100,000 "
                    "pieces, so Lazarus stopped following it "
                    "there; no real folder is that large."
                )
                break
            value = self.fat_entry(cluster)
            if value == 0 or self._is_eoc(value) or self._is_bad(value):
                break
            cluster = value
        return chain

    def scan_root(self) -> None:
        layout = self.layout
        if self.fat_type == "FAT32":
            first = layout.root_cluster
            if first < 2:
                raise UndeleteError(
                    "This image's FAT records are too damaged to "
                    "read: its opening record does not say where "
                    "the main folder begins."
                )
            chain = self.follow_chain(first)
            for c in chain:
                self.visited_dir_clusters.add(c)
            data = b"".join(self.read_cluster(c) for c in chain)
            self.scan_directory(data, "", 0)
        else:
            root_offset = (
                layout.data_start
                - layout.root_sectors * layout.bytes_per_sector
            )
            root_length = layout.root_entry_count * 32
            data = _read_padded(self.source, root_offset, root_length)
            self.scan_directory(data, "", 0)

    def scan_directory(self, data: bytes, directory: str, depth: int) -> None:
        pending_lfn: list[bytes] = []
        index = 0
        total_slots = len(data) // 32
        while index < total_slots:
            if self.stopped:
                return
            if self.entries_examined >= _MAX_ENTRIES:
                self.note(
                    "Lazarus stopped looking after checking "
                    "200,000 folder entries, so a badly damaged "
                    "drive cannot trap it in an endless search. "
                    "What it found before stopping is still "
                    "reported."
                )
                self.stopped = True
                return
            self.entries_examined += 1
            entry = data[index * 32 : (index + 1) * 32]
            index += 1
            first_byte = entry[0]
            if first_byte == 0x00:
                break
            attr = entry[11]
            if attr == 0x0F:
                pending_lfn.append(bytes(entry))
                continue
            if attr & 0x08:
                pending_lfn = []
                continue
            deleted = first_byte == 0xE5
            plain = (
                bytes(entry[0:8])
                .decode("ascii", errors="replace")
                .strip()
            )
            if plain in (".", ".."):
                pending_lfn = []
                continue
            lfn_name = _reconstruct_lfn(pending_lfn)
            had_lfn = lfn_name is not None
            pending_lfn = []
            display = lfn_name if had_lfn else _short_name(entry, deleted)
            if not display:
                continue
            first_cluster = _u16(entry, 26)
            if self.fat_type == "FAT32":
                # The high half of the cluster number is the
                # 16-bit field at byte 20; the bytes after it
                # are the time fields, not part of the number.
                first_cluster |= (_u32(entry, 20) & 0xFFFF) << 16
            size = _u32(entry, 28)
            if attr & 0x10:
                self.enter_subdirectory(
                    display, first_cluster, deleted, directory, depth
                )
                continue
            if not deleted:
                continue
            self.record_deleted_file(
                entry, display, had_lfn, first_cluster, size, directory
            )

    def enter_subdirectory(
        self,
        display: str,
        first_cluster: int,
        deleted: bool,
        directory: str,
        depth: int,
    ) -> None:
        child_path = f"{directory}/{display}" if directory else display
        if depth + 1 > _MAX_DEPTH:
            self.note(
                "Lazarus stopped following folders nested more "
                "than 16 deep; folders that deep usually mean "
                "the records are damaged rather than real."
            )
            return
        if first_cluster < 2:
            return
        if deleted:
            self.note(
                f"Lazarus looked only at the first piece of "
                f"the deleted folder '{child_path}'; when a "
                f"folder is deleted, the map of its later "
                f"pieces is erased, so only the first piece "
                f"can still be read with confidence."
            )
            if first_cluster in self.visited_dir_clusters:
                return
            self.visited_dir_clusters.add(first_cluster)
            data = self.read_cluster(first_cluster)
            self.scan_directory(data, child_path, depth + 1)
            return
        chain = self.follow_chain(first_cluster)
        fresh = [c for c in chain if c not in self.visited_dir_clusters]
        if not fresh:
            return
        for c in fresh:
            self.visited_dir_clusters.add(c)
        data = b"".join(self.read_cluster(c) for c in fresh)
        self.scan_directory(data, child_path, depth + 1)

    def record_deleted_file(
        self,
        entry: bytes,
        display: str,
        had_lfn: bool,
        first_cluster: int,
        size: int,
        directory: str,
    ) -> None:
        evidence: list[str] = []
        if not had_lfn:
            evidence.append(_FIRST_LETTER_EVIDENCE)
        evidence.append(_CHAIN_EVIDENCE)
        extents: list[Extent] = []
        layout = self.layout
        if size == 0:
            extents = []
        elif first_cluster < 2:
            evidence.append(_NO_START_EVIDENCE)
            extents = []
        else:
            cluster_bytes = layout.cluster_bytes
            wanted = (size + cluster_bytes - 1) // cluster_bytes
            remaining = size
            past_end = False
            for step in range(wanted):
                cluster = first_cluster + step
                if cluster < 2 or cluster >= 2 + layout.cluster_count:
                    past_end = True
                    break
                start = self.cluster_offset(cluster)
                if start >= layout.data_end:
                    past_end = True
                    break
                length = min(cluster_bytes, remaining)
                extents.append(
                    Extent(
                        start=start,
                        length=length,
                        in_use=(self.fat_entry(cluster) != 0),
                    )
                )
                remaining -= length
            if past_end:
                evidence.append(_PAST_END_EVIDENCE)
        self.files.append(
            DeletedFile(
                name=display,
                fs_type=self.fat_type,
                size=size,
                extents=extents,
                evidence=evidence,
                directory=directory,
                modified_utc=_modified_utc(entry),
            )
        )


def scan(source: BlockReader) -> FsScan:
    """Every deleted file this FAT source still holds a record of.

    Raises :class:`UndeleteError` only when the opening record
    looks like FAT but the layout it describes cannot be followed
    at all. Damage inside folders — the ordinary kind, on a real
    deleted file's drive — is reported in the scan's notes
    alongside whatever was still found.
    """
    boot = _safe_read(source, 0, 512)
    layout = _plausible_layout(boot)
    if layout is None:
        if len(boot) >= 512 and boot[510] == 0x55 and boot[511] == 0xAA:
            raise UndeleteError(
                "This image's FAT records are too damaged to "
                "read: its opening record claims to be a drive "
                "but describes a layout that cannot exist."
            )
        raise UndeleteError(
            "Lazarus could not read this image as a FAT "
            "drive: its opening records do not look like a "
            "FAT drive."
        )
    if layout.cluster_bytes == 0 or layout.data_sectors < 1:
        raise UndeleteError(
            "This image's FAT records are too damaged to read: "
            "its opening record describes a drive with no room "
            "for files in it."
        )
    scanner = _Scanner(source, layout)
    scanner.scan_root()
    return FsScan(files=scanner.files, notes=scanner.notes)
