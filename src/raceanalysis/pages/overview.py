# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Overview tab: how the day went. Headline numbers, every lap time as a
chart, the three turns where the selected lap lost the most (with a reason),
and the lap list."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from raceoverlay.syncgui import _btn, _note

from ..common import (INK, INK_DIM, INK_FAINT, LOSS, NEUTRAL, PANEL, PURPLE, REF_COLOR,
                      fmt_gap, fmt_lap)
from ..laplist import LapTree


def loss_reason(w, cr, ld=None) -> str:
    """One line on why a turn cost time, from the biggest difference."""
    cands = []
    dv = w.spd(cr.vmin) - w.spd(cr.vmin_ref)
    if dv <= -2:
        cands.append((abs(dv) / 4, f"min speed {abs(dv):.0f} {w.speed_unit()} lower"))
    elif dv >= 2:
        cands.append((dv / 8, f"min speed {dv:.0f} {w.speed_unit()} higher, lost on exit"))
    if cr.brake_d is not None and cr.brake_d_ref is not None:
        db = w.x(cr.brake_d - cr.brake_d_ref)
        if abs(db) >= (40 if w.use_mph else 12):
            word = "earlier" if db < 0 else "later"
            cands.append((abs(db) / (120 if w.use_mph else 36),
                          f"braked {abs(db):.0f} {w.dist_unit()} {word}"))
    g = cr.grip
    if g is not None and g.used is not None and g.used_ref is not None:
        du = (g.used - g.used_ref) * 100
        if du <= -6:
            cands.append((abs(du) / 10, f"used {g.used:.0%} of the grip vs {g.used_ref:.0%}"))
    if not cands:
        return _where_lost(w, cr, ld) if ld is not None else "slower all through this stretch"
    return max(cands)[1]


def _where_lost(w, cr, ld) -> str:
    """No single cause: say whether the time went before or after the apex."""
    a, c = w.a, cr.corner
    dl = a.delta(ld)
    d0, da, d1 = (float(np.interp(x, a.grid, dl)) for x in (c.start_d, c.apex_d, c.end_d))
    tot = d1 - d0
    if tot <= 0:
        return "slower all through this stretch"
    if da - d0 > 0.65 * tot:
        return "lost on entry, up to the apex"
    if d1 - da > 0.65 * tot:
        return "lost after the apex, on the exit"
    return "a little everywhere: entry, apex and exit"


