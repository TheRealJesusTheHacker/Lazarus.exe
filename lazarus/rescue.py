"""Drive rescue: copying a failing drive to an image, gently.

A dying drive gets worse the harder it is worked, so a rescue does
not read the way an ordinary copy does. The first pass is one fast,
sequential sweep: each stretch of the drive is asked for once, and a
stretch that does not answer is left alone instead of being ground
on. Later passes come back for the gaps with much smaller reads,
which sometimes succeed where one big read did not — and sometimes
do not, because the bytes are simply gone.

Every step is written down in a progress file (a :class:`RegionMap`)
that sits beside the image. It records which stretches were copied
and which are still missing, and the places still missing read back
as zeros in the image — Lazarus never makes up bytes to fill a hole.
The progress file is saved before the first read and after every
single region, so a rescue can stop, or be stopped, at any moment
and a later run picks up exactly where this one left off.

The source is only ever read. It comes in as a
:class:`~lazarus.source.BlockReader`, or as a path that Lazarus
opens read-only itself through :class:`~lazarus.source.ReadOnlySource`,
and bytes only ever move one way: out of the source, into the image.
The image's parent folder is created if it is missing — writing the
image there is the job that was asked for, so Lazarus does not make
the user build the folder first.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import RescueError, SourceError
from .hashing import fsync_dir, fsync_file
from .source import ReadOnlySource

STATE_UNTESTED = "untested"
STATE_RECOVERED = "recovered"
STATE_UNRECOVERED = "unrecovered"

_STATES = (STATE_UNTESTED, STATE_RECOVERED, STATE_UNRECOVERED)

MAP_FORMAT = 1

_MIN_BLOCK_SIZE = 512


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _expected_region_count(source_size: int, block_size: int) -> int:
    if source_size <= 0:
        return 0
    return (source_size + block_size - 1) // block_size


def _length_of_region(source_size: int, block_size: int, index: int) -> int:
    start = index * block_size
    return min(block_size, source_size - start)


@dataclass
class RegionMap:
    """The progress file for one rescue, region by region.

    The source is divided into regions of ``block_size`` bytes (the
    last one is shorter when the size does not divide evenly). Each
    region is a record with a ``state`` — untested, recovered, or
    unrecovered — and ``holes``: the byte ranges *within* the region,
    as ``[start, length]`` pairs counted from the region's first
    byte, that have not been copied yet. A region with no holes has
    been copied in full, whatever its state says.
    """

    format: int = MAP_FORMAT
    source_size: int = 0
    block_size: int = 1024 * 1024
    regions: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        # A progress file is data, and data can be damaged or
        # tampered with. Wrong-typed fields are rejected here, at
        # load, so a bad record fails as "damaged" instead of
        # exploding later in the middle of a rescue.
        if not _is_int(self.format):
            raise TypeError("a progress record has an unusable format number")
        if self.format != MAP_FORMAT:
            raise ValueError(
                f"a progress record uses format {self.format}, "
                f"and this Lazarus only understands format {MAP_FORMAT}"
            )
        if not _is_int(self.source_size):
            raise TypeError("a progress record has an unusable source size")
        if self.source_size < 0:
            raise ValueError("a progress record has a negative source size")
        if not _is_int(self.block_size):
            raise TypeError("a progress record has an unusable block size")
        if self.block_size <= 0:
            raise ValueError("a progress record has a block size of zero or less")
        if not isinstance(self.regions, list):
            raise TypeError("a progress record's regions are not a list")
        normalised: list[dict] = []
        for index, region in enumerate(self.regions):
            normalised.append(self._checked_region(index, region))
        self.regions = normalised

    def _checked_region(self, index: int, region: object) -> dict:
        if not isinstance(region, dict):
            raise TypeError("a progress record holds a region that is not a record")
        state = region.get("state")
        if not isinstance(state, str):
            raise TypeError("a progress record holds a region with no usable state")
        if state not in _STATES:
            raise ValueError(f"a progress record holds an unknown region state: {state!r}")
        raw_holes = region.get("holes")
        if not isinstance(raw_holes, list):
            raise TypeError("a progress record holds a region whose holes are not a list")
        holes: list[list[int]] = []
        for hole in raw_holes:
            if not isinstance(hole, (list, tuple)) or len(hole) != 2:
                raise TypeError("a progress record holds a hole that is not a [start, length] pair")
            start, length = hole
            if not _is_int(start) or not _is_int(length):
                raise TypeError("a progress record holds a hole with unusable numbers")
            if start < 0 or length <= 0:
                raise ValueError("a progress record holds a hole that starts before, or covers, nothing")
            if index < _expected_region_count(self.source_size, self.block_size):
                if start + length > self.region_length(index):
                    raise ValueError(
                        "a progress record holds a hole that reaches past the end of its region"
                    )
            holes.append([start, length])
        return {"state": state, "holes": holes}

    # -- shape -------------------------------------------------------

    @property
    def region_count(self) -> int:
        return len(self.regions)

    def region_length(self, index: int) -> int:
        """The length in bytes of region ``index``."""
        if not _is_int(index):
            raise TypeError("a region number must be a whole number")
        if index < 0 or index >= _expected_region_count(self.source_size, self.block_size):
            raise IndexError(f"there is no region number {index}")
        return _length_of_region(self.source_size, self.block_size, index)

    # -- records -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "source_size": self.source_size,
            "block_size": self.block_size,
            "regions": [
                {
                    "state": region["state"],
                    "holes": [[start, length] for start, length in region["holes"]],
                }
                for region in self.regions
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RegionMap":
        if not isinstance(data, dict):
            raise TypeError("a progress record is not a record")
        source_size = data.get("source_size")
        block_size = data.get("block_size")
        raw_regions = data.get("regions")
        if not isinstance(raw_regions, list):
            raise TypeError("a progress record's regions are not a list")
        if _is_int(source_size) and _is_int(block_size) and block_size > 0:
            expected = _expected_region_count(source_size, block_size)
            if len(raw_regions) != expected:
                raise ValueError(
                    "a progress record lists "
                    f"{len(raw_regions)} region(s), but a source of "
                    f"{source_size} bytes in blocks of {block_size} "
                    f"has {expected}"
                )
        return cls(
            format=data.get("format", MAP_FORMAT),
            source_size=source_size,
            block_size=block_size,
            regions=raw_regions,
        )

    # -- files ---------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Write this progress record to ``path``, atomically.

        The record is written to a temporary file beside the real
        one and then moved into place, so a rescue stopped halfway
        through a save still leaves the previous, whole record on
        disk — never half of a new one.
        """
        path = Path(path)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        fsync_file(path)
        fsync_dir(path.parent)

    @classmethod
    def load(cls, path: str | Path) -> "RegionMap":
        """Read a progress record back from ``path``."""
        path = Path(path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise RescueError(
                f"There is no progress file at '{path}'. "
                "It may have been moved or deleted."
            ) from None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise RescueError(
                f"The progress file at '{path}' could not be read. "
                "It may be damaged; Lazarus will not guess at what it held."
            ) from None
        try:
            return cls.from_dict(data)
        except (TypeError, ValueError) as error:
            raise RescueError(
                f"The progress file at '{path}' is damaged and cannot be trusted: {error}."
            ) from None


@dataclass
class RescueReport:
    """What one rescue run did, in plain language."""

    source_description: str
    destination: str
    map_path: str
    total_bytes: int
    block_size: int
    regions_total: int
    regions_recovered: int
    regions_partial: int
    regions_unrecovered: int
    regions_untested: int
    bytes_recovered: int
    bytes_missing: int
    passes_run: int

    @property
    def status(self) -> str:
        """complete | partial — the whole rescue in one word."""
        if self.bytes_missing == 0:
            return "complete"
        return "partial"

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        lines.append(f"Drive rescue from {self.source_description}.")
        lines.append(f"Image written to: {self.destination}")
        if self.status == "complete":
            lines.append(
                f"Everything was copied: {self.total_bytes:,} bytes, "
                f"in {self.regions_total} region(s)."
            )
        else:
            missing_regions = (
                self.regions_partial
                + self.regions_unrecovered
                + self.regions_untested
            )
            lines.append(
                f"{self.bytes_recovered:,} of {self.total_bytes:,} bytes were copied."
            )
            lines.append(
                f"{self.bytes_missing:,} bytes in {missing_regions} region(s) "
                "could not be read. Those missing places are zeros in the "
                "image — nothing was made up to fill them."
            )
        lines.append(f"Passes over the drive: {self.passes_run}.")
        if self.status == "complete":
            lines.append(
                f"The progress file at {self.map_path} records that the "
                "copy is finished and nothing is left to copy."
            )
        else:
            lines.append(
                f"The progress file at {self.map_path} records what is "
                "still missing, so a later rescue run can pick up where "
                "this one stopped."
            )
        return lines

    def to_text(self) -> str:
        return "\n".join(self.summary_lines())

    def to_dict(self) -> dict:
        return {
            "source_description": self.source_description,
            "destination": self.destination,
            "map_path": self.map_path,
            "total_bytes": self.total_bytes,
            "block_size": self.block_size,
            "regions_total": self.regions_total,
            "regions_recovered": self.regions_recovered,
            "regions_partial": self.regions_partial,
            "regions_unrecovered": self.regions_unrecovered,
            "regions_untested": self.regions_untested,
            "bytes_recovered": self.bytes_recovered,
            "bytes_missing": self.bytes_missing,
            "passes_run": self.passes_run,
            "status": self.status,
        }


def _fresh_map(source_size: int, block_size: int) -> RegionMap:
    regions = []
    for index in range(_expected_region_count(source_size, block_size)):
        length = _length_of_region(source_size, block_size, index)
        regions.append({"state": STATE_UNTESTED, "holes": [[0, length]]})
    return RegionMap(
        format=MAP_FORMAT,
        source_size=source_size,
        block_size=block_size,
        regions=regions,
    )


def _remove_from_holes(
    holes: list[list[int]], start: int, length: int
) -> list[list[int]]:
    """``holes`` with the range [start, start+length) taken out."""
    if length <= 0:
        return [[h_start, h_len] for h_start, h_len in holes]
    end = start + length
    kept: list[list[int]] = []
    for h_start, h_len in holes:
        h_end = h_start + h_len
        if h_end <= start or h_start >= end:
            kept.append([h_start, h_len])
            continue
        if h_start < start:
            kept.append([h_start, start - h_start])
        if h_end > end:
            kept.append([end, h_end - end])
    return [[h_start, h_len] for h_start, h_len in kept if h_len > 0]


def _write_all_at(fd: int, data: bytes, offset: int) -> None:
    """Write all of ``data`` to the image at ``offset``."""
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.pwrite(fd, view[written:], offset + written)
        if count <= 0:
            raise OSError("writing to the image stopped partway through")
        written += count


def _build_report(
    region_map: RegionMap,
    source_description: str,
    destination: str,
    map_path: str,
    passes_run: int,
) -> RescueReport:
    recovered = 0
    partial = 0
    unrecovered = 0
    untested = 0
    bytes_missing = 0
    for index, region in enumerate(region_map.regions):
        hole_bytes = sum(length for _start, length in region["holes"])
        bytes_missing += hole_bytes
        if not region["holes"]:
            recovered += 1
        elif region["state"] == STATE_UNTESTED:
            untested += 1
        elif hole_bytes >= region_map.region_length(index):
            unrecovered += 1
        else:
            partial += 1
    total = region_map.source_size
    return RescueReport(
        source_description=source_description,
        destination=destination,
        map_path=map_path,
        total_bytes=total,
        block_size=region_map.block_size,
        regions_total=region_map.region_count,
        regions_recovered=recovered,
        regions_partial=partial,
        regions_unrecovered=unrecovered,
        regions_untested=untested,
        bytes_recovered=total - bytes_missing,
        bytes_missing=bytes_missing,
        passes_run=passes_run,
    )


def rescue_to_image(
    source,
    destination,
    map_path=None,
    *,
    block_size: int = 1024 * 1024,
    max_passes: int = 3,
) -> RescueReport:
    """Copy ``source`` to an image at ``destination``, gently.

    ``source`` is a BlockReader, or a path (``str`` or ``Path``)
    that Lazarus opens read-only itself. ``destination`` is the
    image file to write; its parent folder is created if missing.
    ``map_path`` is the progress file — by default the destination's
    name with ``.map.json`` added.

    Pass 1 asks each untouched region for all of its bytes in one
    read, in order, and moves on at once when a region does not
    answer. Passes 2 up to ``max_passes`` come back for whatever is
    still missing in smaller reads. The progress file is saved
    before the first read and after every region, so stopping at
    any point leaves an honest record, and running again with the
    same destination and progress file resumes instead of starting
    over.
    """
    if not _is_int(block_size):
        raise TypeError("the block size must be a whole number of bytes")
    if block_size < _MIN_BLOCK_SIZE:
        raise ValueError(
            f"the block size must be at least {_MIN_BLOCK_SIZE} bytes; "
            f"{block_size} is too small to rescue in"
        )
    if not _is_int(max_passes):
        raise TypeError("the number of passes must be a whole number")
    if max_passes < 1:
        raise ValueError("a rescue needs at least one pass")

    destination_path = Path(destination)
    if map_path is None:
        map_file = Path(str(destination) + ".map.json")
    else:
        map_file = Path(map_path)

    reader = source
    owns_reader = False
    source_path: Path | None = None
    if isinstance(source, (str, Path)):
        source_path = Path(source)
        if destination_path.resolve() == source_path.resolve():
            raise RescueError(
                "The image would be written over the drive it is meant "
                f"to rescue: '{source_path}' is both the source and the "
                "destination. Pick a different place for the image."
            )
    elif isinstance(source, ReadOnlySource):
        source_path = source.path

    if source_path is not None and destination_path.resolve() == source_path.resolve():
        raise RescueError(
            "The image would be written over the drive it is meant "
            f"to rescue: '{source_path}' is both the source and the "
            "destination. Pick a different place for the image."
        )

    if map_file.resolve() == destination_path.resolve():
        raise RescueError(
            "The progress file and the image are the same file: "
            f"'{destination_path}'. Saving the rescue's progress "
            "would overwrite the image it is copying. Pick a "
            "different place for the progress file."
        )

    if source_path is not None and map_file.resolve() == source_path.resolve():
        raise RescueError(
            "The progress file would be written onto the drive being "
            f"rescued: '{source_path}'. A rescue never writes to its "
            "source, in any form. Pick a different place for the "
            "progress file."
        )

    if source_path is not None and isinstance(source, (str, Path)):
        reader = ReadOnlySource(source_path)
        owns_reader = True
        source_description = str(source_path)
    elif source_path is not None:
        source_description = str(source_path)
    else:
        source_description = f"the drive handed to Lazarus ({type(source).__name__})"

    image_fd: int | None = None
    try:
        total = reader.size
        if not _is_int(total):
            raise TypeError("the source reports a size that is not a whole number")
        if total < 0:
            raise ValueError("the source reports a negative size")
        if total == 0:
            raise RescueError(
                "There is nothing to rescue: the source reports a size "
                "of zero bytes, so there is no drive to copy."
            )

        try:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if map_file.parent != destination_path.parent:
                map_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RescueError(
                "Lazarus could not create the folder for the image at "
                f"'{destination_path.parent}': {error.strerror or 'the folder could not be made'}."
            ) from error

        map_exists = map_file.exists()
        destination_exists = destination_path.exists()

        region_map: RegionMap | None = None
        if map_exists:
            region_map = RegionMap.load(map_file)
            if (
                region_map.source_size != total
                or region_map.block_size != block_size
            ):
                raise RescueError(
                    "This progress file belongs to a different rescue: "
                    f"the one at '{map_file}' was copying a source of "
                    f"{region_map.source_size:,} bytes in blocks of "
                    f"{region_map.block_size:,} bytes, and this run is "
                    f"copying {total:,} bytes in blocks of "
                    f"{block_size:,} bytes. Lazarus will not mix two "
                    "rescues together — pick a new image name, or point "
                    "at the progress file for this drive."
                )
            if not destination_exists:
                raise RescueError(
                    f"The progress file at '{map_file}' is there, but "
                    f"the image it belongs to, '{destination_path}', is "
                    "missing. The bytes it says were copied were in "
                    "that image, so Lazarus will not pretend this "
                    "rescue can carry on from it."
                )
        if destination_exists:
            if not destination_path.is_file():
                raise RescueError(
                    f"'{destination_path}' is not a file. "
                    "Pick a file name for the image."
                )
            if not map_exists:
                raise RescueError(
                    f"There is already a file at '{destination_path}' "
                    "and no progress file beside it, so it is not an "
                    "image from an earlier rescue. Lazarus will not "
                    "write over a file it cannot account for — pick a "
                    "new name for the image."
                )
            if destination_path.stat().st_size != total:
                raise RescueError(
                    f"The image at '{destination_path}' is "
                    f"{destination_path.stat().st_size:,} bytes, but the "
                    f"source is {total:,} bytes, so the two do not "
                    "belong together. Lazarus will not mix two rescues "
                    "together — pick a new image name."
                )

        fresh = region_map is None
        if fresh:
            region_map = _fresh_map(total, block_size)
            try:
                image_fd = os.open(
                    destination_path,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_TRUNC
                    | getattr(os, "O_BINARY", 0),
                )
                os.ftruncate(image_fd, total)
            except OSError as error:
                raise RescueError(
                    "Lazarus could not create the image at "
                    f"'{destination_path}': {error.strerror or 'the file could not be created'}."
                ) from error
            # The progress file goes down before the first read, so a
            # rescue interrupted at any moment leaves an honest map.
            region_map.save(map_file)
        else:
            try:
                image_fd = os.open(
                    destination_path, os.O_RDWR | getattr(os, "O_BINARY", 0)
                )
            except OSError as error:
                raise RescueError(
                    "Lazarus could not open the image at "
                    f"'{destination_path}' to carry on the rescue: "
                    f"{error.strerror or 'the file could not be opened'}."
                ) from error

        passes_run = 0

        # -- pass 1: one fast sequential sweep -----------------------
        pass_attempted = False
        for index, region in enumerate(region_map.regions):
            if region["state"] != STATE_UNTESTED:
                continue
            region_offset = index * block_size
            region_len = region_map.region_length(index)
            pass_attempted = True
            data = b""
            failed = False
            try:
                data = reader.read_at(region_offset, region_len)
            except (OSError, SourceError):
                # This stretch did not answer. Note it and move on —
                # a dying drive is never ground on.
                failed = True
            if not failed:
                if len(data) > region_len:
                    data = data[:region_len]
                if data:
                    _write_all_at(image_fd, bytes(data), region_offset)
                    region["holes"] = _remove_from_holes(
                        region["holes"], 0, len(data)
                    )
            region["state"] = (
                STATE_RECOVERED if not region["holes"] else STATE_UNRECOVERED
            )
            region_map.save(map_file)
        if pass_attempted:
            passes_run += 1

        # -- later passes: back for the gaps, in smaller reads -------
        sub_block = max(512, block_size // 16)
        for _pass_number in range(2, max_passes + 1):
            pass_attempted = False
            for index, region in enumerate(region_map.regions):
                if not region["holes"]:
                    continue
                region_offset = index * block_size
                holes = [[h_start, h_len] for h_start, h_len in region["holes"]]
                for hole_start, hole_len in list(region["holes"]):
                    hole_end = hole_start + hole_len
                    position = hole_start
                    while position < hole_end:
                        take = min(sub_block, hole_end - position)
                        pass_attempted = True
                        try:
                            data = reader.read_at(
                                region_offset + position, take
                            )
                        except (OSError, SourceError):
                            data = None
                        if data:
                            if len(data) > take:
                                data = data[:take]
                            _write_all_at(
                                image_fd,
                                bytes(data),
                                region_offset + position,
                            )
                            holes = _remove_from_holes(
                                holes, position, len(data)
                            )
                        position += take
                region["holes"] = holes
                region["state"] = (
                    STATE_RECOVERED if not holes else STATE_UNRECOVERED
                )
                region_map.save(map_file)
            if pass_attempted:
                passes_run += 1

        try:
            os.fsync(image_fd)
        except OSError:
            pass

        return _build_report(
            region_map,
            source_description,
            str(destination_path),
            str(map_file),
            passes_run,
        )
    finally:
        if image_fd is not None:
            try:
                os.close(image_fd)
            except OSError:
                pass
        if owns_reader:
            reader.close()
