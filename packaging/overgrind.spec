# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for both Overgrind executables (onedir):  pyinstaller --noconfirm --clean packaging/overgrind.spec
#   dist/Overgrind/Overgrind.exe              GUI (windowed)
#   dist/overgrind-cli/overgrind-cli.exe      console tool
#
# Why a spec: the worker builds its adapter from an importlib *string* ("overgrind.adapters.uia:
# UiaAdapter") and pywinauto/comtypes import lazily, so PyInstaller's static scan cannot see them.
# SPECPATH is this file's directory; entry scripts live next to it.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hidden = collect_submodules("overgrind")                  # adapters.*, strategies.*, worker, ... by string
hidden += collect_submodules("pywinauto")                 # uia backend + controls loaded via registry
hidden += ["win32api", "win32con", "win32gui", "win32process", "win32event", "pythoncom", "pywintypes",
           "six", "multiprocessing.popen_spawn_win32"]
datas, binaries = [], []
for pkg in ("comtypes", "win32com"):                      # comtypes ships .tlb/.idl data + runtime-generated wrappers
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h

COMMON = dict(pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=hidden,
              hookspath=[], runtime_hooks=[], noarchive=False)

# ---- GUI (PySide6 plugins are collected by PyInstaller's built-in PySide6 hooks) ----
gui_a = Analysis([os.path.join(SPECPATH, "gui_entry.py")], excludes=[], **COMMON)
gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True, name="Overgrind", console=False, upx=False)
COLLECT(gui_exe, gui_a.binaries, gui_a.datas, strip=False, upx=False, name="Overgrind")

# ---- CLI (no Qt: keeps the spawned worker import-clean and small) ----
cli_a = Analysis([os.path.join(SPECPATH, "cli_entry.py")], excludes=["PySide6", "shiboken6"], **COMMON)
cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True, name="overgrind-cli", console=True, upx=False)
COLLECT(cli_exe, cli_a.binaries, cli_a.datas, strip=False, upx=False, name="overgrind-cli")
