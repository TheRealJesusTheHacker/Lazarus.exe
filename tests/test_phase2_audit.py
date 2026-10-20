"""Phase 2 audit pins: the hostile-input checks from the build's
own audit round, kept as tests so the fixes cannot quietly rot.

Covered: parser fuzzing (mutated images must never crash or hang
the scan, and never raise anything but a Lazarus error), odds
invariants on hostile records, the assess/recover agreement on
sparse stretches, progress-map collisions (a map may never land
on the image or on the source), corrupt progress maps, output-name
sanitising, and the destination-is-a-file refusal.
"""

import json
import os
import random
from pathlib import Path

import pytest

from lazarus import fs_fat, fs_ntfs
from lazarus.errors import LazarusError, RecoveryRefusedError, RescueError
from lazarus.recovery_model import DeletedFile, Extent
from lazarus.rescue import rescue_to_image
from lazarus.undelete import _recover_one, _safe_output_name, assess, recover_deleted

import fat_image
import ntfs_image


class BytesSource:
    def __init__(self, data):
        self._data = bytes(data)
        self.size = len(self._data)

    def read_at(self, offset, length):
        if offset >= self.size:
            return b""
        return self._data[offset : offset + length]


def _fat_bytes() -> bytes:
    image = fat_image.create_fat16()
    record = image.add_file("", "HELLO.TXT", b"hello world" * 40)
    image.delete(record)
    return bytes(image.data)


def _ntfs_bytes() -> bytes:
    builder = ntfs_image.NtfsImageBuilder()
    builder.add_record(16, flags=0, filename="gone.txt", data_resident=b"resident data")
    return builder.build()


@pytest.mark.parametrize("parser,base", [(fs_fat, _fat_bytes()), (fs_ntfs, _ntfs_bytes())])
def test_parser_survives_mutated_images(parser, base):
    rng = random.Random(20261020)
    for _ in range(150):
        data = bytearray(base)
        mode = rng.randrange(3)
        if mode == 0:
            for _ in range(rng.randrange(1, 32)):
                data[rng.randrange(len(data))] = rng.randrange(256)
        elif mode == 1:
            data = data[: rng.randrange(len(data))]
        else:
            start = rng.randrange(len(data))
            stop = min(len(data), start + rng.randrange(1, 1024))
            data[start:stop] = b"\x00" * (stop - start)
        source = BytesSource(bytes(data))
        try:
            if parser.detect(source):
                scan = parser.scan(source)
                for found in scan.files:
                    assert assess(found).verdict in ("strong", "partial", "gone")
        except LazarusError:
            pass  # a plain refusal is an acceptable answer to damage


def test_parser_detects_nothing_in_garbage():
    for parser in (fs_fat, fs_ntfs):
        for size in (0, 1, 100, 512, 4096):
            assert parser.detect(BytesSource(os.urandom(size))) is False


def test_assess_invariants_on_hostile_records():
    rng = random.Random(7)
    for _ in range(500):
        size = rng.choice([0, 1, 50, 100, 10**6])
        extents = [
            Extent(
                rng.choice([-1, 0, 512]),
                rng.choice([0, 10, 100, 10**6]),
                rng.choice([True, False, None]),
            )
            for _ in range(rng.randrange(0, 4))
        ]
        result = assess(DeletedFile(name="X", fs_type="NTFS", size=size, extents=extents))
        assert result.surviving_bytes + result.missing_bytes == size
        if result.verdict == "strong":
            assert result.missing_bytes == 0


def test_sparse_stretch_agrees_between_odds_and_recovery(tmp_path):
    # A sparse stretch was never stored: the odds call it surviving
    # (as zeros), and recovery must write those zeros without
    # counting them missing — the two may never disagree.
    record = DeletedFile(
        name="SPARSE.BIN", fs_type="NTFS", size=100,
        extents=[Extent(-1, 100, True)],
    )
    result = assess(record)
    assert result.verdict == "strong"
    out = tmp_path / "SPARSE.BIN"
    item = _recover_one(BytesSource(b""), result, out)
    assert item.outcome == "recovered"
    assert item.missing_bytes == 0
    assert out.read_bytes() == b"\x00" * 100


def test_output_names_cannot_escape_the_destination():
    taken: set[str] = set()
    for raw in ("..", ".", "", "../evil", "a/b", "a\\b", "x\x00y", "/abs"):
        out = _safe_output_name(raw, taken)
        assert "/" not in out and "\\" not in out and "\x00" not in out
        assert out not in ("", ".", "..")


class _GoodDrive:
    def __init__(self, data: bytes):
        self._data = data
        self.size = len(data)

    def read_at(self, offset, length):
        return self._data[offset : offset + length]


def test_progress_map_may_not_sit_on_the_image(tmp_path):
    source = _GoodDrive(b"\x11" * 8192)
    dest = tmp_path / "out.img"
    with pytest.raises(RescueError):
        rescue_to_image(source, dest, map_path=dest, block_size=4096)
    assert not dest.exists() or dest.read_bytes() == b"\x11" * 8192


def test_progress_map_may_not_sit_on_the_source(tmp_path):
    src = tmp_path / "drive.img"
    src.write_bytes(b"\x22" * 8192)
    dest = tmp_path / "out.img"
    with pytest.raises(RescueError):
        rescue_to_image(src, dest, map_path=src, block_size=4096)
    assert src.read_bytes() == b"\x22" * 8192


def test_corrupt_progress_map_is_a_plain_refusal(tmp_path):
    source = _GoodDrive(b"\x5a" * 16384)
    dest = tmp_path / "img.bin"
    rescue_to_image(source, dest, block_size=4096)
    map_path = Path(str(dest) + ".map.json")
    map_path.write_text("{not json")
    with pytest.raises(RescueError):
        rescue_to_image(source, dest, block_size=4096)
    # A shortened region list is just as unacceptable:
    rescue_ok = tmp_path / "img2.bin"
    rescue_to_image(source, rescue_ok, block_size=4096)
    map2 = Path(str(rescue_ok) + ".map.json")
    data = json.loads(map2.read_text())
    data["regions"] = data["regions"][:1]
    map2.write_text(json.dumps(data))
    with pytest.raises(RescueError):
        rescue_to_image(source, rescue_ok, block_size=4096)


def test_destination_that_is_a_file_is_refused_plainly(tmp_path):
    image = fat_image.create_fat16()
    record = image.add_file("", "A.TXT", b"data data")
    image.delete(record)
    src = Path("/tmp") / "lazarus-audit-dest-src.img"
    src.write_bytes(bytes(image.data))
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    try:
        if os.stat(blocker).st_dev == os.stat(src).st_dev:
            pytest.skip("need two devices for this check")
        with pytest.raises(RecoveryRefusedError):
            recover_deleted(src, blocker)
    finally:
        src.unlink(missing_ok=True)
