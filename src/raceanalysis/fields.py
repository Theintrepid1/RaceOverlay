# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Small number-entry widgets for the Day tab's notes: a start time split
into year / month / day / hour / minute, and tire pressures laid out like
the car seen from above (fronts on top)."""
from __future__ import annotations

import datetime as dt
import re

from PySide6 import QtCore, QtGui, QtWidgets

from .common import GAIN, INK_DIM, LOSS

ERR_STYLE = "QLineEdit { border: 1px solid #e66767; }"


class NumField(QtWidgets.QLineEdit):
    """A short number box that can be left blank. Only numbers in range are
    accepted; the mouse wheel and the up/down keys nudge the value."""

    nudged = QtCore.Signal()

    def __init__(self, lo: float, hi: float, decimals: int = 0, chars: int = 2,
                 step: float = 1.0, placeholder: str = "", pad: int = 0):
        super().__init__()
        self.lo, self.hi, self.decimals, self.step, self.pad = lo, hi, decimals, step, pad
        if decimals:
            v = QtGui.QDoubleValidator(lo, hi, decimals, self)
            v.setNotation(QtGui.QDoubleValidator.StandardNotation)
        else:
            v = QtGui.QIntValidator(int(lo), int(hi), self)
        self.setValidator(v)
        self.setPlaceholderText(placeholder)
        self.setAlignment(QtCore.Qt.AlignCenter)
        w = self.fontMetrics().horizontalAdvance("8" * chars) + 18
        self.setFixedWidth(w)

    def value(self) -> float | None:
        t = self.text().strip()
        if not t:
            return None
        try:
            v = float(t)
        except ValueError:
            return None
        return v if self.lo <= v <= self.hi else None

    def set_value(self, v: float | None):
        if v is None:
            self.setText("")
        elif self.decimals:
            self.setText(f"{v:.{self.decimals}f}".rstrip("0").rstrip("."))
        else:
            self.setText(f"{int(v):0{self.pad}d}" if self.pad else str(int(v)))

    def _nudge(self, n: int):
        v = self.value()
        if v is None:
            return
        self.set_value(min(self.hi, max(self.lo, v + n * self.step)))
        self.nudged.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down):
            self._nudge(1 if ev.key() == QtCore.Qt.Key_Up else -1)
            return
        super().keyPressEvent(ev)

    def wheelEvent(self, ev):
        if self.hasFocus() and self.value() is not None:
            self._nudge(1 if ev.angleDelta().y() > 0 else -1)
            ev.accept()
            return
        super().wheelEvent(ev)


def _sep(text: str) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text)
    lbl.setStyleSheet(f"color:{INK_DIM};")
    return lbl


class StartFields(QtWidgets.QWidget):
    """Year / Month / Day  Hour : Minute, 24-hour track time. Blank when the
    start isn't known. text() is "YYYY-MM-DD HH:MM", or "" until every box
    holds a real date and time."""

    edited = QtCore.Signal()

    def __init__(self):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.y = NumField(2000, 2099, chars=4, placeholder="YYYY")
        self.mo = NumField(1, 12, placeholder="MM", pad=2)
        self.d = NumField(1, 31, placeholder="DD", pad=2)
        self.h = NumField(0, 23, placeholder="hh", pad=2)
        self.mi = NumField(0, 59, placeholder="mm", pad=2)
        self.fields = [self.y, self.mo, self.d, self.h, self.mi]
        for w, sep in zip(self.fields, ["-", "-", "  ", ":", ""]):
            lay.addWidget(w)
            if sep:
                lay.addWidget(_sep(sep))
            w.textEdited.connect(self._changed)
            w.nudged.connect(self._changed)
        lay.addStretch(1)
        self.setToolTip("Session start, 24-hour track time. Arrow keys or the mouse wheel "
                        "change a number.")

    def _changed(self, *_):
        bad = self._partial() and not self.text()
        for w in self.fields:
            w.setStyleSheet(ERR_STYLE if bad and w.text() else "")
        self.edited.emit()

    def _partial(self) -> bool:
        return any(w.text().strip() for w in self.fields)

    def complete(self) -> bool:
        return bool(self.text()) or not self._partial()

    def text(self) -> str:
        vals = [w.value() for w in self.fields]
        if any(v is None for v in vals):
            return ""
        y, mo, d, h, mi = (int(v) for v in vals)
        try:
            t = dt.datetime(y, mo, d, h, mi)
        except ValueError:           # e.g. Feb 30
            return ""
        return t.strftime("%Y-%m-%d %H:%M")

    def setText(self, s: str):
        try:
            t = dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
            vals = [t.year, t.month, t.day, t.hour, t.minute]
        except ValueError:
            vals = [None] * 5
        for w, v in zip(self.fields, vals):
            w.set_value(v)
            w.setStyleSheet("")


CORNERS = ("FL", "FR", "RL", "RR")


def parse_pressures(s: str) -> list[float | None] | None:
    """'28/28/27.5/27' (also commas or spaces) -> four values, blanks as
    None; None if it isn't four numbers (an old free-text note)."""
    s = (s or "").strip()
    if not s:
        return [None] * 4
    parts = re.split(r"\s*[/,;]\s*|\s+", s)
    if len(parts) != 4:
        return None
    out = []
    for p in parts:
        if not p:
            out.append(None)
            continue
        try:
            out.append(float(p))
        except ValueError:
            return None
    return out


