# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Sync a video to a data file by hand, for when the motion sensors can't
(no GoPro sensor track, a phone or other camera, a weak match) or the
automatic sync is a little off.

    1. Pause the video at something you can spot: crossing the start/finish
       line, or the moment you hit the brakes for a corner.
    2. Click the same moment on the speed trace (dashed lines are where laps
       start), or pick a lap start from the list when the video is on the
       line.
    3. Fine-tune with the nudge buttons until the live readout under the
       video matches the speedo and what you see.

The result is the usual offset: data time = video time + offset.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from racecore.laps import LapModel
from raceoverlay import media
from raceoverlay.syncgui import MpvNativeWidget, MpvWidget, _btn, use_native_video

from .common import fmt_lap


def offset_for(video_t: float, data_t: float) -> float:
    """The offset that puts video time video_t at data time data_t."""
    return float(data_t) - float(video_t)


def readout(tel, laps: LapModel | None, data_t: float, use_mph: bool) -> str:
    """'87 mph · lat 1.12 g · brake 0.85 g · lap 3 at 1:02.4' for data time
    data_t, or why there's nothing there."""
    t = tel.time
    if data_t < t[0] - 0.5 or data_t > t[-1] + 0.5:
        return "outside the data file (before it starts or after it ends)"
    bits = []
    v = tel.channels.get("speed")
    if v is not None:
        s = float(np.interp(data_t, t, np.nan_to_num(v)))
        bits.append(f"{s * (0.621371 if use_mph else 1.0):.0f} {'mph' if use_mph else 'km/h'}")
    lat, lon = tel.channels.get("lat_g"), tel.channels.get("lon_g")
    if lat is not None:
        bits.append(f"lateral {abs(float(np.interp(data_t, t, np.nan_to_num(lat)))):.2f} g")
    if lon is not None:
        g = float(np.interp(data_t, t, np.nan_to_num(lon)))
        bits.append(f"{'braking' if g < 0 else 'accel'} {abs(g):.2f} g")
    if laps is not None and laps.ok:
        lap = laps.lap_at(data_t)
        if lap is not None:
            bits.append(f"lap {lap.n} at {fmt_lap(data_t - lap.start, 1)}")
    return "  ·  ".join(bits)


