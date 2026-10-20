# PyInstaller spec for LAZARUS.EXE — the whole product in one
# file: snapshot store and restore engine, undelete and drive
# rescue, decrypt and unlock, backup automation, the interface,
# and the self-install module, routed at launch by
# lazarus.ui.app.main.
#
# Build (from the repo root, on Windows):
#     pyinstaller packaging/Lazarus.spec
# or, the way releases are built, locally and in CI alike:
#     python tools/build_release.py
# Output: dist/Lazarus.exe
#
# One file, no console window: this is the program a person
# downloads and double-clicks. PySide6 is bundled by PyInstaller's
# Qt hooks; the Qt modules are also named in hiddenimports because
# the interface imports them in its own modules and a build that
# silently dropped one must fail here, not on a user's machine.

import re
import sys
from pathlib import Path

# PyInstaller 6 does NOT inject the Windows version-info classes
# into the spec namespace — they must be imported explicitly, or
# the build dies with NameError the first time the spec runs.
# Import exactly the classes used below. The import is Windows-
# only: the classes describe a Windows version resource, their
# module needs pefile (which PyInstaller only installs on
# Windows), and a non-Windows build has no resource to fill —
# it gets version=None below.
if sys.platform.startswith("win"):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VSVersionInfo,
        VarFileInfo,
        VarStruct,
    )

ROOT = Path(SPECPATH).parent

# The version lives in one place — lazarus/_version.py — and the
# spec reads it from there, so the exe can never claim a version
# the code does not. (Read as text, not imported: importing the
# package here would run its __init__ during the build.)
_version_text = (ROOT / "lazarus" / "_version.py").read_text(encoding="utf-8")
_match = re.search(r'__version__\s*=\s*"([^"]+)"', _version_text)
if _match is None:
    raise RuntimeError("Could not read __version__ from lazarus/_version.py")
VERSION = _match.group(1)
_parts = [int(piece) for piece in re.findall(r"\d+", VERSION)][:4]
while len(_parts) < 4:
    _parts.append(0)
VERSION_TUPLE = tuple(_parts)
VERSION_4 = ".".join(str(piece) for piece in VERSION_TUPLE)

# Windows version resource for the exe: File Explorer, the task
# manager, and SmartScreen all read THIS — an exe with no version
# info looks anonymous.
version_info = (
    VSVersionInfo(  # noqa: F821 — imported above, on Windows only
        ffi=FixedFileInfo(
        filevers=VERSION_TUPLE,
        prodvers=VERSION_TUPLE,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "Sentinel Safeguard LLC"),
                        StringStruct(
                            "FileDescription",
                            "LAZARUS.EXE — Sentinel Safeguard backup, "
                            "restore, and file recovery",
                        ),
                        StringStruct("FileVersion", VERSION_4),
                        StringStruct("InternalName", "Lazarus"),
                        StringStruct(
                            "LegalCopyright",
                            "© Sentinel Safeguard LLC",
                        ),
                        StringStruct("OriginalFilename", "Lazarus.exe"),
                        StringStruct("ProductName", "LAZARUS.EXE"),
                        StringStruct("ProductVersion", VERSION),
                    ],
                )
            ]
        ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
    if sys.platform.startswith("win")
    else None
)

a = Analysis(
    [str(ROOT / "packaging" / "launch_lazarus.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # The decryptor list ships as data inside the package, and
    # lazarus.decryptors finds it beside its own module file
    # (default_registry_path() uses Path(__file__).with_name(...)).
    # In the frozen program that file lives at
    # <_MEIPASS>/lazarus/decryptors.py, so the list must land in
    # the bundle's "lazarus" folder — exactly where this entry
    # puts it. If this entry is ever dropped, the registry load
    # fails on a user's machine while every source test stays
    # green, which is why it is spelled out instead of collected.
    datas=[
        (str(ROOT / "lazarus" / "decryptors_registry.json"), "lazarus"),
    ],
    hiddenimports=[
        # The interface's Qt stack, named outright (see header).
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        # The entry points the launcher reaches by name.
        "lazarus.ui.app",
        "lazarus.installer",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Development and unrelated stacks never ship: the test
        # suite, this repo's build tools, and libraries neither
        # the engine (standard library only) nor the interface
        # uses.
        "tests",
        "tools",
        "pytest",
        "tkinter",
        "_tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "PIL",
        "IPython",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Lazarus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    # True: a windowed build must never answer an unexpected error
    # with PyInstaller's raw traceback dialog. The interface turns
    # failures into plain-language screens; anything that still
    # escapes belongs in a log, not on a person's screen.
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
)
