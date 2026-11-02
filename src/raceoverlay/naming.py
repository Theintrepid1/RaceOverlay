# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Export file names from what's known about the recording:

    2026-09-27 VIR S1.mp4                 whole video
    2026-09-27 VIR S1 lap5 2'19.806.mp4   one file per lap
    2026-09-27 VIR S1 laps 2-4,6.mp4      chosen laps in one file

Any part that isn't known is left out. With no date and no track the
video's own name stands in for them, so a name is never empty.
"""
from __future__ import annotations

import datetime as dt
import re

BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')     # not allowed in Windows names


def safe(name: str) -> str:
    name = BAD.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name or "render"


def short_track(name: str | None) -> str:
    """'VIR Full Course' -> 'VIR'; names without a leading abbreviation stay
    whole ('Carolina Motorsports Park')."""
    name = (name or "").strip()
    if not name:
        return ""
    first = name.split()[0]
    if len(first) >= 2 and first.isupper() and first.isalpha():
        return first
    return name


def fmt_laptime(sec: float) -> str:
    """139.806 -> 2'19.806"""
    m, s = divmod(max(0.0, float(sec)), 60)
    s = round(s, 3)
    if s >= 60:                     # 59.9996 rounds up to the next minute
        m, s = m + 1, s - 60
    return f"{int(m)}'{s:06.3f}"


def lap_list(ns) -> str:
    """[2, 3, 4, 6] -> '2-4,6' (runs of three or more joined with a dash)."""
    ns = sorted(set(int(n) for n in ns))
    out, i = [], 0
    while i < len(ns):
        j = i
        while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1:
            j += 1
        if j - i >= 2:
            out.append(f"{ns[i]}-{ns[j]}")
        else:
            out.extend(str(n) for n in ns[i:j + 1])
        i = j + 1
    return ",".join(out)


def base(date: dt.date | None, track: str | None, session: int | None,
         fallback: str) -> str:
    parts = []
    if date is not None:
        parts.append(date.isoformat())
    t = short_track(track)
    if t:
        parts.append(t)
    if not parts:
        parts.append(fallback)
    if session:
        parts.append(f"S{session}")
    return " ".join(parts)


def whole(b: str) -> str:
    return safe(b) + ".mp4"


def lap(b: str, n: int, seconds: float) -> str:
    return safe(f"{b} lap{n} {fmt_laptime(seconds)}") + ".mp4"


def laps(b: str, ns) -> str:
    ns = list(ns)
    word = "lap" if len(ns) == 1 else "laps"
    return safe(f"{b} {word} {lap_list(ns)}") + ".mp4"
