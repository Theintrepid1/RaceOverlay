# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Track turns dialog: put the official turn numbers on your GPS trace.

1. Load the track's map image (the one with the turn numbers). It's placed
   roughly over the trace.
2. Line it up: click a spot on the image, then the same spot on your trace;
   do that twice (start/finish and a hairpin work well). The image is
   scaled and turned onto the trace.
3. Place turns: click each turn number on the image; it snaps to the nearest
   point of the trace and takes the name in "Next turn" (T1, T2…; type 4A
   before a split turn and the next click gets 4B). Double-click a name in
   the list to rename it.

Saved per track (racecore.tracks), so the next visit already has them.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from racecore import tracks
from raceoverlay.syncgui import _btn, _note, pick_file

TRACE = "#3987e5"
TURN = "#f2f1ec"
PICK = "#c98500"


class TurnsDialog(QtWidgets.QDialog):
    def __init__(self, parent, a, profile: tracks.TrackProfile | None):
        super().__init__(parent)
        self.setWindowTitle("Track turns")
        self.resize(1300, 860)
        self.a = a
        lat0, lon0 = a.origin
        if profile is None:
            profile = tracks.TrackProfile(tracks.name_from_file(a.sessions[0].path),
                                          lat0, lon0)
        self.profile = profile
        # everything is drawn in the profile's frame (its start line = 0,0)
        ll = tracks.to_latlon(a.ref.xy, lat0, lon0)
        self.trace = tracks.to_xy(ll[:, 0], ll[:, 1], profile.lat0, profile.lon0)
        self.img = None            # numpy RGBA
        self.img_item = None
        self.mode = None           # None | "align" | "place"
        self.pairs: list[tuple] = []
        self._pending_img = None

        lay = QtWidgets.QHBoxLayout(self)
        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.plot.setMenuEnabled(False)
        self.plot.scene().sigMouseClicked.connect(self._clicked)
        lay.addWidget(self.plot, 1)

        side = QtWidgets.QVBoxLayout()
        form = QtWidgets.QFormLayout()
        self.name_edit = QtWidgets.QLineEdit(profile.name)
        form.addRow("Track", self.name_edit)
        side.addLayout(form)
        side.addWidget(_btn("1 · Load track map image…", self._load_image,
                            "A picture of the track with the turn numbers on it"))
        self.btn_align = _btn("2 · Line it up (2 points)", self._start_align)
        side.addWidget(self.btn_align)
        self.btn_place = _btn("3 · Place turns", self._start_place,
                              "Click each turn number on the map")
        self.btn_place.setCheckable(True)
        side.addWidget(self.btn_place)
        nrow = QtWidgets.QHBoxLayout()
        nrow.addWidget(_note("Next turn"))
        self.next_edit = QtWidgets.QLineEdit(profile.next_name())
        self.next_edit.setToolTip("Name for the next turn you click. Type 4A for a "
                                  "split turn; the one after becomes 4B.")
        self.next_edit.textEdited.connect(lambda _t: self._hint())
        nrow.addWidget(self.next_edit, 1)
        side.addLayout(nrow)
        side.addWidget(_btn("Start from detected corners", self._from_detected,
                            "Add a turn at every corner found from your speed"))
        orow = QtWidgets.QHBoxLayout()
        orow.addWidget(_note("Map opacity"))
        self.opacity = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.opacity.setRange(10, 100)
        self.opacity.setValue(55)
        self.opacity.valueChanged.connect(self._set_opacity)
        orow.addWidget(self.opacity, 1)
        side.addLayout(orow)
        self.msg = _note("")
        self.msg.setMinimumHeight(70)
        side.addWidget(self.msg)
        side.addWidget(_note("Turns (double-click to rename)", small=True))
        self.lst = QtWidgets.QListWidget()
        self.lst.itemChanged.connect(self._renamed)
        side.addWidget(self.lst, 1)
        brow = QtWidgets.QHBoxLayout()
        brow.addWidget(_btn("Delete turn", self._delete))
        brow.addWidget(_btn("Clear all", self._clear))
        side.addLayout(brow)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save
                                        | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        side.addWidget(bb)
        w = QtWidgets.QWidget()
        w.setLayout(side)
        w.setFixedWidth(330)
        lay.addWidget(w)

        if profile.image:
            self._read_image(profile.image)
        self._draw()
        self._hint()

    # ------------------------------------------------------------ image ---
    def _read_image(self, path: str) -> bool:
        try:
            from PIL import Image
            im = Image.open(path).convert("RGBA")
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Can't open image", f"{path}\n\n{e}")
            return False
        self.img = np.asarray(im)
        self.profile.image = path
        if self.profile.ab() is None:
            h, w = self.img.shape[:2]
            self.profile.set_ab(*tracks.fit_box(w, h, self.trace))
        return True

    def _load_image(self):
        p = pick_file(self, "Track map image", "trackmap",
                      "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*)")
        if p:
            self.profile.transform = None
            if self._read_image(p):
                self._draw()
                self._start_align()

    def _set_opacity(self, v):
        if self.img_item is not None:
            self.img_item.setOpacity(v / 100)

    # ------------------------------------------------------------- draw ---
    def _draw(self):
        self.plot.clear()
        self.img_item = None
        ab = self.profile.ab()
        if self.img is not None and ab is not None:
            a, b = ab
            self.img_item = pg.ImageItem(self.img, axisOrder="row-major")
            # pixel (x, y down) -> world: wx = ar*x + ai*y + br, wy = ai*x - ar*y + bi
            self.img_item.setTransform(QtGui.QTransform(a.real, a.imag, a.imag, -a.real,
                                                        b.real, b.imag))
            self.img_item.setOpacity(self.opacity.value() / 100)
            self.img_item.setZValue(-10)
            self.plot.addItem(self.img_item)
        self.plot.plot(self.trace[:, 0], self.trace[:, 1], pen=pg.mkPen(TRACE, width=3))
        sx, sy = self.trace[0]
        self.plot.plot([sx], [sy], pen=None, symbol="s", symbolSize=10,
                       symbolBrush=pg.mkBrush(TURN))
        txy = self.profile.turns_xy(self.profile.lat0, self.profile.lon0)
        if len(txy):
            self.plot.plot(txy[:, 0], txy[:, 1], pen=None, symbol="o", symbolSize=11,
                           symbolPen=pg.mkPen("#0b0b0b", width=2),
                           symbolBrush=pg.mkBrush(TURN))
            for t, (x, y) in zip(self.profile.turns, txy):
                lbl = pg.TextItem(t.name, color=TURN, anchor=(0.5, 1.3),
                                  fill=pg.mkBrush(11, 11, 11, 170))
                lbl.setPos(x, y)
                self.plot.addItem(lbl)
        for img_pt, world_pt in self.pairs:
            if self.profile.ab() is not None:
                ip = tracks.apply(*self.profile.ab(), [img_pt])[0]
                self.plot.plot([ip[0]], [ip[1]], pen=None, symbol="x", symbolSize=14,
                               symbolPen=pg.mkPen(PICK, width=3))
            if world_pt is not None:
                self.plot.plot([world_pt[0]], [world_pt[1]], pen=None, symbol="o",
                               symbolSize=12, symbolBrush=pg.mkBrush(PICK))
        self._fill_list()

    def _fill_list(self):
        self.lst.blockSignals(True)
        cur = self.lst.currentRow()
        self.lst.clear()
        for t in self.profile.turns:
            it = QtWidgets.QListWidgetItem(t.name)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsEditable)
            self.lst.addItem(it)
        if 0 <= cur < self.lst.count():
            self.lst.setCurrentRow(cur)
        self.lst.blockSignals(False)

    # ------------------------------------------------------------ modes ---
    def _hint(self):
        if self.mode == "align":
            n = len(self.pairs)
            step = n + 1 if self.pairs and self.pairs[-1][1] is not None or not self.pairs \
                else n
            if self.pairs and self.pairs[-1][1] is None:
                self.msg.setText(f"<b>Point {step} of 2:</b> now click the <b>same spot "
                                 "on your blue trace</b>.")
            else:
                self.msg.setText(f"<b>Point {step} of 2:</b> click a landmark on the "
                                 "<b>map image</b> (start/finish, a hairpin apex…).")
        elif self.mode == "place":
            self.msg.setText(f"Click turn numbers on the map; each snaps to your trace. "
                             f"Next: <b>{self._next()}</b> (change it in Next turn, "
                             "e.g. 4A for a split turn). Click \"Place turns\" "
                             "again to stop.")
        elif self.img is None:
            self.msg.setText("Load the track's map image (with turn numbers), or start "
                             "from your detected corners and rename them.")
        else:
            self.msg.setText("Line the map up with your trace, then place the turns.")

    def _start_align(self):
        if self.img is None:
            self.msg.setText("Load a map image first.")
            return
        self.mode = "align"
        self.pairs = []
        self.btn_place.setChecked(False)
        self._draw()
        self._hint()

    def _start_place(self):
        self.mode = "place" if self.btn_place.isChecked() else None
        self.pairs = []
        self._draw()
        self._hint()

    def _clicked(self, ev):
        if ev.button() != QtCore.Qt.LeftButton or self.mode is None:
            return
        p = self.plot.getViewBox().mapSceneToView(ev.scenePos())
        pt = np.array([p.x(), p.y()])
        if self.mode == "align":
            self.click_align(pt)
        else:
            self.click_place(pt)

    def click_align(self, pt):
        """One alignment click at world point pt (image first, then trace)."""
        ab = self.profile.ab()
        if not self.pairs or self.pairs[-1][1] is not None:
            img_pt = tracks.invert(*ab, [pt])[0]
            self.pairs.append((img_pt, None))
        else:
            world = self.trace[tracks.snap(self.trace, pt)]
            self.pairs[-1] = (self.pairs[-1][0], world)
            if len(self.pairs) == 2:
                try:
                    a, b = tracks.similarity([q[0] for q in self.pairs],
                                             [q[1] for q in self.pairs])
                except ValueError as e:
                    self.msg.setText(str(e))
                    self.pairs = []
                    return
                self.profile.set_ab(a, b)
                self.pairs = []
                self.mode = "place"
                self.btn_place.setChecked(True)
                self._draw()
                self.msg.setText("Lined up. If it's off, press \"Line it up\" again "
                                 "with points farther apart.<br>" )
                self._hint()
                return
        self._draw()
        self._hint()

    def click_place(self, pt):
        """Add a turn at the trace point nearest pt."""
        world = self.trace[tracks.snap(self.trace, pt)]
        lat, lon = tracks.to_latlon([world], self.profile.lat0, self.profile.lon0)[0]
        self.profile.turns.append(tracks.Turn(self._next(), float(lat), float(lon)))
        self.next_edit.setText(self.profile.next_name())
        self._draw()
        self._hint()

    def _next(self) -> str:
        return self.next_edit.text().strip() or self.profile.next_name()

    def _from_detected(self):
        a = self.a
        saved, a.profile = a.profile, None
        a._find_corners()  # the detector's corners, not the profile's
        detected = list(a.corners)
        a.profile = saved
        a._find_corners()
        for c in detected:
            x = float(np.interp(c.apex_d, a._ref_s, a.ref.xy[:, 0]))
            y = float(np.interp(c.apex_d, a._ref_s, a.ref.xy[:, 1]))
            lat, lon = tracks.to_latlon([[x, y]], *a.origin)[0]
            self.profile.turns.append(tracks.Turn(self.profile.next_name(), float(lat), float(lon)))
        self.next_edit.setText(self.profile.next_name())
        self._draw()
        self.msg.setText(f"Added {len(detected)} turns from your corners. Rename them "
                         "to the track's numbers, add the ones it missed with "
                         "Place turns, and delete extras.")

    def _renamed(self, it):
        i = self.lst.row(it)
        name = it.text().strip()
        if 0 <= i < len(self.profile.turns) and name:
            self.profile.turns[i].name = name
            if i == len(self.profile.turns) - 1:
                self.next_edit.setText(self.profile.next_name())
            self._draw()

    def _delete(self):
        i = self.lst.currentRow()
        if 0 <= i < len(self.profile.turns):
            del self.profile.turns[i]
            self.next_edit.setText(self.profile.next_name())
            self._draw()
            self._hint()

    def _clear(self):
        self.profile.turns.clear()
        self.next_edit.setText(self.profile.next_name())
        self._draw()
        self._hint()

    def _save(self):
        self.profile.name = self.name_edit.text().strip() or self.profile.name
        try:
            self.profile.save()
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Couldn't save", str(e))
            return
        self.accept()
