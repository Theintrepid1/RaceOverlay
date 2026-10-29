# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Track tab: which saved track these sessions use, and its turns.

The track is picked by start line (within racecore.tracks.SAME_TRACK_M) when
the first session is opened. Here you can see which one matched, use another
saved track instead (another layout at the same place, say), start a new one,
and put the official turn numbers on your trace (the turns editor).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtWidgets

from racecore import tracks
from raceoverlay.syncgui import _btn, _note

from ..turns import TurnsEditor


def _dist_m(p: tracks.TrackProfile, lat: float, lon: float) -> float:
    return float(np.hypot(*tracks.to_xy(lat, lon, p.lat0, p.lon0)[0]))


class TrackPage(QtWidgets.QWidget):
    title = "Track"
    video_default = False

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        self.status = _note("")
        self.status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        bar.addWidget(self.status, 1)
        bar.addWidget(QtWidgets.QLabel("Use"))
        self.pick = QtWidgets.QComboBox()
        self.pick.setMinimumContentsLength(28)
        self.pick.setToolTip("Saved tracks, nearest start line first. Pick another layout "
                             "at the same place, or start a new track.")
        self.pick.activated.connect(self._picked)
        bar.addWidget(self.pick)
        self.btn_new = _btn("Save as new track…", self._save_new,
                            "Save these turns as a separate track (another layout here)")
        bar.addWidget(self.btn_new)
        lay.addLayout(bar)
        self.editor = TurnsEditor()
        self.editor.saved.connect(self._saved)
        self.editor.reverted.connect(self._revert)
        self.editor.dirty_changed.connect(self._dirty)
        lay.addWidget(self.editor, 1)
        self.empty = _note("Open a session to set up its track.")
        lay.addWidget(self.empty)
        self._key = None          # (reference lap key, profile path) the editor was loaded for

    # ------------------------------------------------------------ page API
    def refresh(self):
        a = self.win.a
        has = a.ref is not None
        self.editor.setVisible(has)
        self.empty.setVisible(not has)
        for w in (self.pick, self.btn_new):
            w.setEnabled(has)
        if not has:
            self._key = None
            self.status.setText("")
            return
        key = (a.ref.key, a.profile.path if a.profile is not None else None)
        if self.editor.dirty and self._key is not None:
            self.editor.retrace(a)       # keep unsaved edits
        elif key != self._key:
            self.editor.load(a, a.profile)
        self._key = key
        self._fill_pick()
        self._status()

    def laps_changed(self):
        pass

    def cursor(self, d: float):
        self.editor.show_cursor(d)

    def ref_cursor(self, rd):
        pass

    # ------------------------------------------------------------ parts
    def _status(self):
        a = self.win.a
        p = a.profile
        lat, lon = a.origin
        dirty = " <b>(unsaved changes)</b>" if self.editor.dirty else ""
        if p is None:
            self.status.setText("<b>No saved track here yet.</b> Corners come from your speed. "
                                "Load the track map and place the turns, then Save." + dirty)
        else:
            n = len(p.turns)
            self.status.setText(
                f"<b>{p.name}</b>: {n} turn{'s' if n != 1 else ''}, start line "
                f"{_dist_m(p, lat, lon):.0f} m from this session's. "
                f"<span style='color:#8a8983'>{p.path or ''}</span>" + dirty)

    def _fill_pick(self):
        a = self.win.a
        lat, lon = a.origin
        near = sorted(((_dist_m(p, lat, lon), p) for p in tracks.load_all()),
                      key=lambda t: t[0])
        self.pick.blockSignals(True)
        self.pick.clear()
        cur = a.profile.path if a.profile is not None else None
        for d, p in near:
            far = f"{d / 1000:.0f} km" if d >= 1000 else f"{d:.0f} m"
            self.pick.addItem(f"{p.name}  ({far} away)", p.path)
        self.pick.addItem("New track…", "__new__")
        i = next((j for j in range(self.pick.count()) if self.pick.itemData(j) == cur), -1)
        self.pick.setCurrentIndex(i if i >= 0 else self.pick.count() - 1)
        self.pick.blockSignals(False)

    def _discard_ok(self) -> bool:
        if not self.editor.dirty:
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Unsaved changes", "Throw away the unsaved changes to this track?",
            QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel)
        return r == QtWidgets.QMessageBox.Discard

    def _picked(self, i):
        data = self.pick.itemData(i)
        if not self._discard_ok():
            self._fill_pick()
            return
        a = self.win.a
        if data == "__new__":
            self.editor.load(a, None)
            self._key = (a.ref.key, "__new__")
            self._status()
            return
        p = next((p for p in tracks.load_all() if p.path == data), None)
        if p is None:
            return
        # use it straight away: it's already saved
        a.use_profile(p)
        self.win.remember_track(p)
        self._key = None
        self.win.refresh()
        self.win.status(f"Using {p.name}.")

    def _save_new(self):
        if self.editor.profile is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Save as new track", "Name (e.g. VIR North):",
            text=self.editor.name_edit.text())
        name = name.strip()
        if not ok or not name:
            return
        if any(p.name.lower() == name.lower() for p in tracks.load_all()):
            QtWidgets.QMessageBox.information(self, "Name taken",
                                              f"There's already a track called {name}.")
            return
        import copy
        p = copy.deepcopy(self.editor.profile)
        p.name, p.path = name, None
        self.editor.profile = p
        self.editor.name_edit.setText(name)
        self.editor.save()

    def _saved(self, p):
        self.win.a.use_profile(p)
        self.win.remember_track(p)
        self._key = None
        self.win.refresh()
        self.win.status(f"Saved {p.name} ({Path(p.path).name}).")

    def _revert(self):
        if not self._discard_ok():
            return
        self.editor.load(self.win.a, self.win.a.profile)
        self._status()

    def _dirty(self, _on):
        if self.win.a.ref is not None:
            self._status()
