"""Lap timing, distance-matched delta, and corner detection.

Laps come from the logger's lap channel. The first lap (logger power-up to
the first line crossing) is the out-lap and the last one is the in-lap (or a
lap cut short when logging stopped); neither is timed. A flying lap more
than 1.5x the median is treated as a slow/pit lap and isn't timed either.

Delta compares where you are now with where the reference lap was at the
same point *around the track* (fraction of lap distance, from integrated
speed), not at the same elapsed time.

Corners are found once on the session-best lap (speed dips with enough
drop, plus long high-g sweepers that don't dip) and then measured on every
lap at the same track positions, so T4 is T4 on every lap.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .telemetry import Telemetry

SLOW_LAP_FACTOR = 1.5
CORNER_MIN_DROP_KMH = 8.0      # speed must dip this much to count as a corner
SWEEPER_G = 0.8                # sustained lateral g that counts as a corner
SWEEPER_MIN_S = 1.0
EXIT_G = 0.35                  # corner ends when lateral g falls below this


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or x.size < n:
        return x
    k = np.ones(n) / n
    pad = n // 2
    xp = np.pad(x, (pad, n - 1 - pad), mode="edge")
    return np.convolve(xp, k, mode="valid")


def _fill_nan(t: np.ndarray, x: np.ndarray) -> np.ndarray:
    ok = np.isfinite(x)
    if ok.all() or not ok.any():
        return np.nan_to_num(x)
    return np.interp(t, t[ok], x[ok])


@dataclass
class Lap:
    n: int
    i0: int            # first sample index
    i1: int            # one past the last sample (= next lap's i0)
    start: float
    end: float
    kind: str          # out | flying | slow | in
    length: float      # metres (integrated speed)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def timed(self) -> bool:
        return self.kind == "flying"


class LapModel:
    def __init__(self, tel: Telemetry):
        self.laps: list[Lap] = []
        self.time = tel.time
        lap = tel.channels.get("lap")
        spd = tel.channels.get("speed")
        single = tel.meta.get("single_lap")
        if spd is None or len(self.time) < 10 or (lap is None and not single):
            return
        t = self.time
        self.v = _fill_nan(t, spd)                       # km/h
        dt = np.diff(t, prepend=t[0])
        self.dist = np.cumsum(self.v / 3.6 * dt)         # metres
        lapn = np.round(_fill_nan(t, lap)).astype(int) if lap is not None \
            else np.zeros(len(t), int)
        change = np.flatnonzero(np.diff(lapn)) + 1
        starts = np.concatenate([[0], change])
        ends = np.concatenate([change, [len(t)]])
        if len(starts) < 2:
            if not single:
                return  # never crossed the line
            # One-lap export (e.g. AiM "fastest lap"): the whole file is it.
            n, dur = single
            self.laps.append(Lap(int(n), 0, len(t), float(t[0]), float(t[-1]),
                                 "flying", float(self.dist[-1] - self.dist[0])))
            self._finish(t)
            return
        for k, (i0, i1) in enumerate(zip(starts, ends)):
            end_t = t[i1] if i1 < len(t) else t[-1]
            end_d = self.dist[i1] if i1 < len(t) else self.dist[-1]
            kind = "out" if k == 0 else ("in" if k == len(starts) - 1 else "flying")
            self.laps.append(Lap(int(lapn[i0]), int(i0), int(i1), float(t[i0]),
                                 float(end_t), kind, float(end_d - self.dist[i0])))
        self._finish(t)

    def _finish(self, t):
        flying = [l.duration for l in self.laps if l.kind == "flying"]
        if flying:
            med = float(np.median(flying))
            for l in self.laps:
                if l.kind == "flying" and l.duration > SLOW_LAP_FACTOR * med:
                    l.kind = "slow"
        timed = [l for l in self.laps if l.timed]
        self.ref_len = float(np.median([l.length for l in timed])) if timed else \
            float(max(l.length for l in self.laps)) or 1.0
        # Fraction around the lap for every sample. The out-lap is aligned
        # from its end (it starts in the pits), everything else from its start.
        self.frac = np.full(len(t), np.nan)
        for l in self.laps:
            d = self.dist[l.i0:l.i1] - self.dist[l.i0]
            if l.kind == "out":
                end_d = self.dist[l.i1] if l.i1 < len(t) else self.dist[-1]
                self.frac[l.i0:l.i1] = 1.0 - (end_d - self.dist[l.i0:l.i1]) / self.ref_len
            elif l.timed or l.kind == "slow":
                self.frac[l.i0:l.i1] = d / max(l.length, 1.0)
            else:
                self.frac[l.i0:l.i1] = d / self.ref_len
        self._starts = np.array([l.start for l in self.laps])

    @property
    def ok(self) -> bool:
        return len(self.laps) >= 1

    def index_at(self, t: float) -> int:
        return int(np.clip(np.searchsorted(self._starts, t, side="right") - 1,
                           0, len(self.laps) - 1))

    def lap_at(self, t: float) -> Lap | None:
        if not self.laps or t < self.laps[0].start or t > self.laps[-1].end:
            return None
        return self.laps[self.index_at(t)]

    def best_before(self, t: float) -> Lap | None:
        done = [l for l in self.laps if l.timed and l.end <= t + 1e-6]
        return min(done, key=lambda l: l.duration) if done else None

    def session_best(self) -> Lap | None:
        timed = [l for l in self.laps if l.timed]
        return min(timed, key=lambda l: l.duration) if timed else None

    def reference(self, t: float, mode: str) -> Lap | None:
        return self.session_best() if mode == "session" else self.best_before(t)

    def frac_at(self, t: float, lap: Lap) -> float:
        tt = self.time[lap.i0:lap.i1]
        ff = self.frac[lap.i0:lap.i1]
        return float(np.interp(t, tt, ff))

    def time_at_frac(self, lap: Lap, f: float) -> float:
        """Elapsed time into `lap` when it reached fraction f."""
        ff = self.frac[lap.i0:lap.i1]
        tt = self.time[lap.i0:lap.i1] - lap.start
        return float(np.interp(f, ff, tt))

    def delta(self, t: float, lap: Lap, ref: Lap | None) -> float | None:
        if ref is None or not lap.timed or ref is lap and False:
            return None
        f = self.frac_at(t, lap)
        return (t - lap.start) - self.time_at_frac(ref, f)


# ------------------------------------------------------------------ corners --
#
# Corners are found on every lap directly from the speed trace with a
# peak/valley detector: speed falls at least DROP below its running peak
# (you braked or lifted), then rises at least RISE above the low point (you're
# back on it). Entry = the peak where slowing began, apex = the low point,
# the corner ends once lateral g settles. Nothing is mapped from another lap,
# so a different line or braking point can't throw it off.

DROP_KMH = 8.0
RISE_KMH = 5.0
MIN_ENTRY_KMH = 40.0   # below this it's pit lane / paddock, not a corner
MIN_PEAK_G = 0.35


@dataclass
class CornerPass:
    i_entry: int
    i_apex: int
    i_exit: int
    t_entry: float
    t_exit: float
    entry: float          # km/h
    vmin: float           # km/h
    gpeak: float | None
    gdir: str | None      # "L"/"R": which way the car was turning at peak g
    lap: int | None       # index into LapModel.laps
    frac: float | None    # apex position around the lap


class CornerModel:
    def __init__(self, tel: Telemetry, laps: LapModel):
        self.laps = laps
        self.passes: list[CornerPass] = []
        t = tel.time
        spd = tel.channels.get("speed")
        if spd is None or len(t) < 20:
            self.time = t
            return
        self.time = t
        self.v = _fill_nan(t, spd)
        lat = tel.channels.get("lat_g")
        lat_s = _fill_nan(t, lat) if lat is not None else None
        self.lat = np.abs(lat_s) if lat_s is not None else None
        self.lat_signed = lat_s  # + = right-hand turn (checked against GPS)
        dt = float(np.median(np.diff(t))) or 0.1
        vs = _smooth(self.v, max(1, int(round(0.6 / dt))))
        lat_s = _smooth(self.lat, max(1, int(round(0.4 / dt)))) \
            if self.lat is not None else None
        lat_ss = _smooth(self.lat_signed, max(1, int(round(0.4 / dt)))) \
            if self.lat_signed is not None else None

        events = []  # (entry, apex, rise)
        pk, vl = 0, None
        for i in range(1, len(vs)):
            if vl is None:
                if vs[i] >= vs[pk]:
                    pk = i
                elif vs[pk] - vs[i] >= DROP_KMH:
                    vl = i
            else:
                if vs[i] <= vs[vl]:
                    vl = i
                elif vs[i] - vs[vl] >= RISE_KMH:
                    events.append((pk, vl, i))
                    pk, vl = i, None
        for j, (e, a, r) in enumerate(events):
            nxt = events[j + 1][0] if j + 1 < len(events) else len(t) - 1
            x = r
            if lat_s is not None:  # stay "in the corner" while still turning
                lim = min(nxt, r + int(round(3.0 / dt)))
                while x < lim and lat_s[x] >= 0.4:
                    x += 1
            for (s0, s1, first) in self._split_by_direction(e, x, lat_ss, dt, vs):
                if first:
                    ie = s0 + int(np.argmax(self.v[s0:a + 1])) if s0 <= a <= s1 \
                        else s0 + int(np.argmax(self.v[s0:s1 + 1]))
                else:
                    ie = s0  # second half of a combo: entry = speed at the switch
                ia = s0 + int(np.argmin(self.v[s0:s1 + 1]))
                entry = float(self.v[ie])
                vmin = float(self.v[ie:s1 + 1].min())
                gp = gdir = None
                if self.lat is not None:
                    ip = ie + int(np.argmax(self.lat[ie:s1 + 1]))
                    gp = float(self.lat[ip])
                    gdir = "R" if self.lat_signed[ip] > 0 else "L"
                if entry < MIN_ENTRY_KMH or (gp is not None and gp < MIN_PEAK_G):
                    continue
                li = fr = None
                if laps.ok and laps.laps[0].start <= t[ia] <= laps.laps[-1].end:
                    li = laps.index_at(float(t[ia]))
                    fr = float(laps.frac[ia]) if np.isfinite(laps.frac[ia]) else None
                self.passes.append(CornerPass(ie, ia, s1, float(t[ie]), float(t[s1]),
                                              entry, vmin, gp, gdir, li, fr))
        self._entries = np.array([p.t_entry for p in self.passes])
        self._exits = np.array([p.t_exit for p in self.passes])

    @staticmethod
    def _split_by_direction(e: int, x: int, lat_ss, dt: float, vs=None):
        """Split [e, x] where the car changes turning direction AND gets back
        on the gas in between (separate corners). A direction change under one
        continuous slowdown (VIR's Roller Coaster, a left-right flick) stays
        one corner. Yields (start, end, is_first)."""
        if lat_ss is None or x - e < 3:
            yield e, x, True
            return
        seg = lat_ss[e:x + 1]
        min_len = max(2, int(round(0.6 / dt)))
        runs, cur, start = [], 0, 0
        for i, val in enumerate(seg):
            sg = 1 if val > 0.25 else (-1 if val < -0.25 else 0)
            if sg == 0 or sg == cur or cur == 0:
                if cur == 0 and sg != 0:
                    cur = sg
                continue
            runs.append((start, i - 1, cur))
            start, cur = i, sg
        runs.append((start, len(seg) - 1, cur))
        # keep only real turns: long enough and loaded up
        strong = [r for r in runs if r[1] - r[0] + 1 >= min_len
                  and np.max(np.abs(seg[r[0]:r[1] + 1])) >= 0.5]
        if len(strong) <= 1:
            yield e, x, True
            return
        bounds = [e] + [e + r[0] for r in strong[1:]] + [x + 1]
        if vs is not None:
            # Keep a split only if speed recovers between the two parts.
            keep = [bounds[0]]
            for k in range(1, len(bounds) - 1):
                a0, b, b1 = keep[-1], bounds[k], bounds[k + 1]
                low_before = float(vs[a0:b].min())
                nxt_min = b + int(np.argmin(vs[b:b1]))
                rise = float(vs[b:nxt_min + 1].max()) - low_before
                if rise >= RISE_KMH:
                    keep.append(b)
            bounds = keep + [x + 1]
            if len(bounds) <= 2:
                yield e, x, True
                return
        for k in range(len(bounds) - 1):
            yield bounds[k], bounds[k + 1] - 1, k == 0

    @property
    def ok(self) -> bool:
        return bool(self.passes)

    def matching(self, p: CornerPass, ref: Lap | None) -> CornerPass | None:
        """The same corner on the reference lap (nearest apex position)."""
        if ref is None or p.frac is None or not self.laps.ok:
            return None
        li = self.laps.laps.index(ref)
        if p.lap == li:
            return None
        cands = [q for q in self.passes if q.lap == li and q.frac is not None]
        if not cands:
            return None
        q = min(cands, key=lambda q: abs(q.frac - p.frac))
        return q if abs(q.frac - p.frac) <= 0.015 else None

    def state(self, t: float, live: bool):
        """(pass, entry, vmin, gpeak, gdir, in_progress) for time t, or None
        before the first corner. Between corners the last one stays up."""
        if not self.passes:
            return None
        key = self._entries if live else self._exits
        j = int(np.searchsorted(key, t, side="right")) - 1
        if j < 0:
            return None
        p = self.passes[j]
        if live and t < p.t_exit:
            k = int(np.searchsorted(self.time, t, side="right"))
            k = max(p.i_entry + 1, min(k, p.i_exit + 1))
            vmin = float(self.v[p.i_entry:k].min())
            gp = gdir = None
            if self.lat is not None:
                ip = p.i_entry + int(np.argmax(self.lat[p.i_entry:k]))
                gp = float(self.lat[ip])
                gdir = "R" if self.lat_signed[ip] > 0 else "L"
            return p, p.entry, vmin, gp, gdir, True
        return p, p.entry, p.vmin, p.gpeak, p.gdir, False
