# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""What the Laps tab's charts can show: the built-in traces plus any
engine/OBD channel in the loaded files (the same list the Engine tab uses,
so empty columns like APEX's RPM/TPS without a dongle never appear).

A chart is named by a key: "delta", "speed", "lon_g", "lat_g", "comb_g",
"grip", "gear" or "col:<column name>".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

DEFAULT = ["delta", "speed", "lon_g", "grip"]
MAX_CHARTS = 6
MIN_CHARTS = 1

BUILTIN = {
    "delta": "Delta",
    "speed": "Speed",
    "lon_g": "Brake/accel g",
    "lat_g": "Lateral g",
    "comb_g": "Combined g",
    "grip": "Grip used",
    "gear": "Gear",
}


@dataclass
class Choice:
    key: str
    label: str          # for the picker and "no X in this file"
    axis: str           # left-axis title


def _has_g(a) -> bool:
    return any(ld.lat_g is not None and ld.lon_g is not None for ld in a.laps)


def choices(w) -> list[Choice]:
    """Everything that can be charted for what's loaded now."""
    a = w.a
    out = [Choice("delta", "Delta", "Delta (s)"),
           Choice("speed", "Speed", f"Speed ({w.speed_unit()})")]
    if _has_g(a):
        out += [Choice("lon_g", "Brake/accel g", "Brake/accel (g)"),
                Choice("lat_g", "Lateral g", "Lateral (g)"),
                Choice("comb_g", "Combined g", "Combined (g)")]
        if a.envelope is not None:
            out.append(Choice("grip", "Grip used", "Grip used (%)"))
    e = getattr(w, "engine", None)
    if e is not None:
        if any(g is not None for g in e.gears):
            out.append(Choice("gear", "Gear", "Gear"))
        for c in e.channels:
            unit = f" ({c.unit})" if c.unit else ""
            out.append(Choice("col:" + c.col, c.label, c.label + unit))
    return out


def label_of(key: str) -> str:
    """A name for a key even when it isn't in the loaded files."""
    if key in BUILTIN:
        return BUILTIN[key]
    col = key[4:] if key.startswith("col:") else key
    return re.sub(r"\s*\(.*\)\s*$", "", col).strip() or col


def values(w, key: str, ld) -> np.ndarray | None:
    """key for lap ld on the analysis grid, None if that lap's file
    doesn't have it."""
    a = w.a
    if key == "delta":
        return np.zeros_like(a.grid, dtype=float) if ld is a.ref else a.delta(ld)
    if key == "speed":
        v = a.channel_on_grid(ld, "speed")
        return None if v is None else w.spd(v)
    if key in ("lon_g", "lat_g"):
        return a.channel_on_grid(ld, key)
    if key == "comb_g":
        lat, lon = a.channel_on_grid(ld, "lat_g"), a.channel_on_grid(ld, "lon_g")
        return None if lat is None or lon is None else np.hypot(lat, lon)
    if key == "grip":
        u = a.grip_on_grid(ld)
        return None if u is None else u * 100
    e = getattr(w, "engine", None)
    if e is None:
        return None
    if key == "gear":
        return e.gear_on_grid(ld)
    if key.startswith("col:"):
        return e.on_grid(ld, key[4:])
    return None


def y_range(key: str) -> tuple[float, float] | None:
    """Fixed vertical range for charts that have a natural one."""
    return {"grip": (0.0, 115.0)}.get(key)


def ref_lines(key: str) -> list[float]:
    """Horizontal guide lines (dashed)."""
    return {"grip": [100.0]}.get(key, [])


def load(saved) -> list[str]:
    """Saved chart list, cleaned up (falls back to the default four)."""
    if isinstance(saved, str):
        saved = [s for s in saved.split("|") if s]
    keys = [str(k) for k in (saved or []) if k]
    keys = keys[:MAX_CHARTS]
    return keys or list(DEFAULT)


def dump(keys: list[str]) -> str:
    return "|".join(keys)
