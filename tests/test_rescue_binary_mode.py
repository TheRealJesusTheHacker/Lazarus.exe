"""Rescue image opens: the image must be opened in binary mode.

On Windows a file opened without ``os.O_BINARY`` is in text mode,
and the C runtime translates every 0x0A byte written through it
into 0x0D 0x0A. Rescue writes rescued drive bytes through a raw
file descriptor with ``os.pwrite``, so nothing else can undo that
translation: the image would come out longer than the drive, with
extra 0x0D bytes in it and everything after the first 0x0A shifted.

Linux has no text mode, so these tests model the Windows runtime
instead. ``os.O_BINARY`` is given a stand-in value, ``os.open``
records whether that flag was asked for, and ``os.pwrite`` applies
the translation to writes on descriptors opened without it — and
passes bytes straight through on descriptors opened with it.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from lazarus.rescue import (
    STATE_RECOVERED,
    STATE_UNRECOVERED,
    RegionMap,
    rescue_to_image,
)

BLOCK = 4096

# A stand-in for the Windows-only flag, in a bit no real flag used
# by this code occupies. The model strips it before handing the
# flags to the real os.open, so the host system never sees it.
_FAKE_O_BINARY = 1 << 30

_real_open = os.open
_real_pwrite = os.pwrite
_real_close = os.close


class WindowsTextMode:
    """A model of the Windows C runtime's text-mode translation."""

    def __init__(self):
        self.binary_fds: dict[int, bool] = {}

    def open(self, path, flags, *args, **kwargs):
        is_binary = bool(flags & _FAKE_O_BINARY)
        fd = _real_open(path, flags & ~_FAKE_O_BINARY, *args, **kwargs)
        self.binary_fds[fd] = is_binary
        return fd

    def pwrite(self, fd, data, offset):
        raw = bytes(data)
        if self.binary_fds.get(fd, False):
            return _real_pwrite(fd, raw, offset)
        translated = raw.replace(b"\n", b"\r\n")
        _real_pwrite(fd, translated, offset)
        # The caller is told it wrote the bytes it handed over; the
        # extra 0x0D bytes on disk are exactly the quiet corruption
        # this model exists to expose.
        return len(raw)

    def close(self, fd):
        self.binary_fds.pop(fd, None)
        return _real_close(fd)


@pytest.fixture()
def windows_crt(monkeypatch):
    model = WindowsTextMode()
    monkeypatch.setattr(os, "O_BINARY", _FAKE_O_BINARY, raising=False)
    monkeypatch.setattr(os, "open", model.open)
    monkeypatch.setattr(os, "pwrite", model.pwrite)
    monkeypatch.setattr(os, "close", model.close)
    return model


def _overlaps(offset: int, length: int, start: int, end: int) -> bool:
    return offset < end and start < offset + length


class FakeDrive:
    """A BlockReader whose bytes include 0x0A and 0x0D 0x0A pairs."""

    def __init__(self, data: bytes, *, fail=()):
        self._data = bytes(data)
        self.size = len(self._data)
        self._fail = list(fail)

    def read_at(self, offset: int, length: int) -> bytes:
        for start, end in self._fail:
            if _overlaps(offset, length, start, end):
                raise OSError("the drive did not answer")
        return self._data[offset : offset + length]


def make_data(size: int) -> bytes:
    """Deterministic bytes rich in 0x0A and in 0x0D 0x0A pairs, so a
    text-mode write cannot pass for a clean one by luck."""
    pattern = b"A\nB\r\nC\rD\n\n" + bytes(range(256))
    repeats = size // len(pattern) + 1
    return (pattern * repeats)[:size]


# -- the model itself ---------------------------------------------------


def test_model_translates_text_mode_and_passes_binary_through(
    tmp_path, windows_crt
):
    """The control: without the flag the model really does translate,
    and with it the same bytes pass through untouched. Without this,
    the rescue tests below could pass vacuously."""
    payload = b"x\ny\r\nz\n"

    text_path = tmp_path / "text.bin"
    text_fd = os.open(text_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        assert os.pwrite(text_fd, payload, 0) == len(payload)
    finally:
        os.close(text_fd)
    assert text_path.read_bytes() == b"x\r\ny\r\r\nz\r\n"
    assert text_path.read_bytes() != payload

    binary_path = tmp_path / "binary.bin"
    binary_fd = os.open(
        binary_path,
        os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
    )
    try:
        assert os.pwrite(binary_fd, payload, 0) == len(payload)
    finally:
        os.close(binary_fd)
    assert binary_path.read_bytes() == payload


# -- the fresh image (first open site) ----------------------------------


def test_fresh_rescue_image_is_byte_exact_under_windows_text_mode(
    tmp_path, windows_crt
):
    data = make_data(BLOCK * 2)
    destination = tmp_path / "image.bin"

    report = rescue_to_image(FakeDrive(data), destination, block_size=BLOCK)

    assert report.status == "complete"
    assert destination.read_bytes() == data


# -- the resumed image (second open site) -------------------------------


def test_resumed_rescue_image_is_byte_exact_under_windows_text_mode(
    tmp_path, windows_crt
):
    data = make_data(BLOCK * 3)
    destination = tmp_path / "image.bin"

    # A first run, stopped with the middle region still missing:
    # the image holds the outer regions with zeros between them,
    # and the progress file beside it says so. Setting that state
    # up directly means the run below must take the resume open.
    # (These setup writes go through Python's binary file objects,
    # not os.pwrite, so the model does not translate them.)
    destination.write_bytes(
        data[:BLOCK] + b"\x00" * BLOCK + data[BLOCK * 2 :]
    )
    region_map = RegionMap(
        source_size=len(data),
        block_size=BLOCK,
        regions=[
            {"state": STATE_RECOVERED, "holes": []},
            {"state": STATE_UNRECOVERED, "holes": [[0, BLOCK]]},
            {"state": STATE_RECOVERED, "holes": []},
        ],
    )
    region_map.save(tmp_path / "image.bin.map.json")

    report = rescue_to_image(FakeDrive(data), destination, block_size=BLOCK)

    assert report.status == "complete"
    assert destination.read_bytes() == data
