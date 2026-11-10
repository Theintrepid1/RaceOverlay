# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Updates from the project's GitHub releases, and rolling back.

    releases()          every published version, newest first
    newer(rels, cur)    the newest release above `cur`, or None
    install_kind()      how this copy was installed: "appimage", "windows",
                        "source" (only checks, never installs)
    download()          fetch one release file, checked against GitHub's
                        SHA-256 for it
    apply_appimage()    swap a downloaded AppImage in under the running
                        file's own name, keeping the old one for rollback
    run_windows_setup() start a downloaded installer silently; it closes
                        the app, installs over it and starts it again

Rollback has to work at a track with no internet, so the version being
replaced is always kept on disk (one copy): on Linux the old AppImage
itself, on Windows the installer for the version that was installed.
Both live in data_dir()/previous with previous.json describing them.

The once-a-day check's state (last check, skipped version, on/off) is in
data_dir()/update.json, shared by both apps.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import osutil

REPO = "Theintrepid1/RaceOverlay"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=40"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
CHECK_EVERY = dt.timedelta(hours=24)
ASSET_RE = re.compile(r"^RaceOverlay-(\d+(?:\.\d+){1,3})-(x86_64\.AppImage|windows-setup\.exe)$")


class UpdateError(Exception):
    pass


def parse_version(v: str) -> tuple[int, ...]:
    """'1.4.0' / 'V1.4' / 'v1.4.0-beta' -> (1, 4, 0); missing parts are 0."""
    m = re.search(r"(\d+(?:\.\d+)*)", v or "")
    if not m:
        return (0, 0, 0)
    parts = [int(x) for x in m.group(1).split(".")][:4]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def current_version() -> str:
    from raceoverlay import __version__
    return __version__


@dataclass
class Asset:
    name: str
    url: str
    size: int
    sha256: str | None


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    published: str
    assets: dict = field(default_factory=dict)     # "appimage" | "windows" -> Asset

    @property
    def key(self) -> tuple[int, ...]:
        return parse_version(self.version)


def parse_releases(data: list) -> list[Release]:
    """GitHub's release list -> Releases that have at least one of our
    files, newest first. The version comes from the file names, which the
    build sets (the tag is typed by hand: "V1.4")."""
    out = []
    for r in data or []:
        if r.get("draft") or r.get("prerelease"):
            continue
        assets, version = {}, None
        for a in r.get("assets") or []:
            m = ASSET_RE.match(a.get("name", ""))
            if not m:
                continue
            version = version or m.group(1)
            digest = a.get("digest") or ""
            assets["appimage" if m.group(2).endswith("AppImage") else "windows"] = Asset(
                a["name"], a.get("browser_download_url", ""), int(a.get("size") or 0),
                digest[7:].lower() if digest.startswith("sha256:") else None)
        if not assets:
            continue
        out.append(Release(version, r.get("tag_name", ""), r.get("body") or "",
                           (r.get("published_at") or "")[:10], assets))
    out.sort(key=lambda r: r.key, reverse=True)
    return out


