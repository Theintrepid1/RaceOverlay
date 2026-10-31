# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Build the Windows app folder and installer. Runs on Windows (the GitHub
Actions workflow in .github/workflows/windows.yml, or by hand).

    python packaging/windows/fetch_deps.py        (once: ffmpeg + libmpv)
    python packaging/windows/build_windows.py [--no-installer]

Makes:
    build/windows/dist/RaceOverlay/        the app (RaceOverlay.exe + _internal)
    dist/RaceOverlay-<version>-windows-setup.exe   the installer (Inno Setup)

Needs: PyInstaller, Inno Setup 6 (ISCC.exe), and windows-bin/ filled by
fetch_deps.py (ffmpeg.exe, ffprobe.exe, libmpv-2.dll).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
from build_appimage import EXCLUDE_MODULES  # noqa: E402  (same pruning)

WORK = ROOT / "build" / "windows"
BIN = ROOT / "windows-bin"


def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def version() -> str:
    text = (ROOT / "src" / "raceoverlay" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def make_icon(dest: Path, svg: str = "icon.svg") -> Path:
    """An app icon SVG -> multi-size .ico (Qt draws the SVG, Pillow writes the ICO)."""
    from PIL import Image
    from PySide6 import QtCore, QtGui, QtSvg
    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication(["x"])  # noqa: F841
    r = QtSvg.QSvgRenderer(str(ROOT / "src" / "raceoverlay" / svg))
    img = QtGui.QImage(256, 256, QtGui.QImage.Format_ARGB32)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    r.render(p)
    p.end()
    png = dest.with_suffix(".png")
    img.save(str(png))
    Image.open(png).save(dest, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                     (64, 64), (128, 128), (256, 256)])
    return dest


def iscc() -> str:
    for c in (shutil.which("iscc"), shutil.which("ISCC"),
              r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
              r"C:\Program Files\Inno Setup 6\ISCC.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")):
        if c and Path(c).exists():
            return c
    sys.exit("Inno Setup 6 (ISCC.exe) not found")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-installer", action="store_true")
    a = ap.parse_args(argv)
    if sys.platform != "win32":
        sys.exit("Build this on Windows (PyInstaller can't cross-compile).")
    missing = [f for f in ("ffmpeg.exe", "ffprobe.exe", "libmpv-2.dll")
               if not (BIN / f).exists()]
    if missing:
        sys.exit(f"windows-bin is missing {missing}: run packaging/windows/fetch_deps.py")

    ver = version()
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    icon = make_icon(WORK / "raceoverlay.ico")
    icon_an = make_icon(WORK / "raceanalysis.ico", "icon_analysis.svg")
    src = ROOT / "src" / "raceoverlay"
    sep = os.pathsep  # ';' on Windows, for --add-data
    cmd = ["pyinstaller", "--noconfirm", "--clean", "--windowed",
           "--name", "RaceOverlay", "--icon", icon,
           "--distpath", WORK / "dist", "--workpath", WORK / "pyi",
           "--specpath", WORK, "--paths", ROOT / "src",
           "--collect-submodules", "raceoverlay",
           "--collect-submodules", "racecore",
           "--collect-submodules", "raceanalysis",
           "--add-data", f"{src / 'fonts'}{sep}raceoverlay/fonts",
           "--add-data", f"{src / 'icon.svg'}{sep}raceoverlay",
           "--add-data", f"{src / 'icon_analysis.svg'}{sep}raceoverlay",
           "--add-data", f"{ROOT / 'tracks'}{sep}tracks",
           "--add-binary", f"{BIN / 'ffmpeg.exe'}{sep}bin",
           "--add-binary", f"{BIN / 'ffprobe.exe'}{sep}bin",
           "--add-binary", f"{BIN / 'libmpv-2.dll'}{sep}bin",
           "--hidden-import", "mpv",
           "--runtime-hook", ROOT / "packaging" / "windows" / "boot_win.py"]
    for m in EXCLUDE_MODULES:
        cmd += ["--exclude-module", m]
    run(cmd + [ROOT / "packaging" / "raceoverlay_app.py"])

    app = WORK / "dist" / "RaceOverlay"
    shutil.copy2(icon_an, app / "raceanalysis.ico")   # for its shortcuts
    lic = app / "licenses"
    lic.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", lic / "LICENSE-RaceOverlay.txt")
    shutil.copy2(ROOT / "packaging" / "windows" / "NOTICE-windows.txt", lic / "NOTICE.txt")
    for f in ("FFmpeg-LICENSE.txt", "mpv-VERSION.txt"):
        if (BIN / f).exists():
            shutil.copy2(BIN / f, lic / f)

    if a.no_installer:
        print(f"Built {app}")
        return 0
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    run([iscc(), f"/DAppVersion={ver}", f"/DAppDir={app}", f"/DIconFile={icon}",
         f"/DLicenseFile={ROOT / 'LICENSE'}", f"/O{out}",
         ROOT / "packaging" / "windows" / "raceoverlay.iss"])
    setup = out / f"RaceOverlay-{ver}-windows-setup.exe"
    print(f"Built {setup} ({setup.stat().st_size >> 20} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
