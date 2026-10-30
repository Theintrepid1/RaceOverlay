# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""racecore — shared data layer for RaceOverlay and RaceAnalysis.

Logger parsing and g-sensor alignment (telemetry), lap and corner detection
(laps), GoPro GPMF telemetry (gpmf), GoPro-to-logger sync (gosync), and
SD card import with logger pairing (ingest), lap comparison (analysis) and
per-track turn profiles (tracks), grip use (grip) and engine/OBD channels
with gears and shift points (engine), and a track day across sessions
(day: grip by session, notes, weather).
Nothing here imports Qt or PIL, so it is safe for any front end or worker.
"""
from . import osutil  # noqa: F401  (first: Windows console fix below)
from . import telemetry, laps, gpmf, gosync, ingest, analysis, tracks, grip, engine, day  # noqa: F401

osutil.hide_child_consoles()  # Windows only; every process imports racecore
