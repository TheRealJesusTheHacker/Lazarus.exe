"""Building FAT images from the specification, for the tests.

This builder owes nothing to the parser under test: every number
here — the boot record, the allocation table packing, the folder
entries, the long-name segments and their checksum — is written
straight from the FAT specification, so a parser that merely
agrees with itself still has to agree with this.

Images are kept as small as their kind allows. FAT32 cannot be
smaller than its definition (a data region of at least 65,525
clusters), so it uses one sector per cluster and lives as an
in-memory bytearray of about thirty-four megabytes.
"""

from __future__ import annotations

import struct

DEFAULT_MODIFIED = (2024, 3, 5, 12, 34, 56)


class BytesSource:
    """A BlockReader over a bytes-like object, read live."""

    def __init__(self, data):
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError("negative read")
        if length == 0 or offset >= len(self._data):
            return b""
        return bytes(self._data[offset : offset + length])


def _short_bytes(name: str) -> bytes:
    """An 11-byte short name field from a 'NAME.EXT' string."""
    if "." in name:
        base, ext = name.split(".", 1)
    else:
        base, ext = name, ""
    base = (base.upper() + " " * 8)[:8]
    ext = (ext.upper() + " " * 3)[:3]
    return (base + ext).encode("ascii")


def _display_short(name: str) -> str:
    raw = _short_bytes(name)
    base = raw[:8].decode("ascii").strip()
    ext = raw[8:].decode("ascii").strip()
    return f"{base}.{ext}" if ext else base


def _lfn_checksum(short: bytes) -> int:
    total = 0
    for byte in short:
        total = (((total & 1) << 7) | (total >> 1)) + byte
        total &= 0xFF
    return total


