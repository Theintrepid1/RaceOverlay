# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""The math channels editor, shared by Race Overlay and Race Analysis.

A list of the library's channels (tick = on), and for the picked one its
name, formula, units and note, checked live against the open file with a
preview of what it gives. The file's channels are listed by the name to type
(double-click puts one in the formula)."""
from __future__ import annotations

import html

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from racecore import mathch
from racecore.mathch import FormulaError, MathChannel, MissingInput

HELP = (
    "<b>Maths:</b> + − * / ** % ( ) &nbsp; &lt; &lt;= &gt; &gt;= == != &nbsp; and or not"
    "<br><b>Functions:</b> abs min max sqrt hypot clip(x, lo, hi) round floor ceil log exp "
    "sin cos &nbsp; <b>if</b>(cond, a, b) &nbsp; <b>smooth</b>(x, 0.2) moving average "
    "over 0.2 s &nbsp; <b>rate</b>(x) change per second &nbsp; <b>integral</b>(x) running "
    "total over time &nbsp; <b>prev</b>(x, 1) the value 1 s earlier &nbsp; pi nan"
    "<br>speed is km/h (speed_mph, speed_ms for mph and m/s); throttle and brake are "
    "0–100 %. Columns are named in lower case with _ for spaces, or col(\"Exact "
    "Name\"); other math channels the same way. # starts a comment.")


def _mono() -> QtGui.QFont:
    f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    f.setPointSizeF(max(f.pointSizeF(), 10.0))
    return f


class MathDialog(QtWidgets.QDialog):
    """tels: the open files (the first is used for the check and preview)."""

    def __init__(self, parent, tels=(), select: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Math channels")
        self.tels = [t for t in tels if t is not None]
        self.chans = [MathChannel(**vars(c)) for c in mathch.library()]
        self._loading = False
        lay = QtWidgets.QHBoxLayout(self)

        # ---- left: the library
        left = QtWidgets.QVBoxLayout()
        self.list = QtWidgets.QListWidget()
        self.list.setMinimumWidth(220)
        self.list.currentRowChanged.connect(self._picked)
        self.list.itemChanged.connect(self._ticked)
        left.addWidget(self.list, 1)
        row = QtWidgets.QHBoxLayout()
        b_new = QtWidgets.QPushButton("New")
        b_new.clicked.connect(lambda: self._add(MathChannel("New channel", "speed_mph")))
        row.addWidget(b_new)
        b_pre = QtWidgets.QPushButton("Add preset")
        menu = QtWidgets.QMenu(b_pre)
        for p in mathch.PRESETS:
            a = menu.addAction(p.name, lambda p=p: self._add(MathChannel(**vars(p))))
            a.setToolTip(p.note)
        menu.setToolTipsVisible(True)
        b_pre.setMenu(menu)
        row.addWidget(b_pre)
        left.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.b_dup = QtWidgets.QPushButton("Duplicate")
        self.b_dup.clicked.connect(self._duplicate)
        self.b_del = QtWidgets.QPushButton("Delete")
        self.b_del.clicked.connect(self._delete)
        row.addWidget(self.b_dup)
        row.addWidget(self.b_del)
        left.addLayout(row)
        note = QtWidgets.QLabel("Ticked ones show up in both apps for any file that has "
                                "what they use.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        left.addWidget(note)
        lay.addLayout(left, 1)

        # ---- right: the picked one
        right = QtWidgets.QVBoxLayout()
        self.editor = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.editor)
        form.setContentsMargins(0, 0, 0, 0)
        self.name_ed = QtWidgets.QLineEdit()
        self.name_ed.textEdited.connect(self._edited)
        form.addRow("Name", self.name_ed)
        self.formula_ed = QtWidgets.QPlainTextEdit()
        self.formula_ed.setFont(_mono())
        self.formula_ed.setTabChangesFocus(True)
        self.formula_ed.setFixedHeight(QtGui.QFontMetrics(_mono()).lineSpacing() * 5 + 14)
        self.formula_ed.textChanged.connect(self._edited)
        form.addRow("Formula", self.formula_ed)
        urow = QtWidgets.QHBoxLayout()
        self.unit_ed = QtWidgets.QLineEdit()
        self.unit_ed.setPlaceholderText("e.g. hp, %, m")
        self.unit_ed.setMaximumWidth(120)
        self.unit_ed.textEdited.connect(self._edited)
        urow.addWidget(self.unit_ed)
        urow.addStretch(1)
        form.addRow("Units", urow)
        self.note_ed = QtWidgets.QLineEdit()
        self.note_ed.setPlaceholderText("What it's for (optional)")
        self.note_ed.textEdited.connect(self._edited)
        form.addRow("Note", self.note_ed)
        right.addWidget(self.editor)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.RichText)
        right.addWidget(self.status)
        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(140)
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setLabel("bottom", "Time (s)")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve = self.plot.plot([], [], pen=pg.mkPen("#4aa3ff", width=1.5))
        right.addWidget(self.plot, 1)

        names = QtWidgets.QLabel("<b>This file's channels</b> (double-click to put one in "
                                 "the formula):")
        right.addWidget(names)
        self.names = QtWidgets.QListWidget()
        self.names.setFlow(QtWidgets.QListView.LeftToRight)
        self.names.setWrapping(True)
        self.names.setResizeMode(QtWidgets.QListView.Adjust)
        self.names.setSpacing(2)
        self.names.setMaximumHeight(110)
        self.names.itemDoubleClicked.connect(self._insert)
        right.addWidget(self.names)
        self._fill_names()
        hl = QtWidgets.QLabel(HELP)
        hl.setWordWrap(True)
        hl.setStyleSheet("color: gray;")
        right.addWidget(hl)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save
                                        | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        right.addWidget(bb)
        lay.addLayout(right, 3)
        self.resize(1040, 720)

        self._fill_list()
        i = next((k for k, c in enumerate(self.chans) if c.name == select), 0)
        self.list.setCurrentRow(i if self.chans else -1)
        self._picked(self.list.currentRow())

    # ----- the list
    def _fill_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for c in self.chans:
            it = QtWidgets.QListWidgetItem(c.name)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if c.on else QtCore.Qt.Unchecked)
            it.setToolTip(c.note or c.formula)
            self.list.addItem(it)
        self.list.blockSignals(False)

    def _unique(self, name: str) -> str:
        taken = {mathch.ident(c.name) for c in self.chans}
        out, k = name, 2
        while mathch.ident(out) in taken:
            out = f"{name} {k}"
            k += 1
        return out

    def _add(self, c: MathChannel):
        c.name = self._unique(c.name)
        self.chans.append(c)
        self._fill_list()
        self.list.setCurrentRow(len(self.chans) - 1)
        self.formula_ed.setFocus()

    def _duplicate(self):
        c = self._current()
        if c is not None:
            self._add(MathChannel(c.name + " copy", c.formula, c.unit, c.on, c.note))

    def _delete(self):
        r = self.list.currentRow()
        if 0 <= r < len(self.chans):
            del self.chans[r]
            self._fill_list()
            self.list.setCurrentRow(min(r, len(self.chans) - 1))
            self._picked(self.list.currentRow())

    def _ticked(self, it):
        r = self.list.row(it)
        if 0 <= r < len(self.chans):
            self.chans[r].on = it.checkState() == QtCore.Qt.Checked

    def _current(self) -> MathChannel | None:
        r = self.list.currentRow()
        return self.chans[r] if 0 <= r < len(self.chans) else None

    def _picked(self, r: int):
        c = self._current()
        self.editor.setEnabled(c is not None)
        self.b_dup.setEnabled(c is not None)
        self.b_del.setEnabled(c is not None)
        self._loading = True
        self.name_ed.setText(c.name if c else "")
        self.formula_ed.setPlainText(c.formula if c else "")
        self.unit_ed.setText(c.unit if c else "")
        self.note_ed.setText(c.note if c else "")
        self._loading = False
        self._check()

    def _edited(self, *_):
        c = self._current()
        if c is None or self._loading:
            return
        c.name = self.name_ed.text().strip()
        c.formula = self.formula_ed.toPlainText()
        c.unit = self.unit_ed.text().strip()
        c.note = self.note_ed.text().strip()
        it = self.list.currentItem()
        if it is not None and it.text() != c.name:
            self.list.blockSignals(True)
            it.setText(c.name)
            self.list.blockSignals(False)
        self._check()

    # ----- checking against the file
    def _check(self):
        c = self._current()
        self.curve.setData([], [])
        if c is None:
            self.status.setText("Add a channel: New, or start from a preset.")
            return
        if not c.name:
            return self._say("bad", "Give it a name.")
        if sum(mathch.ident(x.name) == mathch.ident(c.name) for x in self.chans) > 1:
            return self._say("bad", "Another math channel already has that name.")
        try:
            mathch.parse(c.formula)
        except FormulaError as e:
            return self._say("bad", str(e))
        tel = self.tels[0] if self.tels else None
        if tel is None:
            return self._say("ok", "The formula reads fine. Open a file to see what it gives.")
        try:
            v = mathch.evaluate(tel, c, self.chans)
        except MissingInput as e:
            return self._say("warn", f"{e} It won't show up for this file, but will for "
                                     "files that have it.")
        except FormulaError as e:
            return self._say("bad", str(e))
        ok = np.isfinite(v)
        if not ok.any():
            return self._say("warn", "Works, but gives no numbers for this file (all blank).")
        self.curve.setData(tel.time, np.where(ok, v, np.nan), connect="finite")
        u = f" {c.unit}" if c.unit else ""
        self._say("ok", f"Works for this file: {np.nanmin(v):.4g} to {np.nanmax(v):.4g}{u}, "
                        f"average {np.nanmean(v):.4g}{u}.")

    def _say(self, kind: str, text: str):
        col = {"ok": "#199e70", "warn": "#c98500", "bad": "#e66767"}[kind]
        mark = {"ok": "✓", "warn": "!", "bad": "✗"}[kind]
        self.status.setText(f"<span style='color:{col}'><b>{mark}</b> {html.escape(text)}</span>")

    def _fill_names(self):
        tel = self.tels[0] if self.tels else None
        self.names.clear()
        if tel is None:
            self.names.addItem("(no file open)")
            return
        items = []
        if tel.has("speed"):
            items += [("speed", "km/h"), ("speed_mph", "mph"), ("speed_ms", "m/s")]
        for r in ("throttle", "brake", "rpm", "lat_g", "lon_g", "lap", "gps_lat", "gps_lon"):
            if tel.has(r):
                items.append((r, {"throttle": "%", "brake": "%", "lat_g": "g",
                                  "lon_g": "g"}.get(r, "")))
        items.append(("time", "s"))
        taken = {n for n, _ in items} | set(mathch.BUILT_IN_NAMES)
        for col in tel.source_columns:
            if tel.has("col:" + col):
                name = mathch.ident(col)
                if name in taken:            # "Lap" next to lap: use the exact name
                    name = f'col("{col}")'
                taken.add(name)
                items.append((name, f"{col}"
                              + (f" [{tel.units[col]}]" if tel.units.get(col) else "")))
        for name, what in items:
            it = QtWidgets.QListWidgetItem(name)
            it.setToolTip(what)
            it.setData(QtCore.Qt.UserRole, name)
            self.names.addItem(it)

    def _insert(self, it):
        name = it.data(QtCore.Qt.UserRole)
        if name and self.editor.isEnabled():
            self.formula_ed.insertPlainText(name)
            self.formula_ed.setFocus()

    # ----- finish
    def _save(self):
        for c in self.chans:
            if not c.name:
                self.list.setCurrentRow(self.chans.index(c))
                return self._say("bad", "Give it a name.")
            if sum(mathch.ident(x.name) == mathch.ident(c.name) for x in self.chans) > 1:
                self.list.setCurrentRow(self.chans.index(c))
                return self._say("bad", "Two math channels have the same name.")
            try:
                mathch.parse(c.formula)
            except FormulaError as e:
                self.list.setCurrentRow(self.chans.index(c))
                return self._say("bad", f"{c.name}: {e}")
        try:
            mathch.save_library(self.chans)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Math channels", f"Couldn't save: {e}")
            return
        self.accept()


def add_math_items(cb: QtWidgets.QComboBox, tel):
    """Append this file's math channels to a channel combo (after a
    separator); ones that don't work for the file are greyed out."""
    lib = [m for m in mathch.library() if m.on]
    if not lib:
        return
    cb.insertSeparator(cb.count())
    model = cb.model()
    for m in lib:
        live = tel is not None and tel.has(m.key)
        cb.addItem(f"ƒ {m.name}" + (f"  [{m.unit}]" if m.unit else "")
                   + ("" if live else "  (not for this file)"), m.key)
        if not live:
            model.item(cb.count() - 1).setEnabled(False)
