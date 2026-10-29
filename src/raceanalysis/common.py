# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Colours, number formats and small helpers shared by the Race Analysis tabs."""
from __future__ import annotations

from pathlib import Path

from racecore import ingest

# Series colours (fixed order, dark surface); reference = neutral grey.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500"]
REF_COLOR = "#b5b4ad"
GAIN, LOSS, NEUTRAL = "#199e70", "#e66767", "#6b6a64"
LIMIT = "#4fb3bf"   # the grip envelope (dashed), not a lap colour
PURPLE = "#b38cff"
CURSOR = "#f2f1ec"
INK, INK_DIM, INK_FAINT = "#ecebe6", "#a8a79f", "#6b6a64"
PANEL = "#17171a"
MAX_COMPARE = len(SERIES)
FT = 3.28084
MPH = 0.621371


def session_name(path: str) -> str:
    """'Session 2 · Sep 27' for APEX Pro exports, else the file name."""
    lg = ingest.log_info(path) if Path(path).exists() else None
    if lg and lg.session and ingest.APEX_DATE_RE.search(Path(path).name):
        return f"Session {lg.session} · {lg.date:%b %d}"
    return Path(path).stem


def fmt_lap(s: float, decimals: int = 3) -> str:
    m = int(s // 60)
    sec = s - 60 * m
    w = 3 + decimals if decimals else 2
    return f"{m}:{sec:0{w}.{decimals}f}" if m else f"{sec:.{decimals}f}"


def fmt_gap(s: float) -> str:
    return f"{s:+.2f}"
