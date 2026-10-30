# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Value labels on the points of a line chart, without the clutter.

Auto density: every point gets its label while there's room for them side
by side (alternating above and below the points when that makes room);
when they'd still overlap, only the important points (fastest lap,
reference, picked laps...) keep theirs. Hovering any point shows its value.
Labels are re-checked whenever the chart is resized.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg

LABEL_COLOR = "#d8d7d0"
DIM_COLOR = "#a8a79f"
HOVER_PX = 12          # how close the mouse has to be to a point
GAP_PX = 4             # between neighbouring labels


@dataclass
class Point:
    x: float
    y: float
    text: str
    important: bool = False
    color: str | None = None     # label colour; None = the default
    tip: str | None = None       # hover text; None = text
    label: bool = True           # False: hover only, never a label


class PointLabels:
    """Labels for one PlotWidget. Call set() after every redraw (clearing
    the plot removes the items; set() adds them back)."""

    def __init__(self, plot: pg.PlotWidget, above: bool = True, stagger: bool = True):
        self.plot = plot
        self.above = above            # labels above the points (else below)
        self.stagger = stagger        # when tight, alternate above / below
        self.points: list[Point] = []
        self.items: list[pg.TextItem | None] = []
        self.hover = pg.TextItem("", color=LABEL_COLOR, fill=pg.mkBrush(23, 23, 26, 230),
                                 border=pg.mkPen("#3a3a37"), anchor=(0.5, 1.15))
        self.hover.setZValue(100)
        vb = plot.getViewBox()
        vb.sigResized.connect(self._layout)
        vb.sigRangeChanged.connect(self._layout)
        plot.scene().sigMouseMoved.connect(self._moved)

    def set(self, points: list[Point]):
        self.points = list(points)
        for it in self.items:
            if it is not None and it.scene() is not None:
                self.plot.removeItem(it)
        self.items = []
        for p in self.points:
            if not p.label:
                self.items.append(None)
                continue
            it = pg.TextItem(p.text, color=p.color or (LABEL_COLOR if p.important else DIM_COLOR),
                             anchor=(0.5, 1.25) if self.above else (0.5, -0.25))
            it.setPos(p.x, p.y)
            self.plot.addItem(it, ignoreBounds=True)
            self.items.append(it)
        if self.hover.scene() is None:
            self.plot.addItem(self.hover, ignoreBounds=True)
        self.hover.hide()
        self._layout()

    def _px(self) -> tuple[float, float]:
        """Data units per pixel in x and y."""
        vb = self.plot.getViewBox()
        (x0, x1), (y0, y1) = vb.viewRange()
        w, h = max(1.0, vb.width()), max(1.0, vb.height())
        return (x1 - x0) / w, (y1 - y0) / h

    def _layout(self, *_):
        """Every label if each can sit above or below its point without
        touching another label; otherwise the important ones only."""
        shown = [(p, it) for p, it in zip(self.points, self.items) if it is not None]
        if not shown:
            return
        vb = self.plot.getViewBox()
        (x0, _), (_, y1) = vb.viewRange()
        ux, uy = self._px()
        if ux <= 0 or uy <= 0:
            return
        pts = sorted(shown, key=lambda pi: pi[0].x)
        ys = [p.y for p, _ in pts]

        def rect(p, it, up):
            br = it.boundingRect()
            w, h = br.width(), br.height()
            px, py = (p.x - x0) / ux, (y1 - p.y) / uy      # pixels, y down
            top = py - 1.25 * h if up else py + 0.25 * h
            return (px - w / 2 - GAP_PX / 2, top, px + w / 2 + GAP_PX / 2, top + h)

        def hits(a, b):
            return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

        placed, sides, ok = [], {}, True
        for i, (p, it) in enumerate(pts):
            # try the side away from the line first: below a dip, above a peak
            prev_y = ys[i - 1] if i else p.y
            next_y = ys[i + 1] if i + 1 < len(ys) else p.y
            dip = p.y <= min(prev_y, next_y) and p.y < max(prev_y, next_y)
            order = (False, True) if (dip if self.above else not dip) else (True, False)
            if not self.stagger:
                order = (self.above,)
            for up in order:
                r = rect(p, it, up)
                if not any(hits(r, q) for q in placed):
                    placed.append(r)
                    sides[id(it)] = up
                    break
            else:
                ok = False
                break
        for p, it in shown:
            up = sides.get(id(it), self.above) if ok else self.above
            it.setAnchor((0.5, 1.25) if up else (0.5, -0.25))
            it.setVisible(ok or p.important)

    def _moved(self, pos):
        if not self.points:
            return
        vb = self.plot.getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            self.hover.hide()
            return
        m = vb.mapSceneToView(pos)
        ux, uy = self._px()
        if ux <= 0 or uy <= 0:
            return
        d = [np.hypot((p.x - m.x()) / ux, (p.y - m.y()) / uy) for p in self.points]
        i = int(np.argmin(d))
        if d[i] > HOVER_PX:
            self.hover.hide()
            return
        p = self.points[i]
        self.hover.setText(p.tip or p.text)
        self.hover.setPos(p.x, p.y)
        self.hover.show()

