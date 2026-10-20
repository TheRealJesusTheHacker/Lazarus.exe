"""Reading deleted files out of an NTFS drive.

NTFS — the filesystem on Windows system drives — keeps a table of
every file it has ever held: the Master File Table, one record per
file, each record carrying the file's name and saying where its
contents sat on the drive. Deleting a file clears one flag in its
record and frees its space for reuse, but the record itself often
survives a long while — and a surviving record is enough for
Lazarus to find the file and say where its contents were.

That is exactly, and only, what this parser does: an MFT-record
scan. It reads the boot sector, follows it to the Master File
Table, and walks every record slot there, applying each record's
own fixup (the update-sequence check NTFS stamps into every
sector) before trusting a word of it. It does not walk folder
indexes, does not read the change journal, and does not go
looking anywhere else — a file whose record is already gone, or
whose record was too damaged to pass its own fixup, is beyond
this scan, and the scan's notes say how many records were skipped
that way rather than pretending the drive held less than it did.
Files inside deleted folders still have their own records and
are found on their own. A file's contents are never read here:
for files small enough to live inside their own record the bytes
come back with the record, and for every other file the parser
reports the stretches of the drive its contents sat in, with the
drive's own in-use map ($BITMAP) saying which of those stretches
have been handed to something else since.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .errors import LazarusError, UndeleteError
from .recovery_model import DeletedFile, Extent, FsScan
from .source import BlockReader

FS_FAMILY = "NTFS"

# Attribute types this parser reads.
_ATTR_STANDARD_INFORMATION = 0x10
_ATTR_FILE_NAME = 0x30
_ATTR_DATA = 0x80
_ATTR_END = 0xFFFFFFFF

# Record flags (u16 at offset 22).
_FLAG_IN_USE = 0x01
_FLAG_DIRECTORY = 0x02

# The record that holds the drive's in-use map.
_BITMAP_RECORD = 6

# Bounds that keep a hostile or hopelessly corrupt image from
# hanging the scan: no record carries more attributes than this,
# and no attribute claims more data runs than this.
_MAX_ATTRIBUTES = 128
_MAX_RUNS = 4096

# FILETIME counts 100-nanosecond ticks since 1601-01-01.
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

_BITMAP_UNKNOWN_EVIDENCE = (
    "the drive's in-use map could not be read, so Lazarus cannot "
    "tell which parts have been reused"
)


@dataclass
class _Boot:
    """The handful of boot-sector numbers the scan runs on."""

    bytes_per_sector: int
    cluster_bytes: int
    mft_lcn: int
    record_size: int


@dataclass
class _Attribute:
    """One parsed attribute, resident value or runlist in hand."""

    type: int
    nonresident: bool
    name: str  # "" for the unnamed (main) attribute of its type
    value: Optional[bytes] = None  # resident attributes only
    runlist: Optional[bytes] = None  # non-resident attributes only
    real_size: int = 0  # non-resident attributes only


@dataclass
class _Record:
    """One MFT record after its fixup has been applied."""

    number: int
    flags: int
    attributes: list[_Attribute] = field(default_factory=list)

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & _FLAG_DIRECTORY)

    @property
    def in_use(self) -> bool:
        return bool(self.flags & _FLAG_IN_USE)


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _parse_boot(source: BlockReader) -> Optional[_Boot]:
    """The boot sector's numbers, or None if this is not a sane
    NTFS boot sector. Never raises: anything unreadable, truncated,
    or implausible is simply "not NTFS" to this check."""
    try:
        size = source.size
        if not isinstance(size, int) or size < 512:
            return None
        boot = source.read_at(0, 512)
        if len(boot) < 512:
            return None
        if boot[3:11] != b"NTFS    ":
            return None
        if boot[510:512] != b"\x55\xaa":
            return None
        bytes_per_sector = _u16(boot, 11)
        if (
            bytes_per_sector < 512
            or bytes_per_sector > 4096
            or bytes_per_sector & (bytes_per_sector - 1)
        ):
            return None
        sectors_per_cluster = boot[13]
        if sectors_per_cluster == 0:
            return None
        if sectors_per_cluster > 0x80:
            # The stored byte is a power of two in disguise: the
            # cluster size is 1 << (256 - value) bytes.
            exponent = 256 - sectors_per_cluster
            if exponent > 24:
                return None
            cluster_bytes = 1 << exponent
            if cluster_bytes < bytes_per_sector:
                return None
        else:
            if sectors_per_cluster & (sectors_per_cluster - 1):
                return None
            cluster_bytes = bytes_per_sector * sectors_per_cluster
        if cluster_bytes > 16 * 1024 * 1024:
            return None
        mft_lcn = struct.unpack_from("<q", boot, 48)[0]
        if mft_lcn <= 0:
            return None
        record_byte = struct.unpack_from("<b", boot, 64)[0]
        if record_byte == 0:
            return None
        if record_byte < 0:
            record_size = 1 << (-record_byte)
        else:
            record_size = record_byte * cluster_bytes
        if (
            record_size < 512
            or record_size > 65536
            or record_size & (record_size - 1)
        ):
            return None
        return _Boot(bytes_per_sector, cluster_bytes, mft_lcn, record_size)
    except Exception:
        return None


def detect(source: BlockReader) -> bool:
    """True if the source's boot sector marks it as NTFS and the
    numbers in it are sane enough to scan by. False — never an
    error — on anything else, garbage and truncated images
    included."""
    return _parse_boot(source) is not None


def _apply_fixup(raw: bytes, boot: _Boot) -> Optional[bytes]:
    """A record with its update-sequence fixup applied, or None
    if the record fails that check and is too damaged to trust.

    NTFS stores the last two bytes of every sector of a record in
    the record's own update-sequence array and stamps a sequence
    word in their place. If any sector end does not carry the
    sequence word, the record was half-written or overwritten,
    and nothing in it can be believed.
    """
    if len(raw) < 48:
        return None
    usa_offset = _u16(raw, 4)
    usa_count = _u16(raw, 6)
    sector = boot.bytes_per_sector
    if len(raw) % sector != 0:
        return None
    if usa_count != len(raw) // sector + 1:
        return None
    if usa_offset + usa_count * 2 > len(raw):
        return None
    sequence = raw[usa_offset : usa_offset + 2]
    fixed = bytearray(raw)
    for index in range(1, usa_count):
        end = index * sector - 2
        if raw[end : end + 2] != sequence:
            return None
        stored = raw[usa_offset + index * 2 : usa_offset + index * 2 + 2]
        fixed[end : end + 2] = stored
    return bytes(fixed)


def _parse_attributes(record: bytes) -> tuple[list[_Attribute], bool]:
    """The attributes in a fixup-applied record. The second value
    is True when an attribute's claimed length ran off the record
    or the attribute list never reached its end marker — the parse
    stops there, and the caller decides what that means for the
    record as a whole."""
    attributes: list[_Attribute] = []
    attrs_offset = _u16(record, 20)
    if attrs_offset < 16 or attrs_offset >= len(record):
        return attributes, True
    position = attrs_offset
    broken = False
    ended = False
    while position + 8 <= len(record):
        if len(attributes) >= _MAX_ATTRIBUTES:
            broken = True
            break
        attr_type = _u32(record, position)
        if attr_type == _ATTR_END:
            ended = True
            break
        length = _u32(record, position + 4)
        if length < 16 or position + length > len(record):
            broken = True
            break
        attribute = _parse_attribute(record, position, length, attr_type)
        if attribute is None:
            broken = True
            break
        attributes.append(attribute)
        position += length
    if not ended and not broken:
        # The record ran out before its end marker: the attribute
        # list cannot be trusted to be complete.
        broken = True
    return attributes, broken


def _parse_attribute(
    record: bytes, position: int, length: int, attr_type: int
) -> Optional[_Attribute]:
    """One attribute, or None if its own header does not hold
    together inside its claimed length."""
    try:
        nonresident = record[position + 8] != 0
        name_length = record[position + 9]
        name_offset = _u16(record, position + 10)
        name = ""
        if name_length:
            start = position + name_offset
            end = start + name_length * 2
            if name_offset == 0 or end > position + length:
                return None
            name = record[start:end].decode("utf-16-le", errors="replace")
        if not nonresident:
            if length < 24:
                return None
            value_length = _u32(record, position + 16)
            value_offset = _u16(record, position + 20)
            if value_offset + value_length > length:
                return None
            start = position + value_offset
            return _Attribute(
                type=attr_type,
                nonresident=False,
                name=name,
                value=record[start : start + value_length],
            )
        if length < 64:
            return None
        runlist_offset = _u16(record, position + 32)
        if runlist_offset > length:
            return None
        return _Attribute(
            type=attr_type,
            nonresident=True,
            name=name,
            runlist=record[position + runlist_offset : position + length],
            real_size=_u64(record, position + 48),
        )
    except (IndexError, struct.error, UnicodeDecodeError):
        return None


def _parse_record(
    raw: bytes, number: int, boot: _Boot
) -> tuple[Optional[_Record], bool]:
    """One raw record slot as (record, damaged). An all-zero slot
    is simply unused — (None, False), skipped in silence. A slot
    that fails its fixup, carries no FILE marker, or has an
    attribute list that breaks after looking like a file record
    is (None, True): damaged, and counted by the caller."""
    if not any(raw):
        return None, False
    if raw[:4] != b"FILE":
        return None, True
    fixed = _apply_fixup(raw, boot)
    if fixed is None:
        return None, True
    flags = _u16(fixed, 22)
    attributes, broken = _parse_attributes(fixed)
    if broken:
        # A broken attribute list in a record that looked like a
        # file means the record is damaged; a broken directory
        # record is just passed over — its files are found through
        # their own records either way.
        return None, not (flags & _FLAG_DIRECTORY)
    return _Record(number=number, flags=flags, attributes=attributes), False


def _decode_runlist(
    runlist: bytes, cluster_bytes: int
) -> Optional[list[tuple[Optional[int], int]]]:
    """A runlist decoded to (cluster number, byte length) stretches.

    Each run says how many clusters it covers and, as a signed
    step from the previous run, where it starts. A run with no
    starting step at all is a sparse stretch — space the file
    was promised but that was never stored anywhere. None comes
    back for a runlist that does not hold together.
    """
    runs: list[tuple[Optional[int], int]] = []
    position = 0
    current_lcn = 0
    while position < len(runlist):
        header = runlist[position]
        if header == 0:
            break
        length_size = header & 0x0F
        offset_size = header >> 4
        if length_size == 0 or len(runs) >= _MAX_RUNS:
            return None
        end = position + 1 + length_size + offset_size
        if end > len(runlist):
            return None
        length_clusters = int.from_bytes(
            runlist[position + 1 : position + 1 + length_size], "little"
        )
        if length_clusters == 0:
            return None
        if offset_size == 0:
            runs.append((None, length_clusters * cluster_bytes))
        else:
            delta = int.from_bytes(
                runlist[position + 1 + length_size : end],
                "little",
                signed=True,
            )
            current_lcn += delta
            if current_lcn < 0:
                return None
            runs.append((current_lcn, length_clusters * cluster_bytes))
        position = end
    return runs


def _data_extents(
    attribute: _Attribute, cluster_bytes: int
) -> Optional[list[Extent]]:
    """A non-resident $DATA attribute as byte extents, clipped to
    the size the file actually was. Sparse stretches come back
    with start -1 and in_use False: they were never stored, so
    nothing can have overwritten them — they read back as zeros.
    The in_use of every stored stretch is left for the caller to
    fill in from the drive's in-use map."""
    runs = _decode_runlist(attribute.runlist or b"", cluster_bytes)
    if runs is None:
        return None
    extents: list[Extent] = []
    remaining = attribute.real_size
    for lcn, length in runs:
        if remaining <= 0:
            break
        take = min(length, remaining)
        remaining -= take
        if take <= 0:
            continue
        if lcn is None:
            extents.append(Extent(start=-1, length=take, in_use=False))
        else:
            extents.append(
                Extent(start=lcn * cluster_bytes, length=take, in_use=None)
            )
    return extents


