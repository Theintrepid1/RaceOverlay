# SPDX-License-Identifier: GPL-3.0-or-later
"""AiM exports from a MyChron (and other AiM loggers without a lap channel)
keep the lap ends in the header's Beacon Markers row."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import telemetry  # noqa: E402
from racecore.laps import LapModel  # noqa: E402


def _mychron_csv(path, marks, t_end=200.0, start=0.0):
    t = np.arange(start, start + t_end, 0.05)
    lines = ['"Format","AiM CSV File"', '"Venue","Kart track"', '"Sample Rate","20"',
             '"Segment","Session"',
             '"Beacon Markers",' + ",".join(f'"{m:.3f}"' for m in marks),
             '"Segment Times","0:50.000","0:45.000","0:46.000"', "",
             '"Time","GPS Speed","GPS LatAcc","GPS LonAcc","GPS Latitude","GPS Longitude",'
             '"RPM","Water Temp"',
             '"s","mph","g","g","deg","deg","rpm","°F"', '"1","2","3","4","5","6","7","8"', ""]
    ang = 2 * np.pi * (t - start) / 45.0                     # a lap every 45 s
    for k, x in enumerate(t):
        lines.append(",".join(f'"{v:.7f}"' for v in (
            x, 40 + 10 * np.sin(ang[k] * 3), 1.2 * np.sin(ang[k]), 0.3 * np.cos(ang[k] * 2),
            35.5 + 0.001 * np.sin(ang[k]), -82.5 + 0.001 * np.cos(ang[k]),
            9000 + 2000 * np.sin(ang[k] * 3), 120 + x / 100)))
    path.write_text("\n".join(lines) + "\n", encoding="latin-1")


def test_beacon_markers_make_the_laps(tmp_path):
    p = tmp_path / "mychron.csv"
    _mychron_csv(p, [50.0, 95.0, 141.0])
    tel = telemetry.load(str(p))
    assert tel.meta["lap_source"] == "AiM beacon markers"
    assert {"speed", "rpm", "lat_g", "lon_g", "gps_lat", "lap"} <= \
        {r for r in ("speed", "rpm", "lat_g", "lon_g", "gps_lat", "lap") if tel.has(r)}
    lm = LapModel(tel)
    timed = [(lap.n, lap.duration) for lap in lm.laps if lap.timed]
    # (the edges then move to the GPS line crossing, by hundredths here)
    assert [n for n, _d in timed] == [1, 2]
    assert np.allclose([d for _n, d in timed], [45.0, 46.0], atol=0.1)
    assert tel.has("col:Water Temp")


def test_markers_follow_the_files_own_clock(tmp_path):
    p = tmp_path / "mychron.csv"
    _mychron_csv(p, [1050.0, 1095.0, 1141.0], start=1000.0)   # time doesn't start at 0
    lm = LapModel(telemetry.load(str(p)))
    assert np.allclose([lap.duration for lap in lm.laps if lap.timed], [45.0, 46.0], atol=0.1)


def test_lap_column_wins_over_markers(tmp_path):
    p = tmp_path / "aim.csv"
    _mychron_csv(p, [50.0, 95.0, 141.0])
    text = p.read_text(encoding="latin-1").replace('"Water Temp"', '"Lap"')
    p.write_text(text, encoding="latin-1")
    tel = telemetry.load(str(p))
    assert "lap_source" not in tel.meta
