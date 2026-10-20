"""NTFS parser tests, against images built byte by byte.

Every image here comes from tests/ntfs_image.py, an independent
builder written from the NTFS layout — so each test states its
ground truth directly: these clusters were marked reused, this
record's fixup was broken, this file was still live. The parser
has to find exactly what the builder put in, no more, no less.
"""

from datetime import datetime, timezone

import ntfs_image
from ntfs_image import (
    CLUSTER,
    DIRECTORY,
    IN_USE,
    MFT_LCN,
    RECORD,
    MemorySource,
    NtfsImageBuilder,
)

from lazarus import fs_ntfs
from lazarus.errors import LazarusError
from lazarus.recovery_model import FsScan

BITMAP_EVIDENCE = (
    "the drive's in-use map could not be read, so Lazarus cannot "
    "tell which parts have been reused"
)


def filetime(moment: datetime) -> int:
    """FILETIME ticks (100ns since 1601-01-01) for a UTC moment."""
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return int((moment - epoch).total_seconds() * 10_000_000)


# ---------------------------------------------------------------------
# detect()


def test_detect_accepts_the_built_image():
    assert fs_ntfs.detect(NtfsImageBuilder().source()) is True


def test_detect_rejects_garbage():
    garbage = bytes(range(256)) * 8
    assert fs_ntfs.detect(MemorySource(garbage)) is False
    assert fs_ntfs.detect(MemorySource(b"\x00" * 1024)) is False
    assert fs_ntfs.detect(MemorySource(b"")) is False


def test_detect_rejects_a_fat_looking_image():
    image = bytearray(2048)
    image[0:512] = ntfs_image.fat_boot_sector()
    assert fs_ntfs.detect(MemorySource(bytes(image))) is False


# ---------------------------------------------------------------------
# Resident deleted files


def test_deleted_resident_file_comes_back_with_its_bytes():
    builder = NtfsImageBuilder()
    builder.add_record(
        20, flags=IN_USE | DIRECTORY, filename="Docs", parent=5
    )
    moment = datetime(2024, 3, 5, 6, 7, 8, tzinfo=timezone.utc)
    builder.add_record(
        16,
        flags=0,
        filename="notes.txt",
        parent=20,
        data_resident=b"hello recovered world",
        modified_filetime=filetime(moment),
    )
    scan = fs_ntfs.scan(builder.source())
    assert isinstance(scan, FsScan)
    assert len(scan.files) == 1
    found = scan.files[0]
    assert found.name == "notes.txt"
    assert found.fs_type == "NTFS"
    assert found.size == len(b"hello recovered world")
    assert found.resident_data == b"hello recovered world"
    assert found.extents == []
    assert found.directory == "Docs"
    assert found.modified_utc == "2024-03-05T06:07:08Z"
    assert "its contents live inside the surviving record itself" in found.evidence
    assert any(note.startswith("Scanned ") for note in scan.notes)
    assert any("1 deleted file(s) found" in note for note in scan.notes)


# ---------------------------------------------------------------------
# Non-resident deleted files and the in-use map


def test_nonresident_file_splits_where_the_bitmap_says_reused():
    builder = NtfsImageBuilder()
    builder.add_record(
        16,
        flags=0,
        filename="photo.bin",
        data_runs=[(100, 3)],
        data_size=1300,
    )
    builder.mark_used(101)  # the middle cluster has been handed out again
    scan = fs_ntfs.scan(builder.source())
    assert len(scan.files) == 1
    found = scan.files[0]
    assert found.size == 1300
    assert found.resident_data is None
    assert [extent.in_use for extent in found.extents] == [False, True, False]
    assert [extent.start for extent in found.extents] == [
        100 * CLUSTER,
        101 * CLUSTER,
        102 * CLUSTER,
    ]
    # 1300 bytes over three clusters: the last is clipped to 276.
    assert [extent.length for extent in found.extents] == [512, 512, 276]
    assert "its data sat in 3 stretch(es) of the drive" in found.evidence


