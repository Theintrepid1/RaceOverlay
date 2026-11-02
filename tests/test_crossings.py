# SPDX-License-Identifier: GPL-3.0-or-later
"""Lap edges placed between samples where the car actually crossed the line,
not on the first 10 Hz sample after it."""
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import laps  # noqa: E402
from racecore.telemetry import Telemetry, load  # noqa: E402

LAT0, LON0 = 36.56, -79.20


def _circle(period=63.37, laps_n=5, r=300.0, hz=10.0, stale_every=0, noise=0.0, seed=1):
    """Constant speed round a circle; the line is at angle 0 (east side,
    driving north). The logger's lap number flips on the first sample past
    it, like a real one."""
    rng = np.random.default_rng(seed)
    t = np.arange(0.0, period * laps_n + 5.0, 1.0 / hz)
    ang = 2 * np.pi * (t / period) - np.pi / 2          # start a quarter lap early
    x, y = r * np.cos(ang), r * np.sin(ang)
    x += rng.normal(0, noise, len(t))
    y += rng.normal(0, noise, len(t))
    lap = np.floor((ang + np.pi / 2) / (2 * np.pi) + 0.25).astype(float)
    if stale_every:                                   # logger repeats the last fix
        for i in range(stale_every, len(t), stale_every):
            x[i], y[i] = x[i - 1], y[i - 1]
    lat = LAT0 + y / 110540.0
    lon = LON0 + x / (np.cos(np.radians(LAT0)) * 111320.0)
    v = np.full(len(t), 2 * np.pi * r / period * 3.6)
    return Telemetry(time=t, channels={"speed": v, "lap": lap, "gps_lat": lat,
                                       "gps_lon": lon})


def test_flying_laps_are_exact_not_rounded_to_samples():
    tel = _circle()
    lm = laps.LapModel(tel)
    assert lm.interpolated
    timed = [l for l in lm.laps if l.timed]
    assert len(timed) >= 3
    for l in timed:
        assert abs(l.duration - 63.37) < 0.005, l.duration
    # each lap starts where the previous one ended
    for a, b in zip(lm.laps, lm.laps[1:]):
        assert a.end == b.start


def test_stale_fixes_and_gps_noise():
    tel = _circle(period=71.234, stale_every=7, noise=0.3)
    lm = laps.LapModel(tel)
    for l in (l for l in lm.laps if l.timed):
        assert abs(l.duration - 71.234) < 0.03, l.duration


def test_without_gps_keeps_logger_edges():
    tel = _circle()
    del tel.channels["gps_lat"], tel.channels["gps_lon"]
    lm = laps.LapModel(tel)
    assert not lm.interpolated
    for l in lm.laps:
        assert abs(l.start * 10 - round(l.start * 10)) < 1e-6   # on a sample


def test_real_apex_logs_move_edges_by_less_than_a_sample():
    data = Path(__file__).parent / "data"
    for p in data.glob("*.csv"):
        tel = load(str(p))
        lm = laps.LapModel(tel)
        if len(lm.laps) < 2:
            continue
        for l in lm.laps[1:]:
            i = l.i0
            assert tel.time[i] - 0.15 <= l.start <= tel.time[i] + 1e-9
