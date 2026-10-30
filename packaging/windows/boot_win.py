# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""PyInstaller runtime hook for the Windows build (runs before any app code).

  * bin\\ inside the bundle (ffmpeg.exe, ffprobe.exe, libmpv-2.dll) goes first
    on PATH, so python-mpv finds the DLL and the app finds ffmpeg; child
    processes (the render engine, its workers) inherit it.
  * A windowed app has no console. When nothing is attached to stdout or
    stderr (started from the Start Menu), output goes to a log file. When the
    app started us with pipes (the render engine, which reports progress on
    stderr), those pipes are used.
"""
import os
import sys


def _attach_std(fd: int, std_handle: int):
    """The stream for an inherited pipe/file handle, or None."""
    try:
        import ctypes
        import msvcrt
        h = ctypes.windll.kernel32.GetStdHandle(std_handle)
        if h in (0, -1, None) or ctypes.windll.kernel32.GetFileType(h) == 0:
            return None
        return open(msvcrt.open_osfhandle(h, os.O_WRONLY | os.O_TEXT), "w",
                    encoding="utf-8", errors="replace", buffering=1, closefd=False)
    except (OSError, ValueError, AttributeError):
        return None


def _log_file():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "raceoverlay")
    try:
        os.makedirs(d, exist_ok=True)
        # The app writes launch.log; its engine/workers append to their own.
        name = "launch.log" if len(sys.argv) < 2 or not sys.argv[1].startswith("--") \
            or sys.argv[1] == "--analysis" else "helper.log"
        return open(os.path.join(d, name), "a" if name == "helper.log" else "w",
                    encoding="utf-8", buffering=1)
    except OSError:
        return None


def _boot():
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    bin_dir = os.path.join(base, "bin")
    if os.path.isdir(bin_dir):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(bin_dir)
        except (OSError, AttributeError):
            pass
    os.environ.setdefault("RACEOVERLAY_MPV_SOURCE", "bundled")
    if sys.stdout is None:
        sys.stdout = _attach_std(1, -11)  # STD_OUTPUT_HANDLE
    if sys.stderr is None:
        sys.stderr = _attach_std(2, -12)  # STD_ERROR_HANDLE
    if sys.stdout is None or sys.stderr is None:
        log = _log_file()
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


_boot()