def _get(url: str, timeout: float = 15.0):
    from raceoverlay import __version__
    req = urllib.request.Request(url, headers={
        "User-Agent": f"RaceOverlay/{__version__} (update check)",
        "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def releases(url: str = API) -> list[Release]:
    try:
        with _get(url) as resp:
            return parse_releases(json.loads(resp.read().decode("utf-8")))
    except (OSError, ValueError) as e:
        raise UpdateError(f"Couldn't reach GitHub: {e}") from e


def newer(rels: list[Release], current: str, kind: str | None = None) -> Release | None:
    """The newest release above `current` that has a file for this kind of
    install (any file when kind is None)."""
    cur = parse_version(current)
    for r in rels:
        if r.key > cur and (kind is None or kind not in ("appimage", "windows")
                            or kind in r.assets):
            return r
    return None


def install_kind() -> str:
    """"appimage", "windows" (the installed .exe) or "source"."""
    if os.environ.get("APPIMAGE") and os.path.exists(os.environ["APPIMAGE"]):
        return "appimage"
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        return "windows"
    return "source"


# ------------------------------------------------------------- daily check --

def _state_file() -> Path:
    return osutil.data_dir() / "update.json"


def load_state() -> dict:
    try:
        d = json.loads(_state_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(**kw) -> dict:
    d = load_state()
    d.update(kw)
    try:
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(json.dumps(d, indent=1), encoding="utf-8")
    except OSError:
        pass
    return d


def auto_enabled() -> bool:
    return bool(load_state().get("auto", True))


def due(now: dt.datetime | None = None) -> bool:
    """Time for the once-a-day check?"""
    st = load_state()
    if not st.get("auto", True):
        return False
    try:
        last = dt.datetime.fromisoformat(st.get("last_check", ""))
    except (TypeError, ValueError):
        return True
    return (now or dt.datetime.now()) - last >= CHECK_EVERY


def mark_checked(now: dt.datetime | None = None):
    save_state(last_check=(now or dt.datetime.now()).isoformat(timespec="seconds"))


# ---------------------------------------------------------------- download --

def download(asset: Asset, dest: str | Path, progress=None, cancelled=None) -> Path:
    """Fetch asset to dest (via dest.part), checking size and SHA-256.
    progress(done, total) is called as it goes; cancelled() -> True stops."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    try:
        with _get(asset.url, timeout=30) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or asset.size or 0)
            while True:
                if cancelled and cancelled():
                    raise UpdateError("Cancelled.")
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if asset.size and done != asset.size:
            raise UpdateError(f"Download incomplete ({done} of {asset.size} bytes).")
        if asset.sha256 and h.hexdigest() != asset.sha256:
            raise UpdateError("The download doesn't match GitHub's checksum; not using it.")
        os.replace(part, dest)
        return dest
    except OSError as e:
        raise UpdateError(f"Download failed: {e}") from e
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- previous --

def previous_dir() -> Path:
    return osutil.data_dir() / "previous"


def previous() -> dict | None:
    """{"version", "file", "kind"} for the kept older version, if its file
    is still there."""
    try:
        d = json.loads((previous_dir() / "previous.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("file"):
        return None
    if not (previous_dir() / d["file"]).exists():
        return None
    return d


def _set_previous(version: str, path: Path, kind: str):
    """Make path (already in previous_dir) the one kept version; drop any
    other kept files."""
    d = previous_dir()
    for f in d.iterdir():
        if f.name not in (path.name, "previous.json"):
            try:
                f.unlink()
            except OSError:
                pass
    (d / "previous.json").write_text(json.dumps(
        {"version": version, "file": path.name, "kind": kind}), encoding="utf-8")


# ---------------------------------------------------------------- AppImage --

def appimage_target() -> Path | None:
    p = os.environ.get("APPIMAGE")
    return Path(p) if p and os.path.exists(p) else None


def appimage_writable(target: Path) -> bool:
    return os.access(target.parent, os.W_OK)


def apply_appimage(new_file: Path, target: Path, current: str) -> None:
    """Put new_file in place of target (same name, so menu entries and file
    associations keep working) and keep the old one for rollback. new_file
    must be on the same filesystem as target (download it next to it)."""
    keep_dir = previous_dir()
    keep_dir.mkdir(parents=True, exist_ok=True)
    kept = keep_dir / f"RaceOverlay-{current}-x86_64.AppImage"
    tmp = kept.with_name(kept.name + ".part")
    shutil.copy2(target, tmp)                 # the running copy stays mounted
    os.replace(tmp, kept)
    os.chmod(new_file, 0o755)
    os.replace(new_file, target)              # atomic: never half a file
    _set_previous(current, kept, "appimage")


def rollback_appimage(target: Path, current: str) -> str:
    """Swap the kept older AppImage back in; the current one becomes the
    kept one (so you can go forward again). Returns the version now in
    place. Works offline."""
    prev = previous()
    if not prev or prev.get("kind") != "appimage":
        raise UpdateError("No earlier version is kept on this computer.")
    src = previous_dir() / prev["file"]
    staged = target.with_name(f".{target.name}.rollback")
    shutil.copy2(src, staged)
    os.chmod(staged, 0o755)
    kept = previous_dir() / f"RaceOverlay-{current}-x86_64.AppImage"
    tmp = kept.with_name(kept.name + ".part")
    shutil.copy2(target, tmp)
    os.replace(tmp, kept)
    os.replace(staged, target)
    _set_previous(current, kept, "appimage")
    return prev["version"]


def restart_appimage(target: Path, analysis: bool):
    """Start the (new) AppImage; the caller quits."""
    args = [str(target)] + (["--analysis"] if analysis else [])
    env = dict(os.environ)
    for k in ("APPIMAGE", "APPDIR", "ARGV0", "OWD"):
        env.pop(k, None)
    subprocess.Popen(args, env=env, start_new_session=True, close_fds=True)


# ----------------------------------------------------------------- Windows --

def keep_windows_installer(setup: Path, version: str):
    """Keep the installer for `version` (the one being replaced) for an
    offline rollback."""
    d = previous_dir()
    d.mkdir(parents=True, exist_ok=True)
    kept = d / setup.name
    if setup.resolve() != kept.resolve():
        shutil.copy2(setup, kept)
    _set_previous(version, kept, "windows")


def run_windows_setup(setup: Path, relaunch: str = "overlay"):
    """Start the installer silently and detached. It closes the running app
    (Restart Manager), installs over it and starts it again (the [Run]
    entries check /RELAUNCH=). The caller should quit right after."""
    args = [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CLOSEAPPLICATIONS", f"/RELAUNCH={relaunch}"]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, creationflags=flags, close_fds=True)


def rollback_windows(folder: Path, current: str) -> Path:
    """Get the kept older installer ready to run (copied out of the kept
    folder first), and keep the current version's installer in its place
    if it's still in the download folder, so you can go forward again.
    Returns the installer to run. Works offline."""
    prev = previous()
    if not prev or prev.get("kind") != "windows":
        raise UpdateError("No earlier version is kept on this computer.")
    folder.mkdir(parents=True, exist_ok=True)
    run = folder / prev["file"]
    src = previous_dir() / prev["file"]
    if src.resolve() != run.resolve():
        shutil.copy2(src, run)
    cur = folder / f"RaceOverlay-{current}-windows-setup.exe"
    if cur.exists():
        keep_windows_installer(cur, current)
    return run


def windows_download_dir() -> Path:
    return osutil.cache_dir() / "updates"


def scratch_dir() -> Path:
    return Path(tempfile.gettempdir())
