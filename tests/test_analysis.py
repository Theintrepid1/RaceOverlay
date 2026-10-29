# SPDX-License-Identifier: GPL-3.0-or-later
"""racecore.analysis on a synthetic track: a rounded rectangle driven for
several laps with braking into each corner; one lap is slower through one
corner only."""
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import analysis  # noqa: E402
from racecore.telemetry import Telemetry  # noqa: E402

LAT0, LON0 = 36.56, -79.20


def _track(n=4000, w=600.0, h=300.0, r=60.0):
    """Closed path (metres) around a rounded rectangle, evenly spaced."""
    pts = []
    for cx, cy, a0 in [(w - r, r, -90), (w - r, h - r, 0), (r, h - r, 90), (r, r, 180)]:
        for a in np.radians(np.linspace(a0, a0 + 90, 40)):
            pts.append((cx + r * np.cos(a), cy + r * np.sin(a)))
    p = np.array(pts + [pts[0]])
    s = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))])
    u = np.linspace(0, s[-1], n, endpoint=False)
    return np.column_stack([np.interp(u, s, p[:, 0]), np.interp(u, s, p[:, 1])]), s[-1]


def _speed_profile(path, slow_corner=None, slow=0.0):
    """m/s along the path: fast on straights, slower in corners."""
    xy, L = path
    d = np.linspace(0, L, len(xy), endpoint=False)
    turn = np.zeros(len(xy))
    seg = np.diff(np.vstack([xy, xy[:1]]), axis=0)
    hd = np.unwrap(np.arctan2(seg[:, 1], seg[:, 0]))
    curv = np.abs(np.gradient(hd))
    k = np.ones(120) / 120
    turn = np.convolve(np.concatenate([curv, curv, curv]), k, "same")[len(xy):2 * len(xy)]
    turn = turn / turn.max()
    v = 45.0 - 25.0 * turn
    if slow_corner is not None:
        v -= slow * np.exp(-0.5 * ((d - _corner_mids(path)[slow_corner]) / 40.0) ** 2)
    return d, v


