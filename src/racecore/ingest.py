# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Bring a track day's footage and logger files onto the computer.

  find_cards()       mounted GoPro SD cards (anything with DCIM/1xxGOPRO)
  scan_clips()       a card's recordings, chapters grouped, newest first
  import_clips()     copy recordings into <library>/<YYYY-MM-DD>/, untouched
                     (raw chapters keep their GPMF sensor track), skipping
                     files already copied; nothing on the card is changed
  find_logs()        logger CSVs in a folder, with the date they were recorded
  pair()             which logger file goes with a clip, decided by actually
                     syncing the camera's accelerometer against each candidate
                     (APEX Pro CSVs carry no clock time, so timestamps can't)

No Qt here: callers pass a progress callback and a cancel check.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import osutil

# GoPro chapter names, same rule as raceoverlay.media.CHAPTER_RE:
# G + H/X/L/P, 2-digit chapter, 4-digit recording, e.g. GH020123.MP4.
CHAPTER_RE = re.compile(r"^(G[A-Z])(\d{2})(\d{4})(\.mp4)$", re.IGNORECASE)
GOPRO_DIR_RE = re.compile(r"^\d{3}GOPRO$", re.IGNORECASE)
# APEX Pro export names end like "..._on_09.27_1.2026.csv" (month.day_n.year)
APEX_DATE_RE = re.compile(r"_on_(\d{1,2})\.(\d{1,2})(?:_(\d+))?\.(\d{4})\.csv$",
                          re.IGNORECASE)
COPY_CHUNK = 8 << 20


@dataclass
class Card:
    root: str          # mount point / folder containing DCIM
    label: str         # what to show the user


@dataclass
class Clip:
    number: str                 # recording number, e.g. "0009"
    kind: str                   # "GH" (H.264), "GX" (HEVC), ...
    chapters: list[str]         # paths in chapter order
    size: int                   # bytes, all chapters
    recorded: dt.datetime | None = None   # camera clock (local, naive)
    duration: float | None = None         # seconds, all chapters
    imported: bool = False      # every chapter already in the library

    @property
    def name(self) -> str:
        return Path(self.chapters[0]).name


@dataclass
class Log:
    path: str
    date: dt.date | None        # recording date if the name or file says so
    session: int | None = None  # APEX "_1" style session number


@dataclass
class Pairing:
    clip: Clip
    log: Log | None
    offset: float | None = None
    score: float | None = None
    confident: bool = False
    tried: list[tuple[str, float]] = field(default_factory=list)  # (path, score)
    note: str = ""


# ------------------------------------------------------------------ cards ---

def _mount_points() -> list[str]:
    return osutil.drive_roots()


def is_card(root: str) -> bool:
    dcim = Path(root) / "DCIM"
    try:
        return dcim.is_dir() and any(GOPRO_DIR_RE.match(d.name)
                                     for d in dcim.iterdir() if d.is_dir())
    except OSError:
        return False


def find_cards() -> list[Card]:
    cards = []
    for mp in _mount_points():
        if is_card(mp):
            try:
                st = shutil.disk_usage(mp)
                used = f"{(st.total - st.free) / 1e9:.1f} of {st.total / 1e9:.0f} GB used"
            except OSError:
                used = ""
            name = osutil.drive_label(mp)
            cards.append(Card(mp, f"{name}  ({used})" if used else name))
    return cards


# ------------------------------------------------------------------ clips ---

def _probe(path: str) -> tuple[dt.datetime | None, float | None]:
    """Camera-clock creation time and duration of one MP4."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:format_tags=creation_time", "-of", "json", path],
            capture_output=True, text=True, timeout=30, check=True).stdout
        f = json.loads(out).get("format", {})
        dur = float(f["duration"]) if "duration" in f else None
        ct = f.get("tags", {}).get("creation_time")
        # GoPro writes its local clock time but labels it UTC; keep it naive.
        rec = dt.datetime.fromisoformat(ct.replace("Z", "")[:19]) if ct else None
        return rec, dur
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None, None


def scan_clips(root: str, library: str | None = None, probe: bool = True) -> list[Clip]:
    """Recordings on a card (or any folder with GoPro chapter files)."""
    base = Path(root)
    dirs = [d for d in (base / "DCIM").iterdir()
            if d.is_dir() and GOPRO_DIR_RE.match(d.name)] if (base / "DCIM").is_dir() else [base]
    groups: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    for d in sorted(dirs):
        for f in d.iterdir():
            m = CHAPTER_RE.match(f.name)
            if not m or m.group(1).upper() in ("GL",):  # skip low-res proxies
                continue
            groups.setdefault((m.group(1).upper(), m.group(3)), []).append(
                (int(m.group(2)), f))
    clips = []
    for (kind, num), files in groups.items():
        files.sort()
        paths = [str(f) for _, f in files]
        size = sum(f.stat().st_size for _, f in files)
        rec, dur = (None, None)
        if probe:
            rec, _ = _probe(paths[0])
            durs = [_probe(p)[1] for p in paths]
            dur = sum(d for d in durs if d) if any(durs) else None
        clip = Clip(num, kind, paths, size, rec, dur)
        if library:
            clip.imported = all(dest_path(clip, library, p).exists() and
                                dest_path(clip, library, p).stat().st_size ==
                                Path(p).stat().st_size for p in paths)
        clips.append(clip)
    clips.sort(key=lambda c: (c.recorded or dt.datetime.min, c.number), reverse=True)
    return clips


def day_folder(clip: Clip, library: str) -> Path:
    day = (clip.recorded or dt.datetime.fromtimestamp(
        Path(clip.chapters[0]).stat().st_mtime)).date()
    return Path(library) / day.isoformat()


def dest_path(clip: Clip, library: str, src: str) -> Path:
    return day_folder(clip, library) / Path(src).name


def import_clips(clips: list[Clip], library: str, progress=None, cancelled=None) -> list[list[str]]:
    """Copy clips into the library. Returns each clip's copied chapter paths.

    progress(done_bytes, total_bytes, current_file) is called as it goes;
    cancelled() returning True stops it (the half-copied file is removed).
    Files already in the library with the same size are not copied again.
    """
    todo = [(c, p) for c in clips for p in c.chapters]
    total = sum(Path(p).stat().st_size for _, p in todo)
    done = 0
    out: dict[int, list[str]] = {id(c): [] for c in clips}
    for clip, src in todo:
        dst = dest_path(clip, library, src)
        size = Path(src).stat().st_size
        if dst.exists() and dst.stat().st_size == size:
            done += size
            out[id(clip)].append(str(dst))
            if progress:
                progress(done, total, dst.name)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    if cancelled and cancelled():
                        raise InterruptedError
                    buf = fi.read(COPY_CHUNK)
                    if not buf:
                        break
                    fo.write(buf)
                    done += len(buf)
                    if progress:
                        progress(done, total, dst.name)
            if tmp.stat().st_size != size:
                raise OSError(f"copy of {src} is the wrong size")
            shutil.copystat(src, tmp)
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        out[id(clip)].append(str(dst))
    return [out[id(c)] for c in clips]


# ------------------------------------------------------------------- logs ---

def log_info(path: str) -> Log:
    m = APEX_DATE_RE.search(Path(path).name)
    if m:
        mo, d, n, y = m.groups()
        try:
            return Log(path, dt.date(int(y), int(mo), int(d)), int(n) if n else None)
        except ValueError:
            pass
    return Log(path, dt.date.fromtimestamp(Path(path).stat().st_mtime))


def find_logs(folder: str) -> list[Log]:
    """Logger CSVs in a folder (not recursive), newest first."""
    try:
        files = [str(p) for p in Path(folder).iterdir()
                 if p.is_file() and p.suffix.lower() == ".csv"]
    except OSError:
        return []
    logs = [log_info(f) for f in files]
    logs.sort(key=lambda lg: (lg.date or dt.date.min, lg.session or 0), reverse=True)
    return logs


def candidates(clip: Clip, logs: list[Log], window_days: int = 1) -> list[Log]:
    """Logs from the clip's day (+-window_days for a wrong camera clock);
    every log when the clip's date is unknown."""
    if clip.recorded is None:
        return list(logs)
    d = clip.recorded.date()
    near = [lg for lg in logs if lg.date and abs((lg.date - d).days) <= window_days]
    return near or list(logs)


def pair(clip: Clip, parts: list[tuple[str, float]], logs: list[Log],
         load=None, sync=None, cancelled=None, accel=None) -> Pairing:
    """Pick the log that this clip syncs to, by trying each candidate.

    parts: the clip's files with their start on the joined timeline.
    load(path) -> Telemetry, sync(times, horizontal_g, tel) -> SyncResult and
    accel=(times, (N,3) m/s^2) can be swapped in for tests; by default they
    come from racecore.telemetry, gosync and the clip's files.
    """
    from . import gosync, telemetry
    load = load or telemetry.load
    try:
        t, a = accel if accel is not None else gosync.read_accel(parts)
    except gosync.NoCameraData:
        return Pairing(clip, None, note="no GoPro sensor data in this clip")
    h = gosync.camera_horizontal(a)
    sync = sync or (lambda tt, hh, tel: gosync.find_offset(tt, hh, tel))
    best, tried = None, []
    for lg in candidates(clip, logs):
        if cancelled and cancelled():
            break
        try:
            tel = load(lg.path)
            if tel.channels.get("lon_g") is None:
                continue
            res = sync(t, h, tel)
        except Exception:  # noqa: BLE001 - a bad CSV just isn't a match
            continue
        tried.append((lg.path, res.score))
        if best is None or (res.confident, res.score) > (best[1].confident, best[1].score):
            best = (lg, res)
    if best is None:
        return Pairing(clip, None, tried=tried, note="no logger file to try")
    lg, res = best
    if not res.confident:
        return Pairing(clip, None, res.offset, res.score, False, tried,
                       note="no confident match")
    return Pairing(clip, lg, res.offset, res.score, True, tried)


# --------------------------------------------------------------- sidecars ---
# Race Overlay keeps each video's sync in <first file>.sync.json. Writing the
# pairing there means Race Overlay opens imported videos already synced.

def sidecar_path(parts: list[str]) -> Path:
    p = Path(parts[0])
    return p.with_name(p.name + ".sync.json")


def save_pairing(parts: list[str], csv: str, offset: float) -> Path | None:
    """Record that `parts` (one recording) is synced to `csv` at `offset`
    (logger time = video time + offset). Other settings already in the file
    (gauge layout, style...) are kept."""
    sc = sidecar_path(parts)
    data = {}
    if sc.exists():
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    data.update({"parts": [str(Path(p).resolve()) for p in parts],
                 "csv": str(Path(csv).resolve()), "offset": round(float(offset), 4),
                 "synced": True})
    try:
        sc.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return sc


def synced_sessions(folder: str) -> list[tuple[str, list[str], float]]:
    """(csv, video files, offset) for every synced recording in a folder (a
    track day), oldest recording first."""
    out = []
    for sc in sorted(Path(folder).glob("*.sync.json")):
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        csv = data.get("csv")
        parts = data.get("parts") or [str(sc)[:-len(".sync.json")]]
        if not csv or not data.get("synced", "offset" in data) or not Path(csv).exists():
            continue
        if not all(Path(p).exists() for p in parts):
            continue
        out.append((csv, list(parts), float(data.get("offset", 0.0))))
    return out
