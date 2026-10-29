# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Engine data: whatever OBD (or CAN) channels a log has, gears worked out
from RPM and speed, and shift points.

Channels aren't fixed: any column that isn't GPS, the accelerometer, time,
distance or lap bookkeeping, and that actually changes, counts as an engine
channel. Known names get a kind (rpm, throttle, map, coolant, ...) so they
sort first and get sensible defaults; anything else still shows up.

OBD values update slower than the logger writes rows, so a column repeats
each value until the next reading arrives. `smooth_holds` turns those steps
back into a line through the real readings (keeping genuine plateaus, like
full throttle down a straight).

Gears: in gear with the clutch in, RPM / speed is fixed by the gearing, so a
histogram of that ratio over a session has one peak per gear. Samples near a
peak get that gear; the rest (shifting, clutch in, wheelspin) get 0.
Nothing here imports Qt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

# columns that are never engine data (matched against the lower-case name)
NOT_ENGINE = [r"^time\b", r"^t$", r"timestamp", r"elapsed", r"distance", r"^lap\b",
              r"lap\s*#", r"lap.?(num|count|time)", r"apex.?score", r"\bscore\b",
              r"^a[xyz]\b", r"^g[xyz]\b", r"acc", r"\bg\b.*force", r"^lat", r"^lon",
              r"^lng", r"latitude", r"longitude", r"height", r"altitude", r"elev",
              r"\bspeed\b(?!.*(engine|wheel))", r"velocity", r"heading", r"course",
              r"bearing", r"fix", r"sat", r"hdop", r"pdop", r"accuracy", r"utc",
              r"date", r"^sector", r"yaw", r"pitch", r"roll", r"gyro"]

# kind -> (name patterns, label); order = display order
KINDS: list[tuple[str, list[str], str]] = [
    ("rpm", [r"\brpm\b", r"engine.?speed", r"\brevs?\b"], "RPM"),
    ("throttle", [r"\btps\b", r"throttle", r"pedal", r"accel.?pos"], "Throttle"),
    ("map", [r"\bmap\b", r"manifold", r"boost", r"\bmabs\b"], "Manifold pressure"),
    ("coolant", [r"coolant", r"\bect\b", r"water.?temp", r"\bclt\b", r"engine.?temp"],
     "Coolant"),
    ("oil_temp", [r"oil.?temp", r"\beot\b"], "Oil temp"),
    ("oil_press", [r"oil.?pres", r"\beop\b"], "Oil pressure"),
    ("intake", [r"\biat\b", r"intake.?(air)?.?temp", r"air.?temp", r"\bact\b"],
     "Intake air temp"),
    ("lambda", [r"lambda", r"\bafr\b", r"o2", r"air.?fuel"], "Lambda / AFR"),
    ("load", [r"\bload\b"], "Engine load"),
    ("timing", [r"timing", r"ignition", r"spark.?adv"], "Ignition timing"),
    ("fuel", [r"fuel"], "Fuel"),
    ("battery", [r"batt", r"volt"], "Battery"),
    ("brake", [r"brake"], "Brake"),
    ("steer", [r"steer"], "Steering"),
]
KIND_ORDER = {k: i for i, (k, _p, _l) in enumerate(KINDS)}
TEMPS = ("coolant", "oil_temp", "intake")

MIN_SPEED_KMH = 25.0       # below this the gear estimate isn't trusted
MIN_RPM = 1200.0
GEAR_TOL = 0.07            # within 7% of a gear's RPM/speed ratio
MIN_RUN_S = 0.3            # shorter stretches in one gear are shifting


@dataclass
class Channel:
    col: str          # column name in the file
    kind: str         # one of KINDS, or "other"
    label: str        # what to call it
    unit: str
    rate_hz: float    # how often the value actually updates


def _kind(name: str) -> tuple[str, str]:
    low = name.lower()
    for kind, pats, label in KINDS:
        if any(re.search(p, low) for p in pats):
            return kind, label
    return "other", re.sub(r"\s*\(.*\)\s*$", "", name).strip() or name


def _is_engine(name: str) -> bool:
    low = name.lower().strip()
    if any(re.search(p, low) for k, pats, _l in KINDS if k in ("rpm", "throttle", "map",
                                                               "coolant") for p in pats):
        return True     # e.g. "Engine speed (rpm)" isn't GPS speed
    return not any(re.search(p, low) for p in NOT_ENGINE)


