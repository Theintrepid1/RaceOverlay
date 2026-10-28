# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Load racing telemetry from logger exports into one common form.

Two layers:
  1. Parsing: a device-specific reader turns the file into a time axis plus
     every column (with units and file metadata). Supported: AiM Race Studio
     CSV, and generic CSV (APEX Pro, TrackAddict, Harry's, RaceBox, ...).
  2. Roles: standard channels the built-in gauges use (speed, throttle,
     brake, rpm, lap, g-forces, GPS) are filled from whatever the file has,
     with fallbacks, unit conversion, and the g-force signs checked against
     GPS so a logger or CAN channel with inverted axes can't flip the g-ball.

Every column stays available as "col:<name>" for custom gauges.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field

import numpy as np

# Role -> regexes, tried in order (first = preferred source).
ROLE_PATTERNS: dict[str, list[str]] = {
    "time":     [r"^time$", r"^time\b", r"\btime\b", r"^t$", r"timestamp",
                 r"elapsed"],
    "speed":    [r"gps.?speed",
                 r"^(?!.*(engine|wheel|accura)).*\bspeed\b",
                 r"\bvelocity\b", r"speed.?av(era)?g"],
    "throttle": [r"pedal.?pos", r"accel.*pedal", r"gas.?pedal", r"throttle",
                 r"\btps\b"],
    "brake":    [r"brake.?pres", r"brake.?(pedal|pos)", r"^brake\b",
                 r"\bbrake\b"],
    "rpm":      [r"\brpm\b", r"engine.?rpm", r"rpm", r"engine.?speed",
                 r"\brevs?\b"],
    "lap":      [r"^lap\b(?!.*time)", r"lap\s*#", r"lap.?num", r"lap.?count"],
    "ax":       [r"^ax\b", r"^acc[_ ]?x\b"],
    "ay":       [r"^ay\b", r"^acc[_ ]?y\b"],
    "az":       [r"^az\b", r"^acc[_ ]?z\b"],
    "gps_lat":  [r"gps.?latitude", r"^gps.?lat$", r"^latitude\b",
                 r"^lat\b(?!.*\b(g|acc\w*)\b)"],
    "gps_lon":  [r"gps.?longitude", r"^gps.?lon$", r"^longitude\b",
                 r"^(lon|lng)\b(?!.*\b(g|acc\w*)\b)"],
}
# G roles collect every matching column; the best one (by agreement with
# GPS) wins and its sign is corrected.
G_PATTERNS: dict[str, list[str]] = {
    "lat_g": [r"lateral.*(acc|g\b)", r"acc.*lat", r"\blat.?g\b",
              r"\bg.?lat\b", r"lat.?acc", r"^accel[_ ]?y$", r"^y.?accel"],
    "lon_g": [r"longitud.*(acc|g\b)", r"acc.*(lon|fwd|long)", r"\blon.?g\b",
              r"\bg.?lon\b", r"lon.?acc", r"inline.?acc", r"^accel[_ ]?x$",
              r"^x.?accel"],
}
ROLE_ORDER = ["time", "speed", "throttle", "brake", "rpm", "lap", "ax", "ay",
              "az", "lat_g", "lon_g", "gps_lat", "gps_lon"]
# Kept for older callers.
CHANNEL_PATTERNS = {**ROLE_PATTERNS, **G_PATTERNS}

ROLE_LABELS = {
    "speed": "Speed", "throttle": "Throttle", "brake": "Brake", "rpm": "RPM",
    "lap": "Lap number", "lat_g": "Lateral g", "lon_g": "Longitudinal g",
    "gps_lat": "GPS latitude", "gps_lon": "GPS longitude",
}
USER_ROLES = list(ROLE_LABELS)

SPEED_UNITS = [("km/h", 1.0), ("kmh", 1.0), ("kph", 1.0), ("mph", 1.60934),
               ("m/s", 3.6), ("ft/s", 1.09728), ("knots", 1.852),
               ("kn", 1.852), ("kt", 1.852)]
PRESSURE_UNITS = {"bar", "psi", "kpa", "mpa", "mbar"}


@dataclass
class Telemetry:
    time: np.ndarray                       # seconds from file start
    channels: dict[str, np.ndarray]        # role -> values (normalized units)
    mapping: dict[str, str] = field(default_factory=dict)  # role -> column
    source_columns: list[str] = field(default_factory=list)
    # How lat_g/lon_g were produced: "raw" (rotated ax/ay), "named" (picked
    # from lateral/longitudinal columns) or "" (no g data). For "raw",
    # g_align holds {"angle", "flip", "auto", "lon_fit", "lat_fit"}.
    g_source: str = ""
    g_align: dict = field(default_factory=dict)
    raw: dict[str, np.ndarray] = field(default_factory=dict)  # every column
    units: dict[str, str] = field(default_factory=dict)       # column -> unit
    meta: dict = field(default_factory=dict)                   # file metadata
    device: str = "csv"
    checks: dict = field(default_factory=dict)  # role -> verification info
    _clean: dict = field(default_factory=dict, init=False, repr=False)

    @property
    def duration(self) -> float:
        return float(self.time[-1] - self.time[0]) if len(self.time) else 0.0

    def series(self, key: str) -> np.ndarray | None:
        """Role name ("speed") or column ("col:OilTemp")."""
        if key.startswith("col:"):
            return self.raw.get(key[4:])
        return self.channels.get(key)

    def has(self, key: str) -> bool:
        """True only if the channel exists and actually varies.

        A constant/all-zero channel (e.g. TPS/RPM with no OBD link) counts as
        absent so its gauge is skipped rather than pinned at zero.
        """
        vals = self.series(key)
        if vals is None:
            return False
        finite = vals[np.isfinite(vals)]
        return finite.size > 0 and float(np.ptp(finite)) > 1e-6

    def sample(self, t: float, key: str) -> float:
        """Linearly interpolate a channel at telemetry time `t` (seconds).

        Interpolates across finite samples only, so mixed-rate logs with blank
        cells (GPS at 10Hz in a 50Hz file) don't make gauges blink out.
        Clamps to the first/last value outside the recorded span.
        """
        clean = self._clean.get(key)
        if clean is None:
            vals = self.series(key)
            if vals is None:
                return float("nan")
            ok = np.isfinite(vals)
            clean = (self.time[ok], vals[ok])
            self._clean[key] = clean
        tt, vv = clean
        if tt.size == 0:
            return float("nan")
        return float(np.interp(t, tt, vv))

    def columns_varying(self) -> list[str]:
        return [c for c in self.source_columns if self.has("col:" + c)]


# ------------------------------------------------------------------ parsing --

@dataclass
class Parsed:
    header: list[str]
    units: list[str]
    data: np.ndarray       # rows x columns, NaN where blank/unparseable
    meta: dict
    device: str


def _matrix(rows: list[list[str]], ncol: int) -> np.ndarray:
    out = np.full((len(rows), ncol), np.nan)
    for i, r in enumerate(rows):
        for j in range(min(ncol, len(r))):
            c = r[j].strip().strip('"')
            if c:
                try:
                    out[i, j] = float(c)
                except ValueError:
                    pass
    return out


def _is_aim(first_line: str) -> bool:
    s = first_line.strip().replace('"', "").lower()
    return s.startswith("format,") and "aim" in s


def _parse_aim(lines: list[str]) -> Parsed:
    """AiM Race Studio CSV: metadata block, blank, names, (names again),
    units, channel numbers, blank, data."""
    rows = list(csv.reader(lines))
    i, meta = 0, {}
    while i < len(rows) and rows[i]:
        if len(rows[i]) >= 2:
            meta[rows[i][0].strip()] = rows[i][1].strip()
        i += 1
    while i < len(rows) and not rows[i]:
        i += 1
    header = [h.strip() for h in rows[i]]
    i += 1
    if i < len(rows) and [h.strip() for h in rows[i]] == header:
        i += 1                          # names repeated
    units = [u.strip() for u in rows[i]] if i < len(rows) else []
    i += 1

    def numbery(r):
        return all(not c.strip() or c.strip().isdigit() for c in r)
    if i < len(rows) and rows[i] and numbery(rows[i]):
        i += 1                          # channel number row
    data_rows = [r for r in rows[i:] if r and any(c.strip() for c in r)]
    while header and not header[-1]:   # trailing comma = phantom column
        header.pop()
    units = (units + [""] * len(header))[:len(header)]
    return Parsed(header, units, _matrix(data_rows, len(header)), meta, "aim")


def _parse_generic(lines: list[str]) -> Parsed:
    start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#"):
            start = i + 1
            continue
        break
    sample = "".join(lines[start:start + 20])
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delim = ","
    rows = [r for r in csv.reader(lines[start:], delimiter=delim) if r]
    if not rows:
        raise ValueError("No data rows found")
    header = [h.strip().strip('"') for h in rows[0]]
    while header and not header[-1]:
        header.pop()
    units = []
    for h in header:  # "speed (m/s)", "Oil Temp [C]"
        m = re.search(r"[\(\[]([^\)\]]*)[\)\]]\s*$", h)
        units.append(m.group(1).strip() if m else "")
    return Parsed(header, units, _matrix(rows[1:], len(header)), {}, "csv")


def parse(path: str) -> Parsed:
    with open(path, "rb") as fh:
        blob = fh.read()
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = blob.decode("latin-1")   # AiM writes the degree sign in Latin-1
    lines = text.splitlines(keepends=True)
    first = next((l for l in lines if l.strip()), "")
    if _is_aim(first):
        return _parse_aim(lines)
    return _parse_generic(lines)


DEVICE_NAMES = {"aim": "AiM (Race Studio CSV)", "csv": "Generic CSV"}


def _guess_device(p: Parsed) -> str:
    if p.device != "csv":
        return p.device
    cols = " ".join(p.header).lower()
    if "apex score" in cols:
        return "apex"
    return "csv"


# ------------------------------------------------------------- role mapping --

def _matches(columns: list[str], patterns: list[str], taken: set) -> list[str]:
    out = []
    for pat in patterns:
        rx = re.compile(pat, re.IGNORECASE)
        for col in columns:
            if col not in taken and col not in out and rx.search(col):
                out.append(col)
    return out


def _unit_text(col: str, unit: str) -> str:
    return f"{unit} {col}".lower()


def _speed_to_kmh(vals: np.ndarray, col: str, unit: str) -> np.ndarray:
    u = unit.lower().strip()
    for hint, factor in SPEED_UNITS:
        if u == hint:
            return vals * factor
    text = _unit_text(col, unit)
    for hint, factor in SPEED_UNITS:
        if hint in text:
            return vals * factor
    return vals  # unknown: assume km/h (override/convert if wrong)


def _normalize_pedal(vals: np.ndarray) -> np.ndarray:
    """Normalize throttle/brake to 0..1. Accepts 0..1, 0..100, or 0..255."""
    finite = vals[np.isfinite(vals)]
    if finite.size == 0:
        return vals
    peak = np.nanmax(finite)
    if peak <= 1.5:
        return np.clip(vals, 0, 1)
    if peak <= 101.0:  # float noise: AiM writes 100.000031
        return np.clip(vals / 100.0, 0, 1)
    return np.clip(vals / 255.0, 0, 1)


def _normalize_brake(vals: np.ndarray, unit: str) -> np.ndarray:
    u = unit.lower().strip()
    finite = vals[np.isfinite(vals)]
    if finite.size and (u in PRESSURE_UNITS or (u != "%" and np.nanmax(finite) > 255)):
        top = float(np.percentile(finite[finite > 0], 99.5)) if (finite > 0).any() else 1.0
        return np.clip(vals / (top or 1.0), 0, 1)  # pressure: 1 = hardest stop
    return _normalize_pedal(vals)


def _g_units(vals: np.ndarray, unit: str) -> np.ndarray:
    u = unit.lower().replace(" ", "")
    if u in ("m/s2", "m/s^2", "m/s²"):
        return vals / 9.81
    if u in ("mg", "milli-g"):
        return vals / 1000.0
    return vals


def _gps_refs(time, speed_kmh, glat, glon):
    """References from GPS alone: (moving mask, d(speed)/dt in g,
    speed x yaw rate in g with + = right turn). None if GPS is missing."""
    if speed_kmh is None or glat is None or glon is None:
        return None
    ok = (np.isfinite(speed_kmh) & np.isfinite(glat) & np.isfinite(glon)
          & np.isfinite(time))
    if ok.sum() < 200:
        return None
    t = time[ok]
    dt = float(np.median(np.diff(t))) or 0.1
    n = max(1, int(round(0.5 / dt)))
    v = _smooth(speed_kmh[ok] / 3.6, n)
    la, lo = glat[ok], glon[ok]
    east = (lo - lo[0]) * np.cos(np.radians(np.mean(la))) * 111320.0
    north = (la - la[0]) * 110540.0
    ve = np.gradient(_smooth(east, n), t)
    vn = np.gradient(_smooth(north, n), t)
    heading = np.unwrap(np.arctan2(ve, vn))
    right = v * _smooth(np.gradient(heading, t), n) / 9.81
    dvdt = np.gradient(v, t) / 9.81
    moving = v > 8.0
    if moving.sum() < 100:
        return None
    edge = max(1, n)  # smoothing edges are junk
    moving[:edge] = False
    moving[-edge:] = False
    return ok, moving, dvdt, right, n


def _pick_g(role, cands, raw, units, refs):
    """Best candidate for a g role and whether to flip it."""
    info = {"col": cands[0], "corr": None, "flipped": False,
            "verified": False, "candidates": list(cands)}
    if refs is None:
        return info
    ok, moving, dvdt, right, n = refs
    ref = dvdt if role == "lon_g" else right
    best = None
    for c in cands:
        vals = _g_units(raw[c], units.get(c, ""))[ok]
        if not np.isfinite(vals).all():
            vals = np.nan_to_num(vals)
        r = _corr(_smooth(vals, n)[moving], ref[moving])
        if best is None or abs(r) > abs(best[1]):
            best = (c, r)
    if best and abs(best[1]) >= 0.5:
        info.update(col=best[0], corr=abs(best[1]), flipped=best[1] < 0,
                    verified=True)
    return info


def _single_lap(meta: dict) -> tuple[int, float] | None:
    m = re.search(r"lap\s*(\d+)\s*-\s*(\d+):(\d+(?:\.\d+)?)",
                  meta.get("Segment", ""), re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)) * 60 + float(m.group(3))


