"""End-to-end through the command line, the way a person would run it."""

from lazarus.cli import main


def test_full_round_trip(tmp_path, capsys):
    store = str(tmp_path / "my-store")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "letter.txt").write_text("Dear past me", encoding="utf-8")

    assert main(["init", store]) == 0
    assert main(["snap", store, str(docs), "--label", "before the move"]) == 0

    (docs / "letter.txt").write_text("Dear future me", encoding="utf-8")
    assert main(["snap", store, str(docs)]) == 0

    assert main(["list", store]) == 0
    listing = capsys.readouterr().out
    assert "before the move" in listing

    assert main(["verify", store]) == 0

    out = tmp_path / "back-then"
    assert main(["restore", store, "--to", str(out), "--as-of", "2999-01-01"]) == 0
    # The newest snapshot on or before that date is the second one.
    assert (out / "letter.txt").read_text() == "Dear future me"


def test_restore_in_place_and_its_undo(tmp_path, capsys):
    store = str(tmp_path / "store")
    docs = tmp_path / "docs"
    docs.mkdir()
    letter = docs / "letter.txt"
    letter.write_text("original", encoding="utf-8")

    assert main(["init", store]) == 0
    assert main(["snap", store, str(docs)]) == 0
    letter.write_text("overwritten", encoding="utf-8")

    assert main(["restore", store]) == 0
    assert letter.read_text() == "original"
    report_text = capsys.readouterr().out
    assert "saved as snapshot" in report_text  # the safety copy is named


def test_cli_reports_a_bad_snapshot_id(tmp_path):
    store = str(tmp_path / "store")
    assert main(["init", store]) == 0
    assert main(["restore", store, "--snapshot", "no-such-snapshot"]) == 2


def test_cli_verify_flags_a_tampered_store(tmp_path, capsys):
    store = str(tmp_path / "store")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("alpha", encoding="utf-8")
    assert main(["init", store]) == 0
    assert main(["snap", store, str(docs)]) == 0
    objects = [p for p in (tmp_path / "store" / "objects").rglob("*") if p.is_file()]
    objects[0].write_bytes(b"tampered")
    assert main(["verify", store]) == 1
