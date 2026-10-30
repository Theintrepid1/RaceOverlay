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
    """Short name for a session, from what's known about when it ran:
    'Session 2 · Sep 27' (APEX name with date and number), 'Sep 27 · 10:42'
    (start time from the GoPro's GPS or typed on the Day tab), 'Log 812128378'
    (APEX ID-style name, time not known yet), else the file name, shortened."""
    from racecore import day
    exists = Path(path).exists()
    lg = ingest.log_info(path)
    if lg.session and not lg.guessed:
        return f"Session {lg.session} · {lg.date:%b} {lg.date.day}"
    when = day.Notes.load(path).start_dt() if exists else None
    if when is not None:
        return f"{when:%b} {when.day} · {when:%H:%M}"
    if not lg.guessed and lg.date:
        return f"{lg.date:%b} {lg.date.day}"
    lid = ingest.log_id(path)
    if lid:
        return f"Log {lid}"
    return elide(Path(path).stem, 28)


def elide(text: str, n: int) -> str:
    """Cut text to n characters, ending in '…'."""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def fmt_lap(s: float, decimals: int = 3) -> str:
    m = int(s // 60)
    sec = s - 60 * m
    w = 3 + decimals if decimals else 2
    return f"{m}:{sec:0{w}.{decimals}f}" if m else f"{sec:.{decimals}f}"


def fmt_gap(s: float) -> str:
    return f"{s:+.2f}"


def cap_columns(table, max_px: int = 190):
    """Size columns to their contents, but no column wider than max_px.
    Cut-off cells end in '…' and show their full text on hover, and the
    table scrolls sideways instead of widening the panel it sits in."""
    from PySide6 import QtCore
    table.resizeColumnsToContents()
    table.setTextElideMode(QtCore.Qt.ElideRight)
    table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
    fm = table.fontMetrics()
    for col in range(table.columnCount()):
        if table.columnWidth(col) <= max_px:
            continue
        table.setColumnWidth(col, max_px)
        for row in range(table.rowCount()):
            it = table.item(row, col)
            if it is not None and not it.toolTip() and \
                    fm.horizontalAdvance(it.text()) > max_px - 12:
                it.setToolTip(it.text())