def load(path: str, overrides: dict[str, str] | None = None,
         g_rotate: float | None = None, g_flip: bool = False) -> Telemetry:
    """Load a telemetry file into roles + raw columns.

    `overrides` maps a role to an exact column name (or "" = don't use this
    role). Raw accelerometer axes (ax, ay; e.g. APEX Pro) are rotated into
    the car's frame to make lon_g/lat_g; `g_rotate`/`g_flip` set that
    explicitly, None auto-detects from GPS.
    """
    overrides = dict(overrides or {})
    p = parse(path)
    header = p.header
    unit_of = dict(zip(header, p.units))
    for role, col in overrides.items():
        if col and col not in header:
            raise ValueError(f"Column '{col}' for '{role}' isn't in this file. "
                             f"Columns: {header}")

    mapping: dict[str, str] = {}
    g_cands: dict[str, list[str]] = {}
    taken: set[str] = {c for c in overrides.values() if c}
    for role in ROLE_ORDER:
        if role in overrides:
            if overrides[role]:
                if role in G_PATTERNS:
                    g_cands[role] = [overrides[role]]
                else:
                    mapping[role] = overrides[role]
            continue
        if role in G_PATTERNS:
            cands = _matches(header, G_PATTERNS[role], taken)
            if cands:
                g_cands[role] = cands
                taken.update(cands)
            continue
        found = _matches(header, ROLE_PATTERNS[role], taken)
        if found:
            mapping[role] = found[0]
            taken.add(found[0])

    if "time" not in mapping:
        raise ValueError(f"Could not find a time column. Columns: {header}\n"
                         f"Use --map time=<ColumnName> to set it.")
    ti = header.index(mapping["time"])
    time = p.data[:, ti]
    tu = unit_of.get(mapping["time"], "").lower()
    valid = np.isfinite(time)
    data = p.data[valid]
    time = time[valid]
    if tu in ("ms", "msec") or (len(time) > 2 and np.median(np.diff(np.sort(time))) >= 5
                                 and np.nanmax(time) > 1e5):
        time = time / 1000.0
    order = np.argsort(time, kind="stable")
    data, time = data[order], time[order]
    time = time - time[0] if len(time) else time

    raw = {h: data[:, j] for j, h in enumerate(header)}
    channels: dict[str, np.ndarray] = {}
    for role, col in mapping.items():
        if role == "time":
            continue
        v, u = raw[col].copy(), unit_of.get(col, "")
        if role == "speed":
            v = _speed_to_kmh(v, col, u)
        elif role == "throttle":
            v = _normalize_pedal(v)
        elif role == "brake":
            v = _normalize_brake(v, u)
        channels[role] = v

    tel = Telemetry(time=time, channels=channels, mapping=mapping,
                    source_columns=header, raw=raw, units=unit_of,
                    meta=dict(p.meta), device=_guess_device(p))
    sl = _single_lap(p.meta)
    if sl:
        tel.meta["single_lap"] = sl

    has_raw = "ax" in channels and "ay" in channels
    explicit = {"lat_g", "lon_g"} & {r for r, c in overrides.items() if c}
    if has_raw and not explicit and (not g_cands or g_rotate is not None):
        align_g(tel, g_rotate, g_flip)
    elif g_cands:
        refs = _gps_refs(time, channels.get("speed"), channels.get("gps_lat"),
                         channels.get("gps_lon"))
        for role, cands in g_cands.items():
            info = _pick_g(role, cands, raw, unit_of, refs)
            v = _g_units(raw[info["col"]], unit_of.get(info["col"], ""))
            channels[role] = -v if info["flipped"] else v
            mapping[role] = info["col"]
            tel.checks[role] = info
        tel.g_source = "named"
    return tel


