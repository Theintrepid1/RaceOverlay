# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""The lap list (sessions → laps, with gap, grip and sectors), used on more
than one tab. It only reports clicks; the window decides what changes and
calls restyle() afterwards. It never rebuilds itself from inside a click
(that deleted the row Qt was still handling and crashed)."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .common import NEUTRAL, PURPLE, SERIES, fmt_gap, fmt_lap


class LapTree(QtWidgets.QTreeWidget):
    focused = QtCore.Signal(object)          # lap key
    checked = QtCore.Signal(object, bool)    # lap key, shown on the plots

    def __init__(self, compact: bool = False, parent=None):
        super().__init__(parent)
        self.compact = compact
        cols = ["Lap", "Gap"] if compact else ["Lap", "Gap", "Grip", "S1", "S2", "S3"]
        self.setHeaderLabels(cols)
        self.ncol = len(cols)
        if not compact:
            self.headerItem().setToolTip(
                2, "Grip used while cornering and braking, against the best you've "
                   "shown the car can do (100%)")
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.header().setStretchLastSection(False)
        self.itemChanged.connect(self._item_changed)
        self.currentItemChanged.connect(self._current_changed)
        # clicking the row you're already on brings it back if it was unticked
        self.itemClicked.connect(lambda it, col: col > 0 and self._current_changed(it, None))

    def rebuild(self, win):
        a = win.a
        self.blockSignals(True)
        self.clear()
        if a.ref is None:
            self.blockSignals(False)
            return
        best = a.ref.time
        bsec = [t for t, _ in a.best_sectors()]
        for si, s in enumerate(a.sessions):
            vid = "video" if s.video else "no video"
            top = QtWidgets.QTreeWidgetItem([f"{s.name}  ({vid})"])
            top.setData(0, QtCore.Qt.UserRole, ("session", si))
            top.setFirstColumnSpanned(True)
            top.setToolTip(0, f"{s.name}\n{s.path}")
            top.setFlags(top.flags() & ~QtCore.Qt.ItemIsSelectable)
            self.addTopLevelItem(top)
            for ld in [x for x in a.laps if x.session == si]:
                row = [f"Lap {ld.lap.n}  –  {fmt_lap(ld.time)}",
                       "ref" if ld is a.ref else (fmt_gap(ld.time - best) if ld.timed else "slow")]
                if not self.compact:
                    lg = a.lap_grip(ld)
                    row += [f"{lg * 100:.0f}%" if lg is not None else ""]
                    row += [f"{t:.2f}" for t in ld.sectors]
                it = QtWidgets.QTreeWidgetItem(row)
                it.setData(0, QtCore.Qt.UserRole, ("lap", ld.key))
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(0, QtCore.Qt.Unchecked)
                for c in range(1, self.ncol):
                    it.setTextAlignment(c, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if ld is a.ref:
                    f = it.font(0)
                    f.setBold(True)
                    for c in range(self.ncol):
                        it.setFont(c, f)
                if not self.compact:
                    for k, t in enumerate(ld.sectors):
                        if ld.timed and abs(t - bsec[k]) < 1e-9:
                            it.setForeground(3 + k, QtGui.QColor(PURPLE))
                if not ld.timed:
                    for c in range(self.ncol):
                        it.setForeground(c, QtGui.QColor(NEUTRAL))
                top.addChild(it)
            top.setExpanded(True)
        for c in range(1, self.ncol):
            self.resizeColumnToContents(c)
        self._fit_lap_column()
        self.blockSignals(False)
        self.restyle(win)

    def _fit_lap_column(self):
        """Size the Lap column to the 'Lap 5 – 2:19.806' rows, not the session
        name (that row spans the whole width and is cut off with '…')."""
        opt = QtWidgets.QStyleOptionViewItem()
        self.initViewItemOption(opt)
        widest = 0
        for it in self.lap_items():
            idx = self.indexFromItem(it, 0)
            widest = max(widest, self.itemDelegate(idx).sizeHint(opt, idx).width())
        if widest:
            self.setColumnWidth(0, widest + 2 * self.indentation() + 6)

    def lap_items(self):
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            for j in range(top.childCount()):
                yield top.child(j)

    def restyle(self, win):
        """Check marks, colours and the current row, on the existing rows."""
        a = win.a
        self.blockSignals(True)
        default = self.palette().color(QtGui.QPalette.Text)
        for it in self.lap_items():
            key = it.data(0, QtCore.Qt.UserRole)[1]
            ld = win.lap(key)
            if ld is None:
                continue
            shown = key in win.compare or ld is a.ref
            want = QtCore.Qt.Checked if shown else QtCore.Qt.Unchecked
            if it.checkState(0) != want:
                it.setCheckState(0, want)
            if key in win.compare:
                it.setForeground(0, QtGui.QColor(SERIES[win.compare.index(key)]))
            elif ld.timed:
                it.setForeground(0, default)
            if key == win.focus and self.currentItem() is not it:
                self.setCurrentItem(it)
        self.blockSignals(False)

    def _item_changed(self, it, col):
        data = it.data(0, QtCore.Qt.UserRole)
        if data and data[0] == "lap" and col == 0:
            self.checked.emit(data[1], it.checkState(0) == QtCore.Qt.Checked)

    def _current_changed(self, it, _prev):
        if it is None:
            return
        data = it.data(0, QtCore.Qt.UserRole)
        if data and data[0] == "lap":
            self.focused.emit(data[1])
