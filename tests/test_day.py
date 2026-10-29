# SPDX-License-Identifier: GPL-3.0-or-later
"""Track day: notes, GPS time, weather lookup, grip by session and the
'everything moved vs a few turns moved' comparison."""
import datetime as dt
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from racecore import analysis, day, gpmf  # noqa: E402
from test_analysis import _session  # noqa: E402


def test_notes_roundtrip(tmp_path):
    csv = tmp_path / "s1.csv"
    csv.write_text("x")
    n = day.Notes(start="2026-09-27 10:05", start_source="typed", pressures_hot="32/32/30/30",
                  changes="+1 rear bar", weather={"air_c": 21.5})
    n.save(str(csv))
    assert (tmp_path / "s1.csv.notes.json").exists()
    back = day.Notes.load(str(csv))
    assert back == n
    assert back.start_dt() == dt.datetime(2026, 9, 27, 10, 5)
    assert day.Notes.load(str(tmp_path / "missing.csv")) == day.Notes()


def _klv(key, typ, size, payload, repeat=1):
    pad = (-len(payload)) % 4
    return struct.pack(">4scBH", key.encode(), typ.encode(), size, repeat) + payload + b"\0" * pad


def test_gps_time_needs_a_fix():
    def packet(fix):
        strm = _klv("GPSF", "L", 4, struct.pack(">I", fix)) + \
            _klv("GPSU", "U", 16, b"260927141530.250")
        devc = _klv("STRM", "\0", 1, strm, repeat=len(strm))
        return _klv("DEVC", "\0", 1, devc, repeat=len(devc))
    assert gpmf._gps_time_in([(0.0, 1.0, packet(0))]) is None
    t, utc = gpmf._gps_time_in([(0.0, 1.0, packet(0)), (1.0, 1.0, packet(3))])
    assert t == 1.0
    assert utc == dt.datetime(2026, 9, 27, 14, 15, 30, 250000, tzinfo=dt.timezone.utc)
    assert day.local_from_utc(utc.isoformat(), -4 * 3600) == "2026-09-27 10:15"


def test_weather_url_and_lookup():
    u = day.weather_url(36.5678, -79.2082, dt.date(2026, 9, 27), today=dt.date(2026, 10, 29))
    assert u.startswith(day.FORECAST_URL) and "start_date=2026-09-27" in u
    old = day.weather_url(36.5678, -79.2082, dt.date(2025, 9, 27), today=dt.date(2026, 10, 29))
    assert old.startswith(day.ARCHIVE_URL)
    resp = {"utc_offset_seconds": -14400, "hourly": {
        "time": ["2026-09-27T10:00", "2026-09-27T11:00"],
        "temperature_2m": [20.0, 24.0], "cloud_cover": [100, 0],
        "relative_humidity_2m": [None, 50]}}
    w = day.weather_at(resp, dt.datetime(2026, 9, 27, 10, 30))
    assert w["air_c"] == 22.0 and w["cloud"] == 50.0 and w["humidity"] == 50.0
    assert "sun_wm2" not in w and w["source"] == "Open-Meteo"


def test_compare_sessions():
    names = ["T1", "T2", "T3", "T4", "T5", "T6"]
    s1 = [1.00, 1.05, 0.95, 1.10, 1.00, 0.98]
    # everything up ~0.05 g: car / track / conditions
    s2 = [x + 0.05 + e for x, e in zip(s1, [0.0, 0.01, -0.01, 0.0, 0.005, -0.005])]
    # two turns way up, the rest the same: driving
    s3 = [x + (0.12 if n in ("T3", "T5") else 0.0) for x, n in zip(s1, names)]
    rows = [[a, b, c] for a, b, c in zip(s1, s2, s3)]
    v = day.compare_sessions(names, rows, 0, 1)
    assert v.shift > 0.04 and v.agree == 1.0 and "car, tires, track" in v.text
    v = day.compare_sessions(names, rows, 0, 2)
    assert abs(v.shift) < 0.01 and v.outliers == ["T3", "T5"] and "driving" in v.text
    assert day.compare_sessions(names[:2], rows[:2], 0, 1) is None


def test_grip_by_session():
    a = analysis.Analysis()
    a.add(_session([(None, 0), (None, 0)], noise=0.3), "s1.csv", name="s1")
    tel = _session([(None, 0), (None, 0)], noise=0.3, seed=1)
    tel.channels["lat_g"] = tel.channels["lat_g"] * 1.1      # 10% more cornering g
    a.add(tel, "s2.csv", name="s2")
    corners, rows = day.turn_capacity(a)
    assert len(rows) == len(corners) and all(len(r) == 2 for r in rows)
    ups = [r[1] > r[0] for r in rows if None not in r]
    assert ups and np.mean(ups) >= 0.75
    peaks = day.lap_peaks(a)
    assert len(peaks) == 2 and all(len(p) == 2 for p in peaks)
    assert np.mean([p[1] for p in peaks[1]]) > np.mean([p[1] for p in peaks[0]])


def test_session_envelope():
    from types import SimpleNamespace as NS
    rng = np.random.default_rng(3)

    def lap(si, k):
        th = rng.uniform(0, 2 * np.pi, 3000)
        r = k * rng.uniform(0, 1, 3000) ** 0.3
        return NS(session=si, timed=True, lat_g=r * np.cos(th), lon_g=r * np.sin(th),
                  v=np.full(3000, 100.0))
    a = NS(laps=[lap(0, 1.0), lap(0, 1.0), lap(1, 1.15), lap(1, 1.15)])
    e1, e2 = (day.envelope_summary(day.session_envelope(a, i)) for i in (0, 1))
    assert 0.9 < e1["corner"] < 1.05 and 0.9 < e1["brake"] < 1.05
    assert abs(e2["mean"] / e1["mean"] - 1.15) < 0.03
    assert day.session_envelope(NS(laps=[]), 0) is None
