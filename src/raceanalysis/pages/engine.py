# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Engine tab: whatever engine channels the logs have (OBD: RPM, throttle,
MAP, coolant...), against distance like the Laps tab, plus gears and shift
points worked out from RPM and speed, per-lap engine numbers, and how the
temperatures ran across the day.

Channels come from racecore.engine (any column that isn't GPS/accel/time and
has real data), so a dongle that logs more than expected just shows more.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from raceoverlay.syncgui import _note, settings

from ..common import CURSOR, INK_DIM, REF_COLOR, SERIES, fmt_lap

MPH = 0.621371


def _is_c(unit: str) -> bool:
    u = unit.replace(" ", "").lower()
    return u in ("°c", "degc", "c", "ºc", "deg c") or u.endswith("°c")


class EnginePage(QtWidgets.QWidget):
    title = "Engine"
    video_default = False

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.stack = QtWidgets.QStackedLayout(self)
        self.empty = _note("")
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.stack.addWidget(self.empty)

        body = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Show"))
        self.chk_row = QtWidgets.QHBoxLayout()
        top.addLayout(self.chk_row)
        top.addStretch(1)
        self.rates = _note("")
        top.addWidget(self.rates)
        bl.addLayout(top)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        self.legend = QtWidgets.QLabel("")
        self.legend.setTextFormat(QtCore.Qt.RichText)
        self.legend.setStyleSheet("background:#000; padding:4px 8px;")
        self.legend.setWordWrap(True)
        self.legend.setMinimumWidth(100)
        ll.addWidget(self.legend)
        self.traces = pg.GraphicsLayoutWidget()
        self.traces.scene().sigMouseClicked.connect(self._traces_clicked)
        ll.addWidget(self.traces, 1)
        split.addWidget(left)

        side = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.stats = QtWidgets.QTableWidget(0, 5)
        self.stats.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.stats.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.stats.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.stats.verticalHeader().setVisible(False)
        self.stats.cellClicked.connect(self._stats_clicked)
        side.addWidget(self.stats)
        gbox = QtWidgets.QWidget()
        gl = QtWidgets.QVBoxLayout(gbox)
        gl.setContentsMargins(0, 0, 0, 0)
        self.gear_caption = _note("")
        gl.addWidget(self.gear_caption)
        self.gear_tbl = QtWidgets.QTableWidget(0, 4)
        self.gear_tbl.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.gear_tbl.verticalHeader().setVisible(False)
        self.gear_tbl.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        gl.addWidget(self.gear_tbl, 1)
        side.addWidget(gbox)
        self.trend = pg.PlotWidget()
        self.trend.setMenuEnabled(False)
        self.trend.setMouseEnabled(False, False)
        self.trend.hideButtons()
        self.trend.showGrid(y=True, alpha=0.15)
        for ax in ("left", "bottom"):
            self.trend.getAxis(ax).enableAutoSIPrefix(False)
        side.addWidget(self.trend)
        side.setSizes([260, 230, 240])
        side.setMinimumWidth(360)
        split.addWidget(side)
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([900, 420])
        bl.addWidget(split, 1)
        self.stack.addWidget(body)

        self.checks: dict[str, QtWidgets.QCheckBox] = {}
        self.plots: list = []      # (key, PlotItem, cursor line, ref line, you dot, ref dot)
        self._ys: dict = {}
        self._rd = None

    # ----------------------------------------------------------- helpers
    def _units(self, c) -> tuple[str, float, float]:
        """(unit shown, scale, offset) for a channel."""
        u = c.unit or ""
        if c.kind == "rpm" or u.lower() in ("1/min", "rpm"):
            return "rpm", 1.0, 0.0
        if _is_c(u) and self.win.use_mph:
            return "°F", 1.8, 32.0
        return u, 1.0, 0.0

    def _speed(self, kmh):
        return kmh * MPH if self.win.use_mph else kmh

    # ---------------------------------------------------------- page API
    def refresh(self):
        e = self.win.engine
        if self.win.a.ref is None:
            self.empty.setText("Open a session to see its engine data.")
            self.stack.setCurrentIndex(0)
            return
        if not e:
            self.empty.setText(
                "<b>No engine data in these sessions.</b><br><br>With the APEX Pro OBDII "
                "dongle the log gets RPM, throttle, manifold pressure and coolant (whatever "
                "your car's ECU gives it); they show up here with gears and shift points "
                "worked out from RPM and speed.")
            self.stack.setCurrentIndex(0)
            return
        self.stack.setCurrentIndex(1)
        self._make_checks()
        self.laps_changed()
        self._trend()

    def laps_changed(self):
        if not self.win.engine or self.win.a.ref is None:
            return
        self._draw()
        self._fill_stats()
        self._fill_gears()
        self.cursor(self.win.cursor_d)
        self.ref_cursor(self.win.ref_d)

    def cursor(self, d: float):
        if not self.plots:
            return
        x = float(self.win.x(d))
        for key, p, ln, _rl, you, _ref in self.plots:
            ln.blockSignals(True)
            ln.setValue(x)
            ln.blockSignals(False)
            self._dot(you, 0, key, d)
        self._ref_lines()

    def ref_cursor(self, rd):
        self._rd = rd
        for key, _p, _ln, _rl, _you, ref in self.plots:
            self._dot(ref, 1, key, rd)
        self._ref_lines()

    # ------------------------------------------------------------- parts
    def _make_checks(self):
        e = self.win.engine
        while self.chk_row.count():
            w = self.chk_row.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.checks = {}
        st = settings()
        labels = {c.col: c.label for c in e.channels}
        labels["__gear__"] = "Gear"
        for key in self._order():
            label = labels[key]
            chk = QtWidgets.QCheckBox(label)
            chk.setChecked(st.value(f"analysis/engine/{key}", True, type=bool))
            chk.toggled.connect(lambda on, k=key: self._toggled(k, on))
            self.chk_row.addWidget(chk)
            self.checks[key] = chk
        rates = sorted({round(c.rate_hz) for c in e.channels if c.rate_hz >= 1})
        self.rates.setText("" if not rates else f"updates ~{rates[0]} Hz" if len(rates) == 1
                           else f"updates {rates[0]}–{rates[-1]} Hz")
        self.rates.setToolTip("How often the engine channels get a new reading")

    def _toggled(self, key, on):
        settings().setValue(f"analysis/engine/{key}", on)
        self.laps_changed()

    def _order(self):
        """Channel keys in display order: gear right after RPM."""
        e = self.win.engine
        keys = [c.col for c in e.channels]
        if e.gearing:
            i = next((j + 1 for j, c in enumerate(e.channels) if c.kind == "rpm"), 0)
            keys.insert(i, "__gear__")
        return keys

    def _shown(self):
        cmap = {c.col: c for c in self.win.engine.channels}
        return [(k, cmap.get(k)) for k in self._order()
                if k in self.checks and self.checks[k].isChecked()]

    def _series(self, ld, key, c):
        e = self.win.engine
        if key == "__gear__":
            return e.gear_on_grid(ld)
        y = e.on_grid(ld, key)
        if y is None:
            return None
        _u, k, off = self._units(c)
        return y * k + off

    def _draw(self):
        w, a, e = self.win, self.win.a, self.win.engine
        self.traces.clear()
        self.plots, self._ys = [], {}
        shown = self._shown()
        if not shown:
            self.legend.setText("<span style='color:#8a8983'>Tick a channel above.</span>")
            return
        x = w.x(a.grid)
        laps = [(a.ref, REF_COLOR, 1.6, 1)] + [
            (w.lap(k), SERIES[i], 1.8 if k == w.focus else 1.1, 0)
            for i, k in enumerate(w.compare) if w.lap(k) is not None]
        first = None
        for row, (key, c) in enumerate(shown):
            p = self.traces.addPlot(row=row, col=0)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setMenuEnabled(False)
            for ax in ("left", "bottom"):
                p.getAxis(ax).enableAutoSIPrefix(False)
            p.getAxis("left").setWidth(54)
            if first is None:
                first = p
            else:
                p.setXLink(first)
            if key == "__gear__":
                p.setLabel("left", "Gear")
                n = len(e.gearing.ratios)
                p.setYRange(0.5, n + 0.5, padding=0)
                p.getAxis("left").setTicks([[(g, str(g)) for g in range(1, n + 1)]])
            else:
                u, _k, _o = self._units(c)
                p.setLabel("left", f"{c.label} ({u})" if u else c.label)
            for ld, col, width, which in laps:
                y = self._series(ld, key, c)
                if y is None:
                    continue
                if key == "__gear__":
                    y = np.where(y > 0, y, np.nan)
                    p.plot(x, y, pen=pg.mkPen(col, width=width + 0.6), connect="finite")
                else:
                    p.plot(x, y, pen=pg.mkPen(col, width=width))
                if ld is a.ref:
                    self._ys[(1, key)] = y
                if ld.key == w.focus:
                    self._ys[(0, key)] = y
            if key != "__gear__" and c.kind == "rpm":
                # shift points on the lap you're on
                ld = w.focus_lap()
                if ld is not None:
                    pts = [(s, a.dist_at(ld, s.t - ld.lap.start)) for s in e.lap_shifts(ld)
                           if s.to > s.frm]
                    if pts:
                        p.plot([float(w.x(dd)) for _s, dd in pts], [s.rpm for s, _dd in pts],
                               pen=None, symbol="t1", symbolSize=9, symbolPen=None,
                               symbolBrush=pg.mkBrush(w.color(ld.key)))
            ln = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(CURSOR, width=1))
            ln.sigDragged.connect(lambda l: self.win.set_cursor(self.win.from_x(l.value())))
            p.addItem(ln, ignoreBounds=True)
            rl = pg.InfiniteLine(angle=90, movable=False,
                                 pen=pg.mkPen(REF_COLOR, width=1, style=QtCore.Qt.DashLine))
            rl.hide()
            p.addItem(rl, ignoreBounds=True)
            ld = w.focus_lap()
            you = p.plot([], [], pen=None, symbol="o", symbolSize=10,
                         symbolPen=pg.mkPen(CURSOR, width=1.5),
                         symbolBrush=pg.mkBrush(w.color(ld.key) if ld is not None else CURSOR))
            ref = p.plot([], [], pen=None, symbol="o", symbolSize=9,
                         symbolPen=pg.mkPen(CURSOR, width=1.5), symbolBrush=pg.mkBrush(REF_COLOR))
            self.plots.append((key, p, ln, rl, you, ref))
            for cn in a.corners:
                if cn.n % 2 == 0:
                    band = pg.LinearRegionItem(values=(w.x(cn.start_d), w.x(cn.end_d)),
                                               movable=False, brush=pg.mkBrush(255, 255, 255, 12),
                                               pen=pg.mkPen(None))
                    band.setZValue(-10)
                    p.addItem(band, ignoreBounds=True)
        self.plots[-1][1].setLabel("bottom", f"Distance ({w.dist_unit()})")
        top = self.plots[0][1]
        vr = top.viewRange()[1]
        for cn in a.corners:
            t = pg.TextItem(cn.name, color="#d8d7d0", anchor=(0.5, 0))
            t.setPos(float(w.x(cn.apex_d)), vr[1])
            top.addItem(t, ignoreBounds=True)
        first.setXRange(0, float(x[-1]), padding=0.01)
        parts = []
        for ld, col, _w, _which in laps:
            name = w.name(ld)
            if ld is a.ref:
                name = f"Ref: {name}"
            if ld.key == w.focus:
                name = f"<b>{name}</b>"
            parts.append(f"<span style='color:{col}; font-size:16px;'>━━</span>&nbsp;"
                         f"<span style='color:#d8d7d0;'>{name}</span>")
        parts.append("<span style='color:#8a8983;'>▲ upshift</span>")
        self.legend.setText("&nbsp;&nbsp;&nbsp;&nbsp;".join(parts))

    def _dot(self, item, which, key, d):
        ys = self._ys.get((which, key))
        if d is None or ys is None:
            item.setData([], [])
            return
        y = float(np.interp(d, self.win.a.grid, ys))
        item.setData([float(self.win.x(d))], [y] if np.isfinite(y) else [])

    def _ref_lines(self):
        rd, d = self._rd, self.win.cursor_d
        show = rd is not None and abs(rd - d) > 1.0
        for _k, _p, _ln, rl, _you, _ref in self.plots:
            rl.setVisible(show)
            if show:
                rl.setValue(float(self.win.x(rd)))

    def _traces_clicked(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            return
        pos = ev.scenePos()
        for _k, p, *_ in self.plots:
            vb = p.getViewBox()
            if vb.sceneBoundingRect().contains(pos):
                self.win.set_cursor(self.win.from_x(vb.mapSceneToView(pos).x()), seek=True)
                return

    # ------------------------------------------------------------ tables
    def _fill_stats(self):
        w, a, e = self.win, self.win.a, self.win.engine
        t = self.stats
        temp = next((c for c in e.channels if c.kind == "coolant"), None)
        cols = ["Lap", "Full throttle", "Max RPM", "Upshift RPM",
                f"Max coolant ({self._units(temp)[0]})" if temp else "Shifts"]
        t.setColumnCount(len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.setRowCount(0)
        laps = [ld for ld in a.laps if ld.timed]
        t.setRowCount(len(laps))
        for r, ld in enumerate(laps):
            s = e.lap_stats(ld)
            last = ""
            if temp:
                v = s.get("max_coolant")
                if v is not None:
                    _u, k, off = self._units(temp)
                    last = f"{v * k + off:.0f}"
            else:
                last = str(s.get("shifts", ""))
            cells = [f"{w.label(ld)}  {fmt_lap(ld.time)}",
                     f"{s['full_throttle'] * 100:.0f}%" if "full_throttle" in s else "",
                     f"{s['max_rpm']:,.0f}" if "max_rpm" in s else "",
                     f"{s['upshift_rpm']:,.0f}" if "upshift_rpm" in s else "", last]
            for col, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                it.setData(QtCore.Qt.UserRole, ld.key)
                if col:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if ld.key in w.compare or ld is a.ref:
                    it.setForeground(pg.mkColor(w.color(ld.key)))
                t.setItem(r, col, it)
            if ld.key == w.focus:
                t.selectRow(r)
        t.resizeColumnsToContents()

    def _stats_clicked(self, row, _col):
        it = self.stats.item(row, 0)
        if it is not None:
            self.win.lap_focused(it.data(QtCore.Qt.UserRole))

    def _fill_gears(self):
        w, a, e = self.win, self.win.a, self.win.engine
        t = self.gear_tbl
        ld = w.focus_lap() or a.ref
        if not e.gearing:
            self.gear_caption.setText("Gears need RPM and speed.")
            t.setRowCount(0)
            return
        su = "mph" if w.use_mph else "km/h"
        self.gear_caption.setText(f"<b>Gears</b>, worked out from RPM ÷ speed, on "
                                  f"{w.name(ld)} vs reference.")
        t.setHorizontalHeaderLabels(["Gear", f"RPM per {su}", f"Speed used ({su})",
                                     "Upshift RPM (this / ref)"])
        mine, ref = e.gear_table(ld), e.gear_table(a.ref)
        t.setRowCount(len(mine))
        for r, (g, gr) in enumerate(zip(mine, ref)):
            per = g["ratio"] / (MPH if w.use_mph else 1.0)
            rng = (f"{self._speed(g['v_min']):.0f}–{self._speed(g['v_max']):.0f}"
                   if g["v_min"] is not None else "not used")
            up = "–" if g["upshift_rpm"] is None else f"{g['upshift_rpm']:,.0f}"
            upr = "–" if gr["upshift_rpm"] is None else f"{gr['upshift_rpm']:,.0f}"
            cells = [str(g["gear"]), f"{per:.0f}", rng, up if ld is a.ref else f"{up} / {upr}"]
            for col, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                if col:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if g["v_min"] is None:
                    it.setForeground(pg.mkColor(INK_DIM))
                t.setItem(r, col, it)
        t.resizeColumnsToContents()

    def _trend(self):
        """Temperatures through the day: each session in order, by minutes."""
        a, e = self.win.a, self.win.engine
        self.trend.clear()
        temps = [c for c in e.channels if c.kind in ("coolant", "oil_temp", "intake")]
        if not temps:
            self.trend.setTitle("<span style='color:#8a8983'>No temperature channels</span>")
            return
        self.trend.setTitle("<span style='font-size:10pt'>Temperatures through the day</span>")
        u, k, off = self._units(temps[0])
        self.trend.setLabel("left", u)
        self.trend.setLabel("bottom", "Minutes (sessions S1, S2… in order)"
                            if len(a.sessions) > 1 else "Minutes")
        x0, ymax, labels = 0.0, -1e9, []
        for si, s in enumerate(a.sessions):
            tm = s.tel.time / 60.0
            for i, c in enumerate(temps):
                v = e.series(si, c.col)
                if v is None:
                    continue
                cu, ck, co = self._units(c)
                ymax = max(ymax, float(np.nanmax(v * ck + co)))
                self.trend.plot(x0 + tm, v * ck + co, pen=pg.mkPen(SERIES[i % len(SERIES)],
                                                                    width=1.5),
                                name=c.label if si == 0 else None)
            labels.append((x0, s.name))
            if si:
                self.trend.addItem(pg.InfiniteLine(pos=x0 - 1.0, angle=90,
                                                   pen=pg.mkPen("#3a3a37",
                                                                style=QtCore.Qt.DashLine)))
            x0 += float(tm[-1]) + 2.0
        if self.trend.plotItem.legend is None and len(temps) > 1:
            self.trend.addLegend(offset=(8, 4))
        if len(labels) > 1:      # session numbers just above the highest reading
            for n, (x, name) in enumerate(labels, 1):
                lbl = pg.TextItem(f"S{n}", color=INK_DIM, anchor=(0, 1))
                lbl.setToolTip(name)
                lbl.setPos(x, ymax + 2)
                self.trend.addItem(lbl)
            self.trend.setYRange(self.trend.viewRange()[1][0], ymax + 5, padding=0)
