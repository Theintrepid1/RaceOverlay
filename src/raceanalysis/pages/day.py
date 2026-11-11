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


def _esc(x: str) -> str:
    return x.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _one_line(x: str, n: int = 80) -> str:
    x = "; ".join(p.strip() for p in x.splitlines() if p.strip())
    return x if len(x) <= n else x[:n - 1] + "…"


def _short_names(names: list[str]) -> list[str]:
    """'Session 1 · Sep 27', 'Session 2 · Sep 27' -> 'Session 1', 'Session 2'
    when every name ends the same way."""
    tails = {n.rsplit(" · ", 1)[1] if " · " in n else None for n in names}
    if len(tails) == 1 and None not in tails and len(names) > 1:
        return [n.rsplit(" · ", 1)[0] for n in names]
    return names


# session colours: the lap colours first, then four more, so a day of up to
# eight sessions never repeats one (and none looks like the grip limit's teal)
SESSION_COLORS = SERIES + ["#9b6fe0", "#d6559b", "#8fb339", "#a67c52"]


def _scolor(i: int) -> str:
    return SESSION_COLORS[i % len(SESSION_COLORS)]


class _Worker(QtCore.QObject):
    """Runs start-time and weather lookups off the GUI thread."""
    done = QtCore.Signal(int, str, object)     # session index, what, result


