# SPDX-License-Identifier: GPL-3.0-or-later
"""racecore.grip: envelope shape, grip used, and per-turn grip in the
analysis."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from racecore import analysis, grip  # noqa: E402
from test_analysis import _session  # noqa: E402


def _ellipse_cloud(n=20000, lat_max=1.2, brake_max=0.9, accel_max=0.3, seed=0):
    """Samples filling an envelope: 1.2 g cornering, 0.9 g braking, 0.3 g
    accelerating, with most samples well inside it."""
    rng = np.random.default_rng(seed)
    th = rng.uniform(0, 2 * np.pi, n)
    lon_lim = np.where(np.sin(th) >= 0, accel_max, brake_max)
    edge = 1 / np.sqrt((np.cos(th) / lat_max) ** 2 + (np.sin(th) / lon_lim) ** 2)
    r = edge * np.sqrt(rng.uniform(0, 1, n)) ** 0.3   # biased toward the edge
    return r * np.cos(th), r * np.sin(th)


def test_envelope_follows_the_data():
    lat, lon = _ellipse_cloud()
    env = grip.build_envelope(lat, lon)
    right, accel, left, brake = env.at([0, np.pi / 2, np.pi, 3 * np.pi / 2])
    assert 1.05 < right < 1.25 and 1.05 < left < 1.25, (right, left)
    assert 0.78 < brake < 0.95, brake
    assert 0.22 < accel < 0.35, accel
    # on the envelope = 100%, halfway in = 50%, same direction
    assert abs(env.used(right, 0.0) - 1.0) < 1e-9
    assert abs(env.used(0.0, -brake / 2) - 0.5) < 1e-9
    out = env.outline()
    assert np.allclose(out[0], out[-1])


def test_slow_speed_and_gaps():
    lat2, lon2 = _ellipse_cloud(n=4000)
    keep = np.abs(np.mod(np.arctan2(lon2, lat2), 2 * np.pi) - np.pi / 4) > 0.3  # hole near 45°
    env = grip.build_envelope(lat2[keep], lon2[keep])
    assert np.isfinite(env.radius).all()  # the hole is filled from neighbours
    # samples below walking pace don't count
    spd = np.full(lat2.size, 10.0)
    try:
        grip.build_envelope(lat2, lon2, spd)
        raise AssertionError("expected not enough data")
    except ValueError:
        pass


def test_slow_corner_uses_less_grip():
    tel = _session([(None, 0), (1, 8.0), (None, 0)], noise=0.5)
    a = analysis.Analysis()
    a.add(tel, "s1.csv")
    assert a.envelope is not None
    slow = next(ld for ld in a.timed_laps() if ld.lap.n == 2)
    res = a.corner_results(slow)
    worst = max(res, key=lambda r: r.lost)
    g = worst.grip
    assert g is not None and g.used is not None and g.used_ref is not None
    assert g.used < g.used_ref - 0.05, g          # used less of the grip there
    others = [r.grip for r in res if r is not worst and r.grip and r.grip.used]
    assert all(abs(o.used - o.used_ref) < 0.05 for o in others), others
    assert a.lap_grip(slow) < a.lap_grip(a.ref)
    u = a.grip_on_grid(a.ref)
    assert np.isnan(u).any() and np.nanmax(u) > 0.9   # straights left out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
