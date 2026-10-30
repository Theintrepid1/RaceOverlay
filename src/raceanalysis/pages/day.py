# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Day tab: the sessions of a track day side by side. How much grip each
session used (envelopes overlaid), turn by turn (did every turn change, or
only a few?), lap by lap inside each session (tires warming / fading), and
the notes and weather for each session. See racecore.day for the reasoning.
"""
from __future__ import annotations

import threading

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from racecore import day
from raceoverlay.syncgui import _btn, _note, settings

from .. import fields, gglabels, pointlabels
from ..common import GAIN, INK_DIM, LIMIT, LOSS, NEUTRAL, SERIES, cap_columns, fmt_lap


def _scolor(i: int) -> str:
    return SERIES[i % len(SERIES)]


class _Worker(QtCore.QObject):
    """Runs start-time and weather lookups off the GUI thread."""
    done = QtCore.Signal(int, str, object)     # session index, what, result


class DayPage(QtWidgets.QWidget):
    title = "Day"
    video_default = False

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.notes: list[day.Notes] = []
        self.sel = 0
        self._loading = False
        self._busy: set = set()
        self._offline: set = set()      # sessions whose weather lookup failed
        self.worker = _Worker()
        self.worker.done.connect(self._job_done)
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_notes)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        # ---- left: sessions, grip by turn, plots
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.sessions = QtWidgets.QTableWidget(0, 9)
        self.sessions.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.sessions.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.sessions.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.sessions.verticalHeader().setVisible(False)
        self.sessions.itemSelectionChanged.connect(self._session_picked)
        ll.addWidget(self.sessions)

        mid = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        turnbox = QtWidgets.QWidget()
        tl = QtWidgets.QVBoxLayout(turnbox)
        tl.setContentsMargins(0, 0, 0, 0)
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(QtWidgets.QLabel("Compare"))
        self.cmp_b = QtWidgets.QComboBox()
        crow.addWidget(self.cmp_b)
        crow.addWidget(QtWidgets.QLabel("against"))
        self.cmp_a = QtWidgets.QComboBox()
        crow.addWidget(self.cmp_a)
        crow.addStretch(1)
        for cb in (self.cmp_a, self.cmp_b):
            cb.activated.connect(lambda _i: self._fill_turns())
        tl.addLayout(crow)
        self.verdict = _note("")
        self.verdict.setWordWrap(True)
        tl.addWidget(self.verdict)
        self.turns = QtWidgets.QTableWidget(0, 1)
        self.turns.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.turns.verticalHeader().setVisible(False)
        self.turns.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        tl.addWidget(self.turns, 1)
        mid.addWidget(turnbox)

        self.env = pg.PlotWidget()
        self.env.setAspectLocked(True)
        self.env_labels = gglabels.GGLabels(self.env, which=("accel", "brake"))
        self.env_hover = pg.TextItem("", color="#d8d7d0", fill=pg.mkBrush(23, 23, 26, 230),
                                     border=pg.mkPen("#3a3a37"), anchor=(0, 1))
        self.env_hover.setZValue(100)
        self.env_ray = pg.PlotDataItem(pen=pg.mkPen("#f2f1ec", width=1,
                                                    style=QtCore.Qt.DotLine))
        self.env.scene().sigMouseMoved.connect(self._env_hover)
        self.env.setMenuEnabled(False)
        self.env.hideAxis("left")
        self.env.hideAxis("bottom")
        envbox = QtWidgets.QWidget()
        el = QtWidgets.QVBoxLayout(envbox)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(2)
        el.addWidget(self.env, 1)
        # the outlines in numbers: how far each session went each way
        self.env_table = QtWidgets.QTableWidget(0, 5)
        self.env_table.setHorizontalHeaderLabels(["", "Brake", "Accel", "Left", "Right"])
        self.env_table.verticalHeader().setVisible(False)
        self.env_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.env_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.env_table.setFocusPolicy(QtCore.Qt.NoFocus)
        self.env_table.setToolTip("Each session's grip envelope in the four directions, in g. "
                                  "The best in each column is highlighted. Hover the chart "
                                  "for any direction in between.")
        el.addWidget(self.env_table)
        mid.addWidget(envbox)
        mid.setSizes([520, 380])
        ll.addWidget(mid, 3)

        self.trend = pg.PlotWidget()
        self.trend.setMenuEnabled(False)
        self.trend.setMouseEnabled(False, False)
        self.trend.hideButtons()
        self.trend.showGrid(y=True, alpha=0.15)
        for ax in ("left", "bottom"):
            self.trend.getAxis(ax).enableAutoSIPrefix(False)
        self.trend.setLabel("left", "g (95th pct)")
        self.trend_labels = pointlabels.PointLabels(self.trend)
        ll.addWidget(self.trend, 2)
        split.addWidget(left)

        # ---- right: notes for the picked session
        form_w = QtWidgets.QWidget()
        fl = QtWidgets.QVBoxLayout(form_w)
        fl.setContentsMargins(6, 0, 0, 0)
        self.form_title = QtWidgets.QLabel("")
        self.form_title.setStyleSheet("font-weight:600;")
        fl.addWidget(self.form_title)
        form = QtWidgets.QFormLayout()
        self.f_start = fields.StartFields()
        self.start_src = _note("", small=True)
        sw = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(self.f_start)
        sl.addWidget(self.start_src)
        form.addRow("Start", sw)
        self.f_press = fields.PressureGrid()
        form.addRow("Pressures", self.f_press)
        self.f_track = QtWidgets.QLineEdit()
        self.f_track.setPlaceholderText("e.g. 110 F, from a pyrometer or the timing board")
        form.addRow("Track temp", self.f_track)
        self.f_tires = QtWidgets.QLineEdit()
        self.f_tires.setPlaceholderText("which set, heat cycles")
        form.addRow("Tires", self.f_tires)
        self.f_fuel = QtWidgets.QLineEdit()
        form.addRow("Fuel", self.f_fuel)
        self.f_changes = QtWidgets.QPlainTextEdit()
        self.f_changes.setPlaceholderText("Setup or driving changes before this session")
        self.f_changes.setMaximumHeight(80)
        form.addRow("Changes", self.f_changes)
        self.f_text = QtWidgets.QPlainTextEdit()
        self.f_text.setPlaceholderText("Anything else: traffic, flags, how it felt")
        self.f_text.setMaximumHeight(80)
        form.addRow("Notes", self.f_text)
        fl.addLayout(form)
        wrow = QtWidgets.QHBoxLayout()
        self.weather = _note("")
        self.weather.setWordWrap(True)
        wrow.addWidget(self.weather, 1)
        self.btn_weather = _btn("Get weather", self.fetch_all_weather,
                                "Look up the weather at the track for each session's start "
                                "(needs internet; works later at home too)")
        wrow.addWidget(self.btn_weather, 0, QtCore.Qt.AlignTop)
        fl.addLayout(wrow)
        self.saved_lbl = _note("", small=True)
        fl.addWidget(self.saved_lbl)
        fl.addStretch(1)
        for w in (self.f_track, self.f_tires, self.f_fuel):
            w.textEdited.connect(self._edited)
        self.f_start.edited.connect(self._edited)
        self.f_press.edited.connect(self._edited)
        for w in (self.f_changes, self.f_text):
            w.textChanged.connect(self._edited)
        form_w.setMinimumWidth(320)
        split.addWidget(form_w)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([1000, 360])
        split.setCollapsible(1, False)
        lay.addWidget(split)

    # ---------------------------------------------------------- page API
    def refresh(self):
        a = self.win.a
        self._save_now()
        self.notes = [day.Notes.load(s.path) for s in a.sessions]
        self.sel = min(self.sel, max(0, len(a.sessions) - 1))
        if a.ref is None:
            self.sessions.setRowCount(0)
            self.turns.setRowCount(0)
            self.env.clear()
            self.trend.clear()
            self.verdict.setText("Open the day's sessions (File → Open track day…).")
            self._load_form()
            return
        self.envs = [day.session_envelope(a, i) for i in range(len(a.sessions))]
        self.capacity = day.turn_capacity(a)
        self.peaks = day.lap_peaks(a)
        self._fill_sessions()
        self._fill_pickers()
        self._fill_turns()
        self._draw_env()
        self._draw_trend()
        self._load_form()
        self._auto_lookups()

    def laps_changed(self):
        pass

    def names_changed(self):
        """A session got its short name: update labels only (the notes form
        may be mid-edit, so it isn't reloaded)."""
        a = self.win.a
        for si, s in enumerate(a.sessions):
            it = self.sessions.item(si, 1)
            if it is not None:
                it.setText(s.name)
        self._fill_pickers()
        self._fill_turns()

    def cursor(self, d):
        pass

    def ref_cursor(self, rd):
        pass

    # ------------------------------------------------------------ sessions
    def _fill_sessions(self):
        a, w = self.win.a, self.win
        t = self.sessions
        cols = ["", "Session", "Start", "Laps", "Best", "Corner\ng", "Brake\ng",
                "Air", "Track", "Changes"]
        t.setColumnCount(len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.setRowCount(len(a.sessions))
        for si, s in enumerate(a.sessions):
            n = self.notes[si]
            timed = [ld for ld in a.laps if ld.session == si and ld.timed]
            best = min((ld.time for ld in timed), default=None)
            env = day.envelope_summary(self.envs[si])
            wx = n.weather or {}
            air = ""
            if "air_c" in wx:
                c = wx["air_c"]
                air = f"{c * 1.8 + 32:.0f} °F" if w.use_mph else f"{c:.0f} °C"
            start = n.start[11:] if len(n.start) >= 16 else (n.start or "")
            cells = ["", s.name, start, str(len(timed)),
                     fmt_lap(best) if best else "",
                     f"{env['corner']:.2f}" if env else "", f"{env['brake']:.2f}" if env else "",
                     air, n.track_temp, n.changes.replace("\n", "; ")]
            for col, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                if col == 0:
                    it.setBackground(QtGui.QColor(_scolor(si)))
                if col in (3, 4, 5, 6):
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                t.setItem(si, col, it)
        cap_columns(t)
        t.setColumnWidth(0, 8)
        # the g columns fit their numbers (plus padding), with two-line headers
        hdr = t.horizontalHeader()
        hfm = QtGui.QFontMetrics(hdr.font())
        g_w = max(t.fontMetrics().horizontalAdvance("8.88"),
                  hfm.horizontalAdvance("Corner")) + 18
        for col, tip in ((5, "Cornering g: the session's grip envelope, left and right "
                             "averaged"),
                         (6, "Braking g: the session's grip envelope, straight-line braking")):
            t.setColumnWidth(col, g_w)
            hi = t.horizontalHeaderItem(col)
            if hi is not None:
                hi.setToolTip(tip)
        t.horizontalHeader().setStretchLastSection(True)
        hh = max(t.horizontalHeader().height(), t.horizontalHeader().sizeHint().height())
        h = hh + sum(t.rowHeight(r) for r in range(t.rowCount())) + 6
        t.setFixedHeight(min(max(h, 60), 240))
        t.blockSignals(True)
        t.selectRow(self.sel)
        t.blockSignals(False)

    def _session_picked(self):
        rows = {i.row() for i in self.sessions.selectionModel().selectedRows()}
        if rows:
            self._save_now()
            self.sel = min(rows)
            self._load_form()

    # --------------------------------------------------------- grip by turn
    def _fill_pickers(self):
        a = self.win.a
        n = len(a.sessions)
        for cb, default in ((self.cmp_b, n - 1), (self.cmp_a, 0)):
            cur = cb.currentIndex()
            cb.blockSignals(True)
            cb.clear()
            for s in a.sessions:
                cb.addItem(s.name)
            cb.setCurrentIndex(cur if 0 <= cur < n else default)
            cb.blockSignals(False)
        for cb in (self.cmp_a, self.cmp_b):
            cb.setEnabled(n > 1)

    def _fill_turns(self):
        a = self.win.a
        corners, rows = self.capacity
        n = len(a.sessions)
        t = self.turns
        cols = ["Turn"] + [s.name for s in a.sessions] + (["Change"] if n > 1 else [])
        t.setColumnCount(len(cols))
        t.setHorizontalHeaderLabels(cols)
        for i in range(n):
            hi = t.horizontalHeaderItem(i + 1)
            hi.setForeground(QtGui.QColor(_scolor(i)))
            hi.setToolTip("95th percentile of combined g through the turn, all timed laps "
                          "of the session")
        sa, sb = self.cmp_a.currentIndex(), self.cmp_b.currentIndex()
        t.setRowCount(len(corners))
        for r, (c, row) in enumerate(zip(corners, rows)):
            cells = [c.name] + [f"{v:.2f}" if v is not None else "" for v in row]
            diff = None
            if n > 1 and sa != sb and row[sa] is not None and row[sb] is not None:
                diff = row[sb] - row[sa]
                cells.append(f"{diff:+.2f}")
            for col, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                if col:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if col == len(cells) - 1 and diff is not None:
                    it.setForeground(QtGui.QColor(GAIN if diff > 0.02 else
                                                  LOSS if diff < -0.02 else NEUTRAL))
                t.setItem(r, col, it)
        t.resizeColumnsToContents()
        if n < 2:
            self.verdict.setText("One session so far. Add the day's other sessions to see "
                                 "how the grip you used changed (File → Add data file…).")
        elif sa == sb:
            self.verdict.setText("Pick two different sessions.")
        else:
            v = day.compare_sessions([c.name for c in corners], rows, sa, sb)
            nb, na = a.sessions[sb].name, a.sessions[sa].name
            self.verdict.setText(f"<b>{nb} vs {na}:</b> " + (v.text if v else
                                                            "not enough turns to compare."))

    # --------------------------------------------------------------- plots
    def _draw_env(self):
        a = self.win.a
        p = self.env
        p.clear()
        kx = -1.0 if settings().value("analysis/gg_flip_lat", False, type=bool) else 1.0
        ky = -1.0 if settings().value("analysis/gg_flip_lon", False, type=bool) else 1.0
        R = max([float(np.max(e.radius)) for e in self.envs if e is not None] + [1.0]) * 1.15
        grid = pg.mkPen("#3a3a37", width=1)
        th = np.linspace(0, 2 * np.pi, 121)
        for r in (0.5, 1.0, 1.5):
            if r <= R:
                p.plot(r * np.cos(th), r * np.sin(th), pen=grid)
        p.plot([-R, R], [0, 0], pen=grid)
        p.plot([0, 0], [-R, R], pen=grid)
        if a.envelope is not None:
            o = a.envelope.outline()
            p.plot(kx * o[:, 0], ky * o[:, 1], pen=pg.mkPen(LIMIT, width=1.5,
                                                            style=QtCore.Qt.DashLine))
        for i, e in enumerate(self.envs):
            if e is None:
                continue
            o = e.outline()
            p.plot(kx * o[:, 0], ky * o[:, 1], pen=pg.mkPen(_scolor(i), width=2))
        lbl = pg.TextItem("dashed: whole day", color=INK_DIM, anchor=(0, 1))
        lbl.setPos(-R, -R)
        p.addItem(lbl)
        outl = [e.outline() for e in self.envs if e is not None]
        if a.envelope is not None:
            outl.append(a.envelope.outline())
        self.env_labels.set(gglabels.reach((), outl), kx, ky, R)
        self._env_k = (kx, ky)
        for it in (self.env_hover, self.env_ray):
            p.addItem(it, ignoreBounds=True)
            it.hide()
        self._fill_env_table()

    # direction name, angle of the envelope (unflipped: +lat right, +lon accel)
    ENV_DIRS = (("Brake", 1.5 * np.pi), ("Accel", 0.5 * np.pi), ("Left", np.pi), ("Right", 0.0))

    def _fill_env_table(self):
        a, t = self.win.a, self.env_table
        rows = [(si, e) for si, e in enumerate(self.envs) if e is not None]
        t.setRowCount(len(rows))
        vals = np.array([[float(e.at(th)) for _, th in self.ENV_DIRS] for _, e in rows]) \
            if rows else np.zeros((0, 4))
        best = vals.max(axis=0) if len(rows) > 1 else None
        for r, (si, e) in enumerate(rows):
            sw = QtWidgets.QTableWidgetItem("")
            sw.setBackground(QtGui.QColor(_scolor(si)))
            sw.setToolTip(a.sessions[si].name)
            t.setItem(r, 0, sw)
            for c in range(4):
                v = vals[r, c]
                it = QtWidgets.QTableWidgetItem(f"{v:.2f}")
                it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                it.setToolTip(f"{a.sessions[si].name}: {self.ENV_DIRS[c][0].lower()} {v:.2f} g")
                if best is not None and v >= best[c] - 1e-9:
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                    it.setForeground(QtGui.QColor(GAIN))
                t.setItem(r, c + 1, it)
        t.resizeColumnsToContents()
        t.setColumnWidth(0, 10)
        h = t.horizontalHeader().height() + sum(t.rowHeight(r) for r in range(t.rowCount())) + 4
        t.setFixedHeight(min(h, 200))
        t.setVisible(bool(rows))

    def _env_hover(self, pos):
        """Each session's grip in the direction under the mouse."""
        vb = self.env.getViewBox()
        rows = [(si, e) for si, e in enumerate(getattr(self, "envs", [])) if e is not None]
        if not rows or not vb.sceneBoundingRect().contains(pos):
            self.env_hover.hide()
            self.env_ray.hide()
            return
        m = vb.mapSceneToView(pos)
        kx, ky = getattr(self, "_env_k", (1.0, 1.0))
        lat, lon = m.x() * kx, m.y() * ky          # back to unflipped
        if np.hypot(lat, lon) < 0.08:
            self.env_hover.hide()
            self.env_ray.hide()
            return
        th = float(np.arctan2(lon, lat))
        side = "right" if lat > 0 else "left"
        if abs(lon) > 2.5 * abs(lat):
            what = "braking" if lon < 0 else "accelerating"
        elif abs(lat) > 2.5 * abs(lon):
            what = f"cornering {side}"
        else:
            what = ("braking" if lon < 0 else "accelerating") + f" + {side}"
        a = self.win.a
        lines = [f"<b>{what}</b>"]
        rmax = 0.0
        for si, e in rows:
            r = float(e.at(th))
            rmax = max(rmax, r)
            lines.append(f"<span style='color:{_scolor(si)}'>■</span> "
                         f"{a.sessions[si].name}: {r:.2f} g")
        self.env_hover.setHtml("<br>".join(lines))
        self.env_hover.setAnchor((1.05 if m.x() > 0 else -0.05, 1.05 if m.y() < 0 else -0.05))
        self.env_hover.setPos(m.x(), m.y())
        ux, uy = kx * np.cos(th), ky * np.sin(th)
        self.env_ray.setData([0, ux * rmax * 1.08], [0, uy * rmax * 1.08])
        self.env_hover.show()
        self.env_ray.show()

    def _draw_trend(self):
        p = self.trend
        p.clear()
        x = 0
        ticks = []
        pts = []
        w, a = self.win, self.win.a
        picked = {k for k in [w.focus] + list(w.compare) if k is not None}
        for si, rows in enumerate(self.peaks):
            if not rows:
                continue
            xs = np.arange(x, x + len(rows))
            col = _scolor(si)
            laps = {ld.lap.n: ld for ld in a.laps if ld.session == si and ld.timed}
            best = min(laps.values(), key=lambda ld: ld.time, default=None)
            for xx, (n, cg, bg) in zip(xs, rows):
                ld = laps.get(n)
                imp = ld is not None and (ld is best or ld.key in picked)
                name = f"{a.sessions[si].name} L{n}"
                pts.append(pointlabels.Point(float(xx), cg, f"{cg:.2f}", imp,
                                             tip=f"{name}: cornering {cg:.2f} g"))
                pts.append(pointlabels.Point(float(xx), bg, f"{bg:.2f}", label=False,
                                             tip=f"{name}: braking {bg:.2f} g"))
            p.plot(xs, [r[1] for r in rows], pen=pg.mkPen(col, width=2), symbol="o",
                   symbolSize=6, symbolBrush=pg.mkBrush(col), symbolPen=None)
            p.plot(xs, [r[2] for r in rows], pen=pg.mkPen(col, width=1.2,
                                                          style=QtCore.Qt.DashLine))
            ticks += [(int(xx), f"L{r[0]}") for xx, r in zip(xs, rows)]
            if si:
                p.addItem(pg.InfiniteLine(pos=x - 0.5, angle=90,
                                          pen=pg.mkPen("#3a3a37", style=QtCore.Qt.DashLine)))
            x += len(rows) + 1
        p.getAxis("bottom").setTicks([ticks])
        if x:
            p.setXRange(-0.5, x - 1.5, padding=0.02)
            ys = [q.y for q in pts]
            span = max(ys) - min(ys) if ys else 0.0
            p.setYRange(min(ys) - span * 0.12 - 0.02, max(ys) + span * 0.14 + 0.02, padding=0)
        self.trend_labels.set(pts)
        p.setTitle("<span style='font-size:9pt'>Lap by lap: cornering g (solid), "
                   "braking g (dashed)</span>")
        p.setToolTip("95th percentile of cornering and braking g on each timed lap. "
                     "Rising over the first laps = tires warming up; falling later = "
                     "tires getting hot. Learning a track shows across sessions instead.")

    # ---------------------------------------------------------------- notes
    def _load_form(self):
        a = self.win.a
        self._loading = True
        has = bool(a.sessions)
        for w in (self.f_start, self.f_press, self.f_track, self.f_tires,
                  self.f_fuel, self.f_changes, self.f_text, self.btn_weather):
            w.setEnabled(has)
        if not has:
            self.form_title.setText("Session notes")
            self.f_start.setText("")
            self.f_press.set_values("", "")
            for w in (self.f_track, self.f_tires, self.f_fuel):
                w.setText("")
            self.f_changes.setPlainText("")
            self.f_text.setPlainText("")
            self.weather.setText("")
            self.start_src.setText("")
            self._loading = False
            return
        n = self.notes[self.sel]
        s = a.sessions[self.sel]
        self.form_title.setText(f"<span style='color:{_scolor(self.sel)}'>■</span> "
                                f"Notes: {s.name}")
        self.f_start.setText(n.start)
        self.start_src.setText({"gps": "from the GoPro's GPS clock",
                                "video": "from the GoPro's clock (check it was set)",
                                "typed": "typed in"}.get(n.start_source,
                                                          "type it in, or sync the video"))
        self.f_press.set_values(n.pressures_cold, n.pressures_hot)
        self.f_track.setText(n.track_temp)
        self.f_tires.setText(n.tires)
        self.f_fuel.setText(n.fuel)
        self.f_changes.setPlainText(n.changes)
        self.f_text.setPlainText(n.text)
        self._show_weather()
        self.saved_lbl.setText("")
        self._loading = False

    def _show_weather(self):
        n = self.notes[self.sel]
        wx = n.weather or {}
        if not wx:
            if not n.start_dt():
                self.weather.setText("Weather: needs the start time.")
            elif self.sel in self._offline:
                self.weather.setText("Weather: offline. It'll be looked up next time the "
                                     "laptop's online (or press Get weather).")
            else:
                self.weather.setText("Weather: looking it up…")
            return
        imperial = self.win.use_mph
        bits = []
        if "air_c" in wx:
            bits.append(f"air {wx['air_c'] * 1.8 + 32:.0f} °F" if imperial else
                        f"air {wx['air_c']:.0f} °C")
        if "humidity" in wx:
            bits.append(f"humidity {wx['humidity']:.0f}%")
        if "cloud" in wx:
            bits.append(f"cloud {wx['cloud']:.0f}%")
        if "sun_wm2" in wx:
            bits.append(f"sun {wx['sun_wm2']:.0f} W/m²")
        if "wind_kmh" in wx:
            bits.append(f"wind {wx['wind_kmh'] * 0.621371:.0f} mph" if imperial else
                        f"wind {wx['wind_kmh']:.0f} km/h")
        if wx.get("rain_mm"):
            bits.append(f"rain {wx['rain_mm']:.1f} mm/h")
        self.weather.setText(f"<b>Weather</b> at {wx.get('at', '')[11:]}: " + ", ".join(bits)
                             + " <span style='color:#8a8983'>(Open-Meteo)</span>")

    def _edited(self, *_):
        if self._loading or not self.notes:
            return
        self.saved_lbl.setText("…")
        self._save_timer.start()

    def _save_now(self):
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_notes()

    def _save_notes(self):
        a = self.win.a
        if not self.notes or self.sel >= len(a.sessions):
            return
        n = self.notes[self.sel]
        start = self.f_start.text()
        if start != n.start and self.f_start.complete():
            n.start, n.start_source, n.start_utc = start, "typed", ""
            n.weather = {}            # different time: look it up again
        n.pressures_cold, n.pressures_hot = self.f_press.values()
        n.track_temp = self.f_track.text().strip()
        n.tires = self.f_tires.text().strip()
        n.fuel = self.f_fuel.text().strip()
        n.changes = self.f_changes.toPlainText().strip()
        n.text = self.f_text.toPlainText().strip()
        try:
            n.save(a.sessions[self.sel].path)
            self.saved_lbl.setText("Saved next to the data file.")
        except OSError as e:
            self.saved_lbl.setText(f"Couldn't save: {e}")
        self.start_src.setText("typed in" if n.start_source == "typed" else
                               self.start_src.text())
        self.win.rename_session(self.sel)
        self._fill_sessions_text(self.sel)
        if not n.weather and n.start_dt():
            self._fetch_weather(self.sel, quiet=True)
        self._show_weather()

    def _fill_sessions_text(self, si):
        """Refresh one row of the sessions table after a notes edit."""
        if si < self.sessions.rowCount():
            n = self.notes[si]
            for col, txt in ((2, n.start[11:] if len(n.start) >= 16 else n.start),
                             (8, n.track_temp), (9, n.changes.replace("\n", "; "))):
                it = self.sessions.item(si, col)
                if it is not None:
                    it.setText(txt)

    # ------------------------------------------------------- background jobs
    def _auto_lookups(self):
        """Start times from synced videos, weather for sessions with a time."""
        a = self.win.a
        for si, s in enumerate(a.sessions):
            n = self.notes[si]
            if not n.start and s.video and ("start", si) not in self._busy:
                self._run(si, "start", lambda s=s: day.find_start(s.video, *a.origin))
            elif n.start_dt() and not n.weather:
                self._fetch_weather(si, quiet=True)

    def fetch_all_weather(self):
        for si in range(len(self.win.a.sessions)):
            if self.notes[si].start_dt():
                self._fetch_weather(si, quiet=False)
        if not any(n.start_dt() for n in self.notes):
            self.weather.setText("Weather needs each session's start time.")

    def _fetch_weather(self, si, quiet):
        n = self.notes[si]
        when = n.start_dt()
        if when is None or ("weather", si) in self._busy:
            return
        lat, lon = self.win.a.origin
        self._run(si, "weather", lambda: day.fetch_weather(lat, lon, when.date()))

    def _run(self, si, what, fn):
        self._busy.add((what, si))
        sig = self.worker.done

        def work():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001 - offline etc.
                res = e
            sig.emit(si, what, res)
        threading.Thread(target=work, daemon=True).start()

    def _job_done(self, si, what, res):
        self._busy.discard((what, si))
        a = self.win.a
        if si >= len(self.notes) or si >= len(a.sessions):
            return
        n = self.notes[si]
        if what == "start":
            if isinstance(res, tuple) and not n.start:
                n.start, n.start_source, n.start_utc = res
                n.save(a.sessions[si].path)
                self.win.rename_session(si)
                self._fill_sessions_text(si)
                if si == self.sel:
                    self._load_form()
                self._fetch_weather(si, quiet=True)
        elif what == "weather":
            if isinstance(res, Exception):
                self._offline.add(si)
                if si == self.sel:
                    self._show_weather()
                return
            self._offline.discard(si)
            off = res.get("utc_offset_seconds")
            if n.start_utc and off is not None:     # GPS time: now the exact local time
                n.start = day.local_from_utc(n.start_utc, int(off))
            when = n.start_dt()
            if when is not None:
                n.save(a.sessions[si].path)
                self.win.rename_session(si)
            if when is not None:
                n.weather = day.weather_at(res, when)
                n.save(a.sessions[si].path)
            self._fill_sessions()
            if si == self.sel:
                self._load_form()

    def flush(self):
        """Save pending edits (window closing)."""
        self._save_now()

