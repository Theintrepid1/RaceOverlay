# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Everything that differs between Linux and Windows, in one place.

  data_dir() / cache_dir() / state_dir()   per-user folders
  drive_roots()          where SD cards and USB drives show up
  network_mounts()       mounted network shares (Linux; Windows dialogs
                         already list them)
  suspend_tree() / resume_tree() / terminate_tree()
                         pause, resume and stop a process and its children
                         (the render engine and its ffmpeg workers)
  hide_child_consoles()  Windows: stop every ffmpeg call flashing a console
  install_hint()         what to tell someone missing ffmpeg or mpv

No Qt here, so the render workers can use it too.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

APP = "raceoverlay"
NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs",
              "fuse.kio-fuse", "fuse.rclone", "9p", "davfs", "fuse.gvfsd-fuse"}


# ------------------------------------------------------------------ folders --

def _win_dir(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or (Path.home() / fallback))


APP_IDS = {"overlay": "Theintrepid1.RaceOverlay", "analysis": "Theintrepid1.RaceAnalysis"}


def set_app_id(which: str) -> None:
    """Windows: give this process its own taskbar identity, so Race Overlay
    and Race Analysis (the same .exe) get separate taskbar buttons and
    icons. Matches the AppUserModelID on the installer's shortcuts. Call
    before any window is created. Does nothing elsewhere."""
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_IDS[which])
    except Exception:  # noqa: BLE001 - only cosmetic
        pass


def data_dir() -> Path:
    """Saved tracks and other things worth keeping.
    Linux ~/.local/share/raceoverlay, Windows %APPDATA%\\raceoverlay."""
    if IS_WIN:
        return _win_dir("APPDATA", "AppData/Roaming") / APP
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP


def cache_dir() -> Path:
    """Throwaway: hardware probe results and the like.
    Linux ~/.cache/raceoverlay, Windows %LOCALAPPDATA%\\raceoverlay\\cache."""
    if IS_WIN:
        return _win_dir("LOCALAPPDATA", "AppData/Local") / APP / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP


def state_dir() -> Path:
    """Logs. Linux ~/.local/state/raceoverlay, Windows %LOCALAPPDATA%\\raceoverlay."""
    if IS_WIN:
        return _win_dir("LOCALAPPDATA", "AppData/Local") / APP
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / APP


# ------------------------------------------------------------------- drives --

def _win_drive_letters() -> list[str]:
    """Drive roots (E:\\ ...) that have media in them. Skips A:/B: (floppy
    probes can hang) and CD drives; C: is kept, is_card() filters it out."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS: no "insert a disk" popups
    mask = k32.GetLogicalDrives()
    out = []
    for i in range(2, 26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        kind = k32.GetDriveTypeW(root)
        if kind in (2, 3, 4, 6):  # removable, fixed, network, RAM disk
            if os.path.isdir(root):
                out.append(root)
    return out


def drive_label(root: str) -> str:
    """'E:\\ (GOPRO)' on Windows; the folder name elsewhere."""
    if not IS_WIN:
        return Path(root).name or root
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        if ctypes.windll.kernel32.GetVolumeInformationW(
                root, buf, 261, None, None, None, None, 0) and buf.value:
            return f"{root} ({buf.value})"
    except (OSError, AttributeError):
        pass
    return root


def drive_roots() -> list[str]:
    """Where a camera SD card could be mounted."""
    if IS_WIN:
        return _win_drive_letters()
    pts = []
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                mp = line.split()[1].replace("\\040", " ")
                if mp.startswith(("/media/", "/run/media/", "/mnt/")):
                    pts.append(mp)
    except OSError:
        pass
    user = os.environ.get("USER", "")
    for base in (f"/run/media/{user}", f"/media/{user}", "/media", "/mnt"):
        if os.path.isdir(base):
            pts += [os.path.join(base, d) for d in os.listdir(base)]
    seen, out = set(), []
    for p in pts:
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append(p)
    return out


def network_mounts() -> list[str]:
    """Mounted network shares (SMB/NFS/sshfs/...) plus removable media, for
    file-dialog sidebars. Empty on Windows: its own dialog lists them."""
    if not IS_LINUX:
        return []
    out = []
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[2] in NETWORK_FS:
                    out.append(parts[1].replace("\\040", " "))
    except OSError:
        pass
    user = os.environ.get("USER", "")
    for extra in (f"/run/media/{user}", f"/run/user/{os.getuid()}/gvfs"):
        if os.path.isdir(extra):
            out.append(extra)
    return sorted(set(out))


def hide_path(path) -> None:
    """Windows: set the Hidden attribute (Linux hides dot-names already)."""
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
        except (OSError, AttributeError):
            pass


# ---------------------------------------------------------------- processes --

def new_process_group():
    """Linux: make this process its own group, so the GUI can pause or stop
    it with all its children in one go. Windows needs nothing (see below)."""
    if hasattr(os, "setpgrp"):
        try:
            os.setpgrp()
        except OSError:
            pass


def _win_children() -> dict[int, list[int]]:
    """parent pid -> child pids, from a Toolhelp snapshot."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    kids: dict[int, list[int]] = {}
    if snap in (None, wintypes.HANDLE(-1).value):
        return kids
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            kids.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return kids