def test_sparse_stretch_was_never_stored():
    builder = NtfsImageBuilder()
    builder.add_record(
        16,
        flags=0,
        filename="sparse.bin",
        data_runs=[(None, 1), (120, 1)],
        data_size=2 * CLUSTER,
    )
    scan = fs_ntfs.scan(builder.source())
    (found,) = scan.files
    assert found.extents[0].start == -1
    assert found.extents[0].length == CLUSTER
    assert found.extents[0].in_use is False
    assert found.extents[1].start == 120 * CLUSTER


def test_missing_bitmap_leaves_every_stretch_unknown():
    builder = NtfsImageBuilder()
    builder.add_record(
        16,
        flags=0,
        filename="lost.bin",
        data_runs=[(100, 2)],
        data_size=2 * CLUSTER,
    )
    builder.omit_bitmap()
    scan = fs_ntfs.scan(builder.source())
    (found,) = scan.files
    assert [extent.in_use for extent in found.extents] == [None]
    assert BITMAP_EVIDENCE in found.evidence


# ---------------------------------------------------------------------
# What a scan must not return, and must not hide


def test_live_file_is_never_returned():
    builder = NtfsImageBuilder()
    builder.add_record(
        16, flags=IN_USE, filename="alive.txt", data_resident=b"still here"
    )
    builder.add_record(
        17, flags=0, filename="gone.txt", data_resident=b"was here"
    )
    scan = fs_ntfs.scan(builder.source())
    assert [found.name for found in scan.files] == ["gone.txt"]


def test_broken_fixup_record_is_skipped_and_counted():
    builder = NtfsImageBuilder()
    builder.add_record(
        16, flags=0, filename="good.txt", data_resident=b"good"
    )
    builder.add_record(
        17, flags=0, filename="bad.txt", data_resident=b"bad"
    )
    builder.damage_record(17)
    scan = fs_ntfs.scan(builder.source())
    assert [found.name for found in scan.files] == ["good.txt"]
    assert (
        "1 record(s) were too damaged to read and were skipped" in scan.notes
    )


def test_deleted_folder_is_not_returned_but_its_file_is():
    builder = NtfsImageBuilder()
    builder.add_record(
        20, flags=DIRECTORY, filename="OldStuff", parent=5
    )
    builder.add_record(
        16,
        flags=0,
        filename="kept.txt",
        parent=20,
        data_resident=b"kept",
    )
    scan = fs_ntfs.scan(builder.source())
    assert [found.name for found in scan.files] == ["kept.txt"]
    assert scan.files[0].directory == "OldStuff"
    assert (
        "files inside deleted folders still have their own records "
        "and are found on their own" in scan.notes
    )


def test_named_stream_is_listed_by_its_main_contents_only():
    builder = NtfsImageBuilder()
    builder.add_record(
        16,
        flags=0,
        filename="plain.txt",
        data_resident=b"main contents",
        named_data=b"the extra stream",
    )
    scan = fs_ntfs.scan(builder.source())
    (found,) = scan.files
    assert found.resident_data == b"main contents"
    assert (
        "files with extra data streams were listed by their main "
        "contents only" in scan.notes
    )


# ---------------------------------------------------------------------
# Truncation: nothing but a Lazarus error may escape a scan, and a
# shortened tail is damage to report, not a crash


def test_truncated_image_never_raises_a_foreign_exception():
    builder = NtfsImageBuilder()
    builder.add_record(
        10, flags=0, filename="early.txt", data_resident=b"early"
    )
    builder.add_record(
        24, flags=0, filename="late.txt", data_resident=b"late"
    )
    image = builder.build()
    cut = MFT_LCN * CLUSTER + 12 * RECORD  # the table runs past the end
    try:
        scan = fs_ntfs.scan(MemorySource(image[:cut]))
    except LazarusError:
        return  # a plain refusal is an acceptable outcome here
    assert isinstance(scan, FsScan)
    assert [found.name for found in scan.files] == ["early.txt"]
    assert any("too damaged" in note for note in scan.notes)


def test_scan_of_garbage_is_a_plain_refusal():
    try:
        fs_ntfs.scan(MemorySource(b"\x00" * 2048))
    except LazarusError:
        return
    raise AssertionError("scan of garbage should have been refused")