def update_rate(time: np.ndarray, v: np.ndarray) -> float:
    """How often v really changes (Hz), from the spacing of its changes."""
    ok = np.isfinite(v)
    t, v = time[ok], v[ok]
    if len(v) < 3:
        return 0.0
    ch = np.flatnonzero(np.diff(v) != 0) + 1
    if len(ch) < 3:
        return 0.0
    gaps = np.diff(t[ch])
    gaps = gaps[gaps > 0]
    return float(1.0 / np.median(gaps)) if len(gaps) else 0.0


def channels(tel) -> list[Channel]:
    """Engine channels in this log that have real data, known kinds first."""
    out = []
    for col in tel.source_columns:
        v = tel.raw.get(col)
        if v is None or not _is_engine(col):
            continue
        f = v[np.isfinite(v)]
        if len(f) < 10 or np.nanstd(f) == 0 or np.mean(f != 0) < 0.05:
            continue          # empty, constant or all zeros (no dongle)
        kind, label = _kind(col)
        unit = tel.units.get(col, "") or ""
        out.append(Channel(col, kind, label, unit, update_rate(tel.time, v)))
    out.sort(key=lambda c: (KIND_ORDER.get(c.kind, 99), c.col))
    return out


def smooth_holds(time: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Replace repeated (held) readings with a line through the real ones.
    Each run of equal values keeps its first and last sample, so a value
    that really stayed put (full throttle) stays flat and only the step to
    the next reading is drawn as a ramp."""
    v = np.asarray(v, float)
    ok = np.isfinite(v)
    if ok.sum() < 2:
        return v.copy()
    idx = np.flatnonzero(ok)
    vv = v[idx]
    change = np.flatnonzero(np.diff(vv) != 0)
    keep = np.unique(np.concatenate([[0, len(vv) - 1], change, change + 1]))
    k = idx[keep]
    return np.interp(np.arange(len(v)), k, v[k])


# ------------------------------------------------------------------ gears ---
@dataclass
class Gearing:
    ratios: list[float] = field(default_factory=list)   # RPM per km/h, 1st gear first

    def __bool__(self):
        return bool(self.ratios)

    def gear(self, rpm, speed_kmh) -> np.ndarray:
        """Gear per sample (1 = first), 0 when it can't tell (shifting, clutch
        in, too slow)."""
        rpm, spd = np.asarray(rpm, float), np.asarray(speed_kmh, float)
        g = np.zeros(len(rpm), int)
        if not self.ratios:
            return g
        ok = (spd > MIN_SPEED_KMH) & (rpm > MIN_RPM) & np.isfinite(rpm) & np.isfinite(spd)
        r = np.full(len(rpm), np.nan)
        r[ok] = rpm[ok] / spd[ok]
        cen = np.array(self.ratios)
        with np.errstate(invalid="ignore"):
            err = np.abs(r[:, None] / cen[None, :] - 1.0)
        best = np.nanargmin(np.where(np.isfinite(err), err, 9.0), axis=1)
        good = ok & (err[np.arange(len(rpm)), best] <= GEAR_TOL)
        g[good] = best[good] + 1
        return g


def find_gearing(rpm, speed_kmh, max_gears: int = 7) -> Gearing:
    """Gear ratios (RPM per km/h) from the peaks of the RPM/speed histogram."""
    rpm, spd = np.asarray(rpm, float), np.asarray(speed_kmh, float)
    ok = (spd > MIN_SPEED_KMH) & (rpm > MIN_RPM) & np.isfinite(rpm) & np.isfinite(spd)
    if ok.sum() < 200:
        return Gearing()
    lr = np.log(rpm[ok] / spd[ok])
    lo, hi = np.percentile(lr, [0.5, 99.5])
    if hi - lo < 0.05:
        return Gearing([float(np.exp(np.median(lr)))])
    bins = np.linspace(lo - 0.05, hi + 0.05, 400)
    h, e = np.histogram(lr, bins)
    c = (e[:-1] + e[1:]) / 2
    k = np.exp(-0.5 * (np.arange(-6, 7) / 2.0) ** 2)
    hs = np.convolve(h, k / k.sum(), "same")
    peaks = [i for i in range(1, len(hs) - 1)
             if hs[i] >= hs[i - 1] and hs[i] > hs[i + 1] and hs[i] > 0.04 * hs.max()]
    # merge peaks closer than the gear tolerance, keep the taller
    peaks.sort(key=lambda i: -hs[i])
    chosen: list[int] = []
    for i in peaks:
        if all(abs(c[i] - c[j]) > 2 * GEAR_TOL for j in chosen):
            chosen.append(i)
        if len(chosen) >= max_gears:
            break
    ratios = sorted((float(np.exp(c[i])) for i in chosen), reverse=True)
    return Gearing(ratios)


def clean_runs(gear: np.ndarray, time: np.ndarray, min_s: float = MIN_RUN_S) -> np.ndarray:
    """Drop stretches in one gear shorter than min_s (mid-shift noise)."""
    g = gear.copy()
    if len(g) < 2:
        return g
    edges = np.flatnonzero(np.diff(g) != 0) + 1
    starts = np.concatenate([[0], edges])
    ends = np.concatenate([edges, [len(g)]])
    for s, e in zip(starts, ends):
        if g[s] and time[e - 1] - time[s] < min_s:
            g[s:e] = 0
    return g


@dataclass
class Shift:
    t: float            # session seconds, at the last moment in the old gear
    frm: int
    to: int
    rpm: float          # RPM when the shift started
    speed: float        # km/h


def shifts(gear: np.ndarray, rpm: np.ndarray, speed: np.ndarray, time: np.ndarray,
           max_gap_s: float = 1.5) -> list[Shift]:
    """Gear changes: the last sample in one gear to the first in another,
    no more than max_gap_s apart."""
    out = []
    last = None   # (index, gear)
    for i in np.flatnonzero(gear > 0):
        if last is not None and gear[i] != last[1]:
            j = last[0]
            if time[i] - time[j] <= max_gap_s:
                out.append(Shift(float(time[j]), int(last[1]), int(gear[i]),
                                 float(rpm[j]), float(speed[j])))
        last = (i, gear[i])
    return out


# --------------------------------------------------------- whole analysis ---
class EngineData:
    """Engine channels for every session of an analysis.Analysis, cleaned
    once, with one set of gear ratios shared by all sessions (same car)."""

    def __init__(self, a):
        self.a = a
        self.per_session: list[list[Channel]] = []
        self._smooth: dict[tuple[int, str], np.ndarray] = {}
        self.gears: list[np.ndarray | None] = []
        self.shifts: list[list[Shift]] = []
        rpm_all, spd_all = [], []
        for si, s in enumerate(a.sessions):
            chans = channels(s.tel)
            self.per_session.append(chans)
            for c in chans:
                self._smooth[(si, c.col)] = smooth_holds(s.tel.time, s.tel.raw[c.col])
            rc = self._rpm_col(si)
            if rc is not None and s.tel.channels.get("speed") is not None:
                rpm_all.append(self._smooth[(si, rc)])
                spd_all.append(s.tel.channels["speed"])
        self.gearing = (find_gearing(np.concatenate(rpm_all), np.concatenate(spd_all))
                        if rpm_all else Gearing())
        for si, s in enumerate(a.sessions):
            rc = self._rpm_col(si)
            if rc is None or not self.gearing:
                self.gears.append(None)
                self.shifts.append([])
                continue
            rpm = self._smooth[(si, rc)]
            spd = s.tel.channels["speed"]
            g = clean_runs(self.gearing.gear(rpm, spd), s.tel.time)
            self.gears.append(g)
            self.shifts.append(shifts(g, rpm, spd, s.tel.time))

    # ---------------------------------------------------------- lookups
    @property
    def channels(self) -> list[Channel]:
        """Every channel in any session (first session's details win)."""
        seen, out = set(), []
        for chans in self.per_session:
            for c in chans:
                if c.col not in seen:
                    seen.add(c.col)
                    out.append(c)
        out.sort(key=lambda c: (KIND_ORDER.get(c.kind, 99), c.col))
        return out

    def __bool__(self):
        return any(self.per_session)

    def _rpm_col(self, si) -> str | None:
        return next((c.col for c in self.per_session[si] if c.kind == "rpm"), None)

    def col_of(self, kind: str) -> str | None:
        return next((c.col for c in self.channels if c.kind == kind), None)

    def series(self, si: int, col: str) -> np.ndarray | None:
        return self._smooth.get((si, col))

    def _lap_slice(self, ld):
        n = len(self.a.sessions[ld.session].tel.time)
        return slice(ld.lap.i0, min(ld.lap.i0 + len(ld.t), n))

    def on_grid(self, ld, col: str) -> np.ndarray | None:
        """col for lap ld at each point of the analysis grid (NaN if the
        session doesn't have it)."""
        v = self.series(ld.session, col)
        if v is None or ld.d is None:
            return None
        x = v[self._lap_slice(ld)]
        d = ld.d[:len(x)] + np.arange(len(x)) * 1e-6
        return np.interp(self.a.grid, d, x)

    def gear_on_grid(self, ld) -> np.ndarray | None:
        g = self.gears[ld.session]
        if g is None or ld.d is None:
            return None
        x = g[self._lap_slice(ld)].astype(float)
        d = ld.d[:len(x)] + np.arange(len(x)) * 1e-6
        idx = np.clip(np.searchsorted(d, self.a.grid), 0, len(x) - 1)
        return x[idx]          # steps, not blended between gears

    def at(self, ld, col: str, t_into: float) -> float | None:
        v = self.series(ld.session, col)
        if v is None:
            return None
        tel = self.a.sessions[ld.session].tel
        return float(np.interp(ld.lap.start + t_into, tel.time, v))

    def gear_at(self, ld, t_into: float) -> int:
        g = self.gears[ld.session]
        if g is None:
            return 0
        tel = self.a.sessions[ld.session].tel
        i = int(np.clip(np.searchsorted(tel.time, ld.lap.start + t_into), 0, len(g) - 1))
        return int(g[i])

    def lap_shifts(self, ld) -> list[Shift]:
        t0, t1 = ld.lap.start, ld.lap.start + ld.time
        return [s for s in self.shifts[ld.session] if t0 <= s.t <= t1]

    # ------------------------------------------------------------ stats
    def lap_stats(self, ld) -> dict:
        """Numbers for the laps table: full-throttle share, max RPM, peak
        temperatures, shift count and typical upshift RPM."""
        out: dict = {}
        sl = self._lap_slice(ld)
        tel = self.a.sessions[ld.session].tel
        dt = np.gradient(tel.time[sl]) if sl.stop - sl.start > 1 else np.zeros(0)
        for c in self.per_session[ld.session]:
            v = self.series(ld.session, c.col)[sl]
            if not len(v):
                continue
            if c.kind == "throttle":
                hi = 95.0 if np.nanmax(v) > 1.5 else 0.95
                out["full_throttle"] = float(np.sum(dt[v >= hi]) / max(ld.time, 1e-6))
            elif c.kind == "rpm":
                out["max_rpm"] = float(np.nanmax(v))
            elif c.kind in TEMPS:
                out[f"max_{c.kind}"] = float(np.nanmax(v))
        sh = self.lap_shifts(ld)
        if self.gears[ld.session] is not None:
            out["shifts"] = len(sh)
            ups = [s.rpm for s in sh if s.to > s.frm]
            if ups:
                out["upshift_rpm"] = float(np.median(ups))
        return out

    def gear_table(self, ld) -> list[dict]:
        """Per gear on lap ld: RPM per km/h, speed range used, upshift RPM."""
        if not self.gearing or self.gears[ld.session] is None:
            return []
        sl = self._lap_slice(ld)
        g = self.gears[ld.session][sl]
        spd = self.a.sessions[ld.session].tel.channels["speed"][sl]
        sh = self.lap_shifts(ld)
        rows = []
        for n, r in enumerate(self.gearing.ratios, 1):
            m = g == n
            up = [s.rpm for s in sh if s.frm == n and s.to == n + 1]
            rows.append({"gear": n, "ratio": r,
                         "v_min": float(spd[m].min()) if m.any() else None,
                         "v_max": float(spd[m].max()) if m.any() else None,
                         "upshift_rpm": float(np.median(up)) if up else None,
                         "upshifts": len(up)})
        return rows