def _read_attribute_bytes(
    source: BlockReader, attribute: _Attribute, boot: _Boot
) -> Optional[bytes]:
    """The stored bytes of one attribute: the value itself when
    resident, or its runs read back from the source and clipped
    to its real size when not. None when any of it cannot be
    read, is cut short, or claims more than the source could
    hold — the caller treats that as "could not be read"."""
    if not attribute.nonresident:
        return attribute.value
    if attribute.real_size > source.size + boot.cluster_bytes:
        return None
    runs = _decode_runlist(attribute.runlist or b"", boot.cluster_bytes)
    if runs is None:
        return None
    data = bytearray()
    remaining = attribute.real_size
    try:
        for lcn, length in runs:
            if remaining <= 0:
                break
            take = min(length, remaining)
            remaining -= take
            if lcn is None:
                data.extend(b"\x00" * take)
                continue
            chunk = source.read_at(lcn * boot.cluster_bytes, take)
            if len(chunk) < take:
                return None
            data.extend(chunk)
    except (LazarusError, OSError, ValueError):
        return None
    return bytes(data)


def _read_bitmap(
    source: BlockReader, records: dict[int, _Record], boot: _Boot
) -> Optional[bytes]:
    """The drive's in-use map from record 6 ($BITMAP), or None if
    that record, or the map itself, cannot be read."""
    record = records.get(_BITMAP_RECORD)
    if record is None:
        return None
    for attribute in record.attributes:
        if attribute.type == _ATTR_DATA and attribute.name == "":
            return _read_attribute_bytes(source, attribute, boot)
    return None


