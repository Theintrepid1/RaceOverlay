# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Tie a whole event folder together: data files, front and rear cameras.

For folders organised by hand (one folder per session, raw GoPro clips from
two cameras, the data file, an overlaid export...), not ones made by the
SD-card import.

    scan(folder)      every data file and video in the tree, duplicates and
                      non-telemetry files dropped
    match(scan)       each GoPro recording synced to the data file it fits
                      (from its motion sensors), grouped into sessions with
                      a front and a rear camera
    apply(sessions)   save the pairings next to the front videos (the rear
                      camera as Race Overlay's picture-in-picture) and name
                      the sessions after their folders

Front or rear: syncing a camera also works out how it's mounted, so the two
cameras in a car come out about 180 degrees apart. Every clip in the event
is put in one of two "facing" groups that way. Which group is the front
can't be told from the sensors alone (that needs the camera's axis layout),
so names decide ("front", "rear", "back"...), else a guess the user can
swap for the whole event at once.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov"}
DATA_EXT = {".csv", ".vbo"}
FRONT_WORDS = re.compile(r"(?i)(^|[^a-z])(front|fwd|forward|hood|dash|windshield)([^a-z]|$)")
REAR_WORDS = re.compile(r"(?i)(^|[^a-z])(rear|back|behind|driver|cockpit|in-?car)([^a-z]|$)")


@dataclass
class DataFile:
    path: str
    tel: object = None


@dataclass
class Video:
    parts: list                     # chapter files, in order
    has_sensors: bool = False
    note: str = ""

    @property
    def name(self) -> str:
        return Path(self.parts[0]).name


@dataclass
class Clip:
    """A GoPro recording synced to a data file."""
    video: Video
    data: DataFile
    offset: float                   # data time = video time + offset
    score: float
    forward: tuple                  # the car's forward in this camera's axes (unit)
    facing: str = "A"               # "A" or "B": which way it points (see module doc)


@dataclass
class Session:
    data: DataFile
    title: str
    front: Clip | None = None
    rear: Clip | None = None
    extra: list = field(default_factory=list)    # more recordings that also fit
    use: bool = True


@dataclass
class Scan:
    folder: str
    data: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    skipped: list = field(default_factory=list)   # (path, why)


def _fingerprint(path: str) -> str:
    """Same file, wherever it's been copied: size plus the first and last
    256 kB (whole-file hashes of GoPro clips over a NAS take too long)."""
    h = hashlib.sha1()
    try:
        size = os.path.getsize(path)
        h.update(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(1 << 18))
            if size > 1 << 19:
                f.seek(-(1 << 18), os.SEEK_END)
                h.update(f.read(1 << 18))
    except OSError:
        return path
    return h.hexdigest()


def _is_data_name(p: Path) -> bool:
    from . import ingest
    return p.suffix.lower() in DATA_EXT or ingest.is_apex_name(p.name)


def scan(folder: str, progress=None, cancelled=None, load=None) -> Scan:
    """Find the data files and videos under folder. progress(text) is
    called as it goes; load(path) -> Telemetry can be swapped for tests."""
    from . import telemetry
    from raceoverlay import media
    load = load or telemetry.load
    out = Scan(str(folder))
    data_paths, video_paths = [], []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or name.endswith((".part", ".sync.json", ".notes.json")):
                continue
            p = Path(root) / name
            if p.suffix.lower() in VIDEO_EXT:
                video_paths.append(p)
            elif _is_data_name(p):
                data_paths.append(p)
    seen = set()
    for p in data_paths:
        if cancelled and cancelled():
            return out
        if progress:
            progress(f"Reading {p.name}")
        fp = _fingerprint(str(p))
        if fp in seen:
            out.skipped.append((str(p), "a copy of another data file"))
            continue
        try:
            tel = load(str(p))
        except Exception:  # noqa: BLE001 - not a data file (edit lists...)
            out.skipped.append((str(p), "not a data file"))
            continue
        if tel.channels.get("lon_g") is None or tel.channels.get("lat_g") is None:
            out.skipped.append((str(p), "no g data to sync cameras to"))
            continue
        seen.add(fp)
        out.data.append(DataFile(str(p), tel))
    grouped = set()
    for p in video_paths:
        if str(p) in grouped:
            continue
        parts = media.find_chapters(str(p))
        grouped.update(parts)
        fp = _fingerprint(parts[0])
        if fp in seen:
            out.skipped.append((parts[0], "a copy of another video"))
            continue
        seen.add(fp)
        if cancelled and cancelled():
            return out
        if progress:
            progress(f"Checking {p.name}")
        v = Video(parts)
        try:   # just whether there's a GoPro sensor track (quick); read it later
            from . import gpmf
            v.has_sensors = gpmf._gpmd_stream_index(parts[0]) is not None
        except Exception:  # noqa: BLE001
            v.has_sensors = False
        if not v.has_sensors:
            v.note = ("no GoPro sensor track: an export or edit (re-encoding drops it), "
                      "or a phone video")
            out.skipped.append((parts[0], v.note))
        out.videos.append(v)
    return out


def _parts_with_starts(parts: list) -> list:
    from raceoverlay import media
    info = media.probe(parts)
    return [(x.path, x.start) for x in info.parts]


