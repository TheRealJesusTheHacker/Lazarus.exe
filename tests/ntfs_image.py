"""A small NTFS image builder, written from the NTFS layout.

This builder exists so the parser tests can state their ground
truth in the open: every byte of the image is placed here, by
hand, from the on-disk structures — boot sector, file records
with their update-sequence arrays, runlists, the $BITMAP record
— and never by calling the parser under test. If the builder and
the parser disagree, at least one of them is wrong, and the test
failures show which bytes were expected where.

Geometry, kept deliberately tiny: 512-byte sectors, one sector
per cluster, 1024-byte file records (the boot sector stores that
as the signed byte -10, meaning 2**10), the Master File Table at
cluster 4 with room for 32 records.
"""

from __future__ import annotations

import struct

SECTOR = 512
CLUSTER = 512
RECORD = 1024
MFT_LCN = 4
MFT_RECORDS = 32
TOTAL_CLUSTERS = 256
BITMAP_LCN = 220

# Record flags.
IN_USE = 0x01
DIRECTORY = 0x02

_SEQUENCE = 1  # the update-sequence (fixup) word stamped per sector


def _u16(value: int) -> bytes:
    return struct.pack("<H", value)


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _align8(length: int) -> int:
    return (length + 7) & ~7


def _unsigned_bytes(value: int) -> bytes:
    size = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(size, "little")


def _signed_bytes(value: int) -> bytes:
    size = 1
    while True:
        try:
            return value.to_bytes(size, "little", signed=True)
        except OverflowError:
            size += 1


def encode_runlist(runs) -> bytes:
    """Runlist bytes for [(lcn, clusters), ...]; lcn None (or -1)
    is a sparse run, stored with an offset size of zero."""
    out = bytearray()
    current = 0
    for lcn, clusters in runs:
        length_bytes = _unsigned_bytes(clusters)
        if lcn is None or lcn == -1:
            out.append(len(length_bytes))  # offset size 0
            out.extend(length_bytes)
        else:
            delta_bytes = _signed_bytes(lcn - current)
            current = lcn
            out.append((len(delta_bytes) << 4) | len(length_bytes))
            out.extend(length_bytes)
            out.extend(delta_bytes)
    out.append(0)
    return bytes(out)


def _attribute_resident(attr_type: int, value: bytes, name: str = "") -> bytes:
    name_bytes = name.encode("utf-16-le") if name else b""
    name_offset = 24 if name_bytes else 0
    value_offset = 24 + len(name_bytes)
    total = _align8(value_offset + len(value))
    header = bytearray(total)
    header[0:4] = _u32(attr_type)
    header[4:8] = _u32(total)
    header[8] = 0  # resident
    header[9] = len(name)
    header[10:12] = _u16(name_offset)
    header[16:20] = _u32(len(value))
    header[20:22] = _u16(value_offset)
    if name_bytes:
        header[name_offset : name_offset + len(name_bytes)] = name_bytes
    header[value_offset : value_offset + len(value)] = value
    return bytes(header)


def _attribute_nonresident(
    attr_type: int, runs, real_size: int, name: str = ""
) -> bytes:
    name_bytes = name.encode("utf-16-le") if name else b""
    name_offset = 64 if name_bytes else 0
    runlist = encode_runlist(runs)
    runlist_offset = 64 + len(name_bytes)
    total_clusters = sum(clusters for _lcn, clusters in runs)
    total = _align8(runlist_offset + len(runlist))
    header = bytearray(total)
    header[0:4] = _u32(attr_type)
    header[4:8] = _u32(total)
    header[8] = 1  # non-resident
    header[9] = len(name)
    header[10:12] = _u16(name_offset)
    header[24:32] = _u64(total_clusters - 1)  # highest VCN
    header[32:34] = _u16(runlist_offset)
    header[40:48] = _u64(total_clusters * CLUSTER)  # allocated size
    header[48:56] = _u64(real_size)
    header[56:64] = _u64(real_size)  # initialised size
    if name_bytes:
        header[name_offset : name_offset + len(name_bytes)] = name_bytes
    header[runlist_offset : runlist_offset + len(runlist)] = runlist
    return bytes(header)


