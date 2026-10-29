# SPDX-License-Identifier: GPL-3.0-or-later
"""Engine channels, held-reading smoothing, gears and shift points."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import engine  # noqa: E402
from racecore.telemetry import Telemetry  # noqa: E402

RATIOS = [125.0, 75.5, 53.2, 41.9]      # RPM per km/h


def _drive(seed=0):
    """A 4-speed car pulling through the gears and braking, twice, at 10 Hz."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, 120, 0.1)
    spd = 40 + 110 * np.abs(np.sin(t / 19))         # 40..150 km/h
    gear = np.ones(len(t), int)
    g = 1
    for i in range(len(t)):
        while g < 4 and spd[i] * RATIOS[g - 1] > 7000:
            g += 1
        while g > 1 and spd[i] * RATIOS[g - 2] < 5000:
            g -= 1
        gear[i] = g
    rpm = spd * np.array(RATIOS)[gear - 1] + rng.normal(0, 20, len(t))
    return t, spd, rpm, gear


def test_channels_skip_gps_and_empty():
    t = np.arange(0, 60, 0.1)
    cols = ["Time (s)", "speed (m/s)", "lat (deg)", "ax (G)", "RPM (1/min)", "TPS (%)",
            "Coolant (°C)", "Oil temp (°C)", "Engine speed (rpm)", "Wideband"]
    raw = {c: np.zeros(len(t)) for c in cols}
    raw["speed (m/s)"] = np.linspace(0, 50, len(t))
    raw["lat (deg)"] = np.linspace(36, 36.01, len(t))
    raw["ax (G)"] = np.sin(t)
    raw["RPM (1/min)"] = 3000 + 1000 * np.sin(t)
    raw["Coolant (°C)"] = np.round(85 + t / 6)
    raw["Engine speed (rpm)"] = 3000 + 900 * np.cos(t)
    raw["Wideband"] = 0.9 + 0.1 * np.sin(t)
    tel = Telemetry(time=t, channels={}, source_columns=cols, raw=raw,
                    units={"RPM (1/min)": "1/min", "Coolant (°C)": "°C"})
    got = engine.channels(tel)
    kinds = [(c.col, c.kind) for c in got]
    # TPS and oil temp are all zeros (no data); GPS / accel are never engine
    assert kinds == [("Engine speed (rpm)", "rpm"), ("RPM (1/min)", "rpm"),
                     ("Coolant (°C)", "coolant"), ("Wideband", "other")]


def test_smooth_holds_keeps_plateaus():
    t = np.arange(0, 10, 0.1)
    real = np.where(t < 5, 100.0, 0.0)            # full throttle then lift
    held = real.copy()
    held[51:53] = 100.0                            # the lift arrives 0.2 s late
    s = engine.smooth_holds(t, held)
    assert np.all(s[:50] == 100.0)                 # plateau stays flat
    assert s[60] == 0.0
    ramp = np.array([0, 1, 2, 3, 4, 4, 4, 5, 6, 6, 7], float)   # 3 holds
    sm = engine.smooth_holds(np.arange(len(ramp)) * 0.1, ramp)
    assert np.all(np.diff(sm) >= 0) and sm[0] == 0 and sm[-1] == 7


def test_update_rate():
    t = np.arange(0, 20, 0.1)
    v = np.floor(t * 4) / 4 * 10      # changes every 0.25 s
    assert abs(engine.update_rate(t, v) - 4.0) < 0.3


def test_gears_and_shifts():
    t, spd, rpm, true = _drive()
    g = engine.find_gearing(rpm, spd)
    assert len(g.ratios) == 4
    for got, want in zip(g.ratios, RATIOS):
        assert abs(got / want - 1) < 0.02
    est = engine.clean_runs(g.gear(rpm, spd), t)
    known = est > 0
    assert known.mean() > 0.9
    assert np.mean(est[known] == true[known]) > 0.98
    sh = engine.shifts(est, rpm, spd, t)
    ups = [s for s in sh if s.to == s.frm + 1]
    assert ups and all(6700 < s.rpm < 7100 for s in ups)


def test_no_gearing_without_data():
    assert not engine.find_gearing(np.zeros(500), np.zeros(500))
    assert engine.Gearing().gear([3000], [80]).tolist() == [0]