def _win_tree(pid: int) -> list[int]:
    kids = _win_children()
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        if p in out:
            continue
        out.append(p)
        todo += kids.get(p, [])
    return out


def _win_nt(pid: int, fn: str):
    import ctypes
    from ctypes import wintypes
    k32, ntdll = ctypes.windll.kernel32, ctypes.windll.ntdll
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    if h:
        try:
            getattr(ntdll, fn)(wintypes.HANDLE(h))
        finally:
            k32.CloseHandle(wintypes.HANDLE(h))


def _posix_signal(pid: int, sig):
    try:
        os.killpg(pid, sig)  # the engine runs as its own process group
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def suspend_tree(pid: int):
    if IS_WIN:
        for p in _win_tree(pid):
            _win_nt(p, "NtSuspendProcess")
    else:
        _posix_signal(pid, signal.SIGSTOP)


def resume_tree(pid: int):
    if IS_WIN:
        for p in _win_tree(pid):
            _win_nt(p, "NtResumeProcess")
    else:
        _posix_signal(pid, signal.SIGCONT)


def terminate_tree(pid: int):
    """Stop a process and everything it started. Finished render chunks are
    on disk already, so a hard stop loses nothing that Resume needs."""
    if IS_WIN:
        resume_tree(pid)  # a suspended process can't be torn down cleanly
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=15)
    else:
        _posix_signal(pid, signal.SIGTERM)
        _posix_signal(pid, signal.SIGCONT)  # a paused job must wake to exit


# ------------------------------------------------------------------ console --

_patched = False


def hide_child_consoles():
    """Windows GUI apps have no console, so every console program they start
    (ffmpeg, ffprobe) pops up its own black window. Default every subprocess
    to CREATE_NO_WINDOW instead. No-op elsewhere; safe to call repeatedly."""
    global _patched
    if not IS_WIN or _patched:
        return
    _patched = True
    no_window = 0x08000000  # CREATE_NO_WINDOW
    orig_init = subprocess.Popen.__init__

    def init(self, *a, **kw):
        if not kw.get("creationflags"):
            kw["creationflags"] = no_window
        orig_init(self, *a, **kw)
    subprocess.Popen.__init__ = init


# -------------------------------------------------------------------- hints --

def install_hint(what: str) -> str:
    """How to get a missing dependency, for error dialogs."""
    if IS_WIN:
        return ("The installer includes it. If you're running from source, "
                "run setup-windows.bat again to fetch it.")
    if what == "ffmpeg":
        return "Install it with:\nsudo pacman -S ffmpeg"
    return ("Install libmpv with:\nsudo pacman -S mpv\n"
            "and the Python binding with:\n.venv/bin/pip install python-mpv")
