# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""A track day across sessions: how the grip you used changed through the
day, and the notes and weather that might explain it.

What the data can and can't say: the grip envelope is what you *used*, not
what was there. If a later session's envelope is bigger, either there was
more grip (track rubbered in, temperatures, tires) or you pushed harder into
grip that was already there. Two things narrow it down:
  - every turn moving together points at the car/track/conditions (they
    change grip everywhere); a few turns moving points at the driver
    (technique is corner by corner);
  - within a session, tires show as a rise over the first laps (warming) and
    a fall later (heat); driver learning shows across sessions instead.
Session notes (pressures, temperatures, changes) and the weather at the
session time fill in the rest.

Notes live next to the data file as <csv>.notes.json. Session start times
come from the synced GoPro's GPS clock when it had a fix, else the video's
creation time, else what you type. Weather comes from Open-Meteo for the
track's position and the session time, whenever the laptop is online,
including later at home; it's cached in the notes file.
Nothing here imports Qt.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import grip as gripmod

WEATHER_VARS = ["temperature_2m", "relative_humidity_2m", "cloud_cover",
                "shortwave_radiation", "wind_speed_10m", "precipitation"]
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_PAST_DAYS = 90       # the forecast API serves roughly the last 3 months


# ------------------------------------------------------------------ notes ---
@dataclass
class Notes:
    start: str = ""              # session start, local track time "YYYY-MM-DD HH:MM"
    start_source: str = ""       # "gps", "fixed", "video", "typed"
    start_utc: str = ""          # ISO UTC, when known exactly (GPS)
    pressures_cold: str = ""     # e.g. "30/30/28/28" (FL/FR/RL/RR)
    pressures_hot: str = ""
    track_temp: str = ""         # typed, with unit
    tires: str = ""              # set, heat cycles...
    fuel: str = ""
    changes: str = ""            # setup / driving changes before this session
    text: str = ""               # anything else
    title: str = ""              # session name, e.g. from its folder ("Sunday Scored 1")
    use: str = "in"              # "in", "out" (left out of the day-wide numbers), "hidden"
    weather: dict = field(default_factory=dict)   # filled by fetch: air_c, humidity, ...

    @staticmethod
    def path_for(csv: str) -> Path:
        p = Path(csv)
        return p.with_name(p.name + ".notes.json")

    @classmethod
    def load(cls, csv: str) -> "Notes":
        try:
            d = json.loads(cls.path_for(csv).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, csv: str) -> Path:
        p = self.path_for(csv)
        p.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return p

    def start_dt(self) -> dt.datetime | None:
        try:
            return dt.datetime.strptime(self.start, "%Y-%m-%d %H:%M")
        except ValueError:
            return None


def _video_creation(path: str) -> dt.datetime | None:
    """The MP4's creation_time. GoPros write the camera's local clock there
    (marked UTC, but it isn't), so it's taken as local track time."""
    try:
        out = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries",
                              "format_tags=creation_time", "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        if not out:
            return None
        return dt.datetime.fromisoformat(out.replace("Z", "")[:19])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def log_start_utc(tel) -> dt.datetime | None:
    """When the data file starts, from its own GPS clock: VBO files carry
    the UTC time of day on every row and the UTC date on their first line."""
    meta = getattr(tel, "meta", None) or {}
    day, secs = meta.get("created"), meta.get("utc_start_s")
    if not day or secs is None:
        return None
    try:
        d = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return d + dt.timedelta(seconds=float(secs) % 86400)


def find_start(video: tuple[list[str], float] | None, lat: float, lon: float,
               tel=None) -> tuple[str, str, str] | None:
    """(local "YYYY-MM-DD HH:MM", source, UTC ISO or "") for when the data
    file starts: from the file's own GPS clock (VBO), else the synced
    video; None if neither knows the time.
    `offset` maps data time to video time: video_t = data_t - offset."""
    start_utc = log_start_utc(tel) if tel is not None else None
    if start_utc is not None:
        local = start_utc.astimezone(_local_tz(lon)).replace(tzinfo=None)
        return local.strftime("%Y-%m-%d %H:%M"), "gps", start_utc.isoformat()
    if not video:
        return None
    parts, off = video
    from . import gpmf
    try:
        g = gpmf.gps_time(parts[0])
    except Exception:  # noqa: BLE001 - a broken clip just means no GPS time
        g = None
    if g is not None:
        vt, utc = g
        start_utc = utc - dt.timedelta(seconds=vt + off)
        local = start_utc.astimezone(_local_tz(lon)).replace(tzinfo=None)
        return local.strftime("%Y-%m-%d %H:%M"), "gps", start_utc.isoformat()
    from . import ingest
    rec, fix = ingest.recorded_for(parts)
    if rec is not None and fix:          # the camera clock, corrected at import
        return (rec - dt.timedelta(seconds=off)).strftime("%Y-%m-%d %H:%M"), "fixed", ""
    c = _video_creation(parts[0])
    if c is not None:
        return (c - dt.timedelta(seconds=off)).strftime("%Y-%m-%d %H:%M"), "video", ""
    return None