class DayPage(QtWidgets.QWidget):
    title = "Day"
    video_default = False
    COLS = ["", "Session", "Use", "Start", "Laps", "Best", "Brake", "Accel", "Left", "Right",
            "Air", "Track"]
    C_START, C_G, C_TRACK = 3, 6, 11
    USES = (("In", "in"), ("Left out", "out"), ("Hidden", "hidden"))

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.notes: list[day.Notes] = []
        self.sel = 0
        self.capacity = None
        self.counts: list[bool] = []
        self.envs = []
        self._loading = False
        self._busy: set = set()
        self._offline: set = set()      # sessions whose weather lookup failed
        self.worker = _Worker()
        self.worker.done.connect(self._job_done)
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_notes)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.sub = QtWidgets.QTabWidget()
        self.sub.setDocumentMode(True)
        lay.addWidget(self.sub)

        # ---- Sessions: the table (with its grip numbers) and the notes
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.sessions = QtWidgets.QTableWidget(0, len(self.COLS))
        self.sessions.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.sessions.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.sessions.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.sessions.verticalHeader().setVisible(False)
        self.sessions.itemSelectionChanged.connect(self._session_picked)
        ll.addWidget(self.sessions, 1)
        use_note = _note("<b>Use:</b> <b>In</b> counts towards the day's numbers. <b>Left "
                         "out</b> (a wet session, a shakedown) keeps its laps and video but "
                         "isn't used for the grip limit, the reference, the theoretical best "
                         "or the comparisons. <b>Hidden</b> takes it out of everything until "
                         "you set it back here.", small=True)
        use_note.setWordWrap(True)
        ll.addWidget(use_note)
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
        self.sub.addTab(split, "Sessions")

        # ---- Compare: the day as a story, and turn by turn
        turnbox = QtWidgets.QWidget()
        tl = QtWidgets.QVBoxLayout(turnbox)
        tl.setContentsMargins(4, 4, 4, 0)
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(QtWidgets.QLabel("Compare"))
        self.mode_step = QtWidgets.QRadioButton("Step by step")
        self.mode_step.setToolTip("Each session against the one before it: what changed "
                                  "from one session to the next")
        self.mode_base = QtWidgets.QRadioButton("Against")
        self.mode_base.setToolTip("Every session against one baseline session")
        self.cmp_base = QtWidgets.QComboBox()
        self.cmp_base.setToolTip("The baseline session")
        self.cmp_base.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        mode = settings().value("analysis/day_compare", "consecutive")
        (self.mode_base if mode == "baseline" else self.mode_step).setChecked(True)
        for wdg in (self.mode_step, self.mode_base, self.cmp_base):
            crow.addWidget(wdg)
        crow.addStretch(1)
        self.mode_step.toggled.connect(self._mode_changed)
        self.cmp_base.activated.connect(lambda _i: self._fill_turns())
        tl.addLayout(crow)
        self.verdict = _note("")
        self.verdict.setWordWrap(True)
        self.verdict.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        tl.addWidget(self.verdict)
        self.turns = QtWidgets.QTableWidget(0, 1)
        self.turns.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.turns.verticalHeader().setVisible(False)
        self.turns.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        tl.addWidget(self.turns, 1)
        self.sub.addTab(turnbox, "Compare")

        # ---- Grip: each session's envelope, and lap by lap
        gsplit = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
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
        self.env_legend = QtWidgets.QWidget()          # under the chart, not on it
        self.env_legend_grid = QtWidgets.QGridLayout(self.env_legend)
        self.env_legend_grid.setContentsMargins(4, 2, 4, 2)
        self.env_legend_grid.setHorizontalSpacing(14)
        self.env_legend_grid.setVerticalSpacing(1)
        el.addWidget(self.env_legend)
        gsplit.addWidget(envbox)
        self.trend = pg.PlotWidget()
        self.trend.setMenuEnabled(False)
        self.trend.setMouseEnabled(False, False)
        self.trend.hideButtons()
        self.trend.showGrid(y=True, alpha=0.15)
        for ax in ("left", "bottom"):
            self.trend.getAxis(ax).enableAutoSIPrefix(False)
        self.trend.setLabel("left", "g (95th pct)")
        self.trend_labels = pointlabels.PointLabels(self.trend)
        trendbox = QtWidgets.QWidget()
        tbl = QtWidgets.QVBoxLayout(trendbox)
        tbl.setContentsMargins(0, 0, 0, 0)
        tbl.setSpacing(2)
        tbl.addWidget(self.trend, 1)
        trend_note = _note("95th percentile of cornering and braking g on each timed lap. "
                           "Rising over the first laps = tires warming up; falling later = "
                           "tires getting hot. Learning a track shows across sessions "
                           "instead.", small=True)
        trend_note.setWordWrap(True)
        tbl.addWidget(trend_note)
        gsplit.addWidget(trendbox)
        gsplit.setStretchFactor(0, 2)
        gsplit.setStretchFactor(1, 3)
        gsplit.setSizes([560, 840])
        self.sub.addTab(gsplit, "Grip")

        self.sub.setCurrentIndex(settings().value("analysis/day_sub", 0, type=int)
                                 % self.sub.count())
        self.sub.currentChanged.connect(lambda i: settings().setValue("analysis/day_sub", i))

    def _col(self, si) -> str:
        """A session keeps its colour when another one is hidden."""
        ss = self.win.a.sessions
        return _scolor(ss[si].order - 1 if si is not None and 0 <= si < len(ss) else 0)

    # ---------------------------------------------------------- page API
    def refresh(self):
        a = self.win.a
        self._save_now()
        self.notes = [day.Notes.load(s.path) for s in a.sessions]
        self.sel = min(self.sel, max(0, len(a.sessions) - 1))
        self.counts = [a.counts(i) for i in range(len(a.sessions))]
        if a.ref is None:
            self.capacity = None
            self.envs = []
            self._fill_sessions()           # hidden sessions can still be brought back
            self.turns.setRowCount(0)
            self.env.clear()
            self._set_legend([])
            self.trend.clear()
            self.verdict.setText("Open the day's sessions (File → Open track day…)."
                                 if not a.hidden else
                                 "Every session is hidden: set one back to In on the "
                                 "Sessions tab.")
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
    def _set_legend(self, items: list[str], foot: str = ""):
        """One entry per cell, two columns, so nothing wraps or gets cut off."""
        g = self.env_legend_grid
        while g.count():
            w = g.takeAt(0).widget()
            if w is not None:
                w.hide()              # gone now, not just when Qt gets round to it
                w.setParent(None)
                w.deleteLater()
        for k, html in enumerate(items):
            lbl = _note(html, small=True)
            g.addWidget(lbl, k // 2, k % 2)
        if foot:
            lbl = _note(foot, small=True)
            lbl.setWordWrap(True)
            g.addWidget(lbl, (len(items) + 1) // 2, 0, 1, 2)

    # direction name, angle of the envelope (unflipped: +lat right, +lon accel)
    ENV_DIRS = (("Brake", 1.5 * np.pi), ("Accel", 0.5 * np.pi), ("Left", np.pi), ("Right", 0.0))

    def _fill_sessions(self):
        a, w = self.win.a, self.win
        t = self.sessions
        t.blockSignals(True)
        t.clearContents()
        t.setColumnCount(len(self.COLS))
        t.setHorizontalHeaderLabels(self.COLS)
        rows = [(s, si) for si, s in enumerate(a.sessions)] + [(s, None) for s in a.hidden]
        t.setRowCount(len(rows))
        envs = getattr(self, "envs", [])
        g = np.full((len(rows), 4), np.nan)
        for r, (s, si) in enumerate(rows):
            e = envs[si] if si is not None and si < len(envs) else None
            if e is not None:
                g[r] = [float(e.at(th)) for _, th in self.ENV_DIRS]
        counted = [r for r, (s, si) in enumerate(rows) if si is not None and self.counts[si]]
        best = np.nanmax(g[counted], axis=0) if len(counted) > 1 else None
        dim = QtGui.QColor(INK_DIM)
        for r, (s, si) in enumerate(rows):
            hidden = si is None
            n = self.notes[si] if not hidden else day.Notes.load(s.path)
            use = "hidden" if hidden else s.use
            if hidden:
                timed = [lap for lap in s.laps.laps if lap.timed]
                best_t = min((lap.duration for lap in timed), default=None)
            else:
                timed = [ld for ld in a.laps if ld.session == si and ld.timed]
                best_t = min((ld.time for ld in timed), default=None)
            wx = n.weather or {}
            air = ""
            if "air_c" in wx:
                c = wx["air_c"]
                air = f"{c * 1.8 + 32:.0f} °F" if w.use_mph else f"{c:.0f} °C"
            start = n.start[11:] if len(n.start) >= 16 else (n.start or "")
            cells = ["", s.name, "", start, str(len(timed)),
                     fmt_lap(best_t) if best_t else ""]
            cells += ["" if np.isnan(v) else f"{v:.2f}" for v in g[r]]
            cells += [air, n.track_temp]
            for col, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                if col == 0:
                    it.setBackground(QtGui.QColor(self._col(si) if not hidden else "#3a3a37"))
                if col in (4, 5, 6, 7, 8, 9):
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if use != "in" and col:
                    it.setForeground(dim)
                    if hidden:
                        f = it.font()
                        f.setItalic(True)
                        it.setFont(f)
                elif best is not None and self.C_G <= col < self.C_G + 4 \
                        and g[r, col - self.C_G] >= best[col - self.C_G] - 1e-9:
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                    it.setForeground(QtGui.QColor(GAIN))
                if col == 1:
                    tip = f"{s.name}\n{s.path}"
                    if n.changes:
                        tip += f"\n\nChanges: {n.changes}"
                    it.setToolTip(tip)
                t.setItem(r, col, it)
            cb = QtWidgets.QComboBox()
            for label, key in self.USES:
                cb.addItem(label, key)
            cb.setCurrentIndex([k for _l, k in self.USES].index(use))
            cb.setToolTip("In: counts towards the day's numbers. Left out: kept, but not used "
                          "for the grip limit, reference, theoretical best or comparisons. "
                          "Hidden: out of everything.")
            cb.activated.connect(lambda i, s=s, cb=cb: QtCore.QTimer.singleShot(
                0, lambda: self.win.set_session_use(s, cb.itemData(i))))
            t.setCellWidget(r, 2, cb)
        cap_columns(t)
        t.setColumnWidth(0, 8)
        t.resizeColumnToContents(2)
        hdr = t.horizontalHeader()
        hfm = QtGui.QFontMetrics(hdr.font())
        g_w = max(t.fontMetrics().horizontalAdvance("8.88"), hfm.horizontalAdvance("Accel")) + 18
        for k, (name, _th) in enumerate(self.ENV_DIRS):
            col = self.C_G + k
            t.setColumnWidth(col, g_w)
            hi = t.horizontalHeaderItem(col)
            if hi is not None:
                hi.setToolTip(f"{name} g: the session's grip envelope in that direction "
                              "(timed laps). Green = the best of the sessions that count.")
        hdr.setStretchLastSection(True)
        if a.sessions:
            t.selectRow(self.sel)
        t.blockSignals(False)

    def _session_picked(self):
        rows = {i.row() for i in self.sessions.selectionModel().selectedRows()}
        if rows and min(rows) < len(self.win.a.sessions):
            self._save_now()
            self.sel = min(rows)
            self._load_form()

    # --------------------------------------------------------- grip by turn
    def _counted(self) -> list[int]:
        return [i for i, c in enumerate(self.counts) if c]

    def _fill_pickers(self):
        a = self.win.a
        inc = self._counted()
        cur = self.cmp_base.currentData()
        self.cmp_base.blockSignals(True)
        self.cmp_base.clear()
        for si in inc:
            self.cmp_base.addItem(a.sessions[si].name, si)
        i = self.cmp_base.findData(cur) if cur is not None else -1
        self.cmp_base.setCurrentIndex(max(i, 0))
        self.cmp_base.blockSignals(False)
        many = len(inc) > 2           # with two sessions both modes are the same
        for wdg in (self.mode_step, self.mode_base):
            wdg.setEnabled(many)
        self.cmp_base.setEnabled(many and self.mode_base.isChecked())

    def _mode(self) -> tuple[str, int]:
        if self.mode_base.isChecked() and len(self._counted()) > 2:
            base = self.cmp_base.currentData()
            return "baseline", base if base is not None else self._counted()[0]
        return "consecutive", 0

    def _mode_changed(self, *_):
        settings().setValue("analysis/day_compare",
                            "baseline" if self.mode_base.isChecked() else "consecutive")
        self.cmp_base.setEnabled(len(self._counted()) > 2 and self.mode_base.isChecked())
        self._fill_turns()

    def _fill_turns(self):
        a = self.win.a
        if self.capacity is None:
            return
        corners, rows = self.capacity
        n = len(a.sessions)
        counts = self.counts
        mode, base = self._mode()
        t = self.turns
        cols = ["Turn"] + [s.name + ("" if counts[i] else " (left out)")
                           for i, s in enumerate(a.sessions)]
        t.setColumnCount(len(cols))
        t.setHorizontalHeaderLabels(cols)
        against = ("the session before it that counts" if mode == "consecutive"
                   else f"{a.sessions[base].name} (baseline)" if n else "")
        for i in range(n):
            hi = t.horizontalHeaderItem(i + 1)
            hi.setForeground(QtGui.QColor(self._col(i) if counts[i] else INK_DIM))
            hi.setToolTip("95th percentile of combined g through the turn, all timed laps "
                          f"of the session. The small number is the change against {against}."
                          if counts[i] else "Left out of the comparisons (Sessions tab)")
        t.setRowCount(len(corners))
        for r, (c, row) in enumerate(zip(corners, rows)):
            t.setItem(r, 0, QtWidgets.QTableWidgetItem(c.name))
            for si, v in enumerate(row):
                ch = day.cell_change(rows, r, si, mode, base, counts) if v is not None else None
                txt = "" if v is None else f"{v:.2f}"
                if ch is not None:
                    txt += f"  {ch:+.2f}".replace("0.", ".")
                it = QtWidgets.QTableWidgetItem(txt)
                it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if not counts[si]:
                    it.setForeground(QtGui.QColor(INK_DIM))
                elif ch is not None:
                    it.setForeground(QtGui.QColor(GAIN if ch > 0.02 else
                                                  LOSS if ch < -0.02 else NEUTRAL))
                    other = day.compared_with(si, mode, base, counts)
                    it.setToolTip(f"{c.name}: {v:.2f} g, {ch:+.2f} against "
                                  f"{a.sessions[other].name}")
                elif mode == "baseline" and si == base and v is not None:
                    it.setToolTip("Baseline")
                t.setItem(r, si + 1, it)
        t.resizeColumnsToContents()
        inc = self._counted()
        if n < 2 or len(inc) < 2:
            self.verdict.setText(
                "One session so far. Add the day's other sessions to see how the grip you "
                "used changed (File → Add data file…)." if n < 2 else
                "Fewer than two sessions count, so there's nothing to compare. Set "
                "sessions back to In on the Sessions tab.")
            self.verdict.setToolTip("")
            return
        timed = [[ld.time for ld in a.laps if ld.session == si and ld.timed]
                 for si in range(n)]
        bests = [min(x) if x else None for x in timed]
        story = day.story([c.name for c in corners], rows, bests,
                          [nt.changes for nt in self.notes] + [""] * (n - len(self.notes)),
                          mode, base, counts)
        html = self.story_html(story, mode)
        out = [a.sessions[i].name for i in range(n) if not counts[i]]
        if out:
            html += ("<br><span style='color:%s'>Left out: %s</span>"
                     % (INK_DIM, _esc(", ".join(out))))
        self.verdict.setText(html)
        self.verdict.setToolTip("<br><br>".join(
            f"<b>{a.sessions[st.to].name} vs {a.sessions[st.frm].name}:</b> {st.verdict.text}"
            for st in story if st.verdict))

    def story_html(self, story, mode: str) -> str:
        a = self.win.a
        names = _short_names([s.name for s in a.sessions])
        lines = []
        for st in story:
            na, nb = names[st.frm], names[st.to]
            head = (f"{na} → {nb}" if mode == "consecutive" else f"{nb} vs {na}")
            bits = [f"<span style='color:{self._col(st.to)}'>■</span> <b>{_esc(head)}</b>"]
            if st.best_change is not None:
                col = GAIN if st.best_change < -0.05 else LOSS if st.best_change > 0.05 \
                    else NEUTRAL
                bits.append(f"best <span style='color:{col}'>{st.best_change:+.2f} s</span>")
            bits.append(_esc(st.verdict.short) if st.verdict else "too few turns to compare")
            if st.changes and mode == "consecutive":
                bits.append(f"<i>you changed: {_esc(_one_line(st.changes))}</i>")
            lines.append(" · ".join(bits))
        return "<br>".join(lines)

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
        legend = []
        for i, e in enumerate(self.envs):
            if e is None:
                continue
            o = e.outline()
            if self.counts[i]:
                pen = pg.mkPen(self._col(i), width=2)
            else:               # left out: thin and dotted
                pen = pg.mkPen(self._col(i), width=1, style=QtCore.Qt.DotLine)
            p.plot(kx * o[:, 0], ky * o[:, 1], pen=pen)
            legend.append(f"<span style='color:{self._col(i)}'>■</span> {_esc(a.sessions[i].name)}"
                          + ("" if self.counts[i] else " <i>(left out, dotted)</i>"))
        legend.append(f"<span style='color:{LIMIT}'>- -</span> grip limit")
        self._set_legend(legend, "Dashed: the grip limit, from the sessions that count. "
                                 "Hover the chart for any direction.")
        outl = [e.outline() for i, e in enumerate(self.envs) if e is not None and self.counts[i]]
        if a.envelope is not None:
            outl.append(a.envelope.outline())
        self.env_labels.set(gglabels.reach((), outl), kx, ky, R)
        self._env_k = (kx, ky)
        for it in (self.env_hover, self.env_ray):
            p.addItem(it, ignoreBounds=True)
            it.hide()

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
            lines.append(f"<span style='color:{self._col(si)}'>■</span> "
                         f"{a.sessions[si].name}: {r:.2f} g"
                         + ("" if self.counts[si] else " (left out)"))
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
            on = self.counts[si]
            col = self._col(si) if on else QtGui.QColor(self._col(si)).darker(250).name()
            laps = {ld.lap.n: ld for ld in a.laps if ld.session == si and ld.timed}
            best = min(laps.values(), key=lambda ld: ld.time, default=None)
            for xx, (n, cg, bg) in zip(xs, rows):
                ld = laps.get(n)
                imp = on and ld is not None and (ld is best or ld.key in picked)
                name = f"{a.sessions[si].name} L{n}"
                pts.append(pointlabels.Point(float(xx), cg, f"{cg:.2f}", imp, label=on,
                                             tip=f"{name}: cornering {cg:.2f} g"
                                             + ("" if on else " (left out)")))
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
        p.setToolTip("")

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
        self.form_title.setText(f"<span style='color:{self._col(self.sel)}'>■</span> "
                                f"Notes: {s.name}")
        self.f_start.setText(n.start)
        self.start_src.setText({"gps": "from the GoPro's GPS clock",
                                "fixed": "from the GoPro's clock, corrected at import",
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
        changed = n.changes != self.f_changes.toPlainText().strip()
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
        if changed and getattr(self, "capacity", None) is not None:
            self._fill_turns()            # the story quotes the Changes note
        if not n.weather and n.start_dt():
            self._fetch_weather(self.sel, quiet=True)
        self._show_weather()

    def _fill_sessions_text(self, si):
        """Refresh one row of the sessions table after a notes edit."""
        if si < self.sessions.rowCount():
            n = self.notes[si]
            for col, txt in ((self.C_START, n.start[11:] if len(n.start) >= 16 else n.start),
                             (self.C_TRACK, n.track_temp)):
                it = self.sessions.item(si, col)
                if it is not None:
                    it.setText(txt)

    # ------------------------------------------------------- background jobs
    def _auto_lookups(self):
        """Start times from synced videos, weather for sessions with a time."""
        a = self.win.a
        for si, s in enumerate(a.sessions):
            n = self.notes[si]
            if not n.start and (s.video or day.log_start_utc(s.tel)) \
                    and ("start", si) not in self._busy:
                self._run(si, "start", lambda s=s: day.find_start(s.video, *a.origin, tel=s.tel))
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