def align_g(tel: Telemetry, angle: float | None = None, flip: bool = False):
    """(Re)build lat_g/lon_g from raw ax/ay.

        lon = cos(a)*ax - sin(a)*ay
        lat = sin(a)*ax + cos(a)*ay     (negated when flip)

    angle=None auto-detects angle and flip. Afterwards lon_g is + under
    acceleration and lat_g is + toward the right (right-hand turn), when
    detection had GPS to work with.
    """
    info = {"angle": angle, "flip": flip, "auto": angle is None,
            "lon_fit": None, "lat_fit": None}
    if angle is None:
        det = detect_g_alignment(tel.time, tel.channels)
        if det:
            info.update(det)
        else:
            info.update(angle=0.0, flip=False)
    th = np.radians(info["angle"])
    ax, ay = tel.channels["ax"], tel.channels["ay"]
    lat = np.sin(th) * ax + np.cos(th) * ay
    tel.channels["lon_g"] = np.cos(th) * ax - np.sin(th) * ay
    tel.channels["lat_g"] = -lat if info["flip"] else lat
    for name in ("lat_g", "lon_g"):
        tel.mapping.pop(name, None)  # derived from ax/ay, not a named column
        tel._clean.pop(name, None)
    tel.g_source = "raw"
    tel.g_align = info


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0.0


