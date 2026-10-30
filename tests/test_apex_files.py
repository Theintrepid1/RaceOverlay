# SPDX-License-Identifier: GPL-3.0-or-later
"""Real APEX Pro exports (trimmed to the first minute) and the three ways
the app names them."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import day, engine, ingest, telemetry, tracks  # noqa: E402

DATA = Path(__file__).parent / "data"
SPACES = "APEX Pro Data (CSV) from Virginia Int'l Raceway (Full) on 09.27 (1).2026.csv"
UNDERSCORES = "APEX_Pro_Data_CSV_from_Virginia_Intl_Raceway_Full_on_09.27_1.2026.csv"
BY_ID = "Virginia_Intl_Raceway_Full__812128378.csv"


def test_names_with_dates():
    for name in (SPACES, UNDERSCORES):
        lg = ingest.log_info(name)
        assert (lg.date, lg.session, lg.guessed) == (dt.date(2026, 9, 27), 1, False), name
    lg = ingest.log_info("APEX Pro Data (CSV) from CMP on 10.12.2026.csv")
    assert (lg.date, lg.session, lg.guessed) == (dt.date(2026, 10, 12), None, False)
    assert ingest.log_id(SPACES) is None


def test_id_name_has_no_date():
    lg = ingest.log_info(str(DATA / BY_ID))
    assert lg.guessed and lg.session is None      # modified time only
    assert ingest.log_id(BY_ID) == "812128378"


def test_track_names():
    assert tracks.name_from_file(UNDERSCORES) == "Virginia Intl Raceway Full"
    assert tracks.name_from_file(SPACES) == "Virginia Int'l Raceway (Full)"
    assert tracks.name_from_file(BY_ID) == "Virginia Intl Raceway Full"
    assert tracks.name_from_file("other.csv") == "Track"


def test_session_names():
    from raceanalysis.common import session_name
    assert session_name(str(DATA / SPACES)) == "Session 1 · Sep 27"
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp, BY_ID)
        p.write_bytes((DATA / BY_ID).read_bytes())
        assert session_name(str(p)) == "Log 812128378"
        day.Notes(start="2026-09-27 10:42", start_source="gps").save(str(p))
        assert session_name(str(p)) == "Sep 27 · 10:42"
        long = Path(tmp, "a really long logger export file name from somewhere.csv")
        long.write_text("x")
        assert len(session_name(str(long))) <= 28


def test_guessed_dates_stay_candidates():
    """A log dated only by its modified time (today, after copying) must
    still be tried against a clip from last month."""
    clip = ingest.Clip("0009", "GH", ["x"], 0, dt.datetime(2026, 9, 27, 10, 0))
    named = ingest.Log("a.csv", dt.date(2026, 9, 27), 1)
    other_day = ingest.Log("b.csv", dt.date(2026, 10, 12), 1)
    guessed = ingest.Log("c.csv", dt.date(2026, 10, 30), guessed=True)
    got = {lg.path for lg in ingest.candidates(clip, [named, other_day, guessed])}
    assert got == {"a.csv", "c.csv"}
    # only guessed logs: try them all
    assert len(ingest.candidates(clip, [guessed, ingest.Log("d.csv", None, guessed=True)])) == 2


def test_both_exports_load_the_same_and_ignore_empty_engine_columns():
    a = telemetry.load(str(DATA / SPACES))
    b = telemetry.load(str(DATA / BY_ID))
    assert a.source_columns == b.source_columns
    for t in (a, b):
        assert t.device == "apex"
        assert t.has("speed") and t.has("lat_g") and t.has("lon_g")
        # APEX writes RPM and TPS columns of zeros without the OBD dongle
        assert not t.has("rpm") and not t.has("throttle")
        assert engine.channels(t) == []
