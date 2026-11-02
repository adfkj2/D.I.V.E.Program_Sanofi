# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build.

onedir (not onefile) keeps startup fast and avoids the self-extracting stub that
antivirus products like to flag. The build is launched by scripts/build_windows.ps1,
which has already produced ``frontend/dist`` and the icon.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parents[1]
ICON = ROOT / "packaging" / "windows" / "we-once-talked.ico"

datas = [
    (str(ROOT / "frontend" / "dist"), "frontend_dist"),
    (str(ROOT / "backend" / "wot" / "data"), "wot/data"),
]

hiddenimports = [
    # uvicorn picks its protocol implementations at runtime from a string.
    *collect_submodules("uvicorn"),
    # keyring resolves backends through entry points, which PyInstaller cannot see.
    *collect_submodules("keyring.backends"),
    "win32ctypes.core",
    "win32ctypes.pywin32.pywintypes",
    "win32ctypes.pywin32.win32api",
    "win32ctypes.pywin32.win32cred",
    "wot.desktop",
]

excludes = ["pytest", "_pytest", "tests", "tkinter"]

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "entry.py")],
    pathex=[str(ROOT / "backend"), str(ROOT / "vendor" / "dive-memory" / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WeOnceTalked",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WeOnceTalked",
)
