# SPDX-License-Identifier: GPL-3.0-or-later
"""Platform helpers: Linux behaviour for real, Windows folder logic by
pretending (the ctypes parts can only run on Windows itself)."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from racecore import osutil


def test_linux_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(osutil, "IS_WIN", False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    assert osutil.data_dir() == tmp_path / "d" / "raceoverlay"
    assert osutil.cache_dir() == tmp_path / "c" / "raceoverlay"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert osutil.data_dir() == Path.home() / ".local" / "share" / "raceoverlay"


def test_windows_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(osutil, "IS_WIN", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert osutil.data_dir() == tmp_path / "Roaming" / "raceoverlay"
    assert osutil.cache_dir() == tmp_path / "Local" / "raceoverlay" / "cache"
    assert osutil.state_dir() == tmp_path / "Local" / "raceoverlay"


def test_tracks_follow_data_dir(monkeypatch, tmp_path):
    from racecore import tracks
    monkeypatch.setattr(osutil, "IS_WIN", True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert tracks.tracks_dir() == tmp_path / "raceoverlay" / "tracks"


def test_windows_extras_are_harmless_here():
    osutil.hide_child_consoles()  # no-op off Windows
    osutil.hide_path("/nonexistent")
    assert isinstance(osutil.network_mounts(), list)
    assert isinstance(osutil.drive_roots(), list)
    assert "pacman" in osutil.install_hint("ffmpeg") or osutil.IS_WIN


@pytest.mark.skipif(not osutil.IS_LINUX, reason="POSIX job control")
def test_suspend_resume_terminate_tree():
    # A parent in its own process group with a child, like the render engine.
    p = subprocess.Popen([sys.executable, "-c",
                          "import os, subprocess, sys, time; os.setpgrp();"
                          "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
                          "time.sleep(30)"])
    time.sleep(0.5)

    def state(pid):
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0]
    osutil.suspend_tree(p.pid)
    time.sleep(0.2)
    assert state(p.pid) == "T"
    osutil.resume_tree(p.pid)
    time.sleep(0.2)
    assert state(p.pid) in ("S", "R")
    osutil.suspend_tree(p.pid)
    osutil.terminate_tree(p.pid)  # must wake a paused job so it can exit
    assert p.wait(timeout=5) != 0
    # the child went too (same process group); zombies waiting for a reaper
    # count as gone
    time.sleep(0.3)
    out = subprocess.run(["pgrep", "-g", str(p.pid)], capture_output=True, text=True)
    alive = [pid for pid in out.stdout.split() if state(pid) != "Z"]
    assert alive == []


def test_concat_list_uses_forward_slashes(tmp_path):
    from raceoverlay import media
    lst = tmp_path / "l.txt"
    media.concat_list([str(tmp_path / "it's.mp4")], str(lst))
    line = lst.read_text(encoding="utf-8").strip()
    assert line.startswith("file '")
    assert line.endswith("it'\\''s.mp4'")      # quote escaped the ffmpeg way
    path_part = line[len("file '"):-len("it'\\''s.mp4'")]
    assert "\\" not in path_part                 # no Windows backslashes
