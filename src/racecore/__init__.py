# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""racecore — shared data layer for RaceOverlay and RaceAnalysis.

Logger parsing and g-sensor alignment (telemetry), lap and corner detection
(laps), GoPro GPMF telemetry (gpmf), and GoPro-to-logger sync (gosync).
Nothing here imports Qt or PIL, so it is safe for any front end or worker.
"""
from . import telemetry, laps, gpmf  # noqa: F401
