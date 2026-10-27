#!/usr/bin/env python3
"""Build RaceOverlay-x86_64.AppImage.

Needs: pyinstaller, patchelf, appimagetool, a shared ffmpeg build (BtbN
linux64-gpl-shared), and a libmpv built against that ffmpeg (see
packaging/BUILDING.md).

    python3 packaging/build_appimage.py --ffmpeg DIR --libmpv FILE \\
        --appimagetool FILE --excludelist FILE [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The bundled ffmpeg libraries libmpv uses; everything else libmpv needs is
# either always on the host (excludelist) or a host-first fallback.
CORE_PREFIXES = ("libav", "libsw", "libmpv", "libass", "libunibreak")
GLIBC = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0",
         "librt.so.1", "libmvec.so.1", "ld-linux-x86-64.so.2", "libresolv.so.2",
         "libutil.so.1", "libanl.so.1"}
# PySide6 modules the app never uses (keeps the bundle smaller).
EXCLUDE_MODULES = [
    "tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.QtQuickWidgets", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio",
    "PySide6.QtHttpServer", "PySide6.QtNetworkAuth", "PySide6.QtSvgWidgets",
    "matplotlib", "scipy", "pandas", "IPython", "pyqtgraph.opengl",
    "pyqtgraph.examples", "OpenGL",
]


def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def needed(path: Path) -> list[str]:
    out = subprocess.run(["readelf", "-d", str(path)], capture_output=True,
                         text=True).stdout
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", out)


def ldd(path: Path, env=None) -> dict[str, str]:
    out = subprocess.run(["ldd", str(path)], capture_output=True, text=True,
                         env=env).stdout
    res = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\S+) => (/\S+)", line)
        if m:
            res[m.group(1)] = m.group(2)
    return res


def is_elf(p: Path) -> bool:
    try:
        with open(p, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


DROP_PLUGINS = [
    "platformthemes/libqgtk3*",                     # host GTK; we use the portal
    "platforminputcontexts/libqtvirtualkeyboard*",  # on-screen keyboard (Qt Quick)
    "imageformats/libqpdf*",
    "platforms/libqeglfs*", "platforms/libqlinuxfb*", "platforms/libqminimal*",
    "platforms/libqvnc*", "platforms/libqvkkhrdisplay*",
    "egldeviceintegrations/*",
]


def prune(app: Path, internal: Path, exclude: set[str]):
    """Drop bundled libraries the app doesn't need or must not ship.

    * PyInstaller sees python-mpv's ctypes lookup and copies the build host's
      own libmpv (plus ~200 MB of codecs). We ship our own libmpv.
    * Unused Qt plugins (GTK theme, virtual keyboard, PDF, framebuffer
      platforms) and the Qt Quick/QML libraries only they pull in.
    * Libraries on the AppImage excludelist (glibc, fontconfig, freetype,
      X11, GL...): these must come from the host to match its drivers/config.
    """
    plugdir = internal / "PySide6" / "Qt" / "plugins"
    for pat in DROP_PLUGINS:
        for f in plugdir.glob(pat):
            f.unlink()
    libdirs = [internal, internal / "PySide6" / "Qt" / "lib"]
    libs: dict[str, list[Path]] = {}  # name -> every copy/symlink of it
    for d in libdirs:
        for f in d.iterdir() if d.exists() else []:
            if ".so" in f.name and (f.is_symlink() or (f.is_file() and is_elf(f))):
                libs.setdefault(f.name, []).append(f)
    roots = [app / "raceoverlay"]
    for f in internal.rglob("*"):
        if f.is_file() and f.parent not in libdirs and is_elf(f):
            roots.append(f)
    reach, stack = set(), []
    for r in roots:
        stack += needed(r)
    while stack:
        so = stack.pop()
        if so in libs and so not in reach:
            reach.add(so)
            real = next((p for p in libs[so] if p.exists()), None)
            if real is not None:
                stack += needed(real)
    removed = 0
    for name, paths in libs.items():
        keep = name in reach or name.startswith(("libpython", "libshiboken", "libpyside"))
        for f in paths:
            if (not keep) or (f.parent == internal and name in exclude):
                if f.exists() and not f.is_symlink():
                    removed += f.stat().st_size
                f.unlink(missing_ok=True)
    print(f"pruned {removed / 1e6:.0f} MB of unused / host-only libraries")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ffmpeg", required=True, type=Path)
    ap.add_argument("--libmpv", required=True, type=Path)
    ap.add_argument("--appimagetool", required=True, type=Path)
    ap.add_argument("--excludelist", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument("--work", type=Path, default=ROOT / "build")
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT / "src"))
    from raceoverlay import __version__
    work, out = a.work.resolve(), a.out.resolve()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)

    # 1. Python app + Qt
    src = ROOT / "src" / "raceoverlay"
    cmd = ["pyinstaller", "--noconfirm", "--clean", "--name", "raceoverlay",
           "--distpath", str(work / "dist"), "--workpath", str(work / "pyi"),
           "--specpath", str(work), "--paths", str(ROOT / "src"),
           "--collect-submodules", "raceoverlay",
           "--add-data", f"{src / 'fonts'}:raceoverlay/fonts",
           "--add-data", f"{src / 'icon.svg'}:raceoverlay",
           "--hidden-import", "mpv"]
    for m in EXCLUDE_MODULES:
        cmd += ["--exclude-module", m]
    run(cmd + [str(ROOT / "packaging" / "raceoverlay_app.py")])
    app = work / "dist" / "raceoverlay"
    internal = app / "_internal"
    exclude = {l.split("#")[0].strip() for l in a.excludelist.read_text().splitlines()
               if l.split("#")[0].strip()} | GLIBC
    prune(app, internal, exclude)

    # 2. libmpv + the ffmpeg libraries (shared with the ffmpeg CLI)
    mpvlib = internal / "mpvlib"
    mpvlib.mkdir()
    ffl = a.ffmpeg / "lib"
    for f in sorted(ffl.iterdir()):
        if re.match(r"lib(av|sw)\w+\.so\.\d+$", f.name):
            shutil.copy2(f.resolve(), mpvlib / f.name)
    shutil.copy2(a.libmpv.resolve(), mpvlib / "libmpv.so.2")
    env = dict(os.environ, LD_LIBRARY_PATH=str(ffl))
    deps = ldd(mpvlib / "libmpv.so.2", env)
    for so in ("libass.so.9", "libunibreak.so.5", "libunibreak.so.6"):
        if so in deps:
            shutil.copy2(Path(deps[so]).resolve(), mpvlib / so)
    for f in mpvlib.iterdir():
        run(["patchelf", "--set-rpath", "$ORIGIN", str(f)])
    # libplacebo (static inside libmpv) uses a little C++ (std::from_chars)
    # but its pkg-config doesn't say so; declare the host C++ runtime.
    if "libstdc++.so.6" not in needed(mpvlib / "libmpv.so.2"):
        run(["patchelf", "--add-needed", "libstdc++.so.6", str(mpvlib / "libmpv.so.2")])

    # 3. ffmpeg + ffprobe, reusing those libraries
    ffbin = internal / "ffmpeg" / "bin"
    ffbin.mkdir(parents=True)
    for exe in ("ffmpeg", "ffprobe"):
        shutil.copy2(a.ffmpeg / "bin" / exe, ffbin / exe)
        run(["patchelf", "--set-rpath", "$ORIGIN/../../mpvlib", str(ffbin / exe)])

    # 4. Host-first fallbacks for libmpv's desktop dependencies
    exclude = {l.split("#")[0].strip() for l in a.excludelist.read_text().splitlines()
               if l.split("#")[0].strip()} | GLIBC
    core = {f.name for f in mpvlib.iterdir()}
    fb = mpvlib / "fallback"
    fb.mkdir()
    closure = ldd(mpvlib / "libmpv.so.2", dict(os.environ, LD_LIBRARY_PATH=str(mpvlib)))
    fall = {so: p for so, p in closure.items()
            if so not in exclude and so not in core and not so.startswith(CORE_PREFIXES)}
    for so, p in fall.items():
        shutil.copy2(Path(p).resolve(), fb / so)
    direct = [so for f in ("libmpv.so.2", "libass.so.9")
              if (mpvlib / f).exists() for so in needed(mpvlib / f) if so in fall]
    manifest = {"direct": sorted(set(direct)),
                "needs": {so: [d for d in needed(fb / so) if d in fall] for so in fall}}
    (fb / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"fallback libs: {len(fall)}; direct: {manifest['direct']}")

    # 5. AppDir
    appdir = work / "AppDir"
    (appdir / "usr" / "lib").mkdir(parents=True)
    shutil.move(str(app), appdir / "usr" / "lib" / "raceoverlay")
    (appdir / "AppRun").write_text(
        '#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
        'exec "$HERE/usr/lib/raceoverlay/raceoverlay" "$@"\n')
    (appdir / "AppRun").chmod(0o755)
    (appdir / "raceoverlay.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Race Overlay\n"
        "Comment=Sync racing telemetry to video and render gauge overlays\n"
        "Exec=raceoverlay %f\nIcon=raceoverlay\nTerminal=false\n"
        "Categories=AudioVideo;Video;\nMimeType=video/mp4;video/quicktime;\n"
        "StartupWMClass=raceoverlay\n")
    shutil.copy2(src / "icon.svg", appdir / "raceoverlay.svg")
    (appdir / ".DirIcon").symlink_to("raceoverlay.svg")
    lic = appdir / "usr" / "share" / "licenses" / "raceoverlay"
    lic.mkdir(parents=True)
    shutil.copy2(a.ffmpeg / "LICENSE.txt", lic / "FFmpeg-LICENSE.txt")
    for f in ("NOTICE.txt",):
        p = ROOT / "packaging" / f
        if p.exists():
            shutil.copy2(p, lic / f)

    # 6. AppImage
    target = out / f"RaceOverlay-{__version__}-x86_64.AppImage"
    env = dict(os.environ, ARCH="x86_64", APPIMAGE_EXTRACT_AND_RUN="1")
    run([str(a.appimagetool), "--no-appstream", str(appdir), str(target)], env=env)
    print(f"\nBuilt {target} ({target.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
