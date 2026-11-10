# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync by hand: the offset from a picked moment, and the readout that tells
you whether it's right."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pyqtgraph")
from racecore.laps import LapModel  # noqa: E402
from racecore.telemetry import Telemetry  # noqa: E402
from raceanalysis.manualsync import offset_for, readout  # noqa: E402


def test_offset_for():
    # the video at 12.0 s shows what the data has at 250.5 s
    assert offset_for(12.0, 250.5) == 238.5
    assert offset_for(30.0, 10.0) == -20.0          # video starts before the data


def test_readout():
    t = np.arange(0, 200, 0.1)
    tel = Telemetry(time=t, channels={
        "speed": np.full(len(t), 160.934), "lat_g": np.full(len(t), -1.2),
        "lon_g": np.where(t < 100, -0.8, 0.3), "lap": np.floor(t / 60)})
    laps = LapModel(tel)
    txt = readout(tel, laps, 70.0, True)
    assert "100 mph" in txt and "lateral 1.20 g" in txt and "braking 0.80 g" in txt
    assert "lap 1 at" in txt
    assert "accel 0.30 g" in readout(tel, None, 150.0, False)
    assert "161 km/h" in readout(tel, None, 150.0, False)
    assert "outside" in readout(tel, laps, 999.0, True)