class ManualSyncDialog(QtWidgets.QDialog):
    def __init__(self, parent, tel, parts: list[str], offset: float | None = None,
                 title: str = "", use_mph: bool = True):
        super().__init__(parent)
        self.setWindowTitle(f"Sync by hand — {title}" if title else "Sync by hand")
        self.tel = tel
        self.parts = list(parts)
        self.use_mph = use_mph
        try:
            self.info = media.probe(self.parts)
            self.fps = self.info.fps or 30.0
        except Exception:  # noqa: BLE001 - still playable without a probe
            self.info, self.fps = None, 30.0
        try:
            self.laps = LapModel(tel)
        except Exception:  # noqa: BLE001
            self.laps = None
        self.offset = offset
        self._dirty = False

        lay = QtWidgets.QVBoxLayout(self)
        steps = QtWidgets.QLabel(
            "<b>1.</b> Pause the video at something you can spot: crossing the start/finish "
            "line, or the moment you hit the brakes. &nbsp;<b>2.</b> Click the same moment on "
            "the speed trace (dashed lines = lap starts), or pick the lap start below. "
            "&nbsp;<b>3.</b> Nudge until the readout matches the speedo and what you see.")
        steps.setWordWrap(True)
        lay.addWidget(steps)

        native = use_native_video()
        self.video = MpvNativeWidget(self) if native else MpvWidget(self)
        self.video.setMinimumSize(480, 270)
        self.player = self.video.player
        lay.addWidget(self.video, 3)

        self.readout = QtWidgets.QLabel("")
        self.readout.setAlignment(QtCore.Qt.AlignCenter)
        self.readout.setStyleSheet("font-size: 15pt; font-weight: bold; padding: 4px;")
        lay.addWidget(self.readout)

        vrow = QtWidgets.QHBoxLayout()
        vrow.addWidget(QtWidgets.QLabel("Video:"))
        for text, fn, tip in (("−1 s", lambda: self._step(-1.0), "Back 1 s (←)"),
                              ("−1 frame", lambda: self._frame(False), "Back one frame (,)"),
                              ("Play", self._toggle, "Play / pause (Space)"),
                              ("+1 frame", lambda: self._frame(True), "Forward one frame (.)"),
                              ("+1 s", lambda: self._step(1.0), "Forward 1 s (→)")):
            b = _btn(text, fn, tip)
            if text == "Play":
                self.btn_play = b
            vrow.addWidget(b)
        self.vtime = QtWidgets.QLabel("")
        vrow.addWidget(self.vtime, 1)
        lay.addLayout(vrow)

        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(150)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setLabel("bottom", "Data time (s)")
        self.plot.setLabel("left", "Speed")
        self.plot.showGrid(x=True, y=False, alpha=0.2)
        sp = tel.channels.get("speed")
        if sp is not None:
            k = 0.621371 if use_mph else 1.0
            self.plot.plot(tel.time, np.nan_to_num(sp) * k, pen=pg.mkPen("#4aa3ff", width=1.5))
        if self.laps is not None and self.laps.ok:
            for lap in self.laps.laps[1:]:
                self.plot.addItem(pg.InfiniteLine(lap.start, angle=90, pen=pg.mkPen(
                    "#999", width=1, style=QtCore.Qt.DashLine)))
        self.here = pg.InfiniteLine(0, angle=90, pen=pg.mkPen("#ff5050", width=2))
        self.plot.addItem(self.here)
        self.plot.scene().sigMouseClicked.connect(self._clicked)
        lay.addWidget(self.plot, 2)

        lrow = QtWidgets.QHBoxLayout()
        self.lap_cb = QtWidgets.QComboBox()
        if self.laps is not None and self.laps.ok:
            for lap in self.laps.laps[1:]:
                self.lap_cb.addItem(f"Lap {lap.n} starts (data {lap.start:.1f} s)", lap.start)
        lrow.addWidget(QtWidgets.QLabel("The video is on the line where"))
        lrow.addWidget(self.lap_cb, 1)
        lrow.addWidget(_btn("Line up here", self._line_up_lap,
                            "Pause the video exactly as the car crosses the start/finish "
                            "line, pick that lap's start, then press this"))
        lay.addLayout(lrow)
        if self.lap_cb.count() == 0:      # no laps found: only the trace and nudges
            for i in range(lrow.count()):
                lrow.itemAt(i).widget().setVisible(False)

        nrow = QtWidgets.QHBoxLayout()
        nrow.addWidget(QtWidgets.QLabel("Nudge the data:"))
        for text, d in (("−1 s", -1.0), ("−0.1 s", -0.1), ("−1 frame", "-f"),
                        ("+1 frame", "+f"), ("+0.1 s", 0.1), ("+1 s", 1.0)):
            nrow.addWidget(_btn(text, lambda _=False, d=d: self._nudge(d),
                                "Shift which moment of the data goes with this video "
                                "frame (+ = later in the data)"))
        self.off_lbl = QtWidgets.QLabel("")
        nrow.addWidget(self.off_lbl, 1)
        lay.addLayout(nrow)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save
                                        | QtWidgets.QDialogButtonBox.Cancel)
        self.btn_save = bb.button(QtWidgets.QDialogButtonBox.Save)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(1000, 860)

        for key, fn in ((QtCore.Qt.Key_Space, self._toggle),
                        (QtCore.Qt.Key_Left, lambda: self._step(-1.0)),
                        (QtCore.Qt.Key_Right, lambda: self._step(1.0)),
                        (QtCore.Qt.Key_Comma, lambda: self._frame(False)),
                        (QtCore.Qt.Key_Period, lambda: self._frame(True))):
            sc = QtGui.QShortcut(key, self)
            sc.activated.connect(fn)

        self.video.load(media.mpv_url(self.parts))
        try:
            self.player.pause = True
        except Exception:  # noqa: BLE001
            pass
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60)
        self._tick()

    # ----- player -----

    def _vt(self) -> float:
        try:
            t = self.player.time_pos
            return float(t) if t is not None else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _toggle(self):
        try:
            self.player.pause = not self.player.pause
        except Exception:  # noqa: BLE001
            pass

    def _step(self, dt: float):
        try:
            self.player.command("seek", f"{dt:.3f}", "relative+exact")
        except Exception:  # noqa: BLE001
            pass

    def _frame(self, fwd: bool):
        try:
            self.player.command("frame-step" if fwd else "frame-back-step")
        except Exception:  # noqa: BLE001
            pass

    # ----- lining up -----

    def _set_offset(self, off: float):
        self.offset = float(off)
        self._dirty = True
        self._tick()

    def _clicked(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            return
        vb = self.plot.getPlotItem().vb
        if not self.plot.sceneBoundingRect().contains(ev.scenePos()):
            return
        data_t = vb.mapSceneToView(ev.scenePos()).x()
        self._set_offset(offset_for(self._vt(), data_t))

    def _line_up_lap(self):
        t = self.lap_cb.currentData()
        if t is not None:
            self._set_offset(offset_for(self._vt(), t))

    def _nudge(self, d):
        if self.offset is None:
            return
        if d in ("-f", "+f"):
            d = (-1.0 if d == "-f" else 1.0) / self.fps
        self._set_offset(self.offset + d)

    def _tick(self):
        vt = self._vt()
        try:
            paused = bool(self.player.pause)
        except Exception:  # noqa: BLE001
            paused = True
        self.btn_play.setText("Play" if paused else "Pause")
        self.vtime.setText(f"  video {vt:.2f} s")
        if self.offset is None:
            self.readout.setText("Not lined up yet: click the speed trace at the moment the "
                                 "video shows")
            self.off_lbl.setText("")
            self.here.setVisible(False)
            self.btn_save.setEnabled(False)
            return
        data_t = vt + self.offset
        self.here.setVisible(True)
        self.here.setValue(data_t)
        self.readout.setText(readout(self.tel, self.laps, data_t, self.use_mph))
        self.off_lbl.setText(f"  offset {self.offset:+.3f} s")
        self.btn_save.setEnabled(True)

    def _save(self):
        if self.offset is not None:
            self.accept()

    def done(self, r):
        self.timer.stop()
        try:
            self.video.shutdown()
        except Exception:  # noqa: BLE001
            pass
        super().done(r)
