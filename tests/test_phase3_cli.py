"""The Phase 3 commands, end to end: volumes, unlock, decryptors,
identify, decrypt, assess — with the honest exit codes the rest of
the kit uses (0 done, 1 done but needs attention, 2 stopped).

The volume seam is faked with monkeypatched platform functions, and
the decrypt run uses a real toy decryptor script through the real
SubprocessRunner, so the CLI's wiring is what is on trial here.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from lazarus import cli
from lazarus.decrypt_model import VolumeInfo

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


def xor(data: bytes) -> bytes:
    return bytes(b ^ XOR_KEY for b in data)


def _write_executable_script(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    script.chmod(
        script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )
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


class FakePlatform:
    """The volume seam: lists what it is given, describes nothing."""

    def __init__(self, volumes=()):
        self._volumes = list(volumes)

    def list_volumes(self):
        return list(self._volumes)

    def describe_volume(self, ref):
        return None


class FakeOutcome:
    def __init__(self, status: str):
        self.status = status

    def to_text(self) -> str:
        return f"Fake unlock outcome: {self.status}."


# -- volumes ---------------------------------------------------------


def test_cli_volumes_lists_each_volume(monkeypatch, capsys):
    platform = FakePlatform(
        [
            VolumeInfo(ref="C:", kind="bitlocker", locked=True),
            VolumeInfo(ref="D:", kind="bitlocker", locked=False),
            VolumeInfo(ref="E:", kind="veracrypt", locked=None),
        ]
    )
    monkeypatch.setattr(cli, "default_platform", lambda: platform)
    assert cli.main(["volumes"]) == 0
    out = capsys.readouterr().out
    assert "C: — BitLocker, locked" in out
    assert "D: — BitLocker, open" in out
    assert "E: — veracrypt" in out


def test_cli_volumes_with_none_to_list(monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_platform", lambda: FakePlatform([]))
    assert cli.main(["volumes"]) == 0
    assert "No volumes could be listed." in capsys.readouterr().out


# -- unlock ----------------------------------------------------------


def _patch_unlock(monkeypatch, status: str) -> None:
    def fake_unlock_volume(platform, ref, key_provider):
        key_provider()  # the key is asked for, through _read_key
        return FakeOutcome(status)

    monkeypatch.setattr(cli, "default_platform", lambda: FakePlatform())
    monkeypatch.setattr(cli, "unlock_volume", fake_unlock_volume)
    monkeypatch.setattr(cli, "_read_key", lambda prompt: "0000-1111")


def test_cli_unlock_happy_path(monkeypatch, capsys):
    _patch_unlock(monkeypatch, "unlocked")
    assert cli.main(["unlock", "C:"]) == 0
    assert "Fake unlock outcome: unlocked." in capsys.readouterr().out


def test_cli_unlock_already_open_is_also_done(monkeypatch, capsys):
    _patch_unlock(monkeypatch, "already-unlocked")
    assert cli.main(["unlock", "C:"]) == 0


def test_cli_unlock_not_accepted_needs_attention(monkeypatch, capsys):
    _patch_unlock(monkeypatch, "not-accepted")
    assert cli.main(["unlock", "C:"]) == 1
    assert "not-accepted" in capsys.readouterr().out


# -- decryptors / identify -------------------------------------------


def test_cli_decryptors_lists_the_registry(capsys):
    assert cli.main(["decryptors"]) == 0
    out = capsys.readouterr().out
    assert "STOP/Djvu" in out
    assert "Emsisoft Decryptor for STOP Djvu" in out
    assert "free from Emsisoft" in out


def _stop_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "locked-files"
    folder.mkdir()
    (folder / "_readme.txt").write_text("your files are locked", encoding="utf-8")
    (folder / "photo.jpg.djvu").write_bytes(xor(b"a photograph"))
    return folder


def test_cli_identify_names_the_family(tmp_path, capsys):
    folder = _stop_folder(tmp_path)
    assert cli.main(["identify", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "STOP/Djvu" in out
    assert "Published decryptor: Emsisoft Decryptor for STOP Djvu" in out


def test_cli_identify_unknown_folder_says_so_plainly(tmp_path, capsys):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "notes.txt").write_text("nothing locked here", encoding="utf-8")
    assert cli.main(["identify", str(folder)]) == 0
    assert "does not recognize the signs" in capsys.readouterr().out


# -- decrypt, end to end ----------------------------------------------


def test_cli_decrypt_end_to_end_on_copies(tmp_path, capsys):
    folder = _stop_folder(tmp_path)
    script = _write_executable_script(tmp_path, "toy-decryptor", TOY_DECRYPTOR)
    work = tmp_path / "work"
    original_before = (folder / "photo.jpg.djvu").read_bytes()

    code = cli.main(
        [
            "decrypt",
            str(folder),
            "--to",
            str(work),
            "--decryptor",
            str(script),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "did not change" in out
    recovered = work / "decrypted" / "photo.jpg.djvu"
    assert recovered.read_bytes() == b"a photograph"
    assert (folder / "photo.jpg.djvu").read_bytes() == original_before


def test_cli_decrypt_without_a_family_stops_before_working(tmp_path, capsys):
    folder = tmp_path / "mystery"
    folder.mkdir()
    (folder / "notes.xyzq").write_bytes(b"unknown signs")
    script = tmp_path / "toy-decryptor"
    script.write_text(TOY_DECRYPTOR, encoding="utf-8")

    code = cli.main(
        [
            "decrypt",
            str(folder),
            "--to",
            str(tmp_path / "work"),
            "--decryptor",
            str(script),
        ]
    )

    assert code == 2
    captured = capsys.readouterr()
    assert "Lazarus stopped:" in captured.err
    assert "will not run a decryptor blind" in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "work").exists()


def test_cli_decrypt_with_an_unknown_family_name_stops(tmp_path, capsys):
    folder = _stop_folder(tmp_path)
    code = cli.main(
        [
            "decrypt",
            str(folder),
            "--to",
            str(tmp_path / "work"),
            "--decryptor",
            str(tmp_path / "whatever"),
            "--family",
            "NoSuchFamily",
        ]
    )
    assert code == 2
    assert "no decryptor listed for 'NoSuchFamily'" in capsys.readouterr().err


# -- assess ------------------------------------------------------------


def test_cli_assess_a_folder(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "default_platform", lambda: FakePlatform())
    folder = _stop_folder(tmp_path)
    assert cli.main(["assess", str(folder)]) == 0
    out = capsys.readouterr().out
    assert f"What Lazarus can do for {folder}" in out
    assert "STOP/Djvu" in out
    assert "cannot break encryption" in out


# -- audit fix: a "may be" is a suggestion, and the CLI does not
# run a decryptor on a suggestion ------------------------------------


def _avaddon_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "maybe-avaddon"
    folder.mkdir()
    (folder / "photo.avdn").write_bytes(xor(b"a photograph"))
    return folder


def _toy_script(tmp_path: Path) -> Path:
    return _write_executable_script(tmp_path, "toy-decryptor", TOY_DECRYPTOR)


def test_cli_decrypt_on_a_suggestion_only_stops(tmp_path, capsys):
    folder = _avaddon_folder(tmp_path)
    code = cli.main(
        [
            "decrypt",
            str(folder),
            "--to",
            str(tmp_path / "work"),
            "--decryptor",
            str(_toy_script(tmp_path)),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "only suggest" in err
    assert "--family" in err
    assert not (tmp_path / "work").exists()


def test_cli_decrypt_on_a_suggestion_runs_when_the_owner_names_it(
    tmp_path, capsys
):
    folder = _avaddon_folder(tmp_path)
    work = tmp_path / "work"
    code = cli.main(
        [
            "decrypt",
            str(folder),
            "--to",
            str(work),
            "--decryptor",
            str(_toy_script(tmp_path)),
            "--family",
            "Avaddon",
        ]
    )
    assert code == 0
    assert (work / "decrypted" / "photo.avdn").read_bytes() == b"a photograph"


# -- second audit round: two plain readings are still a choice, an
# empty name is not a folder, and a run cannot hang forever ---------

SLEEPING_DECRYPTOR = '''#!/usr/bin/env python3
import time

time.sleep(30)
'''


def test_cli_decrypt_with_two_plain_families_stops(tmp_path, capsys):
    folder = tmp_path / "two-families"
    folder.mkdir()
    (folder / "_readme.txt").write_bytes(b"STOP note")
    (folder / "KRAB-DECRYPT.txt").write_bytes(b"GandCrab note")
    (folder / "photo.jpg.djvu").write_bytes(xor(b"a photograph"))

    code = cli.main(
        [
            "decrypt",
            str(folder),
            "--to",
            str(tmp_path / "work"),
            "--decryptor",
            str(_toy_script(tmp_path)),
        ]
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "more than one family" in err
    assert "--family" in err
    assert not (tmp_path / "work").exists()


def test_cli_identify_with_no_folder_named_stops(tmp_path, capsys):
    assert cli.main(["identify", ""]) == 2
    assert "No folder was named" in capsys.readouterr().err


def test_cli_decrypt_stops_a_decryptor_that_never_finishes(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(cli, "_DECRYPT_TIMEOUT_SECONDS", 1)
    folder = _stop_folder(tmp_path)
    script = _write_executable_script(
        tmp_path, "sleeping-decryptor", SLEEPING_DECRYPTOR
    )

    code = cli.main(
        [
            "decrypt",
            str(folder),
            "--to",
            str(tmp_path / "work"),
            "--decryptor",
            str(script),
            "--family",
            "STOP/Djvu",
        ]
    )

    assert code == 2
    assert "ran for too long" in capsys.readouterr().out