def detect_g_alignment(time: np.ndarray, ch: dict) -> dict | None:
    """Find the sensor mount angle by matching the accelerometer to GPS.

    Longitudinal: the rotation whose forward axis best tracks d(speed)/dt.
    Lateral sign: compared against speed x yaw rate from the GPS heading, so
    a right-hand turn reads positive. Fits are correlations (1.0 = perfect).
    """
    ax, ay, spd = ch.get("ax"), ch.get("ay"), ch.get("speed")
    if ax is None or ay is None or spd is None:
        return None
    ok = np.isfinite(ax) & np.isfinite(ay) & np.isfinite(spd) & np.isfinite(time)
    if ok.sum() < 200:
        return None
    t = time[ok]
    dt = float(np.median(np.diff(t))) or 0.1
    n = max(1, int(round(0.5 / dt)))  # ~0.5 s smoothing
    v = _smooth(spd[ok] / 3.6, n)
    a_x, a_y = _smooth(ax[ok], n), _smooth(ay[ok], n)
    dvdt = np.gradient(v, t) / 9.81
    moving = v > 8.0  # ~18 mph: ignore pit lane / paddock
    if moving.sum() < 100:
        return None
    ref = dvdt[moving]
    ths = np.radians(np.arange(360))
    lon_all = (np.cos(ths)[:, None] * a_x[moving][None, :]
               - np.sin(ths)[:, None] * a_y[moving][None, :])
    lon_all = lon_all - lon_all.mean(axis=1, keepdims=True)
    r = ref - ref.mean()
    fits = lon_all @ r / (np.sqrt((lon_all ** 2).sum(axis=1) * (r ** 2).sum()) + 1e-12)
    best = int(np.argmax(fits))
    th = ths[best]
    lat = (np.sin(th) * a_x + np.cos(th) * a_y)[moving]

    flip, lat_fit = False, None
    glat, glon = ch.get("gps_lat"), ch.get("gps_lon")
    if glat is not None and glon is not None:
        la, lo = glat[ok], glon[ok]
        if np.isfinite(la).all() and np.isfinite(lo).all():
            east = (lo - lo[0]) * np.cos(np.radians(la.mean())) * 111320.0
            north = (la - la[0]) * 110540.0
            ve = np.gradient(_smooth(east, n), t)
            vn = np.gradient(_smooth(north, n), t)
            heading = np.unwrap(np.arctan2(ve, vn))  # compass, + = clockwise
            yaw = _smooth(np.gradient(heading, t), n)
            right = (v * yaw / 9.81)[moving]          # + = turning right
            c = _corr(lat, right)
            flip, lat_fit = c < 0, abs(c)
    angle = float(best if best <= 180 else best - 360)
    return {"angle": angle, "flip": bool(flip),
            "lon_fit": float(fits[best]), "lat_fit": lat_fit}