def _standard_information(modified_filetime: int | None) -> bytes:
    value = bytearray(72)
    if modified_filetime is not None:
        value[0:8] = _u64(modified_filetime)  # creation
        value[8:16] = _u64(modified_filetime)  # modification
        value[16:24] = _u64(modified_filetime)
        value[24:32] = _u64(modified_filetime)
    return _attribute_resident(0x10, bytes(value))


def _file_name(name: str, parent: int, namespace: int, data_size: int) -> bytes:
    encoded = name.encode("utf-16-le")
    value = bytearray(66 + len(encoded))
    value[0:8] = _u64(parent)  # parent reference; record number low 48
    value[8:16] = _u64(data_size)
    value[16:24] = _u64(data_size)
    value[64] = len(name)
    value[65] = namespace
    value[66:] = encoded
    return _attribute_resident(0x30, bytes(value))


def _apply_fixup(record: bytearray) -> bytes:
    """Stamp the update sequence over a finished record: the last
    two bytes of each sector move into the update-sequence array
    and the sequence word takes their place."""
    usa_offset = 48
    usa_count = RECORD // SECTOR + 1
    record[usa_offset : usa_offset + 2] = _u16(_SEQUENCE)
    for index in range(1, usa_count):
        end = index * SECTOR - 2
        record[usa_offset + index * 2 : usa_offset + index * 2 + 2] = record[
            end : end + 2
        ]
        record[end : end + 2] = _u16(_SEQUENCE)
    return bytes(record)


def build_record(attributes: list[bytes], flags: int) -> bytes:
    """One 1024-byte file record holding the given attributes."""
    record = bytearray(RECORD)
    usa_offset = 48
    usa_count = RECORD // SECTOR + 1
    attrs_offset = _align8(usa_offset + usa_count * 2)
    record[0:4] = b"FILE"
    record[4:6] = _u16(usa_offset)
    record[6:8] = _u16(usa_count)
    record[16:18] = _u16(1)  # sequence number
    record[18:20] = _u16(1)  # link count
    record[20:22] = _u16(attrs_offset)
    record[22:24] = _u16(flags)
    position = attrs_offset
    for attribute in attributes:
        record[position : position + len(attribute)] = attribute
        position += len(attribute)
    record[position : position + 4] = b"\xff\xff\xff\xff"  # end marker
    position += 8
    record[24:28] = _u32(position)  # used size
    record[28:32] = _u32(RECORD)  # allocated size
    return _apply_fixup(record)


def boot_sector() -> bytes:
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xeb\x52\x90"
    boot[3:11] = b"NTFS    "
    boot[11:13] = _u16(SECTOR)
    boot[13] = 1  # one sector per cluster
    boot[21] = 0xF8  # media descriptor
    boot[40:48] = _u64(TOTAL_CLUSTERS)  # total sectors
    boot[48:56] = _u64(MFT_LCN)
    boot[56:64] = _u64(8)  # mirror table cluster
    boot[64] = (-10) & 0xFF  # clusters per file record: 2**10 bytes
    boot[68] = 1  # clusters per index record
    boot[510:512] = b"\x55\xaa"
    return bytes(boot)


def fat_boot_sector() -> bytes:
    """A FAT12-looking boot sector: sane numbers, wrong maker."""
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"MSWIN4.1"
    boot[11:13] = _u16(SECTOR)
    boot[13] = 1
    boot[14:16] = _u16(1)  # reserved sectors
    boot[16] = 2  # two FATs
    boot[21] = 0xF8
    boot[510:512] = b"\x55\xaa"
    return bytes(boot)


class MemorySource:
    """A BlockReader over an in-memory image, for the tests."""

    def __init__(self, data: bytes):
        self._data = data
        self.size = len(data)

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError("a read cannot start before the beginning")
        if length == 0 or offset >= self.size:
            return b""
        return self._data[offset : offset + length]