def _corner_mids(path):
    """Distance of the sharpest-turning point in each quarter of the lap."""
    xy, L = path
    seg = np.diff(np.vstack([xy, xy[:1]]), axis=0)
    hd = np.unwrap(np.arctan2(seg[:, 1], seg[:, 0]))
    curv = np.convolve(np.abs(np.gradient(hd)), np.ones(25) / 25, "same")
    d = np.linspace(0, L, len(xy), endpoint=False)
    mids = []
    for q in range(4):  # one corner per quarter of the lap
        sl = slice(q * len(xy) // 4, (q + 1) * len(xy) // 4)
        mids.append(float(d[sl][np.argmax(curv[sl])]))
    return mids


def _session(laps_slow, noise=0.0, seed=0, shift=(0.0, 0.0)):
    """Telemetry for an out-lap, the given flying laps, and an in-lap."""
    rng = np.random.default_rng(seed)
    path = _track()
    xy, L = path
    rows_t, rows_xy, rows_v, rows_lap = [], [], [], []
    t = 0.0
    plan = [("out", None, 0.0)] + [("fly", c, s) for c, s in laps_slow] + [("in", None, 0.0)]
    for n, (kind, c, s) in enumerate(plan):
        d, v = _speed_profile(path, c, s)
        if kind != "fly":
            v = v * 0.6
        dt_seg = np.diff(np.concatenate([d, [L]])) / v
        tt = t + np.concatenate([[0], np.cumsum(dt_seg)[:-1]])
        tq = np.arange(tt[0], tt[-1] + dt_seg[-1], 0.1)
        rows_t.append(tq)
        rows_xy.append(np.column_stack([np.interp(tq, tt, xy[:, 0]),
                                        np.interp(tq, tt, xy[:, 1])]))
        rows_v.append(np.interp(tq, tt, v))
        rows_lap.append(np.full(len(tq), n))
        t = tt[-1] + dt_seg[-1]
    T = np.concatenate(rows_t)
    clean = np.vstack(rows_xy)
    XY = clean + np.array(shift) + rng.normal(0, noise, (len(T), 2))  # GPS noise
    V = np.concatenate(rows_v)
    # accelerometer-like g from the clean path (a real logger measures it)
    lon_g = np.gradient(V, T) / 9.81
    hd = np.unwrap(np.arctan2(*np.gradient(clean, axis=0).T[::-1]))
    lat_g = -V * np.gradient(hd, T) / 9.81  # + = right turn
    lat = LAT0 + XY[:, 1] / 110540.0
    lon = LON0 + XY[:, 0] / (np.cos(np.radians(LAT0)) * 111320.0)
    return Telemetry(time=T, channels={
        "speed": V * 3.6, "lap": np.concatenate(rows_lap).astype(float),
        "gps_lat": lat, "gps_lon": lon, "lat_g": lat_g, "lon_g": lon_g})


def test_lap_times_delta_and_sectors():
    tel = _session([(None, 0), (1, 8.0), (None, 0)], noise=0.5)
    a = analysis.Analysis()
    a.add(tel, "s1.csv")
    timed = a.timed_laps()
    assert len(timed) == 3
    assert a.ref.lap.n in (1, 3)
    slow = next(ld for ld in timed if ld.lap.n == 2)
    gap = slow.time - a.ref.time
    assert gap > 0.3
    # delta at the line equals the lap-time gap (within one grid step of time)
    assert abs(a.delta(slow)[-1] - gap) < 0.1
    for ld in timed:
        assert abs(sum(ld.sectors) - ld.time) < 1e-6
        assert np.all(np.diff(ld.d) >= 0) and ld.d[-1] > 0.98 * a.length
    assert a.theoretical_best() <= a.ref.time + 1e-9
    # the loss is in one corner's stretch, not smeared round the lap
    res = a.corner_results(slow)
    assert len(a.corners) == 4, [(c.n, round(c.apex_d), c.direction) for c in a.corners]
    assert all(c.direction == "L" for c in a.corners)  # anticlockwise track
    worst = max(res, key=lambda r: r.lost)
    where = _corner_mids(_track())[1]  # where lap 2 was slowed down
    assert worst.corner.start_d <= where <= worst.corner.end_d, \
        (where, [(r.corner.n, round(r.corner.start_d), round(r.lost, 3)) for r in res])
    assert worst.lost > 0.9 * gap, [(r.corner.n, round(r.lost, 3)) for r in res]
    assert abs(sum(r.lost for r in res) - a.delta(slow)[-1]) < 0.05


def test_two_sessions_same_track_and_reject_other_track():
    a = analysis.Analysis()
    a.add(_session([(None, 0), (2, 6.0)], noise=0.5, seed=1), "a.csv")
    a.add(_session([(None, 0), (None, 0)], noise=0.5, seed=2, shift=(3.0, -2.0)), "b.csv")
    assert len(a.sessions) == 2 and len(a.timed_laps()) == 4
    # theoretical best can take sectors from different laps / sessions
    assert a.theoretical_best() <= min(ld.time for ld in a.timed_laps()) + 1e-9
    try:
        a.add(_session([(None, 0)], shift=(5000.0, 0.0)), "far.csv")
        raise AssertionError("expected a different-track error")
    except ValueError as e:
        assert "different track" in str(e)


def test_find_video_from_sidecar():
    with tempfile.TemporaryDirectory() as tmp:
        day = Path(tmp, "2026-09-27")
        day.mkdir()
        csv = Path(tmp, "log.csv")
        csv.write_text("x")
        v1, v2 = day / "GH010009.MP4", day / "GH020009.MP4"
        v1.write_bytes(b"x")
        v2.write_bytes(b"x")
        (day / "GH010009.MP4.sync.json").write_text(json.dumps(
            {"csv": str(csv), "parts": [str(v1), str(v2)], "offset": 123.27, "synced": True}))
        got = analysis.find_video(str(csv), [tmp])
        assert got == ([str(v1), str(v2)], 123.27)
        assert analysis.find_video(str(Path(tmp, "other.csv")), [tmp]) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
