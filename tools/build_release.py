"""Build the Lazarus release: one exe, one portable zip, one checksum file.

This is the ONLY way a release is built — the same script runs
on a developer's machine and in the tag-gated release workflow,
so what ships is what was tested locally. It:

1. builds ``Lazarus.exe`` with PyInstaller from
   ``packaging/Lazarus.spec`` (one file, windowed);
2. stages the exe with the portable readme in ``dist/stage/``;
3. writes ``LazarusPortable.zip`` holding exactly those two
   files, at the zip's root and nothing else;
4. puts ``Lazarus.exe`` itself beside the zip;
5. writes ``SHA256SUMS.txt`` covering both, in the fleet format
   (``<sha256>  <filename>``);
6. prints each artifact's exact size and SHA-256, so a build log
   is itself a record of what was made.

Usage (from anywhere; paths are worked out from this file):

    python tools/build_release.py [--skip-build] [--out DIR]

``--skip-build`` reuses the program file already in the output
folder — for fast local iteration on packaging, never for a
release. ``--out`` chooses the output folder (default ``dist/``).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "Lazarus.spec"
PORTABLE_README = ROOT / "packaging" / "README-PORTABLE.txt"

EXE_NAME = "Lazarus.exe"
ZIP_NAME = "LazarusPortable.zip"
SUMS_NAME = "SHA256SUMS.txt"
#: The portable zip holds exactly these two entries, at its root,
#: in this (sorted) order.
ZIP_ENTRIES = (EXE_NAME, "README-PORTABLE.txt")


def copy_program(source: Path, destination: Path) -> None:
    """Copy the program file, keeping it runnable.

    A plain data copy drops the permission bits — harmless on
    Windows, but on any other system the copy would no longer be
    executable, so the mode travels with the bytes and the
    owner's execute bit is made sure of.
    """
    shutil.copyfile(source, destination)
    shutil.copystat(source, destination)
    if os.name != "nt":
        mode = destination.stat().st_mode
        destination.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_program(out_dir: Path, work_dir: Path) -> Path:
    """Run PyInstaller and return the program file it produced."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(out_dir),
            "--workpath",
            str(work_dir),
            str(SPEC),
        ],
        check=True,
        cwd=ROOT,
    )
    # The name carries .exe on Windows and no extension elsewhere;
    # either way it is the one file the spec's EXE step produced.
    for candidate in (out_dir / EXE_NAME, out_dir / "Lazarus"):
        if candidate.is_file():
            return candidate
    raise SystemExit(
        f"PyInstaller finished but no program file was found in {out_dir}."
    )


def stage_and_package(built: Path, out_dir: Path) -> list[Path]:
    """Stage, zip, place, and checksum. Returns the artifacts."""
    stage = out_dir / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    staged_exe = stage / EXE_NAME
    copy_program(built, staged_exe)
    shutil.copyfile(PORTABLE_README, stage / "README-PORTABLE.txt")

    zip_path = out_dir / ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(ZIP_ENTRIES):
            archive.write(stage / entry, arcname=entry)

    final_exe = out_dir / EXE_NAME
    if built.resolve() != final_exe.resolve():
        copy_program(staged_exe, final_exe)
    if built.name != EXE_NAME and built.exists():
        # A non-Windows proof build names its binary "Lazarus";
        # the release folder holds the artifacts, not the stray.
        built.unlink()

    sums_path = out_dir / SUMS_NAME
    lines = [
        f"{sha256_of(out_dir / name)}  {name}"
        for name in (EXE_NAME, ZIP_NAME)
    ]
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [final_exe, zip_path, sums_path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse the program file already in the output folder",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "dist",
        help="output folder (default: dist/ in the repo root)",
    )
    args = parser.parse_args(argv)
    out_dir = args.out.resolve()

    if args.skip_build:
        out_dir.mkdir(parents=True, exist_ok=True)
        built = next(
            (
                candidate
                for candidate in (out_dir / EXE_NAME, out_dir / "Lazarus")
                if candidate.is_file()
            ),
            None,
        )
        if built is None:
            raise SystemExit(
                f"--skip-build was asked for, but there is no built "
                f"program file in {out_dir} to reuse."
            )
        print(f"Reusing the built program file: {built}")
    else:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        work_dir = out_dir.parent / "build"
        if work_dir.exists():
            shutil.rmtree(work_dir)
        built = build_program(out_dir, work_dir)

    artifacts = stage_and_package(built, out_dir)
    for artifact in artifacts:
        print(
            f"{artifact.name}: {artifact.stat().st_size} bytes  "
            f"sha256 {sha256_of(artifact)}"
        )
    print(f"Artifacts are in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
