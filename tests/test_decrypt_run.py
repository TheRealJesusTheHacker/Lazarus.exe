"""Decryptor runs: copies only, proven by the marker on disk.

The toy "decryptor" here is a small Python script that reverses a
one-byte XOR — the same transform the tests apply to make
"encrypted" files. It runs for real, as a child process, through
SubprocessRunner, so the whole path (prepare, fingerprint, run,
check the originals) is exercised the way the CLI drives it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from lazarus import hashing
from lazarus.decrypt_model import DecryptorEntry
from lazarus.decrypt_run import (
    WORK_AREA_MARKER,
    RunnerOutcome,
    SubprocessRunner,
    WorkArea,
    prepare_work_area,
    run_decryptor,
)
from lazarus.errors import DecryptorError, DecryptorRefusedError

XOR_KEY = 0xAA

TOY_DECRYPTOR = '''#!/usr/bin/env python3
"""Reverse a one-byte XOR for every file under the input folder."""
import os
import sys
from pathlib import Path


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, _dirnames, filenames in os.walk(src):
        for name in filenames:
            source = Path(dirpath) / name
            out = dst / source.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            out.write_bytes(bytes(b ^ 0xAA for b in data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

FAILING_DECRYPTOR = '''#!/usr/bin/env python3
import sys

print("toy internal gibberish that must never be repeated", file=sys.stderr)
sys.exit(3)
'''


def xor(data: bytes) -> bytes:
    return bytes(b ^ XOR_KEY for b in data)


def make_entry(**overrides) -> DecryptorEntry:
    values = dict(
        family="ToyLocker",
        publisher="Toy Publisher",
        decryptor_name="Toy Decryptor",
        source_url="https://example.com/toy-decryptor",
        publisher_url="https://example.com/",
        credit="Published free of charge by Toy Publisher.",
        verified_utc="2026-10-20",
        executable_name=None,
        extensions=[".toylocked"],
        note_filenames=["_readme.txt"],
        conditions="Only opens files locked with the toy key.",
    )
    values.update(overrides)
    return DecryptorEntry(**values)


def write_script(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    if os.name == "nt":
        # Windows cannot execute a shebang script by path, so the
        # body also goes in a .py sibling and a .cmd shim runs it
        # with this Python; subprocess executes .cmd files, and the
        # exit code, stdin and timeout behaviour carry through.
        py_script = tmp_path / f"{name}.py"
        py_script.write_text(body, encoding="utf-8")
        shim = tmp_path / f"{name}.cmd"
        shim.write_text(
            f'"{sys.executable}" "{py_script}" %1 %2\n', encoding="utf-8"
        )
        return shim
    return script


@pytest.fixture()
def toy_decryptor(tmp_path):
    return write_script(tmp_path, "toy-decryptor", TOY_DECRYPTOR)


@pytest.fixture()
def locked_folder(tmp_path):
    """A folder of "encrypted" files, and the bytes they should hold."""
    plain = {
        "notes.txt": b"remember the milk",
        "docs/report.txt": b"quarterly numbers",
    }
    source = tmp_path / "locked"
    for rel, data in plain.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(xor(data))
    (source / "_readme.txt").write_bytes(b"toy ransom note")
    return source, plain


class RecordingRunner:
    """A runner that records its calls and (optionally) produces a file."""

    def __init__(self, outcome: RunnerOutcome | None = None, produce: bool = False):
        self.outcome = outcome or RunnerOutcome(ok=True)
        self.produce = produce
        self.calls: list[tuple[Path, Path, Path]] = []

    def run(self, executable, input_dir, output_dir):
        self.calls.append((Path(executable), Path(input_dir), Path(output_dir)))
        if self.produce:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            (Path(output_dir) / "recovered.txt").write_bytes(b"recovered")
        return self.outcome


# -- preparing the work area ---------------------------------------


def test_prepare_copies_files_with_right_bytes_and_hashes(locked_folder, tmp_path):
    source, plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")

    assert work.copies_dir == tmp_path / "work" / "copies"
    assert work.output_dir == tmp_path / "work" / "decrypted"
    for rel, data in plain.items():
        copied = work.copies_dir / rel
        assert copied.read_bytes() == xor(data)
    by_rel = {item.relpath: item for item in work.files}
    assert by_rel["notes.txt"].sha256 == hashing.hash_file(source / "notes.txt")
    assert by_rel["docs/report.txt"].size == len(xor(plain["docs/report.txt"]))

    marker = json.loads((tmp_path / "work" / WORK_AREA_MARKER).read_text("utf-8"))
    assert marker["kind"] == "lazarus-work-area"
    assert marker["version"] == 1
    assert marker["source"] == str(source)
    assert marker["copies_dir"] == "copies"
    assert marker["output_dir"] == "decrypted"
    assert marker["files"]["notes.txt"] == hashing.hash_file(source / "notes.txt")


def test_prepare_refuses_work_area_overlapping_the_source(locked_folder, tmp_path):
    source, _plain = locked_folder
    with pytest.raises(DecryptorRefusedError):
        prepare_work_area(source, source)  # the folder itself
    with pytest.raises(DecryptorRefusedError):
        prepare_work_area(source, source / "work")  # inside the source
    with pytest.raises(DecryptorRefusedError):
        prepare_work_area(source, tmp_path)  # around the source


def test_prepare_refuses_a_work_folder_that_already_exists(locked_folder, tmp_path):
    source, _plain = locked_folder
    (tmp_path / "work").mkdir()
    with pytest.raises(DecryptorRefusedError) as caught:
        prepare_work_area(source, tmp_path / "work")
    assert "is already there" in str(caught.value)


def test_prepare_missing_source_is_a_plain_error(tmp_path):
    with pytest.raises(DecryptorError):
        prepare_work_area(tmp_path / "not-there", tmp_path / "work")


def test_prepare_with_no_files_raises_and_leaves_no_litter(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    with pytest.raises(DecryptorError) as caught:
        prepare_work_area(source, tmp_path / "work")
    assert "no files" in str(caught.value)
    assert not (tmp_path / "work").exists()


def test_prepare_skips_symlinks_and_records_the_problem(tmp_path):
    source = tmp_path / "locked"
    source.mkdir()
    real = source / "real.txt"
    real.write_bytes(b"real bytes")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"not part of the folder")
    os.symlink(elsewhere, source / "linked.txt")
    os.symlink(tmp_path, source / "linked-dir")

    work = prepare_work_area(source, tmp_path / "work")

    assert [item.relpath for item in work.files] == ["real.txt"]
    assert not (work.copies_dir / "linked.txt").exists()
    joined = "\n".join(work.problems)
    assert "linked.txt" in joined
    assert "linked-dir" in joined


# -- the marker is the enforcement ----------------------------------


def test_run_refuses_a_folder_with_no_marker_and_never_runs(tmp_path):
    plain_folder = tmp_path / "originals"
    plain_folder.mkdir()
    (plain_folder / "notes.txt").write_bytes(b"the only copy")
    work = WorkArea(
        root=plain_folder,
        copies_dir=plain_folder / "copies",
        output_dir=plain_folder / "decrypted",
        source_dir=plain_folder,
        files=[],
        problems=[],
    )
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, tmp_path / "anything", runner)
    assert runner.calls == []


def test_run_refuses_a_tampered_marker(locked_folder, tmp_path, toy_decryptor):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    marker_path = tmp_path / "work" / WORK_AREA_MARKER
    data = json.loads(marker_path.read_text("utf-8"))
    data["copies_dir"] = "somewhere-else"
    marker_path.write_text(json.dumps(data), encoding="utf-8")

    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_a_marker_of_the_wrong_kind(locked_folder, tmp_path, toy_decryptor):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    marker_path = tmp_path / "work" / WORK_AREA_MARKER
    data = json.loads(marker_path.read_text("utf-8"))
    data["kind"] = "not-a-work-area"
    marker_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, RecordingRunner())


def test_run_with_a_missing_program_is_a_plain_error(
    locked_folder, tmp_path
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    with pytest.raises(DecryptorError):
        run_decryptor(
            make_entry(), work, tmp_path / "no-such-program", RecordingRunner()
        )


# -- running, for real ----------------------------------------------


def test_run_happy_path_decrypts_copies_and_leaves_originals_alone(
    locked_folder, tmp_path, toy_decryptor
):
    source, plain = locked_folder
    before = {
        rel: hashing.hash_file(source / rel) for rel in ("notes.txt", "_readme.txt")
    }
    work = prepare_work_area(source, tmp_path / "work")

    report = run_decryptor(
        make_entry(), work, toy_decryptor, SubprocessRunner()
    )

    assert report.status == "complete"
    assert report.originals_untouched is True
    assert report.files_copied == len(work.files)
    assert report.files_produced == len(work.files)
    assert report.produced_bytes > 0
    for rel, data in plain.items():
        assert (work.output_dir / rel).read_bytes() == data
    after = {
        rel: hashing.hash_file(source / rel) for rel in ("notes.txt", "_readme.txt")
    }
    assert after == before
    text = report.to_text()
    assert "did not change" in text
    assert str(work.output_dir) in text
    assert "ToyLocker" in text and "Toy Publisher" in text


def test_run_failure_is_plain_and_never_repeats_the_programs_words(
    locked_folder, tmp_path
):
    source, _plain = locked_folder
    failing = write_script(tmp_path, "failing-decryptor", FAILING_DECRYPTOR)
    work = prepare_work_area(source, tmp_path / "work")

    report = run_decryptor(make_entry(), work, failing, SubprocessRunner())

    assert report.status == "failed"
    assert report.runner_message == (
        "The decryptor program ended with an error (code 3)."
    )
    assert "gibberish" not in report.to_text()


def test_run_that_produces_nothing_needs_attention(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    runner = RecordingRunner(outcome=RunnerOutcome(ok=True), produce=False)

    report = run_decryptor(make_entry(), work, toy_decryptor, runner)

    assert report.status == "attention"
    assert report.files_produced == 0
    assert len(runner.calls) == 1


def test_run_records_a_program_name_that_differs_from_the_publishers(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    entry = make_entry(executable_name="OfficialToyDecryptor.exe")
    work = prepare_work_area(source, tmp_path / "work")
    runner = RecordingRunner(produce=True)

    report = run_decryptor(entry, work, toy_decryptor, runner)

    assert report.status == "complete"
    assert report.program_note is not None
    assert "toy-decryptor" in report.program_note
    assert "OfficialToyDecryptor.exe" in report.program_note
    assert report.program_sha256 == hashing.hash_file(toy_decryptor)
    assert len(runner.calls) == 1
    assert runner.calls[0][1] == work.copies_dir
    assert runner.calls[0][2] == work.output_dir


def test_run_flags_originals_changed_by_something_else(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")

    class MeddlingRunner:
        def run(self, executable, input_dir, output_dir):
            # Something outside Lazarus rewrites an original mid-run.
            (source / "notes.txt").write_bytes(b"changed by someone else")
            (Path(output_dir) / "recovered.txt").write_bytes(b"recovered")
            return RunnerOutcome(ok=True)

    report = run_decryptor(make_entry(), work, toy_decryptor, MeddlingRunner())

    assert report.originals_untouched is False
    assert report.status == "attention"
    assert "Something outside Lazarus changed the original files" in (
        report.to_text()
    )


# -- audit fixes: the work area is re-proven at run time ------------
#
# The first audit round broke the copies-only promise four ways,
# all by attacking what run_decryptor failed to re-check: a forged
# work-area object, folders swapped for links after preparation, a
# marker rewritten to point elsewhere, and a damaged manifest the
# run sailed past. These tests pin every refusal.


def _tamper_marker(work_root: Path, **changes) -> None:
    path = work_root / WORK_AREA_MARKER
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_run_refuses_a_forged_work_area_pointing_elsewhere(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    victim = tmp_path / "victim"
    (victim / "copies").mkdir(parents=True)
    (victim / "decrypted").mkdir()
    forged = WorkArea(
        root=work.root,
        copies_dir=victim / "copies",
        output_dir=victim / "decrypted",
        source_dir=source,
        files=[],
    )
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), forged, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_a_copies_folder_swapped_for_a_link(
    locked_folder, tmp_path, toy_decryptor
):
    import shutil

    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    shutil.rmtree(work.copies_dir)
    os.symlink(source, work.copies_dir)
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_an_output_folder_swapped_for_a_link(
    locked_folder, tmp_path, toy_decryptor
):
    import shutil

    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    shutil.rmtree(work.output_dir)
    os.symlink(tmp_path / "elsewhere", work.output_dir)
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_a_marker_pointing_at_another_source(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    _tamper_marker(work.root, source=str(tmp_path / "decoy"))
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_a_damaged_or_empty_manifest(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    for damaged in ("garbage", {}):
        work = prepare_work_area(source, tmp_path / f"work-{damaged!r}"[:40])
        _tamper_marker(work.root, files=damaged)
        runner = RecordingRunner()
        with pytest.raises(DecryptorRefusedError):
            run_decryptor(make_entry(), work, toy_decryptor, runner)
        assert runner.calls == []


def test_run_refuses_a_manifest_path_that_escapes(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    _tamper_marker(work.root, files={"../outside.txt": "0" * 64})
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_a_marker_from_another_version(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    _tamper_marker(work.root, version=999)
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_when_a_copy_no_longer_matches(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    (work.copies_dir / "notes.txt").write_bytes(b"swapped after preparation")
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_when_a_file_was_added_to_the_copies(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    (work.copies_dir / "planted.txt").write_bytes(b"not one of ours")
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_originals_changed_before_the_run_are_reported_as_such(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    (source / "notes.txt").write_bytes(b"changed before the run")

    report = run_decryptor(make_entry(), work, toy_decryptor, SubprocessRunner())

    assert report.status == "attention"
    assert report.originals_changed_during is False
    assert report.originals_already_different is True
    assert report.originals_untouched is False
    text = report.to_text()
    assert "had already changed" in text
    assert "while this ran" not in text


BAD_BYTES_DECRYPTOR = '''#!/usr/bin/env python3
import os
import sys

os.write(1, b"\\xff\\xfe not text at all \\x80\\x81")
os.write(2, b"\\xff error bytes \\x80")
sys.exit(1)
'''


def test_program_output_that_is_not_text_cannot_break_the_run(
    locked_folder, tmp_path
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    script = write_script(tmp_path, "bad-bytes-decryptor", BAD_BYTES_DECRYPTOR)

    report = run_decryptor(make_entry(), work, script, SubprocessRunner())

    assert report.status == "failed"
    assert "ended with an error (code 1)" in report.runner_message


# -- second audit round: links that share storage, planted output,
# and marker shapes that merely resemble the real ones -------------


def test_run_refuses_a_copy_hard_linked_to_its_original(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    copy = work.copies_dir / "notes.txt"
    copy.unlink()
    # Same bytes, same hash — but the "copy" now IS the original:
    # anything writing to it writes to the original.
    os.link(source / "notes.txt", copy)
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_output_that_was_not_left_empty(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    (work.output_dir / "planted.txt").write_bytes(b"was here first")
    runner = RecordingRunner(produce=True)
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


def test_run_refuses_a_marker_version_that_only_resembles_one(
    locked_folder, tmp_path, toy_decryptor
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    _tamper_marker(work.root, version=True)
    runner = RecordingRunner()
    with pytest.raises(DecryptorRefusedError):
        run_decryptor(make_entry(), work, toy_decryptor, runner)
    assert runner.calls == []


# -- third audit round: names from the disk are printed safely, and
# a decryptor gets no input to wait on -------------------------------

STDIN_DECRYPTOR = '''#!/usr/bin/env python3
import sys
from pathlib import Path

data = sys.stdin.buffer.read()
out = Path(sys.argv[2]) / "answer.txt"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(b"read " + str(len(data)).encode() + b" bytes")
'''


def test_a_decryptor_reading_input_gets_an_empty_answer_at_once(
    locked_folder, tmp_path
):
    source, _plain = locked_folder
    work = prepare_work_area(source, tmp_path / "work")
    script = write_script(tmp_path, "asking-decryptor", STDIN_DECRYPTOR)

    report = run_decryptor(
        make_entry(),
        work,
        script,
        SubprocessRunner(timeout_seconds=10),
    )

    assert report.status == "complete"
    assert (work.output_dir / "answer.txt").read_bytes() == b"read 0 bytes"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the crafted name uses characters Windows filenames cannot contain",
)
def test_prepare_problems_print_a_crafted_name_safely(tmp_path):
    source = tmp_path / "locked"
    source.mkdir()
    (source / "real.txt").write_bytes(b"real")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"elsewhere")
    os.symlink(elsewhere, source / "evil\nFAKE\x1b[31m.txt")

    work = prepare_work_area(source, tmp_path / "work")

    assert work.problems
    for problem in work.problems:
        assert "\n" not in problem
        assert "\x1b" not in problem
    assert any("evil?FAKE?[31m.txt" in problem for problem in work.problems)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the crafted name uses characters Windows filenames cannot contain",
)
def test_run_problems_print_a_crafted_name_safely(tmp_path, toy_decryptor):
    source = tmp_path / "locked"
    source.mkdir()
    crafted = source / "gone\x1b[31m.txt"
    crafted.write_bytes(b"locked bytes")
    work = prepare_work_area(source, tmp_path / "work")
    crafted.unlink()

    report = run_decryptor(
        make_entry(), work, toy_decryptor, RecordingRunner(produce=True)
    )

    text = report.to_text()
    assert "\x1b" not in text
    assert "gone?[31m.txt" in text
    assert report.originals_already_different is True