class NtfsImageBuilder:
    """Assembles a whole NTFS image from hand-placed records.

    Records are added by number with add_record(); $MFT (record 0),
    the root folder (record 5), and $BITMAP (record 6) are written
    by build() itself. The bitmap starts out all-free: clusters
    are marked in use one at a time with mark_used(), which is
    what lets a test say exactly which clusters the drive claims
    have been reused.
    """

    def __init__(self, include_bitmap: bool = True):
        self.records: dict[int, bytes] = {}
        self.cluster_data: dict[int, bytes] = {}
        self.used_clusters: set[int] = set()
        self.include_bitmap = include_bitmap

    # -- ground-truth controls ------------------------------------

    def mark_used(self, lcn: int, count: int = 1) -> None:
        """Mark clusters as in use in the $BITMAP record."""
        for cluster in range(lcn, lcn + count):
            self.used_clusters.add(cluster)

    def set_cluster_data(self, lcn: int, data: bytes) -> None:
        self.cluster_data[lcn] = data

    def damage_record(self, index: int) -> None:
        """Corrupt a record's stamped sequence word, so its fixup
        no longer checks out and the record must be skipped."""
        record = bytearray(self.records[index])
        record[SECTOR - 2] ^= 0xFF
        self.records[index] = bytes(record)

    def omit_bitmap(self) -> None:
        self.include_bitmap = False

    # -- record placement ------------------------------------------

    def add_record(
        self,
        index: int,
        flags: int = 0,
        filename: str | None = None,
        namespace: int = 2,
        parent: int = 5,
        data_resident: bytes | None = None,
        data_runs=None,
        data_size: int | None = None,
        named_data: bytes | None = None,
        modified_filetime: int | None = None,
    ) -> None:
        attributes = [_standard_information(modified_filetime)]
        if data_size is None:
            if data_resident is not None:
                data_size = len(data_resident)
            elif data_runs:
                data_size = sum(c for _l, c in data_runs) * CLUSTER
            else:
                data_size = 0
        if filename is not None:
            attributes.append(
                _file_name(filename, parent, namespace, data_size)
            )
        if data_resident is not None:
            attributes.append(_attribute_resident(0x80, data_resident))
        elif data_runs is not None:
            attributes.append(
                _attribute_nonresident(0x80, data_runs, data_size)
            )
        if named_data is not None:
            attributes.append(
                _attribute_resident(0x80, named_data, name="secret")
            )
        self.records[index] = build_record(attributes, flags)

    # -- assembly ----------------------------------------------------

    def _bitmap_bytes(self) -> bytes:
        size = (TOTAL_CLUSTERS + 7) // 8
        bitmap = bytearray(size)
        for cluster in self.used_clusters:
            bitmap[cluster // 8] |= 1 << (cluster % 8)
        return bytes(bitmap)

    def build(self) -> bytes:
        records = dict(self.records)
        # Record 0: $MFT describing the table itself.
        mft_clusters = (MFT_RECORDS * RECORD) // CLUSTER
        records[0] = build_record(
            [
                _standard_information(None),
                _file_name("$MFT", 5, 2, MFT_RECORDS * RECORD),
                _attribute_nonresident(
                    0x80, [(MFT_LCN, mft_clusters)], MFT_RECORDS * RECORD
                ),
            ],
            IN_USE,
        )
        # Record 5: the root folder.
        if 5 not in records:
            records[5] = build_record(
                [
                    _standard_information(None),
                    _file_name(".", 5, 2, 0),
                ],
                IN_USE | DIRECTORY,
            )
        # Record 6: $BITMAP, its map stored in a real cluster.
        bitmap = self._bitmap_bytes()
        if self.include_bitmap:
            records[6] = build_record(
                [
                    _standard_information(None),
                    _file_name("$Bitmap", 5, 2, len(bitmap)),
                    _attribute_nonresident(
                        0x80,
                        [(BITMAP_LCN, (len(bitmap) + CLUSTER - 1) // CLUSTER)],
                        len(bitmap),
                    ),
                ],
                IN_USE,
            )
        image = bytearray(TOTAL_CLUSTERS * CLUSTER)
        image[0:SECTOR] = boot_sector()
        mft_offset = MFT_LCN * CLUSTER
        for index, record in records.items():
            start = mft_offset + index * RECORD
            image[start : start + RECORD] = record
        for lcn, data in self.cluster_data.items():
            start = lcn * CLUSTER
            image[start : start + len(data)] = data
        start = BITMAP_LCN * CLUSTER
        image[start : start + len(bitmap)] = bitmap
        return bytes(image)

    def source(self) -> MemorySource:
        return MemorySource(self.build())
