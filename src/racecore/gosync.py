# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Line GoPro footage up with a logger using the camera's accelerometer.

The GoPro records ~200 Hz accelerometer data in its GPMF track whether or
not its GPS is on. Every brake zone and corner leaves the same pattern in
the camera's accelerometer and the logger's g channels, so cross-correlating
the two finds the offset with no manual alignment and no camera GPS.

Steps:
  1. camera_horizontal(): gravity is one fixed vector in the camera frame
     (the camera is bolted to the car), so subtract it and keep the two
     horizontal axes. A rolling gravity estimate would eat sustained
     cornering load, so it is deliberately not used.
  2. find_offset(): correlate |horizontal g| (independent of how the camera
     is turned on its mount) against the logger's |g| over every possible
     start time, then refine to 10 ms and re-check it in 60 s windows.
  3. to_car_frame(): rotate the camera's horizontal axes onto the logger's
     lon_g/lat_g, giving GoPro g in the car frame.

offset follows RaceOverlay's convention: logger time = video time + offset.
The logger's lon_g/lat_g must already be car-aligned (telemetry.align_g does
this for raw ax/ay loggers such as the APEX Pro).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .telemetry import Telemetry

G = 9.80665
GRID = 0.1           # s, coarse search grid (logger rate for APEX Pro)
FINE = 0.01          # s, fine search step
MIN_SCORE = 0.5      # below this the match is not trusted
MIN_MARGIN = 0.15    # best must beat the next candidate (>5 s away) by this


@dataclass
class SyncResult:
    offset: float                 # logger time = video time + offset
    score: float                  # correlation at the best offset (1 = perfect)
    runner_up: float              # best correlation more than 5 s away
    confident: bool               # score and margin both clear their floors
    window_offsets: list[float] = field(default_factory=list)  # 60 s re-checks
    spread: float = 0.0           # half-range of the window offsets, seconds
    notes: list[str] = field(default_factory=list)


