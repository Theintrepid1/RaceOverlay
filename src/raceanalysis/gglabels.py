# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Direction labels for the g-g plots (Corners and Day tabs).

Each label sits just past where the data ends in its own direction, so
"Left turn"/"Right turn" clear the cornering g (usually the widest part)
and "Braking"/"Accel" don't float far out past the shorter longitudinal
data. The view is widened just enough that no label gets cut off at the
plot's edge. Labels are re-placed when the plot is resized, since their
size in g depends on how many pixels a g is.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg

LABEL_COLOR = "#8a8983"
GAP_PX = 6          # between the data and a label
EDGE_PX = 4         # between a label and the plot's edge


def reach(points=(), outlines=(), pct: float = 99.5) -> dict[str, float]:
    """How far the data goes in each direction, in g (positive numbers).

    points:   (lat_g, lon_g) arrays of samples; a high percentile is used so
              one spike doesn't push a label out into empty space
    outlines: (N, 2) arrays (lat, lon) such as grip envelopes, used whole
    Unflipped signs: right = +lat, left = -lat, accel = +lon, brake = -lon.
    """
    out = {"right": 0.0, "left": 0.0, "accel": 0.0, "brake": 0.0}

    def side(v):
        return v[np.isfinite(v) & (v > 0)]

    for lat, lon in points:
        lat = np.asarray(lat, float)
        lon = np.asarray(lon, float)
        for v, key in ((lat, "right"), (-lat, "left"), (lon, "accel"), (-lon, "brake")):
            v = side(v)
            if v.size:
                out[key] = max(out[key], float(np.percentile(v, pct)))
    for o in outlines:
        o = np.asarray(o, float)
        if o.size == 0:
            continue
        for v, key in ((o[:, 0], "right"), (-o[:, 0], "left"),
                       (o[:, 1], "accel"), (-o[:, 1], "brake")):
            v = side(v)
            if v.size:
                out[key] = max(out[key], float(v.max()))
    return out


class GGLabels:
    """The four direction labels on one g-g PlotWidget. Call set() after
    every redraw (plot.clear() removes the items; set() adds them back)."""

    TEXT = {"left": "Left turn", "right": "Right turn", "accel": "Accel", "brake": "Braking"}

    def __init__(self, plot: pg.PlotWidget, which=("left", "right", "accel", "brake")):
        self.plot = plot
        self.which = tuple(which)
        self.items = {k: pg.TextItem(self.TEXT[k], color=LABEL_COLOR) for k in self.which}
        self.reach: dict[str, float] = {}
        self.k = (1.0, 1.0)
        self.R = 1.0
        self._placing = False
        plot.getViewBox().sigResized.connect(self.place)

    def set(self, reach: dict[str, float], kx: float, ky: float, R: float):
        """reach from reach(); kx/ky = -1 for a flipped axis; R = the half
        width the plot would show without labels."""
        self.reach, self.k, self.R = reach, (kx, ky), R
        for it in self.items.values():
            if it.scene() is None:
                self.plot.addItem(it, ignoreBounds=True)
        self.place()

    def _pos(self, key: str) -> tuple[float | None, float | None]:
        """Which way this label's direction points on screen: (x sign, None)
        for the lateral labels, (None, y sign) for braking/accel."""
        kx, ky = self.k
        sx = {"right": kx, "left": -kx}.get(key)
        sy = {"accel": ky, "brake": -ky}.get(key)
        return sx, sy

    def place(self, *_):
        if self._placing or not self.reach:
            return
        self._placing = True
        try:
            vb = self.plot.getViewBox()
            w, h = vb.width(), vb.height()
            if w < 10 or h < 10:
                return
            # a few passes: the label sizes in g depend on the range chosen
            X = Y = self.R
            for _ in range(3):
                gx = 2 * X / w            # g per pixel (aspect is locked)
                gy = 2 * Y / h
                g_px = max(gx, gy)
                X = Y = self.R
                for key, it in self.items.items():
                    br = it.boundingRect()
                    tw, th = br.width() * g_px, br.height() * g_px
                    r = self.reach.get(key, 0.0) + GAP_PX * g_px
                    if key in ("left", "right"):
                        X = max(X, r + tw + EDGE_PX * g_px)
                    else:
                        Y = max(Y, r + th + EDGE_PX * g_px)
                X = Y = max(X, Y)
            g_px = max(2 * X / w, 2 * Y / h)
            for key, it in self.items.items():
                sx, sy = self._pos(key)
                r = self.reach.get(key, 0.0) + GAP_PX * g_px
                if sx is not None:      # text runs outward from just past the data
                    it.setAnchor((0.0 if sx > 0 else 1.0, 1.0))
                    it.setPos(sx * r, 0.0)
                else:
                    it.setAnchor((0.5, 1.0 if sy > 0 else 0.0))
                    it.setPos(0.0, sy * r)
            vb.setRange(xRange=(-X, X), yRange=(-Y, Y), padding=0)
        finally:
            self._placing = False
