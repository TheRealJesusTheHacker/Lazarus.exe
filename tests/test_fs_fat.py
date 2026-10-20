"""The FAT parser, checked against images built from the spec.

Ground truth is whatever the builder in fat_image.py put in —
names, sizes, cluster chains, deletion marks — never anything
the parser itself reported, so these tests fail if the parser
and reality drift apart in either direction.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from fat_image import CREATORS, BytesSource, create_fat16

from lazarus import fs_fat
from lazarus.errors import LazarusError
from lazarus.fs_fat import detect, scan

KINDS = ["FAT12", "FAT16", "FAT32"]
CREATOR_PARAMS = [pytest.param(kind, id=kind) for kind in KINDS]

CONTENT = (b"quarterly numbers, handle with care. " * 40)[:1200]


def _read_back(source, deleted) -> bytes:
    out = bytearray()
    for extent in deleted.extents:
        out += source.read_at(extent.start, extent.length)
    return bytes(out[: deleted.size])


def _only(scan_result, name=None):
    files = scan_result.files
    if name is not None:
        files = [f for f in files if f.name == name]
    assert len(files) == 1, [f.name for f in scan_result.files]
    return files[0]


# -- detect ------------------------------------------------------


def test_detect_accepts_each_kind():
    for kind in KINDS:
        image = CREATORS[kind]()
        assert detect(image.source()) is True, kind
        assert fs_fat.FS_FAMILY == "FAT"


def test_detect_rejects_random_garbage():
    rng = random.Random(20261020)
    garbage = bytes(rng.randrange(256) for _ in range(4096))
    assert detect(BytesSource(garbage)) is False


def test_detect_rejects_empty_and_tiny():
    assert detect(BytesSource(b"")) is False
    assert detect(BytesSource(b"\x55\xaa")) is False
    assert detect(BytesSource(b"\x00" * 4096)) is False


def test_detect_rejects_ntfs_shaped_boot_sector():
    # NTFS by its real marks: the maker's name at byte 3, and a
    # reserved-sector count of zero, which no FAT drive has.
    data = bytearray(4096)
    data[3:11] = b"NTFS    "
    data[11:13] = (512).to_bytes(2, "little")
    data[13] = 8
    data[510] = 0x55
    data[511] = 0xAA
    assert detect(BytesSource(bytes(data))) is False


# -- intact deleted files, one per kind ---------------------------


@pytest.mark.parametrize("kind", CREATOR_PARAMS)
def test_intact_deleted_file_each_kind(kind):
    image = CREATORS[kind]()
    record = image.add_file(
        "", "REPORT.TXT", CONTENT, long_name="monthly report.txt"
    )
    image.delete(record)
    result = scan(image.source())
    found = _only(result, "monthly report.txt")
    assert found.fs_type == kind
    assert found.size == len(CONTENT)
    assert found.directory == ""
    assert found.extents, "a file with data must have extents"
    assert all(e.in_use is False for e in found.extents)
    assert _read_back(image.source(), found) == CONTENT
    assert found.modified_utc == "2024-03-05T12:34:56Z"
    assert any(
        "one unbroken stretch" in line for line in found.evidence
    ), found.evidence


# -- reuse: parts of the stretch handed to a new file ------------


@pytest.mark.parametrize("kind", CREATOR_PARAMS)
def test_partly_reused_clusters_flagged(kind):
    image = CREATORS[kind]()
    record = image.add_file("", "DATA.BIN", CONTENT)
    assert len(record.clusters) == 3
    image.delete(record)
    middle = record.clusters[1]
    image.add_file(
        "", "NEW.BIN", b"N" * 200, forced_clusters=[middle]
    )
    found = _only(scan(image.source()), "_ATA.BIN")
    assert len(found.extents) == 3
    assert [e.in_use for e in found.extents] == [False, True, False]


def test_fully_reused_clusters_all_flagged():
    image = create_fat16()
    record = image.add_file("", "DATA.BIN", CONTENT)
    image.delete(record)
    image.add_file(
        "", "FULL.BIN", b"F" * len(CONTENT),
        forced_clusters=list(record.clusters),
    )
    found = _only(scan(image.source()), "_ATA.BIN")
    assert len(found.extents) == 3
    assert all(e.in_use is True for e in found.extents)


# -- names ----------------------------------------------------------


def test_long_name_recovered_in_full_after_delete():
    image = create_fat16()
    long_name = "a very long deleted filename.txt"
    record = image.add_file(
        "", "AVERYL~1.TXT", b"payload", long_name=long_name
    )
    image.delete(record)
    found = _only(scan(image.source()))
    assert found.name == long_name


def test_short_only_deleted_name_loses_first_letter():
    image = create_fat16()
    record = image.add_file("", "REPORT.TXT", b"payload")
    image.delete(record)
    found = _only(scan(image.source()))
    assert found.name == "_EPORT.TXT"
    assert found.name.startswith("_")
    assert any(
        "first letter of its name was erased" in line
        for line in found.evidence
    ), found.evidence


# -- folders ----------------------------------------------------------


def test_deleted_file_inside_live_subdirectory():
    image = create_fat16()
    folder = image.add_subdirectory("", "SUBDIR")
    assert folder == "SUBDIR"
    record = image.add_file(folder, "INNER.TXT", b"inside data")
    image.delete(record)
    found = _only(scan(image.source()))
    assert found.directory == "SUBDIR"
    assert found.name == "_NNER.TXT"
    assert found.display_name == "SUBDIR/_NNER.TXT"


def test_deleted_file_inside_deleted_subdirectory_still_found():
    image = create_fat16()
    folder = image.add_subdirectory("", "OLD")
    record = image.add_file(folder, "GONE.TXT", b"nested data")
    # Delete the inner file, then the folder holding it, using
    # the record the builder kept for the folder's own entry.
    image.delete(record)
    image.delete(image._folder_records[folder])
    result = scan(image.source())
    matches = [f for f in result.files if f.name == "_ONE.TXT"]
    assert len(matches) == 1
    assert matches[0].directory == "_LD"
    assert any("first piece" in note for note in result.notes)


# -- damaged records ----------------------------------------------------


def test_first_cluster_zeroed_means_no_extents():
    image = create_fat16()
    record = image.add_file("", "LOST.TXT", CONTENT)
    image.delete(record)
    image.zero_first_cluster(record)
    found = _only(scan(image.source()))
    assert found.extents == []
    assert any(
        "no longer recorded" in line for line in found.evidence
    ), found.evidence


def test_live_file_is_not_returned():
    image = create_fat16()
    image.add_file("", "ALIVE.TXT", b"still here")
    gone = image.add_file("", "GONE.TXT", b"was here")
    image.delete(gone)
    result = scan(image.source())
    names = [f.name for f in result.files]
    assert names == ["_ONE.TXT"]


def test_zero_size_deleted_file_has_no_extents():
    image = create_fat16()
    record = image.add_file("", "EMPTY.TXT", b"")
    image.delete(record)
    found = _only(scan(image.source()))
    assert found.size == 0
    assert found.extents == []


def test_truncated_image_scans_without_foreign_errors():
    image = create_fat16()
    record = image.add_file("", "FIRST.TXT", b"early entry")
    image.delete(record)
    image.add_file("", "SECOND.TXT", b"later entry")
    # Cut the drive in the middle of the root folder: the boot
    # record and the tables survive, the folder does not.
    cut = image.root_start + 32 + 16
    truncated = BytesSource(bytes(image.data[:cut]))
    try:
        result = scan(truncated)
    except LazarusError:
        result = None
    assert result is not None, "a truncated folder must not raise"
    assert isinstance(result.files, list)
    assert isinstance(result.notes, list)


def test_scan_of_garbage_raises_plain_lazarus_error():
    with pytest.raises(LazarusError):
        scan(BytesSource(b"\x00" * 1024))
