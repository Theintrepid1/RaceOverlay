# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""App-menu entries for the AppImage (Linux).

An AppImage can carry only one menu entry, so app managers (Shelly, Gear
Lever, AppImageLauncher) install just Race Overlay. The bundled entry has a
"Race Analysis" action, and on every start from an AppImage the app makes
sure a separate Race Analysis entry exists for this file
(ensure_analysis_entry). An Overlay entry that some other tool wrote is
never rewritten.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path


def appimage_path() -> str | None:
    p = os.environ.get("APPIMAGE")
    return p if p and os.path.exists(p) else None


def apps_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "applications"


def desktop_file(name: str = "raceoverlay") -> Path:
    return apps_dir() / f"{name}.desktop"


def exec_program(line: str) -> str | None:
    """The program an Exec= value runs: handles quotes, an `env A=B` prefix
    (Shelly writes one when it sets variables) and %-codes."""
    try:
        toks = shlex.split(line)
    except ValueError:
        toks = line.split()
    if toks and os.path.basename(toks[0]) == "env":
        toks = toks[1:]
        while toks and "=" in toks[0] and not toks[0].startswith("/"):
            toks = toks[1:]
    return toks[0] if toks else None


def _entry_exec(f: Path) -> tuple[str | None, str]:
    """(program, whole Exec value) of a desktop file's main entry."""
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""
    main = False
    for line in text.splitlines():
        if line.startswith("["):
            main = line.strip() == "[Desktop Entry]"
        elif main and line.startswith("Exec="):
            return exec_program(line[5:]), line[5:]
    return None, ""


def menu_entry_target() -> str | None:
    """The AppImage path the Race Overlay menu entry launches, or None."""
    return _entry_exec(desktop_file())[0] if desktop_file().exists() else None


def _same(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a == b


def has_entry_for(appimage: str, analysis: bool) -> bool:
    """Is there any menu entry (whoever wrote it) that starts this AppImage,
    as Race Analysis (analysis=True) or Race Overlay?"""
    d = apps_dir()
    if not d.is_dir():
        return False
    for f in d.glob("*.desktop"):
        prog, whole = _entry_exec(f)
        if _same(prog, appimage) and (("--analysis" in whole) == analysis):
            return True
    return False


def _write_icons():
    base = apps_dir().parent
    icon_dir = base / "icons" / "hicolor" / "scalable" / "apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    for src_name, name in (("icon.svg", "raceoverlay"), ("icon_analysis.svg", "raceanalysis")):
        src_icon = Path(__file__).with_name(src_name)
        if src_icon.exists():
            shutil.copyfile(src_icon, icon_dir / f"{name}.svg")


def _refresh_caches():
    base = apps_dir().parent
    for cmd in (["update-desktop-database", str(apps_dir())],
                ["gtk-update-icon-cache", "-q", str(base / "icons" / "hicolor")]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001 - optional tools
            pass


def _overlay_entry(appimage: str) -> str:
    return ("[Desktop Entry]\nType=Application\nName=Race Overlay\n"
            "GenericName=Telemetry Video Overlay\n"
            "Comment=Sync racing telemetry to video and render gauge overlays\n"
            f'Exec="{appimage}" %f\nTryExec={appimage}\nIcon=raceoverlay\nTerminal=false\n'
            "Categories=AudioVideo;Video;\n"
            "MimeType=video/mp4;video/quicktime;video/x-matroska;\n"
            "StartupWMClass=raceoverlay\nActions=analysis;\n\n"
            "[Desktop Action analysis]\nName=Race Analysis\n"
            f'Exec="{appimage}" --analysis %F\nIcon=raceanalysis\n')


def _analysis_entry(appimage: str) -> str:
    return ("[Desktop Entry]\nType=Application\nName=Race Analysis\n"
            "GenericName=Lap Analysis\n"
            "Comment=Compare laps and sessions and find where the time goes\n"
            f'Exec="{appimage}" --analysis %F\nTryExec={appimage}\nIcon=raceanalysis\n'
            "Terminal=false\nCategories=AudioVideo;Video;\nMimeType=text/csv;\n"
            "StartupWMClass=raceanalysis\n")


def add_menu_entry(appimage: str):
    """Both entries, written by us (the Setup tab's Add, or the first-run
    question)."""
    _write_icons()
    apps_dir().mkdir(parents=True, exist_ok=True)
    desktop_file().write_text(_overlay_entry(appimage), encoding="utf-8")
    desktop_file("raceanalysis").write_text(_analysis_entry(appimage), encoding="utf-8")
    _refresh_caches()


def ensure_analysis_entry(appimage: str | None = None) -> bool:
    """When this AppImage is in the menu as Race Overlay (by us or by an app
    manager) but Race Analysis isn't, add Race Analysis quietly. Returns
    True when it wrote one."""
    appimage = appimage or appimage_path()
    if not appimage:
        return False
    if not has_entry_for(appimage, analysis=False) or has_entry_for(appimage, analysis=True):
        return False
    try:
        _write_icons()
        desktop_file("raceanalysis").write_text(_analysis_entry(appimage), encoding="utf-8")
        _refresh_caches()
    except OSError:
        return False
    return True


def remove_menu_entry():
    for name in ("raceoverlay", "raceanalysis"):
        try:
            desktop_file(name).unlink()
        except OSError:
            pass