def match(sc: Scan, progress=None, cancelled=None, read=None) -> list[Session]:
    """Sync every GoPro recording to the data file it fits and build the
    sessions. read(video) -> (times, (N,3) accel) can be swapped for tests."""
    from . import gosync
    clips = []
    todo = [v for v in sc.videos if v.has_sensors]
    for i, v in enumerate(todo):
        if cancelled and cancelled():
            break
        if progress:
            progress(f"Syncing {v.name} ({i + 1} of {len(todo)})")
        try:
            t, a = read(v) if read else gosync.read_accel(_parts_with_starts(v.parts))
        except Exception:  # noqa: BLE001 - unreadable: leave it out
            v.note = "couldn't read its sensors"
            continue
        h = gosync.camera_horizontal(a)
        best = None
        for d in sc.data:
            try:
                res = gosync.find_offset(t, h, d.tel)
            except Exception:  # noqa: BLE001
                continue
            if best is None or (res.confident, res.score) > (best[1].confident, best[1].score):
                best = (d, res)
        if best is None or not best[1].confident:
            v.note = "didn't match any data file"
            continue
        d, res = best
        _car, info = gosync.to_car_frame(t, h, d.tel, res.offset)
        fwd = gosync.car_forward(a, info)
        clips.append(Clip(v, d, res.offset, res.score, tuple(float(x) for x in fwd)))
    _facings(clips)
    return sessions_from(sc, clips)


def _dot(a, b) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def _facings(clips: list[Clip]):
    """Two groups by which way the car's forward points in each camera's
    axes (a camera turned round sees it the opposite way); group "A" holds
    the first clip. Then make "A" the front when names say so."""
    if not clips:
        return
    ref = clips[0].forward
    for c in clips:
        c.facing = "A" if _dot(c.forward, ref) >= 0 else "B"
    votes = {"A": 0, "B": 0}
    for c in clips:
        text = " ".join(Path(p).as_posix() for p in c.video.parts[:1])
        if FRONT_WORDS.search(text):
            votes[c.facing] += 1
        if REAR_WORDS.search(text):
            votes["B" if c.facing == "A" else "A"] += 1
    if votes["B"] > votes["A"]:
        for c in clips:
            c.facing = "B" if c.facing == "A" else "A"


def _title(sc: Scan, d: DataFile, clips: list[Clip]) -> str:
    """The session's name: the folder its videos (or data) are in, when
    that's a folder of its own inside the event, else the data file's."""
    root = Path(sc.folder).resolve()
    for p in [Path(c.video.parts[0]) for c in clips] + [Path(d.path)]:
        parent = p.resolve().parent
        if parent != root and root in parent.parents and not re.match(
                r"^\d{4}-\d{2}-\d{2}$", parent.name):      # not an import's day folder
            return parent.name
    meta = getattr(d.tel, "meta", {}) or {}
    return meta.get("session_notes") or Path(d.path).stem


def sessions_from(sc: Scan, clips: list[Clip], swap: bool = False) -> list[Session]:
    """Sessions from synced clips: per data file, the best-matching clip
    facing front and the best facing rear (swap: the other way round)."""
    front_group = "B" if swap else "A"
    out = []
    for d in sc.data:
        mine = sorted((c for c in clips if c.data is d), key=lambda c: -c.score)
        s = Session(d, _title(sc, d, mine))
        for c in mine:
            if c.facing == front_group and s.front is None:
                s.front = c
            elif c.facing != front_group and s.rear is None:
                s.rear = c
            else:
                s.extra.append(c)
        if s.front is None and s.rear is not None:      # one camera: it's the video
            s.front, s.rear = s.rear, None
        out.append(s)
    return out


def swap_all(sessions: list[Session]):
    """Front <-> rear for every session (the facing guess was wrong)."""
    for s in sessions:
        if s.rear is not None:
            s.front, s.rear = s.rear, s.front


def apply(sessions: list[Session]) -> list[tuple[str, list, float]]:
    """Save each used session: the front video's sidecar points at the data
    file (and holds the rear camera as picture-in-picture), and the data
    file's notes get the session's name. Returns (data file, video parts,
    offset) for Race Analysis to open."""
    import json
    from . import day, ingest
    out = []
    for s in sessions:
        if not s.use:
            continue
        try:
            n = day.Notes.load(s.data.path)
            if s.title and n.title != s.title:
                n.title = s.title
                n.save(s.data.path)
        except OSError:
            pass
        if s.front is None:
            out.append((s.data.path, [], 0.0))
            continue
        fp = s.front.video.parts
        sc = ingest.save_pairing(fp, s.data.path, s.front.offset)
        if sc is not None:
            try:
                data = json.loads(sc.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if s.rear is not None:
                # second camera time = main video time + (front offset - rear offset)
                data["pip"] = {"parts": [str(Path(p).resolve()) for p in s.rear.video.parts],
                               "offset": round(s.front.offset - s.rear.offset, 4),
                               "synced": True}
            else:
                data.pop("pip", None)
            try:
                sc.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
        out.append((s.data.path, list(fp), s.front.offset))
    return out


def summary(s: Session) -> str:
    bits = []
    for label, c in (("front", s.front), ("rear", s.rear)):
        if c is not None:
            bits.append(f"{label} {c.video.name} ({c.score:.0%})")
    return ", ".join(bits) or "no video"


def facing_clarity(clips: list[Clip]) -> float:
    """How cleanly the clips split into two facings: the smallest |cos| of
    the angle to the first clip's forward (1 = perfectly forward or back,
    0 = side-on, can't tell)."""
    if not clips:
        return 1.0
    ref = clips[0].forward
    return float(min(abs(_dot(c.forward, ref)) for c in clips))


