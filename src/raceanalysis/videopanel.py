# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""The video column: the GoPro video sized to its own shape, a readout strip
in the space that would otherwise be black bars (lap, lap time, speed, delta,
and gear/RPM when the data has them), and the play controls.

There is one player for the whole window; tabs show or hide this panel, it
is never moved between them (mpv draws into a native window that doesn't
like being re-parented)."""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from raceoverlay.syncgui import MpvNativeWidget, MpvWidget, _btn, use_native_video

from .common import GAIN, INK, INK_DIM, INK_FAINT, LOSS, PANEL

_FONT_FAMILY = None


def _digits_font() -> str:
    """The bundled DejaVu Sans Bold (equal-width digits, so numbers don't jitter)."""
    global _FONT_FAMILY
    if _FONT_FAMILY is None:
        f = Path(__file__).resolve().parent.parent / "raceoverlay" / "fonts" / "DejaVuSans-Bold.ttf"
        fid = QtGui.QFontDatabase.addApplicationFont(str(f)) if f.exists() else -1
        fams = QtGui.QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        _FONT_FAMILY = fams[0] if fams else "DejaVu Sans"
    return _FONT_FAMILY


class _Readout(QtWidgets.QWidget):
    """One label-over-value block, e.g. SPEED / 90 mph. The value has a fixed
    width (from `template`) so the strip doesn't jump as numbers change."""

    def __init__(self, title: str, unit: str = "", template: str = "888", parent=None):
        super().__init__(parent)
        self.template = template
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.title = QtWidgets.QLabel(title)
        self.title.setStyleSheet(f"color:{INK_FAINT}; font-size:10px; letter-spacing:1px;")
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self.value = QtWidgets.QLabel("–")
        self.value.setStyleSheet(f"color:{INK};")
        self.unit = QtWidgets.QLabel(unit)
        self.unit.setStyleSheet(f"color:{INK_DIM}; font-size:12px;")
        row.addWidget(self.value)
        row.addWidget(self.unit, 0, QtCore.Qt.AlignBottom)
        lay.addWidget(self.title)
        lay.addLayout(row)

    def set_size(self, px: int):
        f = QtGui.QFont(_digits_font())
        f.setPixelSize(px)
        self.value.setFont(f)
        fm = QtGui.QFontMetrics(f)
        self.value.setFixedWidth(fm.horizontalAdvance(self.template) + 2)
        self.value.setFixedHeight(fm.height())


class ReadoutStrip(QtWidgets.QFrame):
    """LAP · TIME · SPEED · DELTA (· GEAR · RPM), centred, with a note line
    under it for things like "this point isn't in the video"."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"ReadoutStrip {{ background:{PANEL}; }}")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 4, 12, 4)
        outer.setSpacing(2)
        outer.addStretch(1)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(22)
        row.addStretch(1)
        self.lap = _Readout("LAP", template="88")
        self.time = _Readout("LAP TIME", template="8:88.8")
        self.speed = _Readout("SPEED", "mph", template="188")
        self.delta = _Readout("VS REF", "s", template="+888.88")
        self.gear = _Readout("GEAR", template="8")
        self.rpm = _Readout("RPM", template="8,888")
        self.items = [self.lap, self.time, self.speed, self.delta, self.gear, self.rpm]
        for w in self.items:
            row.addWidget(w)
        row.addStretch(1)
        outer.addLayout(row)
        self.note = QtWidgets.QLabel("")
        self.note.setStyleSheet(f"color:{INK_DIM}; font-size:11px;")
        self.note.setAlignment(QtCore.Qt.AlignCenter)
        self.note.setWordWrap(True)
        outer.addWidget(self.note)
        outer.addStretch(1)
        self.gear.hide()
        self.rpm.hide()
        self.px = 0
        self.set_scale(30)

    def set_accent(self, color: str):
        """Colour bar down the left edge (and the lap number) matching this
        video's trace, so it's clear which line on the plots is which."""
        if getattr(self, "_accent", None) == color:
            return
        self._accent = color
        self.setStyleSheet(f"ReadoutStrip {{ background:{PANEL}; "
                           f"border-left: 6px solid {color}; }}")
        self.lap.value.setStyleSheet(f"color:{color};")

    def set_scale(self, px: int):
        if px == self.px:
            return
        self.px = px
        for w in self.items:
            w.set_size(px)

    def needed_width(self, px: int) -> int:
        """Width the visible readouts need at a value size of px."""
        f = QtGui.QFont(_digits_font())
        f.setPixelSize(px)
        fm = QtGui.QFontMetrics(f)
        vis = [w for w in self.items if not w.isHidden()]
        total = 0
        for w in vis:      # title over value + unit, as laid out
            val = fm.horizontalAdvance(w.template) + 2
            unit = w.unit.sizeHint().width() + 4 if w.unit.text() else 0
            total += max(w.title.sizeHint().width(), val + unit)
        return total + 22 * (len(vis) - 1) + 24 + 8

    def needed_height(self, px: int) -> int:
        return int(px * 1.25) + 16 + (18 if self.note.text() else 0) + 8

    def show_values(self, lap: str, time: str, speed: str, unit: str, delta: float | None,
                    gear: str | None = None, rpm: str | None = None, note: str = "",
                    gap: tuple[str, str, str] | None = None):
        """gap = (title, value, unit) replaces the VS REF readout (the
        reference video's strip shows how far ahead or behind it is)."""
        self.lap.value.setText(lap)
        self.time.value.setText(time)
        self.speed.value.setText(speed)
        self.speed.unit.setText(unit)
        if gap is not None:
            self.delta.title.setText(gap[0])
            self.delta.value.setText(gap[1])
            self.delta.unit.setText(gap[2])
            self.delta.value.setStyleSheet(f"color:{INK};")
        elif delta is None:
            self.delta.title.setText("VS REF")
            self.delta.value.setText("ref")
            self.delta.value.setStyleSheet(f"color:{INK_DIM};")
            self.delta.unit.setText("")
        else:
            self.delta.title.setText("VS REF")
            self.delta.value.setText(f"{delta:+.2f}")
            col = LOSS if delta > 0.05 else GAIN if delta < -0.05 else INK
            self.delta.value.setStyleSheet(f"color:{col};")
            self.delta.unit.setText("s")
        changed = (self.gear.isHidden() != (gear is None) or self.rpm.isHidden() != (rpm is None)
                   or bool(self.note.text()) != bool(note))
        self.gear.setVisible(gear is not None)
        self.rpm.setVisible(rpm is not None)
        if gear is not None:
            self.gear.value.setText(gear)
        if rpm is not None:
            self.rpm.value.setText(rpm)
        self.note.setText(note)
        if changed and isinstance(self.parent(), VideoPanel):
            self.parent()._relayout()


class VideoPanel(QtWidgets.QWidget):
    """Video at its own aspect ratio, readout strip in the leftover band,
    play controls at the bottom. With "Reference video" on, the reference
    lap's video sits under it with its own strip."""
    compare_changed = QtCore.Signal(bool)
    mode_changed = QtCore.Signal(str)
    MODES = [("Same spot", "spot"), ("Ghost", "clock")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._native = use_native_video()
        native = self._native
        self.video = MpvNativeWidget(self) if native else MpvWidget(self)
        self.video.setMinimumSize(160, 90)
        self.player = self.video.player
        self.strip = ReadoutStrip(self)
        self.controls = QtWidgets.QWidget(self)
        cl = QtWidgets.QHBoxLayout(self.controls)
        cl.setContentsMargins(4, 2, 4, 2)
        self.btn_back = _btn("−2 s", lambda: self.step(-2.0))
        self.btn_play = _btn("Play", self.toggle_play)
        self.btn_fwd = _btn("+2 s", lambda: self.step(2.0))
        self.btn_back.setToolTip("Back 2 s (←; Shift+← 0.2 s; , one frame)")
        self.btn_play.setToolTip("Play / pause (Space)")
        self.btn_fwd.setToolTip("Forward 2 s (→; Shift+→ 0.2 s; . one frame)")
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet(f"color:{INK_DIM};")
        for w in (self.btn_back, self.btn_play, self.btn_fwd):
            cl.addWidget(w)
        cl.addWidget(self.status, 1)
        self.chk_ref = QtWidgets.QCheckBox("Ref video")
        self.chk_ref.setStyleSheet(f"color:{INK};")
        self.chk_ref.setToolTip("Show the reference lap's video under this one, kept in step (R)")
        self.chk_ref.toggled.connect(self._compare_toggled)
        cl.addWidget(self.chk_ref)
        self.mode = QtWidgets.QComboBox()
        for txt, key in self.MODES:
            self.mode.addItem(txt, key)
        self.mode.setToolTip("Same spot: the reference video is held at the same place on "
                             "track as yours.\nGhost: both run in real time from the same "
                             "start, so you see the faster lap pull away.")
        self.mode.currentIndexChanged.connect(
            lambda _i: self.mode_changed.emit(self.mode.currentData()))
        self.mode.setVisible(False)
        cl.addWidget(self.mode)
        # reference video, made the first time it's wanted
        self.ref_video = None
        self.ref_strip = ReadoutStrip(self)
        self.ref_strip.lap.title.setText("REF LAP")
        self.ref_strip.hide()
        self.ref_note = QtWidgets.QLabel("", self)
        self.ref_note.setAlignment(QtCore.Qt.AlignCenter)
        self.ref_note.setWordWrap(True)
        self.ref_note.setStyleSheet(f"color:{INK_DIM}; background:#000;")
        self.ref_note.hide()
        self.compare = False
        self.aspect = 16 / 9
        self.setMinimumWidth(320)
        pal = self.palette()
        pal.setColor(QtGui.QPalette.Window, QtGui.QColor(PANEL))
        self.setAutoFillBackground(True)
        self.setPalette(pal)

    # ------------------------------------------------------------ layout
    def set_aspect(self, w: int, h: int):
        if w > 0 and h > 0:
            self.aspect = w / h
            self._relayout()

    def resizeEvent(self, ev):
        self._relayout()
        super().resizeEvent(ev)

    def _relayout(self):
        W, H = self.width(), self.height()
        ch = self.controls.sizeHint().height()
        room = max(0, H - ch)
        if self.compare and self.ref_video is not None:
            half = room // 2
            self._place(self.video, self.strip, 0, half, W)
            self._place(self.ref_video, self.ref_strip, half, room - half, W)
            self.ref_note.setGeometry(self.ref_video.geometry())
        else:
            self._place(self.video, self.strip, 0, room, W)
        self.controls.setGeometry(0, room, W, ch)

    def _place(self, video, st, y0, room, W):
        """Video as large as fits; the readouts in the band under it, as big
        as that band and the width allow (the group is centred vertically)."""
        px = 44
        while px > 16 and st.needed_width(px) > W:
            px -= 2
        min_strip = st.needed_height(max(16, min(px, 20)))
        vw, vh = W, int(W / self.aspect)
        if vh > room - min_strip:              # too tall: shrink the video
            vh = max(0, room - min_strip)
            vw = int(vh * self.aspect)
        band = room - vh
        while px > 16 and st.needed_height(px) > band:
            px -= 2
        st.set_scale(px)
        sh = min(band, st.needed_height(px) + 12)
        top = y0 + (room - vh - sh) // 2
        video.setGeometry((W - vw) // 2, top, vw, vh)
        st.setGeometry(0, top + vh, W, sh)

    # ------------------------------------------------------- reference
    def _compare_toggled(self, on: bool):
        if on and self.ref_video is None:
            self.ref_video = MpvNativeWidget(self) if self._native else MpvWidget(self)
            self.ref_video.setMinimumSize(160, 90)
            try:
                self.ref_video.player.mute = True
            except Exception:
                pass
            self.ref_note.raise_()
        self.compare = on
        self.mode.setVisible(on)
        if self.ref_video is not None:
            self.ref_video.setVisible(on)
            if not on:
                self.ref_pause()
        self.ref_strip.setVisible(on)
        if not on:
            self.ref_note.hide()
        self._relayout()
        self.compare_changed.emit(on)

    def set_ref_note(self, text: str):
        """Text over the reference video (no video for that lap, etc.)."""
        if not self.compare:
            return
        self.ref_note.setText(text)
        self.ref_note.setVisible(bool(text))
        if text:
            self.ref_note.raise_()

    def _rp(self):
        return self.ref_video.player if self.ref_video is not None else None

    def ref_load(self, url: str):
        if self.ref_video is not None:
            self.ref_video.load(url)

    def ref_stop(self):
        try:
            self._rp().command("stop")
        except Exception:
            pass

    def ref_seek(self, t: float):
        try:
            self._rp().command("seek", f"{max(0.0, t):.3f}", "absolute+exact")
        except Exception:
            pass

    def ref_pause(self):
        try:
            self._rp().pause = True
        except Exception:
            pass

    def ref_play(self, speed: float):
        try:
            p = self._rp()
            if abs((p.speed or 1.0) - speed) > 0.01:
                p.speed = speed
            if p.pause:
                p.pause = False
        except Exception:
            pass

    def ref_state(self) -> tuple[bool, float | None]:
        try:
            p = self._rp()
            return bool(p.pause), p.time_pos
        except Exception:
            return True, None

    # ------------------------------------------------------------ player
    def load(self, url: str):
        self.video.load(url)

    def stop(self):
        try:
            self.player.command("stop")
        except Exception:
            pass

    def toggle_play(self):
        try:
            self.player.pause = not self.player.pause
        except Exception:
            pass

    def play(self):
        try:
            self.player.pause = False
        except Exception:
            pass

    def set_ab(self, span):
        """Loop between two video times, or stop looping (None)."""
        try:
            if span is None:
                self.player["ab-loop-a"] = "no"
                self.player["ab-loop-b"] = "no"
                self.status.setText("")
            else:
                self.player["ab-loop-a"] = f"{span[0]:.3f}"
                self.player["ab-loop-b"] = f"{span[1]:.3f}"
                self.status.setText(f"Looping {span[1] - span[0]:.1f} s")
        except Exception:
            pass

    def pause(self):
        try:
            self.player.pause = True
        except Exception:
            pass

    def frame(self, forward: bool):
        try:
            self.player.command("frame-step" if forward else "frame-back-step")
        except Exception:
            pass

    def step(self, dt: float):
        try:
            self.player.command("seek", f"{dt:.3f}", "relative+exact")
        except Exception:
            pass

    def seek(self, t: float):
        try:
            self.player.command("seek", f"{max(0.0, t):.3f}", "absolute+exact")
        except Exception:
            pass

    def state(self) -> tuple[bool, float | None]:
        """(paused, video time) or (True, None) if nothing is loaded."""
        try:
            return bool(self.player.pause), self.player.time_pos
        except Exception:
            return True, None

    def shutdown(self):
        for v in (self.video, self.ref_video):
            try:
                if v is not None:
                    v.shutdown()
            except Exception:
                pass
