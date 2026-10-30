# SPDX-License-Identifier: GPL-3.0-or-later
"""Laps tab chart list: saving, loading and naming."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

pytest.importorskip("PySide6")
from raceanalysis import lapcharts  # noqa: E402


def test_load_and_dump():
    assert lapcharts.load("") == lapcharts.DEFAULT
    assert lapcharts.load(None) == lapcharts.DEFAULT
    keys = ["delta", "col:RPM (1/min)", "grip"]
    assert lapcharts.load(lapcharts.dump(keys)) == keys
    assert len(lapcharts.load("|".join(["speed"] * 9))) == lapcharts.MAX_CHARTS
    assert lapcharts.load(["speed", "lat_g"]) == ["speed", "lat_g"]


def test_labels():
    assert lapcharts.label_of("lon_g") == "Brake/accel g"
    assert lapcharts.label_of("col:RPM (1/min)") == "RPM"
    assert lapcharts.label_of("col:Oil temp") == "Oil temp"
    assert lapcharts.y_range("grip") == (0.0, 115.0) and lapcharts.y_range("speed") is None