def _cluster_in_use(bitmap: bytes, cluster: int) -> Optional[bool]:
    """One cluster's testimony from the in-use map: True if the
    map says it has been handed to something else, False if it
    is still free, None if the map does not reach that far."""
    byte_index = cluster // 8
    if byte_index >= len(bitmap):
        return None
    return bool(bitmap[byte_index] & (1 << (cluster % 8)))


def _split_by_bitmap(
    extent: Extent, bitmap: bytes, cluster_bytes: int
) -> list[Extent]:
    """One stored extent split into the longest stretches that
    share a single in-use state according to the map."""
    pieces: list[Extent] = []
    position = extent.start
    remaining = extent.length
    while remaining > 0:
        cluster = position // cluster_bytes
        take = min(remaining, cluster_bytes - (position % cluster_bytes))
        state = _cluster_in_use(bitmap, cluster)
        if (
            pieces
            and pieces[-1].in_use == state
            and pieces[-1].start + pieces[-1].length == position
        ):
            pieces[-1].length += take
        else:
            pieces.append(Extent(start=position, length=take, in_use=state))
        position += take
        remaining -= take
    return pieces


# $FILE_NAME namespaces, best first: the name a person would
# recognise (Win32), then POSIX, then the DOS-era short name.
_NAMESPACE_RANK = {2: 0, 1: 1, 0: 2}