def _time_words(modified) -> tuple[int, int]:
    year, month, day, hour, minute, second = modified
    time_word = (hour << 11) | (minute << 5) | (second // 2)
    date_word = ((year - 1980) << 9) | (month << 5) | day
    return time_word, date_word


def _lfn_entries(long_name: str, short: bytes) -> list[bytes]:
    """Long-name entries in physical (reverse segment) order."""
    checksum = _lfn_checksum(short)
    codes = [ord(c) for c in long_name]
    codes.append(0x0000)
    while len(codes) % 13 != 0:
        codes.append(0xFFFF)
    segments = [codes[i : i + 13] for i in range(0, len(codes), 13)]
    entries: list[bytes] = []
    total = len(segments)
    for physical_index in range(total):
        segment_number = total - physical_index
        segment = segments[segment_number - 1]
        entry = bytearray(32)
        seq = segment_number
        if physical_index == 0:
            seq |= 0x40
        entry[0] = seq
        positions = (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30)
        for pos, code in zip(positions, segment):
            struct.pack_into("<H", entry, pos, code)
        entry[11] = 0x0F
        entry[12] = 0
        entry[13] = checksum
        struct.pack_into("<H", entry, 26, 0)
        entries.append(bytes(entry))
    return entries


class FileRecord:
    """Where a built file lives, so tests can delete or reuse it."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class DirInfo:
    def __init__(self, image, path, first_cluster, fixed_offset, fixed_size):
        self.image = image
        self.path = path
        self.first_cluster = first_cluster
        self.fixed_offset = fixed_offset
        self.fixed_size = fixed_size
        self.clusters: list[int] = (
            [first_cluster] if fixed_offset is None and first_cluster else []
        )

    @property
    def entries_per_cluster(self) -> int:
        return self.image.cluster_bytes // 32

    @property
    def capacity(self) -> int:
        if self.fixed_offset is not None:
            return self.fixed_size // 32
        return len(self.clusters) * self.entries_per_cluster

    def entry_offset(self, index: int) -> int:
        if self.fixed_offset is not None:
            return self.fixed_offset + index * 32
        per = self.entries_per_cluster
        cluster = self.clusters[index // per]
        return self.image.cluster_offset(cluster) + (index % per) * 32


class FatImage:
    """One formatted FAT image, built entry by entry."""

    def __init__(self, kind: str):
        if kind == "FAT12":
            bps, spc, reserved, nfats = 512, 1, 1, 2
            root_entries, fat_size, total = 224, 9, 2880
            media = 0xF0
        elif kind == "FAT16":
            bps, spc, reserved, nfats = 512, 1, 1, 2
            root_entries, fat_size, total = 512, 32, 8192
            media = 0xF8
        elif kind == "FAT32":
            bps, spc, reserved, nfats = 512, 1, 32, 2
            root_entries, fat_size, total = 0, 516, 67064
            media = 0xF8
        else:
            raise ValueError(kind)
        self.kind = kind
        self.bytes_per_sector = bps
        self.sectors_per_cluster = spc
        self.reserved_sectors = reserved
        self.fat_count = nfats
        self.root_entry_count = root_entries
        self.fat_size = fat_size
        self.total_sectors = total
        self.media = media
        self.cluster_bytes = bps * spc
        self.root_cluster = 2 if kind == "FAT32" else 0

        self.data = bytearray(total * bps)
        self._folder_records: dict[str, FileRecord] = {}
        root_sectors = ((root_entries * 32) + bps - 1) // bps
        self.fat_start = reserved * bps
        self.root_start = (reserved + nfats * fat_size) * bps
        self.data_start = self.root_start + root_sectors * bps
        self._format()
        self._next_cluster = 3 if kind == "FAT32" else 2
        if kind == "FAT32":
            self.dirs: dict[str, DirInfo] = {
                "": DirInfo(self, "", 2, None, 0)
            }
        else:
            self.dirs = {
                "": DirInfo(self, "", 0, self.root_start, root_entries * 32)
            }

    # -- layout ---------------------------------------------
    def cluster_offset(self, cluster: int) -> int:
        return self.data_start + (cluster - 2) * self.cluster_bytes

    def _format(self) -> None:
        d = self.data
        bps = self.bytes_per_sector
        d[0:3] = b"\xeb\x3c\x90"
        d[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", d, 11, bps)
        d[13] = self.sectors_per_cluster
        struct.pack_into("<H", d, 14, self.reserved_sectors)
        d[16] = self.fat_count
        struct.pack_into("<H", d, 17, self.root_entry_count)
        if self.total_sectors < 65536:
            struct.pack_into("<H", d, 19, self.total_sectors)
        else:
            struct.pack_into("<I", d, 32, self.total_sectors)
        d[21] = self.media
        if self.kind == "FAT32":
            struct.pack_into("<H", d, 22, 0)
            struct.pack_into("<I", d, 36, self.fat_size)
            struct.pack_into("<I", d, 44, self.root_cluster)
        else:
            struct.pack_into("<H", d, 22, self.fat_size)
        d[510] = 0x55
        d[511] = 0xAA
        # The allocation tables: first two entries are reserved.
        if self.kind == "FAT12":
            self.set_fat(0, 0xF00 | self.media)
            self.set_fat(1, 0xFFF)
        elif self.kind == "FAT16":
            self.set_fat(0, 0xFF00 | self.media)
            self.set_fat(1, 0xFFFF)
        else:
            self.set_fat(0, 0x0FFFFF00 | self.media)
            self.set_fat(1, 0x0FFFFFFF)
            self.set_fat(2, 0x0FFFFFFF)

    # -- the allocation table --------------------------------
    def _fat_offset(self, cluster: int, copy: int) -> int:
        base = self.fat_start + copy * self.fat_size * self.bytes_per_sector
        if self.kind == "FAT12":
            return base + cluster + cluster // 2
        if self.kind == "FAT16":
            return base + cluster * 2
        return base + cluster * 4

    def get_fat(self, cluster: int) -> int:
        off = self._fat_offset(cluster, 0)
        if self.kind == "FAT12":
            word = struct.unpack_from("<H", self.data, off)[0]
            return (word & 0x0FFF) if cluster % 2 == 0 else (word >> 4) & 0x0FFF
        if self.kind == "FAT16":
            return struct.unpack_from("<H", self.data, off)[0]
        return struct.unpack_from("<I", self.data, off)[0] & 0x0FFFFFFF

    def set_fat(self, cluster: int, value: int) -> None:
        for copy in range(self.fat_count):
            off = self._fat_offset(cluster, copy)
            if self.kind == "FAT12":
                word = struct.unpack_from("<H", self.data, off)[0]
                if cluster % 2 == 0:
                    word = (word & 0xF000) | (value & 0x0FFF)
                else:
                    word = (word & 0x000F) | ((value & 0x0FFF) << 4)
                struct.pack_into("<H", self.data, off, word)
            elif self.kind == "FAT16":
                struct.pack_into("<H", self.data, off, value & 0xFFFF)
            else:
                old = struct.unpack_from("<I", self.data, off)[0]
                struct.pack_into(
                    "<I", self.data, off,
                    (old & 0xF0000000) | (value & 0x0FFFFFFF),
                )

    def _eoc(self) -> int:
        return {"FAT12": 0xFFF, "FAT16": 0xFFFF, "FAT32": 0x0FFFFFFF}[self.kind]

    # -- allocation -------------------------------------------
    def _claim_clusters(self, count: int, forced=None) -> list[int]:
        if forced is not None:
            clusters = list(forced)
        else:
            clusters = []
            while len(clusters) < count:
                clusters.append(self._next_cluster)
                self._next_cluster += 1
        for i, cluster in enumerate(clusters):
            start = self.cluster_offset(cluster)
            self.data[start : start + self.cluster_bytes] = (
                b"\x00" * self.cluster_bytes
            )
            nxt = clusters[i + 1] if i + 1 < len(clusters) else self._eoc()
            self.set_fat(cluster, nxt)
        return clusters

    def _write_clusters(self, clusters, content: bytes) -> None:
        for i, cluster in enumerate(clusters):
            chunk = content[i * self.cluster_bytes : (i + 1) * self.cluster_bytes]
            start = self.cluster_offset(cluster)
            self.data[start : start + len(chunk)] = chunk

    # -- folder entries ----------------------------------------
    def _append_index(self, info: DirInfo, needed: int) -> int:
        index = 0
        while index < info.capacity:
            if self.data[info.entry_offset(index)] == 0x00:
                if index + needed <= info.capacity:
                    return index
                break
            index += 1
        if info.fixed_offset is not None:
            raise ValueError("fixed folder is full")
        # Grow a cluster-chain folder by one more cluster.
        extra = self._claim_clusters(1)
        info.clusters.extend(extra)
        return self._append_index(info, needed)

    def _write_entries(
        self, parent_path, short_name, attr, first_cluster, size,
        long_name=None, modified=DEFAULT_MODIFIED,
    ) -> FileRecord:
        info = self.dirs[parent_path]
        short = _short_bytes(short_name)
        lfn = _lfn_entries(long_name, short) if long_name else []
        needed = len(lfn) + 1
        start_index = self._append_index(info, needed)
        offsets = []
        for i, entry_bytes in enumerate(lfn):
            off = info.entry_offset(start_index + i)
            self.data[off : off + 32] = entry_bytes
            offsets.append(off)
        short_off = info.entry_offset(start_index + len(lfn))
        entry = bytearray(32)
        entry[0:11] = short
        entry[11] = attr
        time_word, date_word = _time_words(modified)
        struct.pack_into("<H", entry, 22, time_word)
        struct.pack_into("<H", entry, 24, date_word)
        struct.pack_into("<H", entry, 26, first_cluster & 0xFFFF)
        if self.kind == "FAT32":
            struct.pack_into("<H", entry, 20, (first_cluster >> 16) & 0xFFFF)
        struct.pack_into("<I", entry, 28, size)
        self.data[short_off : short_off + 32] = entry
        offsets.append(short_off)
        return FileRecord(
            parent_path=parent_path,
            short_name=short_name,
            long_name=long_name,
            entry_offsets=offsets,
            short_entry_offset=short_off,
            first_cluster=first_cluster,
        )

    # -- the public building verbs ------------------------------
    def add_file(
        self, parent_path, short_name, content,
        long_name=None, modified=DEFAULT_MODIFIED, forced_clusters=None,
    ) -> FileRecord:
        if isinstance(content, str):
            content = content.encode("utf-8")
        content = bytes(content)
        if len(content) == 0:
            clusters: list[int] = []
            first = 0
            if forced_clusters:
                clusters = self._claim_clusters(len(forced_clusters), forced_clusters)
                first = clusters[0]
        else:
            count = (len(content) + self.cluster_bytes - 1) // self.cluster_bytes
            if forced_clusters is not None:
                assert len(forced_clusters) >= count
                clusters = self._claim_clusters(count, list(forced_clusters)[:count])
            else:
                clusters = self._claim_clusters(count)
            first = clusters[0]
            self._write_clusters(clusters, content)
        record = self._write_entries(
            parent_path, short_name, 0x20, first, len(content),
            long_name=long_name, modified=modified,
        )
        record.content = content
        record.clusters = clusters
        return record

    def add_subdirectory(
        self, parent_path, short_name,
        long_name=None, modified=DEFAULT_MODIFIED,
    ):
        clusters = self._claim_clusters(1)
        first = clusters[0]
        display = long_name if long_name else _display_short(short_name)
        path = f"{parent_path}/{display}" if parent_path else display
        record = self._write_entries(
            parent_path, short_name, 0x10, first, 0,
            long_name=long_name, modified=modified,
        )
        record.content = b""
        record.clusters = clusters
        record.path = path
        self._folder_records[path] = record
        info = DirInfo(self, path, first, None, 0)
        self.dirs[path] = info
        # The "." and ".." entries every real folder begins with.
        parent_first = self.dirs[parent_path].first_cluster
        for name, target in ((".", first), ("..", parent_first)):
            raw = bytearray(32)
            raw[0:11] = _short_bytes(name)
            raw[11] = 0x10
            time_word, date_word = _time_words(modified)
            struct.pack_into("<H", raw, 22, time_word)
            struct.pack_into("<H", raw, 24, date_word)
            struct.pack_into("<H", raw, 26, target & 0xFFFF)
            if self.kind == "FAT32":
                struct.pack_into("<H", raw, 20, (target >> 16) & 0xFFFF)
            off = info.entry_offset(0 if name == "." else 1)
            self.data[off : off + 32] = raw
        return path

    def delete(self, record: FileRecord) -> None:
        """Delete the way Windows does: erase the entries' first
        byte and wipe the file's chain from the table. The data
        itself is left exactly where it lay."""
        for off in record.entry_offsets:
            self.data[off] = 0xE5
        for cluster in getattr(record, "clusters", []) or []:
            self.set_fat(cluster, 0)

    def zero_first_cluster(self, record: FileRecord) -> None:
        off = record.short_entry_offset
        struct.pack_into("<H", self.data, off + 26, 0)
        struct.pack_into("<H", self.data, off + 20, 0)

    def source(self) -> BytesSource:
        return BytesSource(self.data)


def create_fat12() -> FatImage:
    return FatImage("FAT12")


def create_fat16() -> FatImage:
    return FatImage("FAT16")


def create_fat32() -> FatImage:
    return FatImage("FAT32")


CREATORS = {
    "FAT12": create_fat12,
    "FAT16": create_fat16,
    "FAT32": create_fat32,
}
