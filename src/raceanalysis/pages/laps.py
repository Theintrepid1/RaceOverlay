# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Laps tab: delta, speed, braking/accel and grip traces against distance,
the track map coloured by time gained and lost, and which laps to overlay."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from racecore import analysis

from ..common import CURSOR, GAIN, LOSS, REF_COLOR, SERIES
from ..laplist import LapTree


class LapsPage(QtWidgets.QWidget):
    title = "Laps"
    video_default = True

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        self.traces = pg.GraphicsLayoutWidget()
        self.p_delta = self.traces.addPlot(row=0, col=0)
        self.p_speed = self.traces.addPlot(row=1, col=0)
        self.p_long = self.traces.addPlot(row=2, col=0)
        self.p_grip = self.traces.addPlot(row=3, col=0)
        self.plots = [self.p_delta, self.p_speed, self.p_long, self.p_grip]
        for p in self.plots:
            p.showGrid(x=True, y=True, alpha=0.15)
            for ax in ("left", "bottom"):
                p.getAxis(ax).enableAutoSIPrefix(False)
            p.setMenuEnabled(False)
            p.getAxis("left").setWidth(48)
            if p is not self.p_delta:
                p.setXLink(self.p_delta)
        self.p_delta.setLabel("left", "Delta (s)")
        self.p_long.setLabel("left", "Brake/accel (g)")
        self.p_grip.setLabel("left", "Grip used (%)")
        self.cursors = []
        for p in self.plots:
            ln = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(CURSOR, width=1))
            ln.sigDragged.connect(lambda l: self.win.set_cursor(self.win.from_x(l.value())))
            p.addItem(ln, ignoreBounds=True)
            self.cursors.append(ln)
        # the reference video's position (ghost mode: not where yours is)
        self.ref_lines = []
        for p in self.plots:
            ln = pg.InfiniteLine(angle=90, movable=False,
                                 pen=pg.mkPen(REF_COLOR, width=1, style=QtCore.Qt.DashLine))
            ln.hide()
            p.addItem(ln, ignoreBounds=True)
            self.ref_lines.append(ln)
        self.dots = []        # (plot, channel key, your dot, reference dot)
        self._ys = {}
        self.traces.scene().sigMouseClicked.connect(self._traces_clicked)
        # legend as a row above the plots, so it never covers a trace
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        self.legend = QtWidgets.QLabel("")
        self.legend.setTextFormat(QtCore.Qt.RichText)
        self.legend.setWordWrap(True)
        self.legend.setStyleSheet("background:#000; padding:4px 8px;")
        self.legend.setMinimumWidth(100)
        ll.addWidget(self.legend)
        ll.addWidget(self.traces, 1)
        split.addWidget(left)

        side = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.map = pg.PlotWidget()
        self.map.setAspectLocked(True)
        self.map.hideAxis("left")
        self.map.hideAxis("bottom")
        self.map.setMenuEnabled(False)
        self.map.scene().sigMouseClicked.connect(self._map_clicked)
        side.addWidget(self.map)
        self.laps = LapTree(compact=True)
        self.laps.focused.connect(win.lap_focused)
        self.laps.checked.connect(win.lap_checked)
        side.addWidget(self.laps)
        side.setSizes([360, 300])
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([760, 260])
        lay.addWidget(split)
        self.marker = None
        self.ref_marker = None

    # ------------------------------------------------------------ updates
    def refresh(self):
        self.laps.rebuild(self.win)
        self.laps_changed()

    def laps_changed(self):
        self.laps.restyle(self.win)
        self._draw_traces()
        self._draw_map()
        self.cursor(self.win.cursor_d)
        self.ref_cursor(self.win.ref_d)

    def zoom(self, d0: float | None, d1: float | None):
        w = self.win
        if d0 is None:
            self.p_delta.setXRange(0, float(w.x(w.a.length)), padding=0.01)
        else:
            self.p_delta.setXRange(float(w.x(d0)), float(w.x(d1)), padding=0.02)

    def cursor(self, d: float):
        w, a = self.win, self.win.a
        if a.ref is None:
            return
        x = float(w.x(d))
        for ln in self.cursors:
            ln.blockSignals(True)
            ln.setValue(x)
            ln.blockSignals(False)
        ld = w.focus_lap()
        if self.marker is not None and ld is not None:
            k = min(int(np.searchsorted(ld.d, d)), len(ld.xy) - 1)
            self.marker.setData([ld.xy[k, 0]], [ld.xy[k, 1]])
        self._place_dots(0, d)
        self._ref_line_check()

    def ref_cursor(self, rd: float | None):
        """Where the reference video is (None: no reference video showing)."""
        self._rd = rd
        a = self.win.a
        ref = a.ref
        if self.ref_marker is not None:
            if rd is None or ref is None:
                self.ref_marker.setData([], [])
            else:
                k = min(int(np.searchsorted(ref.d, rd)), len(ref.xy) - 1)
                self.ref_marker.setData([ref.xy[k, 0]], [ref.xy[k, 1]])
        self._place_dots(1, rd)
        self._ref_line_check()

    _rd = None

    def _ref_line_check(self):
        rd, d = self._rd, self.win.cursor_d
        show = rd is not None and abs(rd - d) > 1.0
        for ln in self.ref_lines:
            ln.setVisible(show)
            if show:
                ln.setValue(float(self.win.x(rd)))

    def _place_dots(self, which: int, d):
        """which 0: your lap's dots at d; 1: the reference's."""
        w, a = self.win, self.win.a
        x = None if d is None else float(w.x(d))
        for _p, key, *items in self.dots:
            item = items[which]
            ys = self._ys.get((which, key))
            if x is None or ys is None:
                item.setData([], [])
                continue
            y = float(np.interp(d, a.grid, ys))
            item.setData([x], [y] if np.isfinite(y) else [])

    # ------------------------------------------------------------ drawing
    def _draw_traces(self):
        self.dots = []
        for p, cur, rl in zip(self.plots, self.cursors, self.ref_lines):
            p.clear()
            p.addItem(cur, ignoreBounds=True)
            p.addItem(rl, ignoreBounds=True)
        w, a = self.win, self.win.a
        if a.ref is None:
            self.legend.setText("")
            return
        x = w.x(a.grid)
        self.p_speed.setLabel("left", f"Speed ({w.speed_unit()})")
        self.p_grip.setLabel("bottom", f"Distance ({w.dist_unit()})")
        for c in a.corners:
            if c.n % 2 == 0:
                for p in self.plots:
                    band = pg.LinearRegionItem(values=(w.x(c.start_d), w.x(c.end_d)),
                                               movable=False, brush=pg.mkBrush(255, 255, 255, 12),
                                               pen=pg.mkPen(None))
                    band.setZValue(-10)
                    p.addItem(band, ignoreBounds=True)
        rp = pg.mkPen(REF_COLOR, width=1.6)
        self.p_delta.plot([0, float(x[-1])], [0, 0], pen=rp)
        entries = [(REF_COLOR, f"Ref: {w.name(a.ref)}", False)]
        self.p_speed.plot(x, w.spd(a.channel_on_grid(a.ref, "speed")), pen=rp)
        g = a.channel_on_grid(a.ref, "lon_g")
        if g is not None:
            self.p_long.plot(x, g, pen=rp)
        self.p_grip.addLine(y=100, pen=pg.mkPen("#6b6a64", style=QtCore.Qt.DashLine))
        u = a.grip_on_grid(a.ref)
        if u is not None:
            self.p_grip.plot(x, u * 100, pen=rp, connect="finite")
        for i, key in enumerate(w.compare):
            ld = w.lap(key)
            if ld is None:
                continue
            pen = pg.mkPen(SERIES[i], width=1.8 if key == w.focus else 1.1)
            self.p_delta.plot(x, a.delta(ld), pen=pen)
            entries.append((SERIES[i], w.name(ld), key == w.focus))
            self.p_speed.plot(x, w.spd(a.channel_on_grid(ld, "speed")), pen=pen)
            g = a.channel_on_grid(ld, "lon_g")
            if g is not None:
                self.p_long.plot(x, g, pen=pen)
            u = a.grip_on_grid(ld)
            if u is not None:
                self.p_grip.plot(x, u * 100, pen=pen, connect="finite")
        parts = []
        for col, txt, focus in entries:
            name = f"<b>{txt}</b>" if focus else txt
            parts.append(f"<span style='color:{col}; font-size:16px;'>━━</span>&nbsp;"
                         f"<span style='color:#d8d7d0;'>{name}</span>")
        self.legend.setText("&nbsp;&nbsp;&nbsp;&nbsp;".join(parts))
        self.p_grip.setYRange(0, 115, padding=0)
        vr = self.p_speed.viewRange()[1]
        for c in a.corners:
            t = pg.TextItem(c.name, color="#d8d7d0", anchor=(0.5, 0))
            t.setPos(float(w.x(c.apex_d)), vr[1])
            self.p_speed.addItem(t, ignoreBounds=True)
        self.p_delta.setXRange(0, float(x[-1]), padding=0.01)
        self._make_dots()

    def _make_dots(self):
        """Dots riding your trace and the reference's, so it's clear which
        line belongs to which video."""
        w, a = self.win, self.win.a
        self.dots, self._ys = [], {}
        ld = w.focus_lap()
        laps = [(0, ld), (1, a.ref)]
        for p, key in ((self.p_speed, "speed"), (self.p_long, "lon_g"), (self.p_grip, "grip")):
            for which, lap in laps:
                if lap is None:
                    continue
                if key == "speed":
                    ys = w.spd(a.channel_on_grid(lap, "speed"))
                elif key == "grip":
                    u = a.grip_on_grid(lap)
                    ys = None if u is None else u * 100
                else:
                    ys = a.channel_on_grid(lap, key)
                if ys is not None:
                    self._ys[(which, key)] = np.asarray(ys, dtype=float)
            you = p.plot([], [], pen=None, symbol="o", symbolSize=10,
                         symbolPen=pg.mkPen(CURSOR, width=1.5),
                         symbolBrush=pg.mkBrush(w.color(ld.key) if ld is not None else CURSOR))
            ref = p.plot([], [], pen=None, symbol="o", symbolSize=9,
                         symbolPen=pg.mkPen(CURSOR, width=1.5), symbolBrush=pg.mkBrush(REF_COLOR))
            self.dots.append((p, key, you, ref))

    def _draw_map(self):
        self.map.clear()
        self.marker = None
        self.ref_marker = None
        w, a = self.win, self.win.a
        if a.ref is None:
            return
        ref = a.ref
        self.map.plot(ref.xy[:, 0], ref.xy[:, 1], pen=pg.mkPen("#3a3a37", width=7))
        ld = w.focus_lap()
        if ld is not None and ld is not a.ref:
            rate = np.gradient(a.delta(ld)) / analysis.STEP_M * 100
            gx = np.interp(a.grid, a._ref_s, ref.xy[:, 0])
            gy = np.interp(a.grid, a._ref_s, ref.xy[:, 1])
            rate = np.convolve(rate, np.ones(15) / 15, "same")
            cols = np.where(rate > 0.02, 1, np.where(rate < -0.02, -1, 0))
            for val, col in ((1, LOSS), (-1, GAIN)):
                m = cols == val
                self.map.plot(gx[m], gy[m], pen=None, symbol="o", symbolSize=4,
                              symbolPen=None, symbolBrush=pg.mkBrush(col))
        else:
            self.map.plot(ref.xy[:, 0], ref.xy[:, 1], pen=pg.mkPen(REF_COLOR, width=2))
        for c in a.corners:
            px = float(np.interp(c.apex_d, a._ref_s, ref.xy[:, 0]))
            py = float(np.interp(c.apex_d, a._ref_s, ref.xy[:, 1]))
            t = pg.TextItem(c.name, color="#d8d7d0", anchor=(0.5, 1.2))
            t.setPos(px, py)
            self.map.addItem(t)
        sx, sy = ref.xy[0]
        self.map.plot([sx], [sy], pen=None, symbol="s", symbolSize=9,
                      symbolBrush=pg.mkBrush("#f2f1ec"))
        self.ref_marker = self.map.plot([], [], pen=None, symbol="o", symbolSize=12,
                                        symbolPen=pg.mkPen(CURSOR, width=2),
                                        symbolBrush=pg.mkBrush(REF_COLOR))
        self.marker = self.map.plot([sx], [sy], pen=None, symbol="o", symbolSize=13,
                                    symbolPen=pg.mkPen("#0b0b0b", width=2),
                                    symbolBrush=pg.mkBrush(w.color(w.focus)))

    # ------------------------------------------------------------- clicks
    def _traces_clicked(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            return
        pos = ev.scenePos()
        for p in self.plots:
            vb = p.getViewBox()
            if vb.sceneBoundingRect().contains(pos):
                self.win.set_cursor(self.win.from_x(vb.mapSceneToView(pos).x()), seek=True)
                return

    def _map_clicked(self, ev):
        a = self.win.a
        if a.ref is None or ev.button() != QtCore.Qt.LeftButton:
            return
        p = self.map.getViewBox().mapSceneToView(ev.scenePos())
        d2 = (a.ref.xy[:, 0] - p.x()) ** 2 + (a.ref.xy[:, 1] - p.y()) ** 2
        self.win.set_cursor(float(a._ref_s[int(np.argmin(d2))]), seek=True)
