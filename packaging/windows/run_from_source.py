# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Start Race Overlay or Race Analysis from a source checkout on Windows.

The launchers in windows/ run this with the venv's pythonw.exe (no console
window). It puts windows-bin/ (ffmpeg, ffprobe, libmpv-2.dll) and src/ on
the path, sends output to a log file (pythonw has nowhere else to put it),
and shows a message box instead of dying silently if something's missing.

    pythonw packaging/windows/run_from_source.py [--analysis] [files...]
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _box(title: str, text: str):
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR


def main() -> int:
    bin_dir = ROOT / "windows-bin"
    src = ROOT / "src"
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    os.environ["PYTHONPATH"] = str(src) + (os.pathsep + os.environ["PYTHONPATH"]
                                           if os.environ.get("PYTHONPATH") else "")
    os.environ.setdefault("RACEOVERLAY_MPV_SOURCE", "bundled")
    sys.path.insert(0, str(src))

    log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "raceoverlay"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "launch.log", "w", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log

    missing = [f for f in ("ffmpeg.exe", "ffprobe.exe", "libmpv-2.dll")
               if not (bin_dir / f).exists()]
    if missing:
        _box("Race Overlay", "Missing from windows-bin:\n" + "\n".join(missing)
             + "\n\nRun windows\\setup.bat to download them.")
        return 1

    args = sys.argv[1:]
    module = "raceoverlay.syncgui"
    if args[:1] == ["--analysis"]:
        module, args = "raceanalysis", args[1:]
    sys.argv = [module] + args
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        return int(e.code or 0) if not isinstance(e.code, str) else 1
    except BaseException:
        import traceback
        text = traceback.format_exc()
        sys.stderr.write(text)
        _box("Race Overlay crashed", text[-1500:] + f"\n\nFull log: {log_dir / 'launch.log'}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
