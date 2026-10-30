# SPDX-License-Identifier: GPL-3.0-or-later
"""Day tab entry fields: start time boxes and the tire pressure grid."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from raceanalysis import fields  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_parse_pressures():
    assert fields.parse_pressures("28/28/27.5/27") == [28, 28, 27.5, 27]
    assert fields.parse_pressures("28, 28, 27, 27") == [28, 28, 27, 27]
    assert fields.parse_pressures("") == [None] * 4
    assert fields.parse_pressures("28//27/27") == [28, None, 27, 27]
    assert fields.parse_pressures("about 30 all round") is None
    assert fields.format_pressures([28, None, 27.5, 27]) == "28//27.5/27"
    assert fields.format_pressures([None] * 4) == ""


def test_pressure_grid_round_trip_and_gain():
    g = fields.PressureGrid()
    g.set_values("28/28/27/27", "32.5/31/29.5/30")
    assert g.values() == ("28/28/27/27", "32.5/31/29.5/30")
    assert [lbl.text() for lbl in g.gain] == ["+4.5", "+3.0", "+2.5", "+3.0"]
    # an old free-text note is kept until boxes for that set are filled in
    g.set_values("about 30", "")
    assert g.values() == ("about 30", "") and g.old.isVisibleTo(g)
    g.cold[0].setText("30")
    assert g.values()[0] == "30///"


def test_start_fields():
    s = fields.StartFields()
    s.setText("2026-09-27 10:42")
    assert s.text() == "2026-09-27 10:42" and s.complete()
    assert [f.text() for f in s.fields] == ["2026", "09", "27", "10", "42"]
    s.d.setText("31")
    s.mo.setText("2")                    # Feb 31: not a date
    assert s.text() == "" and not s.complete()
    s.setText("")
    assert s.text() == "" and s.complete()          # blank = unknown, fine
