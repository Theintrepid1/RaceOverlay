# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Laps tab: delta, speed, braking/accel and grip traces against distance,
the track map coloured by time gained and lost, and which laps to overlay."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from racecore import analysis
from raceoverlay.syncgui import settings

from .. import lapcharts
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
        self.chart_keys = lapcharts.load(settings().value("analysis/laps_charts", ""))
        self.plots, self.cursors, self.ref_lines = [], [], []
        self.missing = []      # "no X in this file" notes, one per chart
        self.dots = []         # (plot, channel key, your dot, reference dot)
        self._ys = {}
        self._build_plots()
        self.traces.scene().sigMouseClicked.connect(self._traces_clicked)
        # legend as a row above the plots, so it never covers a trace
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        # which charts, top to bottom: one picker each, + to add one
        bar = QtWidgets.QWidget()
        self.chart_row = QtWidgets.QHBoxLayout(bar)
        self.chart_row.setContentsMargins(4, 2, 4, 2)
        self.chart_row.setSpacing(4)
        self.pickers: list[QtWidgets.QComboBox] = []
        # scrolls sideways when narrow, so six pickers can't widen the tab
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(bar)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.chart_scroll = scroll
        ll.addWidget(scroll)
        self.legend = QtWidgets.QLabel("")
        self.legend.setTextFormat(QtCore.Qt.RichText)
        self.legend.setWordWrap(True)
        self.legend.setStyleSheet("background:#000; padding:4px 8px;")
        self.legend.setMinimumWidth(100)
        ll.addWidget(self.legend)
        ll.addWidget(self.traces, 1)
        split.addWidget(left)

        side = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        maptop = QtWidgets.QWidget()
        ml = QtWidgets.QVBoxLayout(maptop)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(2)
        names = QtWidgets.QHBoxLayout()
        names.addWidget(QtWidgets.QLabel("Turn names:"))
        st = settings()
        self.names_charts = QtWidgets.QCheckBox("Charts")
        self.names_charts.setToolTip("Turn names along the top of the charts")
        self.names_charts.setChecked(st.value("analysis/laps_names_charts", True, type=bool))
        self.names_map = QtWidgets.QCheckBox("Map")
        self.names_map.setToolTip("Turn names on the track map")
        self.names_map.setChecked(st.value("analysis/laps_names_map", True, type=bool))
        for chk in (self.names_charts, self.names_map):
            chk.toggled.connect(self._names_toggled)
            names.addWidget(chk)
        names.addStretch(1)
        ml.addLayout(names)
        self.map = pg.PlotWidget()
        self.map.setAspectLocked(True)
        self.map.hideAxis("left")
        self.map.hideAxis("bottom")
        self.map.setMenuEnabled(False)
        self.map.scene().sigMouseClicked.connect(self._map_clicked)
        ml.addWidget(self.map, 1)
        side.addWidget(maptop)
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

    def _names_toggled(self, _on):
        st = settings()
        st.setValue("analysis/laps_names_charts", self.names_charts.isChecked())
        st.setValue("analysis/laps_names_map", self.names_map.isChecked())
        self._draw_traces()
        self._draw_map()
        self.cursor(self.win.cursor_d)
        self.ref_cursor(self.win.ref_d)

    # ------------------------------------------------------------ updates
    def refresh(self):
        self.laps.rebuild(self.win)
        self._fill_pickers()
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
            self.plots[0].setXRange(0, float(w.x(w.a.length)), padding=0.01)
        else:
            self.plots[0].setXRange(float(w.x(d0)), float(w.x(d1)), padding=0.02)

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
    # ------------------------------------------------------------ charts
    def _build_plots(self):
        """One plot per chart key, x-linked, each with the cursor and the
        reference video's line."""
        self.traces.clear()
        self.plots, self.cursors, self.ref_lines, self.missing = [], [], [], []
        for i, _key in enumerate(self.chart_keys):
            p = self.traces.addPlot(row=i, col=0)
            p.showGrid(x=True, y=True, alpha=0.15)
            for ax in ("left", "bottom"):
                p.getAxis(ax).enableAutoSIPrefix(False)
            p.setMenuEnabled(False)
            p.getAxis("left").setWidth(48)
            if self.plots:
                p.setXLink(self.plots[0])
            # distance numbers on the bottom chart only: more height for traces
            last = i == len(self.chart_keys) - 1
            p.getAxis("bottom").setStyle(showValues=last)
            if not last:
                p.getAxis("bottom").setHeight(6)
            cur = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(CURSOR, width=1))
            cur.sigDragged.connect(lambda l: self.win.set_cursor(self.win.from_x(l.value())))
            rl = pg.InfiniteLine(angle=90, movable=False,
                                 pen=pg.mkPen(REF_COLOR, width=1, style=QtCore.Qt.DashLine))
            rl.hide()
            self.plots.append(p)
            self.cursors.append(cur)
            self.ref_lines.append(rl)
            self.missing.append(pg.TextItem("", color="#a8a79f", anchor=(0.5, 0.5)))

    def _fill_pickers(self):
        """The chart pickers above the traces, from what's loaded now."""
        while self.chart_row.count():
            it = self.chart_row.takeAt(0)
            wd = it.widget()
            if wd is not None:          # gone now, not at the next event loop pass
                wd.hide()
                wd.setParent(None)
                wd.deleteLater()
        self.pickers = []
        lbl = QtWidgets.QLabel("Charts:")
        self.chart_row.addWidget(lbl)
        avail = lapcharts.choices(self.win) if self.win.a.ref is not None else []
        keys = [c.key for c in avail]
        for i, key in enumerate(self.chart_keys):
            cb = QtWidgets.QComboBox()
            cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
            cb.setToolTip(f"Chart {i + 1} from the top")
            for c in avail:
                cb.addItem(c.label, c.key)
            if key not in keys:
                cb.addItem(f"{lapcharts.label_of(key)} (not in these files)", key)
            cb.insertSeparator(cb.count())
            cb.addItem("Remove this chart", "__remove__")
            if len(self.chart_keys) <= lapcharts.MIN_CHARTS:
                cb.model().item(cb.count() - 1).setEnabled(False)
            cb.setCurrentIndex(max(0, cb.findData(key)))
            cb.activated.connect(lambda _j, i=i, cb=cb: self._picked_chart(i, cb.currentData()))
            self.chart_row.addWidget(cb)
            self.pickers.append(cb)
        add = QtWidgets.QToolButton()
        add.setText("+")
        add.setToolTip("Add a chart (up to 6)")
        add.setEnabled(len(self.chart_keys) < lapcharts.MAX_CHARTS)
        add.clicked.connect(self._add_chart)
        self.chart_row.addWidget(add)
        self.chart_row.addStretch(1)
        self._fit_chart_bar()
        QtCore.QTimer.singleShot(0, self._fit_chart_bar)

    def _fit_chart_bar(self):
        """Height for the picker row, plus a scroll bar only when the row
        is wider than the space it has."""
        bar = self.chart_scroll.widget()
        row = self.chart_row
        m = row.contentsMargins()
        kids = [row.itemAt(i).widget() for i in range(row.count())]
        kids = [k for k in kids if k is not None]
        # from the widgets themselves: the row's own hint lags behind when
        # it was just rebuilt while the tab was hidden
        h = max([k.sizeHint().height() for k in kids] + [20]) + m.top() + m.bottom()
        w = sum(k.sizeHint().width() for k in kids) + row.spacing() * len(kids) \
            + m.left() + m.right()
        if max(w, bar.sizeHint().width()) > self.chart_scroll.viewport().width():
            h += self.chart_scroll.horizontalScrollBar().sizeHint().height()
        self.chart_scroll.setFixedHeight(h)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, "chart_scroll"):
            QtCore.QTimer.singleShot(0, self._fit_chart_bar)

    def showEvent(self, ev):
        super().showEvent(ev)
        if hasattr(self, "chart_scroll"):
            QtCore.QTimer.singleShot(0, self._fit_chart_bar)

    def _charts_changed(self):
        settings().setValue("analysis/laps_charts", lapcharts.dump(self.chart_keys))
        self._build_plots()
        self._fill_pickers()
        self._draw_traces()
        self.cursor(self.win.cursor_d)
        self.ref_cursor(self.win.ref_d)

    def _picked_chart(self, i: int, key):
        if key == "__remove__":
            if len(self.chart_keys) > lapcharts.MIN_CHARTS:
                del self.chart_keys[i]
        elif key and key != self.chart_keys[i]:
            self.chart_keys[i] = key
        else:
            return
        self._charts_changed()

    def _add_chart(self):
        if len(self.chart_keys) >= lapcharts.MAX_CHARTS:
            return
        have = set(self.chart_keys)
        avail = [c.key for c in lapcharts.choices(self.win)] if self.win.a.ref else []
        new = next((k for k in avail if k not in have), "speed")
        self.chart_keys.append(new)
        self._charts_changed()

    # ------------------------------------------------------------ drawing
    def _draw_traces(self):
        self.dots = []
        for p, cur, rl in zip(self.plots, self.cursors, self.ref_lines):
            p.clear()
            p.addItem(cur, ignoreBounds=True)
            p.addItem(rl, ignoreBounds=True)
            p.setLabel("bottom", "")
        w, a = self.win, self.win.a
        if a.ref is None:
            self.legend.setText("")
            return
        x = w.x(a.grid)
        axis = {c.key: c.axis for c in lapcharts.choices(w)}
        self.plots[-1].setLabel("bottom", f"Distance ({w.dist_unit()})")
        for c in a.corners:
            if c.n % 2 == 0:
                for p in self.plots:
                    band = pg.LinearRegionItem(values=(w.x(c.start_d), w.x(c.end_d)),
                                               movable=False, brush=pg.mkBrush(255, 255, 255, 12),
                                               pen=pg.mkPen(None))
                    band.setZValue(-10)
                    p.addItem(band, ignoreBounds=True)
        rp = pg.mkPen(REF_COLOR, width=1.6)
        laps = [(a.ref, rp)]
        entries = [(REF_COLOR, f"Ref: {w.name(a.ref)}", False)]
        for i, key in enumerate(w.compare):
            ld = w.lap(key)
            if ld is None:
                continue
            laps.append((ld, pg.mkPen(SERIES[i], width=1.8 if key == w.focus else 1.1)))
            entries.append((SERIES[i], w.name(ld), key == w.focus))
        for p, key, note in zip(self.plots, self.chart_keys, self.missing):
            p.setLabel("left", axis.get(key, lapcharts.label_of(key)))
            p.getAxis("left").setTicks(None)
            for y in lapcharts.ref_lines(key):
                p.addLine(y=y, pen=pg.mkPen("#6b6a64", style=QtCore.Qt.DashLine))
            drawn = 0
            for lap, pen in laps:
                ys = lapcharts.values(w, key, lap)
                if ys is None or not np.any(np.isfinite(ys)):
                    continue
                p.plot(x, ys, pen=pen, connect="finite")
                drawn += 1
            yr = lapcharts.y_range(key)
            if yr is not None:
                p.setYRange(*yr, padding=0)
            else:
                p.enableAutoRange(axis="y")
            if not drawn:
                note.setText(f"No {lapcharts.label_of(key)} in this file")
                p.addItem(note, ignoreBounds=True)
                p.setYRange(0, 1, padding=0)
                p.getAxis("left").setTicks([[]])     # no numbers on an empty chart
                note.setPos(float(x[-1]) / 2, 0.5)
        parts = []
        for col, txt, focus in entries:
            name = f"<b>{txt}</b>" if focus else txt
            parts.append(f"<span style='color:{col}; font-size:16px;'>━━</span>&nbsp;"
                         f"<span style='color:#d8d7d0;'>{name}</span>")
        self.legend.setText("&nbsp;&nbsp;&nbsp;&nbsp;".join(parts))
        self.plots[0].setXRange(0, float(x[-1]), padding=0.01)
        top = self.plots[0]
        top.getViewBox().updateAutoRange()
        vr = top.viewRange()[1]
        for c in (a.corners if self.names_charts.isChecked() else ()):
            t = pg.TextItem(c.name, color="#d8d7d0", anchor=(0.5, 0))
            t.setPos(float(w.x(c.apex_d)), vr[1])
            top.addItem(t, ignoreBounds=True)
        self._make_dots()

    def _make_dots(self):
        """Dots riding your trace and the reference's, so it's clear which
        line belongs to which video."""
        w, a = self.win, self.win.a
        self.dots, self._ys = [], {}
        ld = w.focus_lap()
        for p, key in zip(self.plots, self.chart_keys):
            if key == "delta":
                continue
            for which, lap in ((0, ld), (1, a.ref)):
                if lap is None:
                    continue
                ys = lapcharts.values(w, key, lap)
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
        for c in (a.corners if self.names_map.isChecked() else ()):
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