class _Card(QtWidgets.QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setStyleSheet(f"_Card {{ background:{PANEL}; border-radius:6px; }}")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(1)
        self.t = QtWidgets.QLabel(title.upper())
        self.t.setStyleSheet(f"color:{INK_FAINT}; font-size:10px; letter-spacing:1px;")
        self.v = QtWidgets.QLabel("–")
        f = self.v.font()
        f.setPixelSize(26)
        f.setBold(True)
        self.v.setFont(f)
        self.v.setStyleSheet(f"color:{INK};")
        self.s = QtWidgets.QLabel("")
        self.s.setStyleSheet(f"color:{INK_DIM}; font-size:11px;")
        for w in (self.t, self.v, self.s):
            lay.addWidget(w)

    def set(self, value: str, sub: str = "", color: str = INK):
        self.v.setText(value)
        self.v.setStyleSheet(f"color:{color};")
        self.s.setText(sub)


class _LossCard(QtWidgets.QFrame):
    clicked = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.n = None
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setStyleSheet(f"_LossCard {{ background:{PANEL}; border-radius:6px; }}"
                           f"_LossCard:hover {{ background:#202024; }}")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        self.name = QtWidgets.QLabel("")
        f = self.name.font()
        f.setPixelSize(20)
        f.setBold(True)
        self.name.setFont(f)
        self.name.setMinimumWidth(70)
        self.name.setStyleSheet(f"color:{INK};")
        self.lost = QtWidgets.QLabel("")
        self.lost.setFont(f)
        self.lost.setStyleSheet(f"color:{LOSS};")
        self.why = QtWidgets.QLabel("")
        self.why.setStyleSheet(f"color:{INK_DIM};")
        self.why.setWordWrap(True)
        lay.addWidget(self.name)
        lay.addWidget(self.lost)
        lay.addWidget(self.why, 1)
        go = QtWidgets.QLabel("›")
        go.setStyleSheet(f"color:{INK_FAINT}; font-size:20px;")
        lay.addWidget(go)

    def set(self, n, name, lost, why):
        self.n = n
        self.name.setText(name)
        self.lost.setText(f"{lost:+.2f} s")
        self.why.setText(why)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton and self.n is not None:
            self.clicked.emit(self.n)


class _LapAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [fmt_lap(v, 1) for v in values]


class OverviewPage(QtWidgets.QWidget):
    title = "Overview"
    video_default = False

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.stack = QtWidgets.QStackedLayout(self)

        # ---- empty: what to do first
        empty = QtWidgets.QWidget()
        el = QtWidgets.QVBoxLayout(empty)
        el.addStretch(1)
        hello = QtWidgets.QLabel("Race Analysis")
        f = hello.font()
        f.setPixelSize(28)
        f.setBold(True)
        hello.setFont(f)
        el.addWidget(hello, 0, QtCore.Qt.AlignHCenter)
        el.addWidget(_note("<b>At the track:</b> import the new recordings off the GoPro card; "
                           "they're matched to your data files and synced.<br>"
                           "<b>Later:</b> open a track day you imported before."),
                     0, QtCore.Qt.AlignHCenter)
        brow = QtWidgets.QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(_btn("Import from GoPro card…", win.import_card, big=True))
        brow.addWidget(_btn("Open track day…", win.open_day, big=True))
        brow.addWidget(_btn("Add data file…", win.add_session_dialog, big=True))
        brow.addStretch(1)
        el.addLayout(brow)
        el.addStretch(2)
        self.stack.addWidget(empty)

        # ---- the day
        page = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        cards = QtWidgets.QHBoxLayout()
        self.c_best = _Card("Best lap")
        self.c_theo = _Card("Theoretical best")
        self.c_top = _Card("Top speed")
        self.c_grip = _Card("Grip used, best lap")
        self.c_laps = _Card("Laps")
        for c in (self.c_best, self.c_theo, self.c_top, self.c_grip, self.c_laps):
            cards.addWidget(c)
        pl.addLayout(cards)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.laps = LapTree()
        self.laps.focused.connect(win.lap_focused)
        self.laps.checked.connect(win.lap_checked)
        split.addWidget(self.laps)
        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.chart = pg.PlotWidget(axisItems={"left": _LapAxis("left")})
        self.chart.setMenuEnabled(False)
        self.chart.setMouseEnabled(False, False)
        self.chart.hideButtons()
        self.chart.showGrid(y=True, alpha=0.15)
        self.chart.getAxis("bottom").setStyle(showValues=False)
        self.chart.setLabel("left", "Lap time")
        self.chart.scene().sigMouseClicked.connect(self._chart_clicked)
        rl.addWidget(self.chart, 3)
        self.loss_title = _note("")
        rl.addWidget(self.loss_title)
        self.loss_cards = [_LossCard() for _ in range(3)]
        for c in self.loss_cards:
            c.clicked.connect(win.goto_turn)
            rl.addWidget(c)
        split.addWidget(right)
        split.setSizes([520, 680])
        pl.addWidget(split, 1)
        self.stack.addWidget(page)
        self._bars: list[tuple[float, tuple]] = []

    # ------------------------------------------------------------ updates
    def refresh(self):
        a = self.win.a
        self.stack.setCurrentIndex(0 if a.ref is None else 1)
        if a.ref is None:
            return
        self.laps.rebuild(self.win)
        self._cards()
        self.laps_changed()

    def laps_changed(self):
        if self.win.a.ref is None:
            return
        self.laps.restyle(self.win)
        self._chart()
        self._losses()

    def cursor(self, d: float):
        pass

    # ------------------------------------------------------------- parts
    def _cards(self):
        w, a = self.win, self.win.a
        timed = a.timed_laps()
        best = a.ref
        fastest = min(timed, key=lambda ld: ld.time)
        self.c_best.set(fmt_lap(fastest.time), w.label(fastest), PURPLE)
        theo = a.theoretical_best()
        parts = " + ".join(f"S{k + 1} L{ld.lap.n}" for k, (_, ld) in enumerate(a.best_sectors()))
        self.c_theo.set(fmt_lap(theo), f"{fmt_gap(theo - fastest.time)} s to find · {parts}")
        top = max(timed, key=lambda ld: float(np.nanmax(ld.v)))
        self.c_top.set(f"{w.spd(np.nanmax(top.v)):.0f} {w.speed_unit()}", w.label(top))
        g = a.lap_grip(best)
        self.c_grip.set(f"{g:.0%}" if g is not None else "–",
                        f"{w.label(best)} (reference)" if g is not None else "no g data")
        n_s = len(a.sessions)
        self.c_laps.set(f"{len(timed)}", f"timed, {n_s} session{'s' if n_s != 1 else ''}")

    def _chart(self):
        w, a = self.win, self.win.a
        self.chart.clear()
        self._bars = []
        timed = a.timed_laps()
        if not timed:
            return
        fastest = min(ld.time for ld in timed)
        cap = fastest * 1.25
        x = 0.0
        for si, s in enumerate(a.sessions):
            laps = [ld for ld in a.laps if ld.session == si]
            if si:
                self.chart.addItem(pg.InfiniteLine(pos=x - 0.75, angle=90,
                                                   pen=pg.mkPen("#3a3a37", style=QtCore.Qt.DashLine)))
            lbl = pg.TextItem(s.name, color=INK_DIM, anchor=(0, 0))
            lbl.setPos(x - 0.4, cap)
            self.chart.addItem(lbl)
            for ld in laps:
                h = min(ld.time, cap)
                if ld.key == w.focus or ld.key in w.compare:
                    col = w.color(ld.key)
                elif ld is a.ref:
                    col = REF_COLOR
                elif not ld.timed:
                    col = "#3a3a37"
                else:
                    col = "#4b5563"
                if ld.timed and abs(ld.time - fastest) < 1e-9 and ld.key != w.focus:
                    col = PURPLE
                bar = pg.BarGraphItem(x=[x], height=[h - fastest * 0.97], y0=[fastest * 0.97],
                                      width=0.75, brush=pg.mkBrush(col), pen=pg.mkPen(None))
                self.chart.addItem(bar)
                txt = pg.TextItem(f"L{ld.lap.n}", color=INK if ld.timed else NEUTRAL,
                                  anchor=(0.5, 1))
                txt.setPos(x, fastest * 0.97)
                self.chart.addItem(txt)
                self._bars.append((x, ld.key))
                x += 1.0
            x += 0.6
        self.chart.setYRange(fastest * 0.97, cap * 1.01, padding=0)
        self.chart.setXRange(-0.8, x - 0.2, padding=0)

    def _chart_clicked(self, ev):
        if ev.button() != QtCore.Qt.LeftButton or not self._bars:
            return
        p = self.chart.getViewBox().mapSceneToView(ev.scenePos())
        xs = np.array([b[0] for b in self._bars])
        i = int(np.argmin(np.abs(xs - p.x())))
        if abs(xs[i] - p.x()) <= 0.5:
            self.win.lap_focused(self._bars[i][1])

    def _losses(self):
        w, a = self.win, self.win.a
        ld = w.focus_lap()
        for c in self.loss_cards:
            c.hide()
        if ld is None or ld is a.ref:
            self.loss_title.setText("<b>Where the time goes:</b> pick a lap other than the "
                                    "reference in the top bar.")
            return
        res = sorted(a.corner_results(ld), key=lambda r: -r.lost)
        res = [r for r in res if r.lost > 0.05][:3]
        total = ld.time - a.ref.time
        self.loss_title.setText(f"<b>Where {w.name(ld)} lost the most</b> vs the reference "
                                f"({fmt_gap(total)} s overall). Click one to open the turn.")
        for card, cr in zip(self.loss_cards, res):
            card.set(cr.corner.n, cr.corner.name, cr.lost, loss_reason(w, cr, ld))
            card.show()
        if not res:
            self.loss_title.setText(f"<b>{w.name(ld)}</b> didn't lose time anywhere against "
                                    "the reference.")