def _file_names(record: _Record) -> list[tuple[int, str, int]]:
    """Every usable $FILE_NAME in a record, as (namespace rank,
    name, parent record number), best first."""
    found: list[tuple[int, str, int]] = []
    for attribute in record.attributes:
        if attribute.type != _ATTR_FILE_NAME or attribute.value is None:
            continue
        value = attribute.value
        if len(value) < 66:
            continue
        parent = _u64(value, 0) & ((1 << 48) - 1)
        name_length = value[64]
        namespace = value[65]
        end = 66 + name_length * 2
        if name_length == 0 or end > len(value):
            continue
        try:
            name = value[66:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        found.append((_NAMESPACE_RANK.get(namespace, 3), name, parent))
    found.sort(key=lambda item: item[0])
    return found


def _modified_time(record: _Record) -> Optional[str]:
    """The file's last-modified time as an ISO line, from
    $STANDARD_INFORMATION. Absurd values — the kind a damaged
    record invents — come back as None rather than as a date
    nobody should believe."""
    for attribute in record.attributes:
        if attribute.type != _ATTR_STANDARD_INFORMATION:
            continue
        if attribute.value is None or len(attribute.value) < 16:
            return None
        ticks = _u64(attribute.value, 8)
        if ticks == 0:
            return None
        try:
            moment = _FILETIME_EPOCH + timedelta(microseconds=ticks // 10)
        except OverflowError:
            return None
        if moment.year < 1601:
            return None
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _unnamed_data(record: _Record) -> Optional[_Attribute]:
    """The record's main $DATA attribute — the unnamed one. Named
    streams are other contents riding on the same file, not the
    file itself."""
    for attribute in record.attributes:
        if attribute.type == _ATTR_DATA and attribute.name == "":
            return attribute
    return None


def _has_named_data(record: _Record) -> bool:
    """True if the record carries any extra, named $DATA stream."""
    return any(
        attribute.type == _ATTR_DATA and attribute.name != ""
        for attribute in record.attributes
    )


def _read_record(source: BlockReader, offset: int, size: int) -> bytes:
    """One record's raw bytes; anything short of the full record
    comes back as whatever was read, and the caller treats a
    short read as damage rather than guessing at the rest."""
    try:
        return source.read_at(offset, size)
    except (LazarusError, OSError, ValueError):
        return b""


def _mft_extents(
    source: BlockReader, boot: _Boot
) -> tuple[list[tuple[int, int]], int]:
    """Where the Master File Table itself lives, as (byte offset,
    byte length) stretches plus the table's total byte size —
    read out of record 0, the table's own record of itself. This
    is the one place a scan can fail outright: with no readable
    $MFT record there is no table to walk, and the refusal says
    so in plain language."""
    offset = boot.mft_lcn * boot.cluster_bytes
    raw = _read_record(source, offset, boot.record_size)
    if len(raw) < boot.record_size:
        raise UndeleteError(
            "Lazarus could not read this NTFS drive's file table: "
            "the table's own first record could not be read. The "
            "drive may be too damaged, or the image may be cut "
            "short. If this is a failing drive, rescue it to an "
            "image first and try the image."
        )
    record, damaged = _parse_record(raw, 0, boot)
    if record is None or damaged:
        raise UndeleteError(
            "Lazarus could not read this NTFS drive's file table: "
            "the table's own first record is damaged. The drive "
            "may be too damaged to scan this way."
        )
    data = _unnamed_data(record)
    if data is None or not data.nonresident:
        raise UndeleteError(
            "Lazarus could not read this NTFS drive's file table: "
            "its first record does not say where the table is. "
            "The drive may be too damaged to scan this way."
        )
    runs = _decode_runlist(data.runlist or b"", boot.cluster_bytes)
    if not runs:
        raise UndeleteError(
            "Lazarus could not read this NTFS drive's file table: "
            "its first record points nowhere. The drive may be "
            "too damaged to scan this way."
        )
    extents: list[tuple[int, int]] = []
    total = 0
    for lcn, length in runs:
        if lcn is None:
            raise UndeleteError(
                "Lazarus could not read this NTFS drive's file "
                "table: part of the table was never stored. The "
                "drive may be too damaged to scan this way."
            )
        extents.append((lcn * boot.cluster_bytes, length))
        total += length
    if data.real_size > 0:
        total = min(total, data.real_size)
    if total < boot.record_size:
        raise UndeleteError(
            "Lazarus could not read this NTFS drive's file table: "
            "the table is smaller than one record. The drive may "
            "be too damaged to scan this way."
        )
    return extents, total


def scan(source: BlockReader) -> FsScan:
    """Scan an NTFS source for deleted files, MFT record by MFT
    record. Raises :class:`UndeleteError` only when the boot
    sector or the file table's own record is unreadable — every
    other kind of damage is reported in the scan's notes, with
    whatever could still be read returned alongside it."""
    boot = _parse_boot(source)
    if boot is None:
        raise UndeleteError(
            "Lazarus could not scan this drive: it does not look "
            "like an NTFS drive. Lazarus scans NTFS drives by "
            "reading their file table; this one's beginning is "
            "missing, damaged, or from a different kind of drive."
        )
    mft_extents, mft_bytes = _mft_extents(source, boot)

    # Walk every record slot in the table, no more than the table
    # can hold, and make a name index as we go: record number to
    # (name, is it a folder), so a file's folder can be named from
    # its parent record afterwards.
    records: dict[int, _Record] = {}
    damaged = 0
    scanned = 0
    deleted_folder_seen = False
    remaining_total = mft_bytes
    logical_base = 0  # this stretch's place in the table's byte stream
    for extent_offset, extent_length in mft_extents:
        take = min(extent_length, remaining_total)
        remaining_total -= take
        position = 0
        while position + boot.record_size <= take:
            number = (logical_base + position) // boot.record_size
            raw = _read_record(
                source, extent_offset + position, boot.record_size
            )
            if len(raw) < boot.record_size:
                damaged += 1
                break
            scanned += 1
            record, was_damaged = _parse_record(raw, number, boot)
            if was_damaged:
                damaged += 1
            if record is not None:
                records[number] = record
                if record.is_directory and not record.in_use:
                    deleted_folder_seen = True
            position += boot.record_size
        if take % boot.record_size:
            # A part-record at the end of a stretch cannot be read
            # as a record at all; count it with the damaged.
            damaged += 1
        logical_base += take

    name_index: dict[int, tuple[str, bool]] = {}
    for number, record in records.items():
        names = _file_names(record)
        if names:
            name_index[number] = (names[0][1], record.is_directory)

    bitmap = _read_bitmap(source, records, boot)

    files: list[DeletedFile] = []
    named_streams_seen = False
    for number in sorted(records):
        record = records[number]
        if record.in_use or record.is_directory:
            continue  # live files and folders are never returned
        names = _file_names(record)
        if not names:
            continue
        data = _unnamed_data(record)
        if data is None:
            continue
        _rank, name, parent = names[0]
        directory = ""
        parent_entry = name_index.get(parent)
        if parent_entry is not None and parent_entry[1]:
            directory = parent_entry[0]
        evidence: list[str] = []
        if not data.nonresident:
            resident = data.value or b""
            files.append(
                DeletedFile(
                    name=name,
                    fs_type=FS_FAMILY,
                    size=len(resident),
                    extents=[],
                    resident_data=resident,
                    evidence=["its contents live inside the surviving record itself"],
                    directory=directory,
                    modified_utc=_modified_time(record),
                )
            )
            if bitmap is None:
                files[-1].evidence.append(_BITMAP_UNKNOWN_EVIDENCE)
            if _has_named_data(record):
                named_streams_seen = True
            continue
        extents = _data_extents(data, boot.cluster_bytes)
        if extents is None:
            damaged += 1
            continue
        if bitmap is None:
            evidence.append(_BITMAP_UNKNOWN_EVIDENCE)
        else:
            split: list[Extent] = []
            for extent in extents:
                if extent.start == -1:
                    split.append(extent)
                else:
                    split.extend(
                        _split_by_bitmap(extent, bitmap, boot.cluster_bytes)
                    )
            extents = split
        evidence.insert(
            0, f"its data sat in {len(extents)} stretch(es) of the drive"
        )
        files.append(
            DeletedFile(
                name=name,
                fs_type=FS_FAMILY,
                size=data.real_size,
                extents=extents,
                resident_data=None,
                evidence=evidence,
                directory=directory,
                modified_utc=_modified_time(record),
            )
        )
        if _has_named_data(record):
            named_streams_seen = True

    notes = [
        f"Scanned {scanned} file record(s): {len(files)} deleted file(s) found."
    ]
    if damaged:
        notes.append(
            f"{damaged} record(s) were too damaged to read and were skipped"
        )
    if deleted_folder_seen:
        notes.append(
            "files inside deleted folders still have their own "
            "records and are found on their own"
        )
    if named_streams_seen:
        notes.append(
            "files with extra data streams were listed by their "
            "main contents only"
        )
    return FsScan(files=files, notes=notes)
