"""Drive rescue: a failing drive, rehearsed on a healthy machine.

FakeDrive stands in for dying hardware: stretches that never
answer, stretches that answer only after being asked again, and
stretches that hand back half of what was asked for. The ground
truth is the image on disk afterwards — bytes where the drive gave
bytes, zeros where it did not — and the progress file that says so.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from lazarus.errors import RescueError
from lazarus.rescue import (
    STATE_RECOVERED,
    STATE_UNRECOVERED,
    STATE_UNTESTED,
    RegionMap,
    rescue_to_image,
)
from lazarus.source import ReadOnlySource

BLOCK = 4096


def make_data(size: int) -> bytes:
    """Deterministic data with no zero bytes, so a zero in an image
    can only have come from Lazarus leaving a hole unfilled."""
    return bytes((i % 255) + 1 for i in range(size))


def _overlaps(offset: int, length: int, start: int, end: int) -> bool:
    return offset < end and start < offset + length


class FakeDrive:
    """A BlockReader with rehearsed failures.

    ``fail``: byte ranges (start, end) that always raise OSError.
    ``flaky``: byte ranges that raise OSError on the first read that
    touches them and answer normally after that.
    ``short``: byte ranges whose first touching read hands back only
    half of what was asked for; later reads answer in full.
    Every read is logged in ``calls`` as (offset, length).
    """

    def __init__(self, data: bytes, *, fail=(), flaky=(), short=()):
        self._data = bytes(data)
        self.size = len(self._data)
        self.calls: list[tuple[int, int]] = []
        self._fail = list(fail)
        self._flaky = list(flaky)
        self._flaky_spent: set[int] = set()
        self._short = list(short)
        self._short_spent: set[int] = set()

    def read_at(self, offset: int, length: int) -> bytes:
        self.calls.append((offset, length))
        for index, (start, end) in enumerate(self._flaky):
            if index not in self._flaky_spent and _overlaps(
                offset, length, start, end
            ):
                self._flaky_spent.add(index)
                raise OSError("the drive did not answer")
        for start, end in self._fail:
            if _overlaps(offset, length, start, end):
                raise OSError("the drive did not answer")
        for index, (start, end) in enumerate(self._short):
            if index not in self._short_spent and _overlaps(
                offset, length, start, end
            ):
                self._short_spent.add(index)
                half = max(1, length // 2)
                return self._data[offset : offset + half]
        return self._data[offset : offset + length]


class GuardReader:
    """A BlockReader that objects to being asked about anything
    other than its size and its bytes.

    If the rescue engine so much as looks at another attribute —
    a path, a close method, anything — this raises AssertionError
    and the test fails.
    """

    def __init__(self, inner: FakeDrive):
        self._inner = inner

    @property
    def size(self) -> int:
        return self._inner.size

    def read_at(self, offset: int, length: int) -> bytes:
        return self._inner.read_at(offset, length)

    def __getattr__(self, name):
        raise AssertionError(
            f"the rescue engine asked its source for {name!r}, "
            "which is none of its business"
        )


# -- the clean case ---------------------------------------------------


def test_clean_source_clones_byte_for_byte(tmp_path):
    data = make_data(BLOCK * 3)
    drive = FakeDrive(data)
    destination = tmp_path / "image.bin"

    report = rescue_to_image(drive, destination, block_size=BLOCK)

    assert destination.read_bytes() == data
    assert report.status == "complete"
    assert report.total_bytes == len(data)
    assert report.bytes_recovered == len(data)
    assert report.bytes_missing == 0
    assert report.regions_total == 3
    assert report.regions_recovered == 3
    assert report.regions_partial == 0
    assert report.regions_unrecovered == 0
    assert report.regions_untested == 0
    assert report.passes_run == 1
    saved = RegionMap.load(report.map_path)
    assert all(region["state"] == STATE_RECOVERED for region in saved.regions)
    assert all(region["holes"] == [] for region in saved.regions)


def test_path_source_clones_byte_for_byte(tmp_path):
    data = make_data(BLOCK * 2)
    source_file = tmp_path / "drive.img"
    source_file.write_bytes(data)
    destination = tmp_path / "image.bin"

    report = rescue_to_image(str(source_file), destination, block_size=BLOCK)

    assert report.status == "complete"
    assert destination.read_bytes() == data
    assert source_file.read_bytes() == data


# -- failing hardware ---------------------------------------------------


def test_always_failing_region_is_zeros_and_counted(tmp_path):
    data = make_data(BLOCK * 3)
    dead = (BLOCK, BLOCK * 2)
    drive = FakeDrive(data, fail=[dead])
    destination = tmp_path / "image.bin"

    report = rescue_to_image(drive, destination, block_size=BLOCK)

    image = destination.read_bytes()
    assert image[:BLOCK] == data[:BLOCK]
    assert image[BLOCK : BLOCK * 2] == b"\x00" * BLOCK
    assert image[BLOCK * 2 :] == data[BLOCK * 2 :]
    assert report.status == "partial"
    assert report.regions_recovered == 2
    assert report.regions_unrecovered == 1
    assert report.regions_partial == 0
    assert report.bytes_missing == BLOCK
    assert report.bytes_recovered == len(data) - BLOCK
    # The fast first pass asked the dead region for its bytes exactly
    # once, as one whole-region read — it did not grind on it.
    whole_region_asks = [
        call for call in drive.calls if call == (BLOCK, BLOCK)
    ]
    assert len(whole_region_asks) == 1
    # The saved progress file tells the same story.
    saved = RegionMap.load(report.map_path)
    assert saved.regions[1]["state"] == STATE_UNRECOVERED
    assert saved.regions[1]["holes"] == [[0, BLOCK]]
    # And the report says, in plain words, that the missing places
    # are zeros and that a later run can pick up where this stopped.
    text = report.to_text()
    assert "zeros in the image" in text
    assert "pick up where this one stopped" in text
    assert report.map_path in text


def test_flaky_region_is_recovered_by_a_later_pass(tmp_path):
    data = make_data(BLOCK * 3)
    drive = FakeDrive(data, flaky=[(BLOCK, BLOCK * 2)])
    destination = tmp_path / "image.bin"

    report = rescue_to_image(drive, destination, block_size=BLOCK)

    assert report.status == "complete"
    assert report.passes_run == 2
    assert destination.read_bytes() == data


def test_short_read_leaves_partial_holes(tmp_path):
    data = make_data(BLOCK * 3)
    drive = FakeDrive(data, short=[(BLOCK, BLOCK * 2)])
    destination = tmp_path / "image.bin"

    report = rescue_to_image(
        drive, destination, block_size=BLOCK, max_passes=1
    )

    image = destination.read_bytes()
    half = BLOCK // 2
    assert image[BLOCK : BLOCK + half] == data[BLOCK : BLOCK + half]
    assert image[BLOCK + half : BLOCK * 2] == b"\x00" * half
    assert report.status == "partial"
    assert report.regions_partial == 1
    assert report.regions_unrecovered == 0
    assert report.bytes_missing == half
    saved = RegionMap.load(report.map_path)
    assert saved.regions[1]["state"] == STATE_UNRECOVERED
    assert saved.regions[1]["holes"] == [[half, BLOCK - half]]


# -- stopping and resuming ----------------------------------------------


def test_resume_reads_only_the_missing_ranges(tmp_path):
    data = make_data(BLOCK * 3)
    destination = tmp_path / "image.bin"

    dying = FakeDrive(data, fail=[(BLOCK, BLOCK * 2)])
    first = rescue_to_image(dying, destination, block_size=BLOCK)
    assert first.status == "partial"

    repaired = FakeDrive(data)
    second = rescue_to_image(repaired, destination, block_size=BLOCK)

    assert second.status == "complete"
    assert destination.read_bytes() == data
    # The second run read nothing outside the one missing region,
    # and covered that region in small reads, start to finish.
    assert repaired.calls
    assert all(
        BLOCK <= offset and offset + length <= BLOCK * 2
        for offset, length in repaired.calls
    )
    assert [offset for offset, _length in repaired.calls] == list(
        range(BLOCK, BLOCK * 2, 512)
    )
    assert all(length == 512 for _offset, length in repaired.calls)


def test_resume_processes_regions_left_untested(tmp_path):
    """A map from a run stopped during pass 1: region 0 done, region 1
    never attempted, region 2 attempted and missing."""
    data = make_data(BLOCK * 3)
    destination = tmp_path / "image.bin"
    destination.write_bytes(b"\x00" * len(data))
    with open(destination, "r+b") as handle:
        handle.write(data[:BLOCK])
    region_map = RegionMap(
        source_size=len(data),
        block_size=BLOCK,
        regions=[
            {"state": STATE_RECOVERED, "holes": []},
            {"state": STATE_UNTESTED, "holes": [[0, BLOCK]]},
            {"state": STATE_UNRECOVERED, "holes": [[0, BLOCK]]},
        ],
    )
    map_path = tmp_path / "image.bin.map.json"
    region_map.save(map_path)

    drive = FakeDrive(data)
    report = rescue_to_image(
        drive, destination, map_path, block_size=BLOCK
    )

    assert report.status == "complete"
    assert destination.read_bytes() == data
    # Region 1, still untested, got the one whole-region pass-1 read.
    assert (BLOCK, BLOCK) in drive.calls
    # Region 0, already recovered, was never read again.
    assert all(offset >= BLOCK for offset, _length in drive.calls)


def test_map_from_a_different_sized_source_is_refused(tmp_path):
    destination = tmp_path / "image.bin"
    small = FakeDrive(make_data(BLOCK * 2))
    rescue_to_image(small, destination, block_size=BLOCK)
    image_before = destination.read_bytes()

    bigger = FakeDrive(make_data(BLOCK * 3))
    with pytest.raises(RescueError) as caught:
        rescue_to_image(bigger, destination, block_size=BLOCK)

    assert "different rescue" in str(caught.value)
    assert destination.read_bytes() == image_before


# -- refusals -----------------------------------------------------------


def test_destination_same_as_source_path_is_refused(tmp_path):
    data = make_data(BLOCK * 2)
    source_file = tmp_path / "drive.img"
    source_file.write_bytes(data)

    with pytest.raises(RescueError):
        rescue_to_image(source_file, source_file, block_size=BLOCK)

    assert source_file.read_bytes() == data


def test_existing_unrelated_destination_is_refused(tmp_path):
    data = make_data(BLOCK * 2)
    destination = tmp_path / "image.bin"
    unrelated = b"x" * len(data)  # even the same size does not help it
    destination.write_bytes(unrelated)

    with pytest.raises(RescueError):
        rescue_to_image(FakeDrive(data), destination, block_size=BLOCK)

    assert destination.read_bytes() == unrelated


def test_empty_source_is_refused(tmp_path):
    destination = tmp_path / "image.bin"

    with pytest.raises(RescueError) as caught:
        rescue_to_image(FakeDrive(b""), destination, block_size=BLOCK)

    assert "nothing to rescue" in str(caught.value)
    assert not destination.exists()


def test_tiny_block_size_is_refused(tmp_path):
    with pytest.raises(ValueError):
        rescue_to_image(
            FakeDrive(make_data(BLOCK)),
            tmp_path / "image.bin",
            block_size=256,
        )


def test_missing_destination_folder_is_created(tmp_path):
    data = make_data(BLOCK)
    destination = tmp_path / "new" / "folder" / "image.bin"

    report = rescue_to_image(FakeDrive(data), destination, block_size=BLOCK)

    assert report.status == "complete"
    assert destination.read_bytes() == data


# -- the source is only ever read ---------------------------------------


def test_read_only_source_stays_read_only_and_open(tmp_path):
    data = make_data(BLOCK * 2)
    source_file = tmp_path / "drive.img"
    source_file.write_bytes(data)
    source = ReadOnlySource(source_file)
    try:
        report = rescue_to_image(
            source, tmp_path / "image.bin", block_size=BLOCK
        )
        assert report.status == "complete"
        assert source.opened_read_only is True
        # The engine did not close a source it did not open.
        assert source.read_at(0, 4) == data[:4]
    finally:
        source.close()
    assert source_file.read_bytes() == data


def test_engine_asks_its_source_for_nothing_but_size_and_bytes(tmp_path):
    data = make_data(BLOCK * 2)
    guard = GuardReader(FakeDrive(data))
    with pytest.raises(AssertionError):
        guard.path  # the guard itself works: anything else is refused

    report = rescue_to_image(guard, tmp_path / "image.bin", block_size=BLOCK)

    assert report.status == "complete"
    assert (tmp_path / "image.bin").read_bytes() == data


# -- the progress record itself ------------------------------------------


def _sample_map() -> RegionMap:
    return RegionMap(
        source_size=BLOCK * 2 + 100,
        block_size=BLOCK,
        regions=[
            {"state": STATE_RECOVERED, "holes": []},
            {"state": STATE_UNRECOVERED, "holes": [[512, 1024]]},
            {"state": STATE_UNTESTED, "holes": [[0, 100]]},
        ],
    )


def test_region_map_shape_and_lengths():
    region_map = _sample_map()

    assert region_map.format == 1
    assert region_map.region_count == 3
    assert region_map.region_length(0) == BLOCK
    assert region_map.region_length(1) == BLOCK
    assert region_map.region_length(2) == 100


def test_region_map_dict_round_trip():
    region_map = _sample_map()

    restored = RegionMap.from_dict(region_map.to_dict())

    assert restored == region_map
    assert restored.to_dict() == region_map.to_dict()


def test_region_map_save_and_load_round_trip(tmp_path):
    region_map = _sample_map()
    path = tmp_path / "progress.map.json"

    region_map.save(path)

    assert RegionMap.load(path) == region_map
    # The atomic save left no temporary files behind.
    assert [p.name for p in tmp_path.iterdir()] == ["progress.map.json"]


def test_region_map_rejects_damaged_records():
    good = _sample_map().to_dict()

    with pytest.raises(TypeError):
        RegionMap.from_dict("not a record")
    with pytest.raises(ValueError):
        RegionMap.from_dict({**good, "format": 99})
    with pytest.raises(TypeError):
        RegionMap.from_dict({**good, "source_size": "big"})

    bad_state = _sample_map().to_dict()
    bad_state["regions"][0]["state"] = "somehow"
    with pytest.raises(ValueError):
        RegionMap.from_dict(bad_state)

    bad_hole = _sample_map().to_dict()
    bad_hole["regions"][1]["holes"] = [["x", 10]]
    with pytest.raises(TypeError):
        RegionMap.from_dict(bad_hole)

    oversized_hole = _sample_map().to_dict()
    oversized_hole["regions"][0]["holes"] = [[BLOCK - 10, 20]]
    with pytest.raises(ValueError):
        RegionMap.from_dict(oversized_hole)

    wrong_count = _sample_map().to_dict()
    wrong_count["regions"] = wrong_count["regions"][:2]
    with pytest.raises(ValueError):
        RegionMap.from_dict(wrong_count)