def describe(t: Telemetry) -> str:
    """Human-readable summary of what was detected."""
    head = [f"Device: {DEVICE_NAMES.get(t.device, t.device)}"
            if t.device != "apex" else "Device: APEX Pro (CSV)"]
    for k in ("Venue", "Vehicle", "User", "Date", "Segment"):
        if t.meta.get(k):
            head.append(f"  {k}: {t.meta[k]}")
    head.append(f"{len(t.source_columns)} columns, {t.duration:.1f}s "
                f"({len(t.time)} samples)")
    lines = head
    for role in ROLE_ORDER[1:]:
        vals = t.channels.get(role)
        if vals is None:
            if role in ("ax", "ay", "az"):
                continue
            lines.append(f"  {role:9s} -- not found")
            continue
        src = t.mapping.get(role, "(derived)")
        unit = t.units.get(src, "")
        finite = vals[np.isfinite(vals)]
        rng = (f"  [{np.min(finite):.2f} .. {np.max(finite):.2f}]"
               if finite.size else "  (no data)")
        live = "" if t.has(role) else "  (constant - skipped)"
        chk = ""
        c = t.checks.get(role)
        if c:
            if c["verified"]:
                chk = f"  ✓ matches GPS {c['corr']:.0%}"
                if c["flipped"]:
                    chk += ", sign flipped"
            else:
                chk = "  (couldn't check against GPS)"
            if len(c["candidates"]) > 1:
                chk += f"  [picked from {len(c['candidates'])}]"
        u = f" [{unit}]" if unit else ""
        lines.append(f"  {role:9s} <- {src}{u}{rng}{live}{chk}")
    return "\n".join(lines)
