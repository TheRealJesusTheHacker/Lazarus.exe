"""Undelete: the odds engine, end to end — verdicts weighed from
evidence, recovery onto a different drive, and the refusals that
keep recovery from ever writing to the drive it is reading."""

import os
import tempfile
from pathlib import Path

import pytest

from lazarus.errors import RecoveryRefusedError, UnsupportedFilesystemError
from lazarus.recovery_model import DeletedFile, Extent
from lazarus.undelete import (
    VERDICT_GONE,
    VERDICT_PARTIAL,
    VERDICT_STRONG,
    assess,
    recover_deleted,
    scan_for_deleted,
)

try:
    import fat_image
except ImportError:  # pragma: no cover
    fat_image = None


def candidate(**kwargs) -> DeletedFile:
    base = dict(name="FILE.TXT", fs_type="FAT32", size=100)
    base.update(kwargs)
    return DeletedFile(**base)


# ---------------------------------------------------------------------
# The odds engine, weighed on records alone


def test_assess_resident_file_is_strong():
    result = assess(candidate(size=5, resident_data=b"hello"))
    assert result.verdict == VERDICT_STRONG
    assert result.surviving_bytes == 5 and result.missing_bytes == 0


def test_assess_empty_file_is_strong():
    result = assess(candidate(size=0, extents=[]))
    assert result.verdict == VERDICT_STRONG


def test_assess_no_extents_is_gone():
    result = assess(candidate(size=100, extents=[]))
    assert result.verdict == VERDICT_GONE
    assert result.missing_bytes == 100


def test_assess_all_free_is_strong():
    extents = [Extent(0, 60, False), Extent(60, 40, False)]
    result = assess(candidate(extents=extents))
    assert result.verdict == VERDICT_STRONG


def test_assess_some_reused_is_partial_with_the_damage_stated():
    extents = [Extent(0, 60, False), Extent(60, 40, True)]
    result = assess(candidate(extents=extents))
    assert result.verdict == VERDICT_PARTIAL
    assert result.surviving_bytes == 60 and result.missing_bytes == 40
    assert "overwritten" in result.summary


def test_assess_all_reused_is_gone():
    extents = [Extent(0, 100, True)]
    result = assess(candidate(extents=extents))
    assert result.verdict == VERDICT_GONE
    assert result.surviving_bytes == 0


def test_assess_unknown_allocation_never_claims_strong():
    extents = [Extent(0, 100, None)]
    result = assess(candidate(extents=extents))
    assert result.verdict != VERDICT_STRONG
    assert any("at risk" in line for line in result.evidence)


def test_assess_sparse_stretch_counts_as_surviving_zeros():
    extents = [Extent(-1, 40, False), Extent(4096, 60, False)]
    result = assess(candidate(extents=extents))
    assert result.verdict == VERDICT_STRONG


def test_assess_unaccounted_tail_counts_as_missing():
    extents = [Extent(0, 40, False)]  # record claims 100 bytes
    result = assess(candidate(extents=extents))
    assert result.verdict == VERDICT_PARTIAL
    assert result.missing_bytes == 60


# ---------------------------------------------------------------------
# End to end on a built FAT image. The file was stored under an 8.3
# name with no long-name entry, so — exactly as on a real drive —
# its first letter did not survive deletion and the scan shows
# "_EPORT.TXT". That honesty is part of what is being proven.


@pytest.fixture()
def fat_image_file(tmp_path):
    if fat_image is None:
        pytest.skip("FAT image builder not present")
    image = fat_image.create_fat16()
    content = b"the quarterly numbers, in full" * 30
    record = image.add_file("", "REPORT.TXT", content)
    image.delete(record)
    path = tmp_path / "drive.img"
    path.write_bytes(bytes(image.data))
    return path, content


def test_scan_reports_the_deleted_file_with_strong_odds(fat_image_file):
    path, content = fat_image_file
    report = scan_for_deleted(path)
    names = [a.name for a in report.assessments]
    assert "_EPORT.TXT" in names
    found = report.assessments[names.index("_EPORT.TXT")]
    assert found.verdict == VERDICT_STRONG
    assert found.size == len(content)


def test_recover_onto_the_same_drive_is_refused(fat_image_file):
    path, _ = fat_image_file
    # The image lives in tmp_path; recovering into tmp_path is the
    # same device — refused before a byte is written.
    with pytest.raises(RecoveryRefusedError):
        recover_deleted(path, path.parent / "out")


def test_recover_onto_a_different_drive_brings_it_back_whole(fat_image_file):
    path, content = fat_image_file
    # Put the source image on /tmp (its own filesystem) and the
    # destination under the home folder (a different one), so the
    # same-drive rule has two genuinely different drives to judge.
    tmpfs_copy = Path("/tmp") / "lazarus-e2e-drive.img"
    try:
        tmpfs_copy.write_bytes(path.read_bytes())
    except OSError:
        pytest.skip("no room on the second filesystem for this check")
    dest = Path(tempfile.mkdtemp(prefix="lazarus-dest-", dir=Path.home()))
    try:
        if os.stat(dest).st_dev == os.stat(tmpfs_copy).st_dev:
            pytest.skip("no second device available for this check")
        report = recover_deleted(tmpfs_copy, dest)
        assert report.status == "complete"
        written = dest / "_EPORT.TXT"
        assert written.read_bytes() == content
        from lazarus.hashing import hash_bytes

        item = report.items[0]
        assert item.sha256 == hash_bytes(content)
    finally:
        tmpfs_copy.unlink(missing_ok=True)
        for child in dest.iterdir():
            child.unlink()
        dest.rmdir()


def test_garbage_image_is_refused_in_plain_language(tmp_path):
    junk = tmp_path / "junk.img"
    junk.write_bytes(os.urandom(4096))
    with pytest.raises(UnsupportedFilesystemError) as caught:
        scan_for_deleted(junk)
    assert "Traceback" not in str(caught.value)