def format_pressures(vals: list[float | None]) -> str:
    if all(v is None for v in vals):
        return ""
    return "/".join("" if v is None else f"{v:g}" for v in vals)


class PressureGrid(QtWidgets.QWidget):
    """Cold and hot pressures, each as FL FR over RL RR, in psi; under each
    hot box, how much that tire gained over cold."""

    edited = QtCore.Signal()

    def __init__(self):
        super().__init__()
        lay = QtWidgets.QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setHorizontalSpacing(4)
        lay.setVerticalSpacing(1)
        self.cold, self.hot, self.gain = [], [], []
        for col0, title, store in ((0, "Cold", self.cold), (3, "Hot", self.hot)):
            t = QtWidgets.QLabel(title)
            t.setStyleSheet(f"color:{INK_DIM};")
            lay.addWidget(t, 0, col0, 1, 2, QtCore.Qt.AlignHCenter)
            for i, name in enumerate(CORNERS):
                f = NumField(0, 99.9, decimals=1, chars=4, step=0.5, placeholder=name)
                f.setToolTip(f"{title} {name}, psi")
                f.textEdited.connect(self._changed)
                f.nudged.connect(self._changed)
                r = 1 + (i // 2) * 2           # fronts on row 1, rears on row 3
                lay.addWidget(f, r, col0 + i % 2)
                store.append(f)
                if title == "Hot":
                    g = QtWidgets.QLabel("")
                    g.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop)
                    g.setStyleSheet("font-size:10px;")
                    lay.addWidget(g, r + 1, col0 + i % 2)
                    self.gain.append(g)
        lay.setColumnMinimumWidth(2, 14)
        unit = QtWidgets.QLabel("psi")
        unit.setStyleSheet(f"color:{INK_DIM};")
        lay.addWidget(unit, 1, 5, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        lay.setColumnStretch(6, 1)
        self.old = QtWidgets.QLabel("")       # an old free-text note, kept as is
        self.old.setWordWrap(True)
        self.old.setStyleSheet(f"color:{INK_DIM}; font-size:11px;")
        lay.addWidget(self.old, 5, 0, 1, 7)
        self.old.hide()
        self._old_cold = self._old_hot = ""

    def _changed(self, *_):
        self._update_gain()
        self.edited.emit()

    def _update_gain(self):
        for c, h, g in zip(self.cold, self.hot, self.gain):
            cv, hv = c.value(), h.value()
            if cv is None or hv is None:
                g.setText("")
                continue
            d = hv - cv
            col = GAIN if d > 0.05 else LOSS if d < -0.05 else INK_DIM
            g.setText(f"{d:+.1f}")
            g.setStyleSheet(f"font-size:10px; color:{col};")

    def set_values(self, cold: str, hot: str):
        old = []
        for text, fields, attr in ((cold, self.cold, "_old_cold"), (hot, self.hot, "_old_hot")):
            vals = parse_pressures(text)
            setattr(self, attr, "" if vals is not None else text)
            if vals is None:
                old.append(text)
                vals = [None] * 4
            for f, v in zip(fields, vals):
                f.set_value(v)
        self.old.setText(("Earlier note: " + " · ".join(old)) if old else "")
        self.old.setVisible(bool(old))
        self._update_gain()

    def values(self) -> tuple[str, str]:
        """(cold, hot) as stored: "28/28/27/27". An old free-text note is
        kept until boxes for that set are filled in."""
        out = []
        for fields, old in ((self.cold, self._old_cold), (self.hot, self._old_hot)):
            s = format_pressures([f.value() for f in fields])
            out.append(s or old)
        return out[0], out[1]