def local_from_utc(utc_iso: str, offset_s: int) -> str:
    """UTC ISO time -> local "YYYY-MM-DD HH:MM" with a known UTC offset."""
    t = dt.datetime.fromisoformat(utc_iso).astimezone(dt.timezone.utc).replace(tzinfo=None)
    return (t + dt.timedelta(seconds=offset_s)).strftime("%Y-%m-%d %H:%M")


def _local_tz(lon: float) -> dt.timezone:
    """Rough local time zone from longitude, only used until the weather
    service returns the real UTC offset (then times are re-based)."""
    return dt.timezone(dt.timedelta(hours=round(lon / 15)))


# ---------------------------------------------------------------- weather ---
def weather_url(lat: float, lon: float, day: dt.date, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    base = ARCHIVE_URL if (today - day).days > FORECAST_PAST_DAYS else FORECAST_URL
    q = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
         "start_date": day.isoformat(), "end_date": day.isoformat(),
         "hourly": ",".join(WEATHER_VARS), "timezone": "auto",
         "temperature_unit": "celsius", "wind_speed_unit": "kmh"}
    return base + "?" + urllib.parse.urlencode(q)


def fetch_weather(lat: float, lon: float, day: dt.date, timeout: float = 10.0) -> dict:
    """Hourly weather for the track on that day (local times). Raises OSError
    when offline."""
    url = weather_url(lat, lon, day)
    req = urllib.request.Request(url, headers={"User-Agent": "RaceAnalysis"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def weather_at(resp: dict, when: dt.datetime) -> dict:
    """Values at a local time, interpolated between hours."""
    h = resp.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return {}
    ts = np.array([dt.datetime.fromisoformat(t).timestamp() for t in times])
    x = when.timestamp()
    out = {}
    names = {"temperature_2m": "air_c", "relative_humidity_2m": "humidity",
             "cloud_cover": "cloud", "shortwave_radiation": "sun_wm2",
             "wind_speed_10m": "wind_kmh", "precipitation": "rain_mm"}
    for k, name in names.items():
        v = h.get(k)
        if not v:
            continue
        arr = np.array([np.nan if y is None else y for y in v], float)
        ok = np.isfinite(arr)
        if ok.any():
            out[name] = round(float(np.interp(x, ts[ok], arr[ok])), 1)
    if out:
        out["at"] = when.strftime("%Y-%m-%d %H:%M")
        out["source"] = "Open-Meteo"
    return out


# ------------------------------------------------------------------- grip ---
def session_envelope(a, si: int) -> gripmod.Envelope | None:
    laps = [ld for ld in a.laps if ld.session == si and ld.timed
            and ld.lat_g is not None and ld.lon_g is not None]
    if not laps:
        return None
    try:
        return gripmod.build_envelope(np.concatenate([ld.lat_g for ld in laps]),
                                      np.concatenate([ld.lon_g for ld in laps]),
                                      np.concatenate([ld.v for ld in laps]))
    except ValueError:
        return None


def envelope_summary(env: gripmod.Envelope | None) -> dict:
    """Cornering (left/right average), braking and the overall size."""
    if env is None:
        return {}
    return {"corner": float((env.at(0.0) + env.at(np.pi)) / 2),
            "brake": float(env.at(1.5 * np.pi)),
            "mean": float(np.mean(env.radius))}


PCT_TURN = 95.0


def turn_capacity(a) -> tuple[list, list[list[float | None]]]:
    """Per turn and session: the 95th percentile of combined g through the
    turn across that session's timed laps; roughly the most grip that turn
    got that session. Returns (corners, rows[turn][session])."""
    rows = []
    for c in a.corners:
        row = []
        for si in range(len(a.sessions)):
            g = []
            for ld in a.laps:
                if ld.session != si or not ld.timed or ld.lat_g is None or ld.d is None:
                    continue
                m = (ld.d >= c.start_d) & (ld.d <= c.end_d) & (ld.v > 30)
                g.append(np.hypot(ld.lat_g[m], ld.lon_g[m]))
            g = np.concatenate(g) if g else np.zeros(0)
            g = g[np.isfinite(g)]
            row.append(float(np.percentile(g, PCT_TURN)) if g.size >= 10 else None)
        rows.append(row)
    return list(a.corners), rows


def lap_peaks(a) -> list[list[tuple[int, float, float]]]:
    """Per session: (lap number, cornering g, braking g) for each timed lap,
    the 95th percentile of |lateral g| and of braking g. Rises over the
    first laps = tires warming; falls later = heat."""
    out = []
    for si in range(len(a.sessions)):
        rows = []
        for ld in a.laps:
            if ld.session != si or not ld.timed or ld.lat_g is None:
                continue
            m = ld.v > 30
            lat = np.abs(ld.lat_g[m])
            brk = -ld.lon_g[m]
            lat, brk = lat[np.isfinite(lat)], brk[np.isfinite(brk)]
            if lat.size < 20:
                continue
            rows.append((ld.lap.n, float(np.percentile(lat, PCT_TURN)),
                         float(np.percentile(brk, PCT_TURN))))
        out.append(rows)
    return out


@dataclass
class Verdict:
    shift: float             # median change across turns (g)
    agree: float             # share of turns that moved the same way as the median
    outliers: list[str]      # turns that moved a lot against the trend
    text: str
    short: str = ""          # one line for the step-by-step story
    kind: str = ""           # "all" | "some" | "uneven" | "flat"
    moved: list = field(default_factory=list)   # (turn, change in g) that stood out, biggest first


def compare_sessions(names: list[str], rows: list[list[float | None]], s0: int, s1: int,
                     min_shift: float = 0.03, big: float = 0.06) -> Verdict | None:
    """Session s1 vs s0, turn by turn: did everything move together (car,
    tires, track, temperatures) or only some turns (you)?"""
    diffs, which = [], []
    for name, r in zip(names, rows):
        if r[s0] is not None and r[s1] is not None:
            diffs.append(r[s1] - r[s0])
            which.append(name)
    if len(diffs) < 3:
        return None
    d = np.array(diffs)
    shift = float(np.median(d))
    same = float(np.mean(np.sign(d) == np.sign(shift))) if abs(shift) > 1e-9 else 0.0
    resid = d - shift
    outliers = [n for n, r in zip(which, resid) if abs(r) >= big]
    moved = sorted(((n, float(r)) for n, r in zip(which, d) if n in outliers),
                   key=lambda x: -abs(x[1]))
    word = "more" if shift > 0 else "less"
    if abs(shift) >= min_shift and same >= 0.75:
        kind = "all"
        text = (f"Nearly every turn used {abs(shift):.2f} g {word} grip ({same:.0%} of turns "
                "moved the same way). Changes everywhere at once usually come from the "
                "car, tires, track or temperature rather than driving.")
        short = f"every turn {shift:+.2f} g: car, tires, track or temperature"
    elif outliers:
        kind = "some"
        text = (f"Most turns stayed about the same ({shift:+.2f} g), but "
                f"{', '.join(outliers)} changed a lot. Changes in a few turns usually come "
                "from driving (line, braking, commitment) there.")
        txt = ", ".join(f"{n} {r:+.2f}" for n, r in moved[:3])
        if len(moved) > 3:
            txt += f" and {len(moved) - 3} more"
        short = f"{txt}, the rest about the same: driving"
    elif abs(shift) >= min_shift:
        kind = "uneven"
        text = (f"Grip used changed {shift:+.2f} g overall, but unevenly ({same:.0%} of turns "
                "the same way): no clear sign of car or driver.")
        short = f"{shift:+.2f} g overall but uneven: no clear cause"
    else:
        kind = "flat"
        text = f"Little change ({shift:+.2f} g across turns)."
        short = f"little change ({shift:+.2f} g)"
    return Verdict(shift, same, outliers, text, short, kind, moved)


@dataclass
class Step:
    """One link in the comparison story: session `to` against `frm`."""
    frm: int
    to: int
    verdict: Verdict | None
    best_change: float | None      # seconds, - = faster
    changes: str                   # what the driver noted before session `to`


def steps(n: int, mode: str = "consecutive", base: int = 0,
          counts: list[bool] | None = None) -> list[tuple[int, int]]:
    """Which pairs to compare: each session against the one before it
    ("consecutive") or every other session against one ("baseline").
    Sessions left out (counts[i] False) are skipped: step by step goes from
    the last session that counts to the next."""
    idx = [i for i in range(n) if counts is None or counts[i]]
    if len(idx) < 2:
        return []
    if mode == "baseline":
        base = min(max(0, base), n - 1)
        if base not in idx:
            return []
        return [(base, i) for i in idx if i != base]
    return list(zip(idx, idx[1:]))


def story(names: list[str], rows: list[list[float | None]], bests: list[float | None],
          changes: list[str], mode: str = "consecutive", base: int = 0,
          counts: list[bool] | None = None) -> list[Step]:
    """The day as a chain of comparisons. names are the turns, rows[turn] the
    grip used per session, bests each session's best lap, changes each
    session's "Changes" note."""
    out = []
    for a, b in steps(len(bests), mode, base, counts):
        v = compare_sessions(names, rows, a, b)
        bc = bests[b] - bests[a] if bests[a] is not None and bests[b] is not None else None
        out.append(Step(a, b, v, bc, (changes[b] or "").strip()))
    return out


def compared_with(si: int, mode: str = "consecutive", base: int = 0,
                  counts: list[bool] | None = None) -> int | None:
    """The session si is compared with, or None (first, baseline, left out)."""
    ok = (lambda i: counts is None or counts[i])
    if not ok(si):
        return None
    if mode == "baseline":
        return base if base != si and ok(base) else None
    return next((i for i in range(si - 1, -1, -1) if ok(i)), None)


def cell_change(rows: list[list[float | None]], r: int, si: int, mode: str = "consecutive",
                base: int = 0, counts: list[bool] | None = None) -> float | None:
    """Change in turn r's grip used for session si against what it's being
    compared with (the session before that counts, or the baseline). None
    for the first/baseline session, one left out, or a missing value."""
    other = compared_with(si, mode, base, counts)
    if other is None:
        return None
    a, b = rows[r][other], rows[r][si]
    return None if a is None or b is None else b - a
