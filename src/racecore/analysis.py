# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Lap-by-lap comparison across one or more sessions at the same track.

Every lap is laid onto one reference path (the fastest lap's GPS trace), so
"the same point on track" means the same place even when a lap takes a
different line or a session logged a little more distance. From that:

  - time at every metre of the lap, so any lap can be compared with any other
  - delta: how far ahead/behind the reference a lap was at each point
  - sectors (equal thirds of the reference lap) and the theoretical best
  - corners (found on the reference lap) and, per lap, the time gained or
    lost in each corner's stretch of track, minimum speed and braking point

Lap boundaries and lap times come from the logger (racecore.laps), not from
the projection, so lap times always match the logger's.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import grip as gripmod
from .laps import CornerModel, Lap, LapModel
from .telemetry import Telemetry

STEP_M = 2.0              # resolution of the common distance grid
SECTORS = 3
BRAKE_G = 0.3             # longitudinal g that counts as braking
BRAKE_LOOK_M = 400.0      # how far before a corner's apex to look for braking
SAME_TRACK_M = 150.0      # start lines this close = same track


def _xy(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> np.ndarray:
    """Degrees to local metres (east, north) around (lat0, lon0)."""
    return np.column_stack([(lon - lon0) * np.cos(np.radians(lat0)) * 111320.0,
                            (lat - lat0) * 110540.0])


def _fill(x):
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(x)
    if ok.all() or not ok.any():
        return np.nan_to_num(x)
    i = np.arange(len(x))
    return np.interp(i, i[ok], x[ok])


@dataclass
class Session:
    path: str
    tel: Telemetry
    laps: LapModel
    name: str = ""
    video: tuple[list[str], float] | None = None   # (video files, offset)

    @property
    def has_gps(self) -> bool:
        return self.tel.channels.get("gps_lat") is not None and \
            self.tel.channels.get("gps_lon") is not None


@dataclass
class LapData:
    key: tuple[int, int]      # (session index, index into LapModel.laps)
    session: int
    lap: Lap
    t: np.ndarray             # seconds into the lap, per sample
    xy: np.ndarray            # metres, per sample
    v: np.ndarray             # km/h
    lat_g: np.ndarray | None
    lon_g: np.ndarray | None
    d: np.ndarray | None = None          # distance along the reference path
    time_at: np.ndarray | None = None    # seconds into the lap at each grid point
    sectors: list[float] = field(default_factory=list)

    @property
    def time(self) -> float:
        return self.lap.duration

    @property
    def timed(self) -> bool:
        return self.lap.timed

    def label(self, sessions: list[Session]) -> str:
        s = sessions[self.session].name if len(sessions) > 1 else ""
        return f"{s} lap {self.lap.n}" if s else f"Lap {self.lap.n}"


@dataclass
class Corner:
    n: int                    # 1.. in track order on the reference lap
    apex_d: float             # metres along the reference path
    start_d: float            # the stretch of track this corner owns
    end_d: float
    direction: str | None     # "L"/"R"
    name: str = ""            # "T4" from a track profile, else "C4"

    def __post_init__(self):
        self.name = self.name or f"C{self.n}"


@dataclass
class CornerResult:
    corner: Corner
    lost: float               # seconds lost (+) / gained (-) vs reference here
    vmin: float               # km/h, this lap
    vmin_ref: float
    brake_d: float | None     # where braking started, metres along the path
    brake_d_ref: float | None
    best_lost: float          # the fastest any lap went through here vs reference
    grip: "gripmod.TurnGrip | None" = None


class Analysis:
    def __init__(self):
        self.sessions: list[Session] = []
        self.laps: list[LapData] = []
        self.ref: LapData | None = None
        self.grid = np.zeros(0)
        self.length = 0.0
        self.corners: list[Corner] = []
        self._origin: tuple[float, float] | None = None
        self._ref_path: np.ndarray | None = None
        self._ref_s: np.ndarray | None = None
        self.profile = None   # racecore.tracks.TrackProfile with named turns
        self.envelope: gripmod.Envelope | None = None

    def use_profile(self, profile):
        """Take corners from a track profile's turns (None = detect them)."""
        self.profile = profile
        if self.ref is not None:
            self._find_corners()

    @property
    def origin(self) -> tuple[float, float] | None:
        return self._origin

    # ------------------------------------------------------------ sessions
    def add(self, tel: Telemetry, path: str = "", name: str = "",
            video: tuple[list[str], float] | None = None) -> Session:
        lm = LapModel(tel)
        if not lm.ok or not any(l.timed for l in lm.laps):
            raise ValueError("no complete laps in this file")
        s = Session(path, tel, lm, name or Path(path).stem or f"Session {len(self.sessions) + 1}",
                    video)
        if not s.has_gps:
            raise ValueError("this file has no GPS position, which lap comparison needs")
        if self.sessions and not self._same_track(s):
            raise ValueError("this file is from a different track (start line "
                             f"more than {SAME_TRACK_M:.0f} m away)")
        self.sessions.append(s)
        self.build()
        return s

    def remove(self, i: int):
        del self.sessions[i]
        self.build()

    def _start_xy(self, s: Session) -> np.ndarray:
        lat = _fill(s.tel.channels["gps_lat"])
        lon = _fill(s.tel.channels["gps_lon"])
        lap = next(l for l in s.laps.laps if l.timed)
        if self._origin is None:
            self._origin = (float(lat[lap.i0]), float(lon[lap.i0]))
        return _xy(lat[lap.i0:lap.i0 + 1], lon[lap.i0:lap.i0 + 1], *self._origin)[0]

    def _same_track(self, s: Session) -> bool:
        a = self._start_xy(self.sessions[0])
        b = self._start_xy(s)
        return float(np.hypot(*(a - b))) <= SAME_TRACK_M

    # --------------------------------------------------------------- build
    def build(self):
        self.laps, self.ref, self.corners = [], None, []
        if not self.sessions:
            return
        self._origin = None
        self._start_xy(self.sessions[0])
        for si, s in enumerate(self.sessions):
            ch = s.tel.channels
            lat, lon = _fill(ch["gps_lat"]), _fill(ch["gps_lon"])
            xy = _xy(lat, lon, *self._origin)
            v = _fill(ch["speed"])
            lg = _fill(ch["lat_g"]) if ch.get("lat_g") is not None else None
            ng = _fill(ch["lon_g"]) if ch.get("lon_g") is not None else None
            t = s.tel.time
            for li, lap in enumerate(s.laps.laps):
                if lap.kind not in ("flying", "slow"):
                    continue
                sl = slice(lap.i0, min(lap.i1 + 1, len(t)))  # include the crossing
                self.laps.append(LapData(
                    (si, li), si, lap, t[sl] - lap.start, xy[sl], v[sl],
                    lg[sl] if lg is not None else None,
                    ng[sl] if ng is not None else None))
        timed = [ld for ld in self.laps if ld.timed]
        if not timed:
            return
        self._build_envelope(timed)
        self.set_reference(min(timed, key=lambda ld: ld.time))

    def _build_envelope(self, laps):
        """Grip envelope from every timed lap of every session."""
        self.envelope = None
        pairs = [(ld.lat_g, ld.lon_g, ld.v) for ld in laps
                 if ld.lat_g is not None and ld.lon_g is not None]
        if not pairs:
            return
        try:
            self.envelope = gripmod.build_envelope(
                np.concatenate([p[0] for p in pairs]), np.concatenate([p[1] for p in pairs]),
                np.concatenate([p[2] for p in pairs]))
        except ValueError:
            self.envelope = None

    def set_reference(self, ref: LapData):
        self.ref = ref
        p = ref.xy
        seg = np.hypot(*np.diff(p, axis=0).T)
        self._ref_path = p
        self._ref_s = np.concatenate([[0.0], np.cumsum(seg)])
        self.length = float(self._ref_s[-1])
        self.grid = np.arange(0.0, self.length, STEP_M)
        for ld in self.laps:
            ld.d = self._distance(ld)
            ld.time_at = self._time_on_grid(ld)
            ld.sectors = self._sectors(ld)
        self._find_corners()

    def _distance(self, ld: LapData) -> np.ndarray:
        """Distance along the reference for every sample of a lap.

        Loggers often repeat the last GPS fix when an update is missed (about
        7% of samples in an APEX Pro log). Taken at face value the car would
        stand still for that sample and the delta would spike, so repeated
        positions are skipped and filled in by time from their neighbours.
        """
        xy = ld.xy
        fresh = np.ones(len(xy), bool)
        fresh[1:] = np.any(np.diff(xy, axis=0) != 0, axis=1)
        d_fresh = self._project(xy[fresh])
        d = np.interp(ld.t, ld.t[fresh], d_fresh)
        d = np.maximum.accumulate(d)
        d[0] = 0.0
        return d

    def _project(self, q: np.ndarray) -> np.ndarray:
        """Distance along the reference path for each point of a lap,
        searched forward from the previous point so it never jumps across
        the track to a nearby straight."""
        P, S = self._ref_path, self._ref_s
        n = len(P)
        a, b = P[:-1], P[1:]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-9)
        out = np.empty(len(q))
        k = 0
        first = slice(0, max(2, n // 10))  # a lap starts near the line
        for i, pt in enumerate(q):
            lo = 0 if i == 0 else max(0, k - 20)
            hi = first.stop if i == 0 else min(n - 1, k + 200)
            aa, dd = a[lo:hi], ab[lo:hi]
            u = np.clip(((pt - aa) * dd).sum(1) / L2[lo:hi], 0.0, 1.0)
            proj = aa + dd * u[:, None]
            j = int(np.argmin(((proj - pt) ** 2).sum(1)))
            k = lo + j
            out[i] = S[k] + u[j] * (S[k + 1] - S[k])
        out = np.maximum.accumulate(out)
        out[0] = 0.0
        return out

    def _time_on_grid(self, ld: LapData) -> np.ndarray:
        d = ld.d + np.arange(len(ld.d)) * 1e-6  # strictly increasing for interp
        tg = np.interp(self.grid, d, ld.t)
        return tg

    def time_at(self, ld: LapData, dist: float) -> float:
        """Seconds into the lap when it reached `dist` along the reference."""
        if dist >= self.length:
            return ld.time
        return float(np.interp(dist, self.grid, ld.time_at))

    def dist_at(self, ld: LapData, t_into: float) -> float:
        return float(np.interp(t_into, ld.t, ld.d))

    def _sectors(self, ld: LapData) -> list[float]:
        b = [self.length * k / SECTORS for k in range(SECTORS + 1)]
        ts = [0.0] + [self.time_at(ld, x) for x in b[1:-1]] + [ld.time]
        return [ts[k + 1] - ts[k] for k in range(SECTORS)]

    # ------------------------------------------------------------ results
    def delta(self, ld: LapData) -> np.ndarray:
        """Seconds behind (+) / ahead (-) of the reference at each grid point."""
        return ld.time_at - self.ref.time_at

    def channel_on_grid(self, ld: LapData, name: str) -> np.ndarray | None:
        x = {"speed": ld.v, "lat_g": ld.lat_g, "lon_g": ld.lon_g}.get(name)
        if x is None:
            return None
        d = ld.d + np.arange(len(ld.d)) * 1e-6
        return np.interp(self.grid, d, x)

    def dt_on_grid(self, ld: LapData) -> np.ndarray:
        """Seconds the lap spent on each grid step."""
        return np.gradient(ld.time_at)

    def grip_on_grid(self, ld: LapData) -> np.ndarray | None:
        """Fraction of the envelope used at each grid point, NaN where the
        car wasn't cornering or braking (straights, pure acceleration)."""
        if self.envelope is None:
            return None
        lat, lon = self.channel_on_grid(ld, "lat_g"), self.channel_on_grid(ld, "lon_g")
        if lat is None or lon is None:
            return None
        u = self.envelope.used(lat, lon)
        u[~gripmod._working(lat, lon)] = np.nan
        return u

    def lap_grip(self, ld: LapData) -> float | None:
        """Time-weighted grip used over the whole lap while working."""
        if self.envelope is None or ld.lat_g is None:
            return None
        lat, lon = self.channel_on_grid(ld, "lat_g"), self.channel_on_grid(ld, "lon_g")
        return gripmod.stretch_grip(self.envelope, lat, lon, self.dt_on_grid(ld))[0]

    def _turn_grip(self, ld: LapData, i0: int, i1: int):
        lat, lon = self.channel_on_grid(ld, "lat_g"), self.channel_on_grid(ld, "lon_g")
        sl = slice(i0, i1 + 1)
        return gripmod.stretch_grip(self.envelope, lat[sl], lon[sl], self.dt_on_grid(ld)[sl])

    def timed_laps(self) -> list[LapData]:
        return [ld for ld in self.laps if ld.timed]

    def best_sectors(self) -> list[tuple[float, LapData]]:
        out = []
        for k in range(SECTORS):
            ld = min(self.timed_laps(), key=lambda x: x.sectors[k])
            out.append((ld.sectors[k], ld))
        return out

    def theoretical_best(self) -> float:
        return float(sum(t for t, _ in self.best_sectors()))

    # ------------------------------------------------------------ corners
    def _find_corners(self):
        if self.profile is not None and self.profile.turns:
            self._corners_from_profile()
            return
        ref = self.ref
        s = self.sessions[ref.session]
        cm = CornerModel(s.tel, s.laps)
        li = ref.key[1]
        apexes = []
        for p in cm.passes:
            if p.lap != li:
                continue
            k = p.i_apex - ref.lap.i0
            if 0 <= k < len(ref.d):
                apexes.append((float(ref.d[k]), p.gdir))
        apexes.sort()
        self.corners = []
        for n, (ad, gdir) in enumerate(apexes, 1):
            prev = apexes[n - 2][0] if n > 1 else 0.0
            nxt = apexes[n][0] if n < len(apexes) else self.length
            start = 0.0 if n == 1 else (prev + ad) / 2
            end = self.length if n == len(apexes) else (ad + nxt) / 2
            self.corners.append(Corner(n, ad, start, end, gdir))

    def _path_distance(self, pt) -> float:
        """Distance along the reference path of the point on it nearest pt."""
        P, S = self._ref_path, self._ref_s
        a, ab = P[:-1], np.diff(P, axis=0)
        L2 = np.maximum((ab * ab).sum(1), 1e-9)
        u = np.clip(((pt - a) * ab).sum(1) / L2, 0.0, 1.0)
        proj = a + ab * u[:, None]
        j = int(np.argmin(((proj - pt) ** 2).sum(1)))
        return float(S[j] + u[j] * (S[j + 1] - S[j]))

    def _corners_from_profile(self):
        p = self.profile
        xy = p.turns_xy(*self._origin)
        lat = self.channel_on_grid(self.ref, "lat_g")
        found = []
        for t, pt in zip(p.turns, xy):
            d = self._path_distance(pt)
            gdir = None
            if lat is not None:
                near = np.abs(self.grid - d) <= 25.0
                g = float(lat[near].mean()) if near.any() else 0.0
                gdir = "R" if g > 0.15 else ("L" if g < -0.15 else None)
            found.append((d, t.name, gdir))
        found.sort()
        self.corners = []
        for n, (ad, name, gdir) in enumerate(found, 1):
            prev = found[n - 2][0] if n > 1 else 0.0
            nxt = found[n][0] if n < len(found) else self.length
            start = 0.0 if n == 1 else (prev + ad) / 2
            end = self.length if n == len(found) else (ad + nxt) / 2
            self.corners.append(Corner(n, ad, start, end, gdir, name))

    def _brake_point(self, ld: LapData, c: Corner) -> float | None:
        if ld.lon_g is None:
            return None
        g = self.channel_on_grid(ld, "lon_g")
        i_apex = int(np.searchsorted(self.grid, c.apex_d))
        i_from = int(np.searchsorted(self.grid, max(c.start_d, c.apex_d - BRAKE_LOOK_M)))
        seg = g[i_from:i_apex + 1]
        hard = np.flatnonzero(seg <= -BRAKE_G)
        if hard.size == 0:
            return None
        return float(self.grid[i_from + hard[0]])

    def corner_results(self, ld: LapData) -> list[CornerResult]:
        if not self.corners:
            return []
        dl = self.delta(ld)
        v, vr = self.channel_on_grid(ld, "speed"), self.channel_on_grid(self.ref, "speed")
        timed = self.timed_laps()
        out = []
        for c in self.corners:
            i0 = int(np.searchsorted(self.grid, c.start_d))
            i1 = min(len(self.grid) - 1, int(np.searchsorted(self.grid, c.end_d)))
            lost = float(dl[i1] - dl[i0])
            near = slice(max(i0, int(np.searchsorted(self.grid, c.apex_d - 120))),
                         min(i1 + 1, int(np.searchsorted(self.grid, c.apex_d + 120)) + 1))
            best = min(float((self.delta(x)[i1] - self.delta(x)[i0])) for x in timed)
            g = None
            if self.envelope is not None and ld.lat_g is not None and self.ref.lat_g is not None:
                u, pk, soft = self._turn_grip(ld, i0, i1)
                ur, pkr, softr = self._turn_grip(self.ref, i0, i1)
                g = gripmod.TurnGrip(u, ur, pk, pkr, soft, softr)
            out.append(CornerResult(c, lost, float(v[near].min()), float(vr[near].min()),
                                    self._brake_point(ld, c), self._brake_point(self.ref, c),
                                    best, g))
        return out


# ------------------------------------------------------------- video lookup

def find_video(csv: str, folders: list[str]) -> tuple[list[str], float] | None:
    """A synced video for this data file, from RaceOverlay's sidecars
    (<video>.sync.json holding "csv", "parts", "offset", "synced") in the
    given folders and their day folders one level down."""
    want = str(Path(csv).resolve())
    seen = set()
    for f in folders:
        base = Path(f)
        if not base.is_dir():
            continue
        cands = list(base.glob("*.sync.json")) + list(base.glob("*/*.sync.json"))
        for sc in cands:
            if sc in seen:
                continue
            seen.add(sc)
            try:
                data = json.loads(sc.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            c = data.get("csv")
            if not c or str(Path(c).resolve()) != want or not data.get("synced", True):
                continue
            video = str(sc)[:-len(".sync.json")]
            parts = data.get("parts") or [video]
            if all(Path(p).exists() for p in parts):
                return list(parts), float(data.get("offset", 0.0))
    return None
