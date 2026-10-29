# SPDX-License-Identifier: GPL-3.0-or-later
"""racecore.tracks: map-image alignment, profiles, and named turns in the
analysis."""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from racecore import analysis, tracks  # noqa: E402
from test_analysis import LAT0, LON0, _corner_mids, _session, _track  # noqa: E402


def test_two_point_alignment_round_trip():
    # a "map image" that is the world rotated 37°, scaled 0.4 px/m, shifted, y down
    rng = np.random.default_rng(0)
    world = rng.uniform(-500, 500, (20, 2))
    ang, sc = np.radians(37), 0.4
    rot = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    img = (world @ rot.T) * sc + np.array([600, -450])
    img[:, 1] = -img[:, 1]                         # pixel y grows downward
    a, b = tracks.similarity(img[:2], world[:2])
    back = tracks.apply(a, b, img)
    assert np.abs(back - world).max() < 1e-6
    assert np.abs(tracks.invert(a, b, world) - img).max() < 1e-6
    a2, b2 = tracks.fit_box(1200, 900, world)
    corners = tracks.apply(a2, b2, [[0, 0], [1200, 900]])
    lo, hi = world.min(0), world.max(0)
    assert corners[:, 0].min() <= lo[0] and corners[:, 0].max() >= hi[0]


def test_profile_save_find_and_names():
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "map.png")
        open(img, "wb").write(b"\x89PNG fake")
        p = tracks.TrackProfile("Test Track", LAT0, LON0)
        p.image = img
        p.set_ab(complex(0.5, 0.1), complex(-20, 30))
        p.turns += [tracks.Turn("T1", LAT0 + 0.001, LON0), tracks.Turn("T3", LAT0, LON0 + 0.001)]
        assert p.next_name() == "T4"
        dest = p.save(os.path.join(tmp, "tracks"))
        assert dest.exists() and os.path.exists(p.image) and p.image != img
        near = tracks.find(LAT0 + 0.0005, LON0, os.path.join(tmp, "tracks"))  # ~55 m away
        assert near is not None and near.name == "Test Track" and len(near.turns) == 2
        assert near.ab() == (complex(0.5, 0.1), complex(-20, 30))
        assert tracks.find(LAT0 + 0.01, LON0, os.path.join(tmp, "tracks")) is None  # ~1.1 km
        # another track that happens to have the same name gets its own file
        other = tracks.TrackProfile("Test Track", LAT0 + 1.0, LON0)
        d2 = other.save(os.path.join(tmp, "tracks"))
        assert d2 != dest and dest.exists()
        assert len(tracks.load_all(os.path.join(tmp, "tracks"))) == 2
    assert tracks.name_from_file(
        "APEX_Pro_Data_CSV_from_Virginia_Intl_Raceway_Full_on_09.27_1.2026.csv") \
        == "Virginia Intl Raceway Full"


def test_analysis_uses_named_turns():
    tel = _session([(None, 0), (1, 8.0), (None, 0)], noise=0.5)
    a = analysis.Analysis()
    a.add(tel, "s1.csv")
    # name the four detected corners, plus a "kink" halfway down a straight
    ref = a.ref
    detected = [c.apex_d for c in a.corners]
    assert len(detected) == 4
    pts = [(np.interp(dd, a._ref_s, ref.xy[:, 0]), np.interp(dd, a._ref_s, ref.xy[:, 1]))
           for dd in detected + [0.5 * (detected[0] + detected[1])]]
    ll = tracks.to_latlon(np.array(pts), *a.origin)
    p = tracks.TrackProfile("Synthetic", LAT0, LON0,
                            [tracks.Turn(n, float(la), float(lo)) for n, (la, lo) in
                             zip(["Turn 1", "Turn 2", "Turn 3", "Turn 4", "Kink"], ll)])
    a.use_profile(p)
    names = [c.name for c in a.corners]
    assert len(names) == 5 and "Kink" in names, names
    assert all(np.diff([c.apex_d for c in a.corners]) > 0)
    slow = next(ld for ld in a.timed_laps() if ld.lap.n == 2)
    res = a.corner_results(slow)
    assert abs(sum(r.lost for r in res) - a.delta(slow)[-1]) < 0.05
    worst = max(res, key=lambda r: r.lost)
    xy, L = _track()  # where lap 2 was slowed, placed on the reference path
    slow_pt = xy[int(len(xy) * _corner_mids((xy, L))[1] / L)]
    ll_slow = tracks.to_latlon([slow_pt], LAT0, LON0)[0]
    where = a._path_distance(tracks.to_xy(ll_slow[0], ll_slow[1], *a.origin)[0])
    assert worst.corner.start_d <= where <= worst.corner.end_d, (where, [
        (r.corner.name, round(r.corner.start_d), round(r.corner.end_d), round(r.lost, 3)) for r in res])
    a.use_profile(None)  # back to detected corners
    assert all(c.name.startswith("C") for c in a.corners)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_split_turn_names():
    after = tracks.name_after
    assert after("T4") == "T5" and after("4") == "5" and after("Turn 9") == "Turn 10"
    assert after("4A") == "4B" and after("T4a") == "T4b" and after("T12B") == "T12C"
    assert after("4Z") == "5"
    assert after("Oak Tree") is None
    p = tracks.TrackProfile("VIR", LAT0, LON0)
    assert p.next_name() == "T1"
    for name in ("T1", "T2", "T3", "4A"):
        p.turns.append(tracks.Turn(name, LAT0, LON0))
    assert p.next_name() == "4B"
    p.turns.append(tracks.Turn("4B", LAT0, LON0))
    p.turns.append(tracks.Turn("Oak Tree", LAT0, LON0))
    assert p.next_name() == "T5"  # carries on from the highest number


def test_split_turns_get_their_own_stretch():
    tel = _session([(None, 0), (1, 8.0), (None, 0)], noise=0.5)
    a = analysis.Analysis()
    a.add(tel, "s1.csv")
    det = [c.apex_d for c in a.corners]
    ref = a.ref
    # corner 2 placed as a split: 4A 30 m before its apex, 4B 30 m after
    at = det[:1] + [det[1] - 30, det[1] + 30] + det[2:]
    names = ["T1", "4A", "4B", "T5", "T6"]
    pts = [(np.interp(d, a._ref_s, ref.xy[:, 0]), np.interp(d, a._ref_s, ref.xy[:, 1])) for d in at]
    ll = tracks.to_latlon(np.array(pts), *a.origin)
    a.use_profile(tracks.TrackProfile("S", LAT0, LON0,
                                      [tracks.Turn(n, float(la), float(lo))
                                       for n, (la, lo) in zip(names, ll)]))
    got = [(c.name, round(c.apex_d)) for c in a.corners]
    assert [c.name for c in a.corners] == names, got
    c4a, c4b = a.corners[1], a.corners[2]
    assert c4a.end_d == c4b.start_d and c4b.start_d - c4a.apex_d > 25  # split halfway
    slow = next(ld for ld in a.timed_laps() if ld.lap.n == 2)
    res = a.corner_results(slow)
    assert abs(sum(r.lost for r in res) - a.delta(slow)[-1]) < 0.05