def _bin_mean(t: np.ndarray, x: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Average samples into bins (a box filter that also resamples)."""
    idx = np.searchsorted(edges, t, side="right") - 1
    ok = (idx >= 0) & (idx < len(edges) - 1)
    n = len(edges) - 1
    out = np.full((n,) + x.shape[1:], np.nan)
    cnt = np.bincount(idx[ok], minlength=n).astype(float)
    if x.ndim == 1:
        s = np.bincount(idx[ok], weights=x[ok], minlength=n)
        out = np.where(cnt > 0, s / np.maximum(cnt, 1), np.nan)
    else:
        for j in range(x.shape[1]):
            s = np.bincount(idx[ok], weights=x[ok, j], minlength=n)
            out[:, j] = np.where(cnt > 0, s / np.maximum(cnt, 1), np.nan)
    return out


def _fill(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float).copy()
    bad = ~np.isfinite(x)
    if bad.all():
        return np.zeros_like(x)
    if bad.any():
        i = np.arange(len(x))
        x[bad] = np.interp(i[bad], i[~bad], x[~bad])
    return x


def _z(x: np.ndarray) -> np.ndarray:
    s = x.std()
    return (x - x.mean()) / s if s > 0 else x * 0.0


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(_z(a) * _z(b)))


def _slide(long: np.ndarray, short: np.ndarray) -> np.ndarray:
    """Pearson correlation of `short` against every full-overlap window of
    `long` (FFT, numpy only)."""
    n, m = len(long), len(short)
    s = _z(short)
    size = 1 << int(np.ceil(np.log2(n + m)))
    num = np.fft.irfft(np.fft.rfft(long, size) * np.conj(np.fft.rfft(s, size)), size)[:n - m + 1]
    c1 = np.concatenate([[0.0], np.cumsum(long)])
    c2 = np.concatenate([[0.0], np.cumsum(long * long)])
    wsum = c1[m:] - c1[:-m]
    wsq = c2[m:] - c2[:-m]
    wstd = np.sqrt(np.maximum(wsq / m - (wsum / m) ** 2, 1e-18))
    return num / (m * wstd)


def camera_horizontal(accel: np.ndarray) -> np.ndarray:
    """(N,3) camera accelerometer in m/s^2 -> (N,2) horizontal linear g.

    The two output axes are horizontal but turned at an unknown angle to
    the car; to_car_frame() fixes that once the offset is known.
    """
    a = np.asarray(accel, dtype=float) / G
    grav = np.median(a, axis=0)
    gz = grav / np.linalg.norm(grav)
    lin = a - grav
    ref = np.eye(3)[int(np.argmin(np.abs(gz)))]  # axis least like gravity
    e1 = ref - (gz @ ref) * gz
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(gz, e1)
    return np.column_stack([lin @ e1, lin @ e2])


def _logger_g(tel: Telemetry):
    lon, lat = tel.channels.get("lon_g"), tel.channels.get("lat_g")
    if lon is None or lat is None:
        raise ValueError("logger has no lon_g/lat_g channels to sync against")
    return tel.time, _fill(lon), _fill(lat)


def find_offset(cam_time: np.ndarray, cam_h: np.ndarray, tel: Telemetry) -> SyncResult:
    """Find where the clip sits in the logger session."""
    lt, lon, lat = _logger_g(tel)
    # coarse: 0.1 s grid, |g| so the camera's mount angle doesn't matter
    ge = np.arange(cam_time[0], cam_time[-1] + GRID, GRID)
    cam_mag = _fill(np.hypot(*_bin_mean(cam_time, cam_h, ge).T))
    le = np.arange(lt[0], lt[-1] + GRID, GRID)
    log_mag = _fill(_bin_mean(lt, np.hypot(lon, lat), le))
    notes = []
    if len(cam_mag) > len(log_mag):
        notes.append("clip is longer than the logger session")
        return SyncResult(0.0, 0.0, 0.0, False, notes=notes)
    c = _slide(log_mag, cam_mag)
    k = int(np.argmax(c))
    away = np.abs(np.arange(len(c)) - k) > int(5 / GRID)
    runner = float(c[away].max()) if away.any() else 0.0
    coarse = le[k] - ge[0]

    # fine: 10 ms steps within +-1 s, on the full-rate camera data
    def score_at(off, sel=slice(None)):
        ct = cam_time[sel]
        lm = np.hypot(np.interp(ct + off, lt, lon), np.interp(ct + off, lt, lat))
        return _corr(lm, cam_fine[sel])

    cam_fine = _smooth_mag(cam_time, cam_h)
    lags = np.arange(coarse - 1.0, coarse + 1.0 + FINE / 2, FINE)
    r = np.array([score_at(L) for L in lags])
    j = int(np.argmax(r))
    off = float(lags[j])
    if 0 < j < len(r) - 1:
        y0, y1, y2 = r[j - 1], r[j], r[j + 1]
        den = y0 - 2 * y1 + y2
        if den != 0:
            off += 0.5 * (y0 - y2) / den * FINE

    # re-check in 60 s windows (30 s apart) to catch drift or a bad lock
    wins = []
    for w0 in np.arange(cam_time[0], cam_time[-1] - 60, 30):
        sel = (cam_time >= w0) & (cam_time < w0 + 60)
        ls = np.arange(off - 0.5, off + 0.5 + FINE / 2, FINE)
        rs = [score_at(L, sel) for L in ls]
        wins.append(float(ls[int(np.argmax(rs))]))
    steady = wins[2:] if len(wins) > 4 else wins  # first minute is often pit-out
    spread = (max(steady) - min(steady)) / 2 if steady else 0.0

    score = float(c[k])
    confident = score >= MIN_SCORE and score - runner >= MIN_MARGIN
    if not confident:
        notes.append("weak match: check the sync by hand")
    if spread > 0.2:
        notes.append(f"offset wanders by +-{spread:.2f} s across the clip")
    return SyncResult(float(round(off, 3)), round(score, 3), round(runner, 3), confident,
                      [round(w, 3) for w in wins], round(spread, 3), notes)


def _box(t: np.ndarray, h: np.ndarray, window: float) -> np.ndarray:
    """Moving-average each column of h over `window` seconds."""
    dt = float(np.median(np.diff(t))) if len(t) > 1 else 0.005
    n = max(1, int(round(window / dt)))
    k = np.ones(n) / n
    return np.column_stack([np.convolve(h[:, i], k, mode="same") for i in range(h.shape[1])])


def _smooth_mag(t: np.ndarray, h: np.ndarray, window: float = 0.1) -> np.ndarray:
    """|horizontal g| box-smoothed to roughly the logger's bandwidth."""
    sm = _box(t, h, window)
    return np.hypot(sm[:, 0], sm[:, 1])


def to_car_frame(cam_time: np.ndarray, cam_h: np.ndarray, tel: Telemetry,
                 offset: float) -> tuple[np.ndarray, dict]:
    """Rotate the camera's horizontal g onto the logger's (lon_g, lat_g).

    Returns ((N,2) [lon_g, lat_g] at the camera's full sample rate, still
    carrying vibration; fit info). The rotation is fitted on 0.25 s-smoothed
    data, and lon_fit/lat_fit are correlations on that smoothed data.
    """
    lt, lon, lat = _logger_g(tel)
    ref = np.column_stack([np.interp(cam_time + offset, lt, lon),
                           np.interp(cam_time + offset, lt, lat)])
    sm = _box(cam_time, cam_h, 0.25)  # fit on smoothed data, not vibration
    best = None
    for flip in (1.0, -1.0):
        src = sm * np.array([1.0, flip])
        u, _, vt = np.linalg.svd(src.T @ ref)
        rot = u @ vt
        if np.linalg.det(rot) < 0:  # keep a pure rotation; flip covers mirroring
            u[:, -1] *= -1
            rot = u @ vt
        err = float(np.linalg.norm(src @ rot - ref))
        if best is None or err < best[0]:
            best = (err, flip, rot)
    _, flip, rot = best
    car = (cam_h * np.array([1.0, flip])) @ rot
    car_sm = (sm * np.array([1.0, flip])) @ rot
    info = {
        "angle": float(np.degrees(np.arctan2(rot[1, 0], rot[0, 0]))),
        "flip": bool(flip < 0),
        "lon_fit": _corr(car_sm[:, 0], ref[:, 0]),
        "lat_fit": _corr(car_sm[:, 1], ref[:, 1]),
    }
    return car, info


class NoCameraData(ValueError):
    """The video has no GoPro accelerometer track (not a GoPro file, or one
    that was merged/re-encoded by a tool that dropped the gpmd track)."""


def read_accel(parts: list[tuple[str, float]]) -> tuple[np.ndarray, np.ndarray]:
    """Accelerometer for a video made of one or more files.

    `parts` = [(path, start of that file on the joined timeline), ...], e.g.
    GoPro chapters. Returns (times on the joined timeline, (N,3) m/s^2).
    Raises NoCameraData if no file has an accelerometer track.
    """
    from . import gpmf
    ts, vs, missing = [], [], []
    for path, start in parts:
        acc = gpmf.read_sensors(path, keys=("ACCL",)).get("ACCL")
        if acc is None or len(acc.values) == 0:
            missing.append(path)
            continue
        ts.append(acc.time + start)
        vs.append(acc.values)
    if not ts or sum(len(v) for v in vs) < 1000:
        raise NoCameraData(
            "no GoPro sensor data in " + ", ".join(p for p, _ in parts))
    return np.concatenate(ts), np.vstack(vs)


def sync_parts(parts: list[tuple[str, float]], tel: Telemetry):
    """Read a GoPro video's accelerometer (all chapters) and sync it to `tel`.

    Returns (sync result, camera sample times, car-frame g, rotation info).
    """
    t, a = read_accel(parts)
    return sync_streams(t, a, tel)


def sync_video(path: str, tel: Telemetry):
    """sync_parts() for a single file."""
    return sync_parts([(path, 0.0)], tel)


def sync_streams(cam_time: np.ndarray, accel: np.ndarray, tel: Telemetry):
    """sync_video() for accelerometer data that is already decoded."""
    h = camera_horizontal(accel)
    res = find_offset(cam_time, h, tel)
    car, info = to_car_frame(cam_time, h, tel, res.offset)
    return res, cam_time, car, info
