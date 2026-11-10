# SPDX-License-Identifier: GPL-3.0-or-later
"""VBO files (Racelogic's format, here a RaceChrono Pro export from VIR,
trimmed to about one lap either side of the event's timing line)."""
import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import analysis, day, ingest, laps, telemetry, tracks  # noqa: E402

VBO = str(Path(__file__).parent / "data" / "racechrono_vir_trimmed.vbo")


def test_reads_positions_time_speed_and_g():
    t = telemetry.load(VBO)
    assert t.device == "vbo"
    lat, lon = t.channels["gps_lat"], t.channels["gps_lon"]
    # minutes -> degrees, and longitude is positive WEST in the file
    assert 36.54 < np.nanmin(lat) and np.nanmax(lat) < 36.58
    assert -79.22 < np.nanmin(lon) and np.nanmax(lon) < -79.19
    assert abs(np.median(np.diff(t.time)) - 0.1) < 1e-6 and t.time[0] == 0
    assert 100 < np.nanmax(t.channels["speed"]) < 200          # km/h
    assert t.checks["lat_g"]["verified"] and t.checks["lon_g"]["verified"]
    assert t.checks["lat_g"]["flipped"] and not t.checks["lon_g"]["flipped"]
    # the zero rows RaceChrono pads the end with are dropped
    assert np.all(np.abs(lat) > 1)
    assert t.meta["created"] == "2021-03-14"
    assert t.meta["session_name"] == "VIR Full South Paddock Timing"


def test_laps_from_the_files_own_line():
    t = telemetry.load(VBO)
    assert t.mapping["lap"] == "Lap" and "Lap (VIR Full Course line)" in t.source_columns
    lm = laps.LapModel(t)
    timed = [l for l in lm.laps if l.timed]
    assert len(timed) == 1
    assert abs(timed[0].duration - 146.70) < 0.15               # RaceChrono: 2:26.7
    # the other line is there to pick
    t2 = telemetry.load(VBO, overrides={"lap": "Lap (VIR Full Course line)"})
    assert laps.LapModel(t2).ok


def test_gate_is_a_point_and_a_direction():
    t = telemetry.load(VBO)
    lat, lon = t.channels["gps_lat"], t.channels["gps_lon"]
    start = next(g for k, g in t.meta["vbo_lines"] if k == "Start")
    fwd = telemetry.gate_crossings(lat, lon, start, min_gap=1)
    back = telemetry.gate_crossings(lat, lon, (start[0], start[1], 2 * start[0] - start[2],
                                               2 * start[1] - start[3]), min_gap=1)
    assert len(fwd) == 2 and len(back) == 0                    # wrong way: no laps


def test_track_found_by_its_path():
    """The event timed laps 1.6 km from VIR's usual line: the track is found
    by the driven path instead of the start point."""
    t = telemetry.load(VBO)
    assert tracks.find(float(t.channels["gps_lat"][0]), float(t.channels["gps_lon"][0])) is None
    found = tracks.start_on_path(t.channels["gps_lat"], t.channels["gps_lon"])
    assert found is not None and found[0].key == "vir_full"


def test_mixing_recounts_laps_from_the_first_files_line():
    """Same track, laps counted from different lines: the second file's laps
    are counted again from the first file's line."""
    sys.path.insert(0, os.path.dirname(__file__))
    from test_crossings import _circle
    a = analysis.Analysis()
    a.add(_circle(period=60.0), "first.csv")
    other = _circle(period=61.0)
    # its logger counted laps from another point on the circle
    ang = 2 * np.pi * (other.time / 61.0) - np.pi / 2
    other.channels["lap"] = np.floor((ang + np.pi / 2) / (2 * np.pi)).astype(float)
    s = a.add(other, "second.csv")
    assert "re-counted" in s.tel.meta["lap_line"]
    t2 = [ld.time for ld in a.laps if ld.timed and ld.session == 1]
    assert t2 and all(abs(x - 61.0) < 0.05 for x in t2)
    # a different track entirely is still refused
    far = _circle(period=60.0)
    far.channels["gps_lat"] = far.channels["gps_lat"] + 0.5
    try:
        a.add(far, "elsewhere.csv")
        raise AssertionError("should have been refused")
    except ValueError as e:
        assert "different track" in str(e)


def test_import_date_track_name_and_start_time():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp, "VIR_TT_Sun_1.vbo")
        shutil.copy(VBO, p)
        lg = ingest.log_info(str(p))
        assert lg.date == dt.date(2021, 3, 14) and not lg.guessed
        assert [Path(x.path).name for x in ingest.find_logs(tmp)] == ["VIR_TT_Sun_1.vbo"]
        assert tracks.name_from_file(str(p)) == "VIR Full South Paddock Timing"
    t = telemetry.load(VBO)
    utc = day.log_start_utc(t)
    assert utc.date() == dt.date(2021, 3, 14) and (utc.hour, utc.minute) == (17, 46)
    local, src, _iso = day.find_start(None, 36.56, -79.2, tel=t)
    assert src == "gps" and local.startswith("2021-03-14 12:46")
