# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Tie a whole event folder together: data files and their videos.

For folders organised by hand (one folder per session, raw GoPro clips, the
data file, an overlaid export...), not ones made by the SD-card import.

    scan(folder)      every data file and video in the tree, duplicates and
                      non-telemetry files dropped
    match(scan)       each GoPro recording synced to the data file it fits
                      (from its motion sensors), grouped into sessions
    apply(sessions)   save the pairings next to the videos and name the
                      sessions after their folders

One camera is the usual case: a session's video is its best-matching
recording. A recording that runs AT THE SAME TIME as that one is a second
camera (Race Overlay's picture-in-picture); recordings one after another
are the same camera stopped and started (in the pits, say), and only the
best of those is used, since a session has one video.

With two cameras, which is the main video: syncing a camera also works out
how it's mounted, so a camera looking back sees the car's forward the
opposite way to one looking forward. Every clip in the event goes in one of
two "facing" groups that way, and names decide which group is the front
("front", "rear", "back"...), else a guess the user can swap for the whole
event at once. A second camera can also be added to a session by hand
(add_second).

What was worked out is kept in the event folder (.raceanalysis/event.json):
each video's sync result, keyed by a fingerprint of the file, and the
choices made in the review (videos picked or lined up by hand, second
cameras, names, sessions left unticked). Opening the folder again can open
straight from that (saved_items), and a re-check only reads the sensors of
videos it hasn't seen, so adding a session's files later is quick.
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
    fp: str = ""                    # fingerprint (see _fingerprint)


@dataclass
class Video:
    parts: list                     # chapter files, in order
    has_sensors: bool = False
    note: str = ""
    fp: str = ""

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
    duration: float = 0.0           # seconds of video
    facing: str = "A"               # "A" or "B": which way it points (see module doc)

    @property
    def span(self) -> tuple[float, float]:
        """Data-file time the recording covers."""
        return self.offset, self.offset + self.duration


@dataclass
class Session:
    data: DataFile
    title: str
    main: Clip | None = None        # the session's video
    second: Clip | None = None      # a second camera running at the same time
    extra: list = field(default_factory=list)    # more recordings that also fit
    use: bool = True


@dataclass
class Scan:
    folder: str
    data: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    skipped: list = field(default_factory=list)   # (path, why)
    results: dict = field(default_factory=dict)   # video fp -> what match() found (cache)
    reused: int = 0                               # videos whose sync came from the cache


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


def scan(folder: str, progress=None, cancelled=None, load=None,
         cache: dict | None = None) -> Scan:
    """Find the data files and videos under folder. progress(text) is
    called as it goes; load(path) -> Telemetry can be swapped for tests.
    cache (load_cache) skips checking videos already seen."""
    known = (cache or {}).get("videos", {})
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
    # of two copies keep the one in a named folder ("Sunday Scored 1") over
    # one in an import's date folder ("2016-01-14"): the name is the session's
    dated = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    video_paths.sort(key=lambda p: (any(dated.match(x) for x in p.parts[:-1]), str(p)))
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
        out.data.append(DataFile(str(p), tel, fp))
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
        v = Video(parts, fp=fp)
        if fp in known:
            v.has_sensors = bool(known[fp].get("has_sensors"))
        else:
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


def match(sc: Scan, progress=None, cancelled=None, read=None,
          cache: dict | None = None) -> list[Session]:
    """Sync every GoPro recording to the data file it fits and build the
    sessions. read(video) -> (times, (N,3) accel) can be swapped for tests.
    cache (load_cache): a video synced before is taken from it as long as
    its data file is still here; one that matched nothing is only tried
    again when there's a data file it wasn't tried against."""
    from . import gosync
    known = (cache or {}).get("videos", {})
    by_fp = {d.fp: d for d in sc.data if d.fp}
    clips = []
    todo = []
    for v in sc.videos:
        if not v.has_sensors:
            sc.results[v.fp] = {"has_sensors": False}
            continue
        k = known.get(v.fp)
        m = (k or {}).get("match")
        if m and m.get("data") in by_fp:
            clips.append(Clip(v, by_fp[m["data"]], float(m["offset"]), float(m["score"]),
                              tuple(m["forward"]), float(m.get("duration", 0.0))))
            sc.results[v.fp] = k
            sc.reused += 1
        elif k and "match" in k and m is None and set(by_fp) <= set(k.get("tried", [])):
            v.note = "didn't match any data file"
            sc.results[v.fp] = k
            sc.reused += 1
        else:
            todo.append(v)
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
        tried = sorted(by_fp)
        if best is None or not best[1].confident:
            v.note = "didn't match any data file"
            sc.results[v.fp] = {"has_sensors": True, "match": None, "tried": tried}
            continue
        d, res = best
        _car, info = gosync.to_car_frame(t, h, d.tel, res.offset)
        fwd = gosync.car_forward(a, info)
        c = Clip(v, d, res.offset, res.score, tuple(float(x) for x in fwd),
                 float(t[-1] - t[0]) if len(t) > 1 else 0.0)
        clips.append(c)
        sc.results[v.fp] = {"has_sensors": True, "tried": tried, "match": {
            "data": d.fp, "offset": c.offset, "score": c.score, "forward": list(c.forward),
            "duration": c.duration}}
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


def overlap(a: Clip, b: Clip) -> float:
    """Share of the shorter recording that runs at the same time as the
    other (0 = one after the other, 1 = fully at the same time)."""
    (a0, a1), (b0, b1) = a.span, b.span
    both = min(a1, b1) - max(a0, b0)
    short = min(a1 - a0, b1 - b0)
    return max(0.0, both) / short if short > 0 else 0.0


SAME_TIME = 0.5     # this much overlap: two cameras, not one stopped and started


def sessions_from(sc: Scan, clips: list[Clip], swap: bool = False) -> list[Session]:
    """Sessions from synced clips. Per data file: the session's video is the
    best-matching clip, from the front-facing group when there's a choice
    (swap: the other group); a clip running at the same time as it, best
    one facing the other way, is the second camera; anything else (the same
    camera stopped and started, copies) is extra."""
    front_group = "B" if swap else "A"
    out = []
    for d in sc.data:
        mine = sorted((c for c in clips if c.data is d), key=lambda c: -c.score)
        s = Session(d, _title(sc, d, mine))
        if mine:
            # main video: the best clip that has a partner running at the same
            # time facing front; with no partner, simply the best clip
            def partners(c):
                return [o for o in mine if o is not c and overlap(c, o) >= SAME_TIME]
            pairs = [c for c in mine if partners(c)]
            if pairs:
                fronts = [c for c in pairs if c.facing == front_group] or pairs
                s.main = fronts[0]
                p = partners(s.main)
                s.second = next((o for o in p if o.facing != s.main.facing), p[0])
            else:
                s.main = mine[0]
            s.extra = [c for c in mine if c is not s.main and c is not s.second]
        out.append(s)
    return out


def swap_all(sessions: list[Session]):
    """Main video <-> second camera for every session that has both."""
    for s in sessions:
        if s.second is not None:
            s.main, s.second = s.second, s.main


def sync_clip(s: Session, parts: list, read=None) -> Clip:
    """Sync a video picked by hand to session s's data file from its motion
    sensors. Raises ValueError (gosync.NoCameraData when it has no GoPro
    sensor track) when it can't; then it can be synced by hand
    (manual_clip)."""
    from . import gosync
    t, a = read(parts) if read else gosync.read_accel(_parts_with_starts(parts))
    h = gosync.camera_horizontal(a)
    res = gosync.find_offset(t, h, s.data.tel)
    if not res.confident:
        raise ValueError(f"its motion doesn't match this session's data well enough "
                         f"({res.score:.0%})")
    _car, info = gosync.to_car_frame(t, h, s.data.tel, res.offset)
    fwd = gosync.car_forward(a, info)
    return Clip(Video(list(parts), True), s.data, res.offset, res.score,
                tuple(float(x) for x in fwd), float(t[-1] - t[0]) if len(t) > 1 else 0.0)


def manual_clip(s: Session, parts: list, offset: float, duration: float = 0.0) -> Clip:
    """A video lined up by hand (score 0: no sensor match behind it)."""
    return Clip(Video(list(parts), False, "synced by hand"), s.data, float(offset), 0.0,
                (0.0, 0.0, 0.0), float(duration))


def set_video(s: Session, clip: Clip):
    """Make clip the session's video; the old one goes to extra."""
    if s.main is not None and s.main is not clip:
        s.extra.insert(0, s.main)
    s.main = clip
    if s.second is clip:
        s.second = None
    s.extra = [c for c in s.extra if c is not clip]


def add_second(s: Session, parts: list, read=None) -> Clip:
    """Sync a video picked by hand to session s's data file and make it the
    second camera. Raises ValueError when its sensors don't match."""
    c = sync_clip(s, parts, read)
    s.second = c
    return c


def apply(sessions: list[Session]) -> list[tuple[str, list, float]]:
    """Save each used session: its video's sidecar points at the data file
    (and holds any second camera as picture-in-picture), and the data
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
        if s.main is None:
            out.append((s.data.path, [], 0.0))
            continue
        fp = s.main.video.parts
        sc = ingest.save_pairing(fp, s.data.path, s.main.offset)
        if sc is not None:
            try:
                data = json.loads(sc.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if s.second is not None:
                # second camera time = main video time + (main offset - its offset)
                data["pip"] = {"parts": [str(Path(p).resolve()) for p in s.second.video.parts],
                               "offset": round(s.main.offset - s.second.offset, 4),
                               "synced": True}
            else:
                data.pop("pip", None)
            try:
                sc.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
        out.append((s.data.path, list(fp), s.main.offset))
    return out


def summary(s: Session) -> str:
    bits = []
    for label, c in (("video", s.main), ("second camera", s.second)):
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




# ---------------------------------------------------------------- saved state
CACHE = Path(".raceanalysis") / "event.json"


def cache_path(folder: str) -> Path:
    return Path(folder) / CACHE


def load_cache(folder: str) -> dict:
    """What was saved last time this event folder was opened ({} if never)."""
    import json
    try:
        d = json.loads(cache_path(folder).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("version") == 1 else {}
    except (OSError, ValueError):
        return {}


def _rel(folder: str, p: str) -> str:
    """Paths inside the event folder are saved relative to it, so the folder
    can move (or be opened from another computer over the network)."""
    try:
        return Path(p).resolve().relative_to(Path(folder).resolve()).as_posix()
    except ValueError:
        return str(Path(p).resolve())


def _abs(folder: str, p: str) -> str:
    q = Path(p)
    return str(q if q.is_absolute() else Path(folder) / q)


def _clip_ref(folder: str, c: Clip | None):
    if c is None:
        return None
    return {"parts": [_rel(folder, p) for p in c.video.parts], "fp": c.video.fp,
            "offset": c.offset, "score": c.score, "forward": list(c.forward),
            "duration": c.duration, "by_hand": c.video.note == "synced by hand"}


def save_cache(folder: str, sc: Scan, sessions: list[Session]):
    """Keep the sync results and the review's choices for next time."""
    import json
    old = load_cache(folder)
    videos = dict(old.get("videos", {}))
    videos.update({k: v for k, v in sc.results.items() if k})
    saved = dict(old.get("sessions", {}))
    for s in sessions:
        saved[s.data.fp or _rel(folder, s.data.path)] = {
            "data": _rel(folder, s.data.path), "title": s.title, "use": s.use,
            "main": _clip_ref(folder, s.main), "second": _clip_ref(folder, s.second)}
    p = cache_path(folder)
    try:
        p.parent.mkdir(exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "videos": videos, "sessions": saved},
                                  indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass                     # a read-only folder: it just won't be quick next time


def _same(c: Clip, ref: dict, folder: str) -> bool:
    if ref.get("fp") and c.video.fp:
        return c.video.fp == ref["fp"]
    return [str(Path(p).resolve()) for p in c.video.parts] == \
        [str(Path(_abs(folder, p)).resolve()) for p in ref["parts"]]


def _take(s: Session, ref: dict | None, folder: str) -> Clip | None:
    """The clip ref describes: the one already in s (taken out of where it
    was), else rebuilt from what was saved if its files are still there."""
    if ref is None:
        return None
    for c in [s.main, s.second] + list(s.extra):
        if c is not None and _same(c, ref, folder):
            if c is s.main:
                s.main = None
            elif c is s.second:
                s.second = None
            else:
                s.extra.remove(c)
            c.offset = float(ref["offset"])        # lined up again by hand, say
            return c
    parts = [_abs(folder, p) for p in ref["parts"]]
    if not all(os.path.exists(p) for p in parts):
        return None
    v = Video(parts, not ref.get("by_hand"), "synced by hand" if ref.get("by_hand") else "",
              ref.get("fp", ""))
    return Clip(v, s.data, float(ref["offset"]), float(ref.get("score", 0.0)),
                tuple(ref.get("forward", (0.0, 0.0, 0.0))), float(ref.get("duration", 0.0)))


def restore(sessions: list[Session], cache: dict, folder: str) -> int:
    """Put back the choices made last time (videos picked or synced by hand,
    second cameras added or removed, names, unticked sessions). Returns how
    many sessions had saved choices."""
    saved = cache.get("sessions", {})
    n = 0
    for s in sessions:
        k = saved.get(s.data.fp) or saved.get(_rel(folder, s.data.path))
        if not k:
            continue
        n += 1
        s.use = bool(k.get("use", True))
        s.title = k.get("title") or s.title
        main = _take(s, k.get("main"), folder) if k.get("main") else None
        second = _take(s, k.get("second"), folder) if k.get("second") else None
        # what the automatic match picked but wasn't chosen goes to extra
        s.extra = [c for c in (s.main, s.second) if c is not None] + s.extra
        s.main, s.second = main, second
        if s.main is None and s.extra:      # its video is gone, or it never had one
            s.main = s.extra.pop(0)
    return n


def saved_items(folder: str) -> list[tuple[str, list, float]]:
    """What the last open of this event folder opened, if every file is
    still there: (data file, video parts, offset), like apply()."""
    out = []
    for k in load_cache(folder).get("sessions", {}).values():
        if not k.get("use", True):
            continue
        data = _abs(folder, k["data"])
        if not os.path.exists(data):
            continue
        m = k.get("main")
        parts = [_abs(folder, p) for p in m["parts"]] if m else []
        if parts and not all(os.path.exists(p) for p in parts):
            parts = []
        out.append((data, parts, float(m["offset"]) if m and parts else 0.0))
    return out
