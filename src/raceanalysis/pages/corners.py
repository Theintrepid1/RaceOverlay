# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Corners & grip tab: per turn, time lost or gained, grip used, minimum
speed and braking point against the reference; the g-g diagram with the grip
envelope, showing the whole lap or just the picked turn."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from raceoverlay.syncgui import _note, settings

from ..common import CURSOR, LIMIT, GAIN, LOSS, NEUTRAL, REF_COLOR, fmt_gap


class CornersPage(QtWidgets.QWidget):
    title = "Corners & grip"
    video_default = True
    SORTS = [("Track order", "track"), ("Most time lost", "lost"),
             ("Most time gained", "gained"), ("Least grip used", "grip"),
             ("Most to gain vs best lap here", "best")]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.turns: list[int] = []     # turns picked (a series loops on the video)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.caption = _note("")
        ll.addWidget(self.caption)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Sort"))
        self.sort = QtWidgets.QComboBox()
        for txt, key in self.SORTS:
            self.sort.addItem(txt, key)
        i = self.sort.findData(settings().value("analysis/corner_sort", "track"))
        self.sort.setCurrentIndex(max(0, i))
        self.sort.currentIndexChanged.connect(self._sort_changed)
        row.addWidget(self.sort)
        row.addSpacing(16)
        self.loop = QtWidgets.QCheckBox("Loop the video on picked turns")
        self.loop.setToolTip("Plays the picked turn (or run of turns) over and over, so you can "
                             "watch the dot trace the g-g. Shift-click to pick a run of turns.")
        self.loop.setChecked(settings().value("analysis/corner_loop", True, type=bool))
        self.loop.toggled.connect(self._loop_toggled)
        row.addWidget(self.loop)
        row.addSpacing(16)
        self.felt = QtWidgets.QCheckBox("G as felt in the car")
        self.felt.setToolTip("Off: the dot moves the way the car accelerates (braking down, "
                             "a right turn to the right).\nOn: the dot moves the way you're "
                             "pushed in the seat (braking up, a right turn to the left), "
                             "like a ball on the dash.")
        self.felt.setChecked(settings().value("analysis/gg_felt", False, type=bool))
        self.felt.toggled.connect(self._felt_toggled)
        row.addWidget(self.felt)
        row.addStretch(1)
        self.btn_all = QtWidgets.QPushButton("Whole lap")
        self.btn_all.setToolTip("Clear the picked turns")
        self.btn_all.clicked.connect(self.clear_turns)
        row.addWidget(self.btn_all)
        ll.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self._filling = False
        ll.addWidget(self.table, 1)
        split.addWidget(left)

        self.gg = pg.PlotWidget()
        self.gg.setAspectLocked(True)
        self.gg.setMenuEnabled(False)
        for ax in ("left", "bottom"):
            self.gg.getAxis(ax).enableAutoSIPrefix(False)
        split.addWidget(self.gg)
        split.setSizes([560, 460])
        lay.addWidget(split)
        self.marker = None
        self.ref_marker = None

    # ------------------------------------------------------------ updates
    def refresh(self):
        have = {c.n for c in self.win.a.corners}
        self.turns = [n for n in self.turns if n in have]
        self.laps_changed()

    def laps_changed(self):
        self._fill()
        self._draw_gg()
        self.cursor(self.win.cursor_d)
        self.ref_cursor(self.win.ref_d)
        self._apply_loop()

    def cursor(self, d: float):
        w, a = self.win, self.win.a
        ld = w.focus_lap()
        if self.marker is None or ld is None or ld.lat_g is None or ld.lon_g is None:
            return
        k = self._k()
        self.marker.setData([k * float(np.interp(d, a.grid, a.channel_on_grid(ld, "lat_g")))],
                            [k * float(np.interp(d, a.grid, a.channel_on_grid(ld, "lon_g")))])

    def ref_cursor(self, rd: float | None):
        """Where the reference video is (None: no reference video showing)."""
        a = self.win.a
        ref = a.ref
        if self.ref_marker is None:
            return
        if rd is None or ref is None or ref.lat_g is None or ref.lon_g is None:
            self.ref_marker.setData([], [])
            return
        k = self._k()
        self.ref_marker.setData([k * float(np.interp(rd, a.grid, a.channel_on_grid(ref, "lat_g")))],
                                [k * float(np.interp(rd, a.grid, a.channel_on_grid(ref, "lon_g")))])

    def _k(self) -> float:
        """+1: car's acceleration; -1: as felt in the seat (both axes flip)."""
        return -1.0 if self.felt.isChecked() else 1.0

    def _felt_toggled(self, on: bool):
        settings().setValue("analysis/gg_felt", on)
        self._draw_gg()

    def show_turn(self, n: int):
        """Pick turn n (from the Overview's biggest losses)."""
        self._pick([n], jump=True)

    def clear_turns(self):
        self._pick([], jump=False)

    # ------------------------------------------------------------ table
    def _fill(self):
        self._filling = True
        try:
            self._fill_rows()
        finally:
            self._filling = False

    def _fill_rows(self):
        w, a, t = self.win, self.win.a, self.table
        t.setRowCount(0)
        ld = w.focus_lap()
        t.setHorizontalHeaderLabels(["Turn", "Time vs ref", "Grip used",
                                     f"Min speed ({w.speed_unit()})",
                                     f"Braked ({w.dist_unit()} vs ref)", "Best lap here"])
        hdr = t.horizontalHeaderItem(5)
        if hdr is not None:
            hdr.setToolTip("The quickest any loaded lap went through this turn, and how much "
                           "quicker than this lap it was.\nAdd them up and that's the time "
                           "on the table if every turn matched the best you've done there.")
        if ld is None or a.ref is None:
            self.caption.setText("")
            return
        is_ref = ld is a.ref
        res = self._sorted(a.corner_results(ld))
        named = a.profile is not None and a.profile.turns
        head = (f"<b>{w.name(ld)}</b> is the reference; pick another lap in the top bar to "
                "compare. " if is_ref else
                f"<b>{w.name(ld)}</b> vs reference: {fmt_gap(ld.time - a.ref.time)} s. ")
        self.caption.setText(
            head + (f"Turns from the {a.profile.name} map. " if named else
                    "Corners found from speed (Track → Track turns… names them like the map). ")
            + "Click a turn to loop it on the video; shift-click for a run of turns.")
        t.setRowCount(len(res))
        for r, cr in enumerate(res):
            c = cr.corner
            if is_ref or cr.brake_d is None or cr.brake_d_ref is None:
                brake = ""
            else:
                diff = w.x(cr.brake_d - cr.brake_d_ref)
                brake = (f"{abs(diff):.0f} later" if diff > 0 else
                         f"{abs(diff):.0f} earlier" if diff < 0 else "same")
            g = cr.grip
            have_g = g is not None and g.used is not None and g.used_ref is not None
            if not have_g:
                grip = ""
            elif is_ref:
                grip = f"{g.used * 100:.0f}%"
            else:
                grip = f"{g.used * 100:.0f}% / {g.used_ref * 100:.0f}%"
            vmin = (f"{w.spd(cr.vmin):.0f}" if is_ref else
                    f"{w.spd(cr.vmin):.0f} / {w.spd(cr.vmin_ref):.0f}")
            to_best = cr.lost - cr.best_lost          # >= 0: how much quicker the best lap was
            best_ld = w.lap(cr.best_key) if cr.best_key is not None else None
            if best_ld is None or to_best < 0.005 or cr.best_key == ld.key:
                best = "this lap"
            else:
                best = f"L{best_ld.lap.n}  {to_best:.2f} quicker"
                if len(a.sessions) > 1:
                    best = f"{w.label(best_ld)}  {to_best:.2f} quicker"
            cells = [c.name + (f" ({c.direction})" if c.direction else ""),
                     "" if is_ref else fmt_gap(cr.lost), grip, vmin, brake, best]
            for col, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                it.setData(QtCore.Qt.UserRole, c.n)
                if col == 1:
                    it.setForeground(QtGui.QColor(LOSS if cr.lost > 0.05 else
                                                  GAIN if cr.lost < -0.05 else NEUTRAL))
                if col == 2 and have_g and not is_ref:
                    it.setForeground(QtGui.QColor(LOSS if g.used < g.used_ref - 0.05 else
                                                  GAIN if g.used > g.used_ref + 0.05 else NEUTRAL))
                    it.setToolTip(f"Grip used while cornering/braking: this lap {g.used:.0%}, "
                                  f"reference {g.used_ref:.0%}. Peak {g.peak_g:.2f} g vs "
                                  f"{g.peak_g_ref:.2f} g; below 80% for {g.soft_s:.1f} s vs "
                                  f"{g.soft_s_ref:.1f} s.")
                if col == 5 and best != "this lap":
                    it.setForeground(QtGui.QColor(LOSS if to_best > 0.1 else NEUTRAL))
                if col > 0:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                t.setItem(r, col, it)
        self._select_rows()
        t.resizeColumnsToContents()

    def _sorted(self, res):
        how = self.sort.currentData()
        if how == "lost":
            return sorted(res, key=lambda r: -r.lost)
        if how == "gained":
            return sorted(res, key=lambda r: r.lost)
        if how == "grip":
            def used(r):
                g = r.grip
                return g.used if g is not None and g.used is not None else 9.0
            return sorted(res, key=used)
        if how == "best":
            return sorted(res, key=lambda r: r.best_lost - r.lost)
        return sorted(res, key=lambda r: r.corner.start_d)

    def _sort_changed(self, _i):
        settings().setValue("analysis/corner_sort", self.sort.currentData())
        self._fill()

    def _select_rows(self):
        t = self.table
        was = self._filling
        self._filling = True
        t.clearSelection()
        sm = t.selectionModel()
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it is not None and it.data(QtCore.Qt.UserRole) in self.turns:
                sm.select(t.model().index(r, 0), QtCore.QItemSelectionModel.Select
                          | QtCore.QItemSelectionModel.Rows)
        self._filling = was

    def _selection_changed(self):
        if self._filling:
            return
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        ns = [self.table.item(r, 0).data(QtCore.Qt.UserRole) for r in sorted(rows)]
        # defer: never redraw from inside the table's own click
        QtCore.QTimer.singleShot(0, lambda: self._pick(ns, jump=True, from_table=True))

    def _picked(self):
        """Picked corners in track order, and the stretch they cover."""
        cs = sorted((c for c in self.win.a.corners if c.n in self.turns),
                    key=lambda c: c.start_d)
        if not cs:
            return cs, None
        return cs, (cs[0].start_d, max(c.end_d for c in cs))

    def _pick(self, ns, jump: bool, from_table: bool = False):
        self.turns = list(ns)
        if not from_table:
            self._select_rows()
        self._draw_gg()
        cs, span = self._picked()
        w = self.win
        if span is None:
            w.set_loop(None)
            w.zoom(None, None)
            self.cursor(w.cursor_d)
            return
        d0, d1 = span
        w.zoom(d0, d1)
        self._apply_loop()
        if jump:
            # land on the approach so the video shows the braking zone
            w.set_cursor(max(d0, cs[0].apex_d - 250.0), seek=True)

    def _apply_loop(self):
        _cs, span = self._picked()
        self.win.set_loop(span if span is not None and self.loop.isChecked() else None)

    def _loop_toggled(self, on):
        settings().setValue("analysis/corner_loop", on)
        self._apply_loop()

    # --------------------------------------------------------------- g-g
    def _draw_gg(self):
        self.gg.clear()
        self.marker = None
        self.ref_marker = None
        w, a = self.win, self.win.a
        if a.ref is None:
            return
        grid_pen = pg.mkPen("#3a3a37", width=1)
        th = np.linspace(0, 2 * np.pi, 121)
        for r in (0.5, 1.0, 1.5):
            self.gg.plot(r * np.cos(th), r * np.sin(th), pen=grid_pen)
            lbl = pg.TextItem(f"{r:g} g", color="#6b6a64", anchor=(0, 1))
            lbl.setPos(r * 0.71, r * 0.71)
            self.gg.addItem(lbl)
        self.gg.plot([-1.6, 1.6], [0, 0], pen=grid_pen)
        self.gg.plot([0, 0], [-1.6, 1.6], pen=grid_pen)
        k = self._k()
        felt = k < 0
        self.gg.setLabel("bottom", "Pushed sideways (g)" if felt else "Lateral g (right turn +)")
        self.gg.setLabel("left", "Pushed forward + / back – (g)" if felt
                         else "Brake – / accel + (g)")
        for txt, x, y, anc in (("Braking", 0, -1.55 * k, (0.5, 0 if not felt else 1)),
                               ("Accel", 0, 1.55 * k, (0.5, 1 if not felt else 0)),
                               ("Left turn", -1.55 * k, 0, (0 if not felt else 1, 1)),
                               ("Right turn", 1.55 * k, 0, (1 if not felt else 0, 1))):
            t = pg.TextItem(txt, color="#8a8983", anchor=anc)
            t.setPos(x, y)
            self.gg.addItem(t)
        if a.envelope is not None:
            o = a.envelope.outline()
            self.gg.plot(k * o[:, 0], k * o[:, 1], pen=pg.mkPen(LIMIT, width=2,
                                                        style=QtCore.Qt.DashLine))
            # label it on the cornering side, so it isn't taken for a lap
            th = np.deg2rad(20)
            r = float(a.envelope.at(th))
            lbl = pg.TextItem("your limit", color=LIMIT, anchor=(0 if k > 0 else 1, 0.5))
            lbl.setPos(k * (r * np.cos(th) + 0.04), k * r * np.sin(th))
            self.gg.addItem(lbl)
        ld = w.focus_lap() or a.ref
        cs, span = self._picked()
        for lap, col, size in ((a.ref, REF_COLOR, 3), (ld if ld is not a.ref else None, None, 4)):
            if lap is None or lap.lat_g is None or lap.lon_g is None:
                continue
            col = col or w.color(lap.key)
            q = QtGui.QColor(col)
            fast = lap.v > 30
            if span is None:
                alpha = 70 if lap is a.ref and ld is not a.ref else 170
                self.gg.plot(k * lap.lat_g[fast], k * lap.lon_g[fast], pen=None, symbol="o",
                             symbolSize=size, symbolPen=None,
                             symbolBrush=pg.mkBrush(q.red(), q.green(), q.blue(), alpha))
            else:
                inside = fast & (lap.d >= span[0]) & (lap.d <= span[1])
                self.gg.plot(k * lap.lat_g[inside], k * lap.lon_g[inside],
                             pen=pg.mkPen(col, width=1.2),
                             symbol="o", symbolSize=size + 2, symbolPen=None,
                             symbolBrush=pg.mkBrush(col))
        if cs:
            names = cs[0].name if len(cs) == 1 else f"{cs[0].name}–{cs[-1].name}"
            t = pg.TextItem(f"{names}: this lap vs reference (grey)", color="#d8d7d0",
                            anchor=(0, 0))
            t.setPos(-1.6, 1.6)
            self.gg.addItem(t)
        # reference video's dot (grey, under) and yours (your lap's colour)
        self.ref_marker = self.gg.plot([], [], pen=None, symbol="o", symbolSize=13,
                                       symbolPen=pg.mkPen(CURSOR, width=2),
                                       symbolBrush=pg.mkBrush(REF_COLOR))
        self.marker = self.gg.plot([0], [0], pen=None, symbol="o", symbolSize=14,
                                   symbolPen=pg.mkPen(CURSOR, width=2),
                                   symbolBrush=pg.mkBrush(w.color(ld.key)))
        self.gg.setRange(xRange=(-1.6, 1.6), yRange=(-1.6, 1.6), padding=0.02)
        self.cursor(w.cursor_d)
        self.ref_cursor(w.ref_d)

