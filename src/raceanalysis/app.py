# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Race Analysis: the track-side app.

Tabs (Overview, Laps, Corners & grip) all follow one shared state: the
sessions loaded, the lap being looked at ("focus"), the laps overlaid on the
plots ("compare"), the reference lap, and a cursor (metres along the
reference lap). The video sits in its own panel beside the tabs, shown or
hidden per tab, and follows the cursor. Any tab can pop out into its own
window and goes back when that window is closed.

    python -m raceanalysis [data.csv ...]
    raceoverlay --analysis [data.csv ...]   (the AppImage)
"""
from __future__ import annotations

import argparse
import locale
import os
import sys
import threading
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from racecore import analysis, engine, gosync, ingest, telemetry, tracks
from raceoverlay import media
from raceoverlay.syncgui import pick_file, preview_mode, settings

from .common import FT, INK_DIM, MAX_COMPARE, MPH, REF_COLOR, SERIES, fmt_lap, session_name
from .pages.corners import CornersPage
from .pages.engine import EnginePage
from .pages.laps import LapsPage
from .pages.overview import OverviewPage
from .pages.track import TrackPage
from .videopanel import VideoPanel


class PopOut(QtWidgets.QWidget):
    """A tab page in its own window; closing it puts the page back."""
    closed = QtCore.Signal(object)

    def __init__(self, page, title):
        super().__init__(None, QtCore.Qt.Window)
        self.page = page
        self.setWindowTitle(f"{title} – Race Analysis")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(page)

    def closeEvent(self, ev):
        self.closed.emit(self)
        super().closeEvent(ev)


class AnalysisWindow(QtWidgets.QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Race Analysis")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.a = analysis.Analysis()
        self.engine = engine.EngineData(self.a)
        self.use_mph = settings().value("use_mph", True, type=bool)
        self.compare: list[tuple[int, int]] = []   # lap keys overlaid, in colour order
        self.focus: tuple[int, int] | None = None  # lap the video, readout and turns follow
        self.cursor_d = 0.0                        # metres along the reference lap
        self._video_session: int | None = None
        self._vt = 0.0
        self._duration = 0.0
        self._sync_job: dict | None = None
        self._popouts: dict[str, PopOut] = {}
        self._pending = False
        self._loop: tuple[float, float] | None = None   # metres along the reference
        self._ref_session: int | None = None   # session loaded in the reference player
        self._ref_duration = 0.0
        self._ref_anchor: tuple[float, float] | None = None   # (your video t, ref video t)
        self._ref_i = 0.0
        self.ref_d: float | None = None   # where the reference video is, metres
        avail = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1680, int(avail.width() * 0.96)),
                    min(1040, int(avail.height() * 0.94)))
        pg.setConfigOptions(antialias=True)
        self._build()
        self.refresh()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    # ================================================================ UI ===
    def _build(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction("Import from GoPro card…", self.import_card, "Ctrl+I")
        m.addAction("Open track day…", self.open_day, "Ctrl+O")
        m.addAction("Add data file…", self.add_session_dialog)
        m.addSeparator()
        self.act_find = m.addAction("Find video for this session…", self.find_video)
        self.act_remove = m.addAction("Remove this session", self.remove_session)
        m.addSeparator()
        m.addAction("Close", self.close, "Ctrl+W")
        m = mb.addMenu("&Track")
        self.act_turns = m.addAction("Track turns…", self.edit_turns, "Ctrl+T")
        m = mb.addMenu("&View")
        self.act_video = m.addAction("Show video on this tab", self._toggle_video, "V")
        self.act_video.setCheckable(True)
        m.addAction("Pop out this tab", self._pop_current, "Ctrl+Shift+P")
        m = mb.addMenu("Vi&deo")
        vp = lambda: self.videop  # noqa: E731 - built below
        self.video_actions = []
        for text, key, fn in (
                ("Play / pause", "Space", lambda: vp().toggle_play()),
                ("Back 2 s", "Left", lambda: vp().step(-2.0)),
                ("Forward 2 s", "Right", lambda: vp().step(2.0)),
                ("Back 0.2 s", "Shift+Left", lambda: vp().step(-0.2)),
                ("Forward 0.2 s", "Shift+Right", lambda: vp().step(0.2)),
                ("Back one frame", ",", lambda: vp().frame(False)),
                ("Forward one frame", ".", lambda: vp().frame(True)),
                ("Back to start of turn / lap", "Home", self._restart),
                ("Reference video", "R", lambda: vp().chk_ref.toggle()),
                ("Loop picked turns", "L", lambda: self._page(CornersPage).loop.toggle())):
            act = m.addAction(text, fn)
            act.setShortcut(QtGui.QKeySequence(key))
            # this window only (not dialogs); pop-outs get the same actions
            act.setShortcutContext(QtCore.Qt.WindowShortcut)
            self.video_actions.append(act)

        central = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(central)
        cl.setContentsMargins(6, 4, 6, 4)
        cl.setSpacing(4)

        # ---- top bar
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Lap"))
        self.lap_pick = QtWidgets.QComboBox()
        self.lap_pick.setMinimumContentsLength(24)
        self.lap_pick.activated.connect(
            lambda i: self.lap_focused(self.lap_pick.itemData(i)))
        bar.addWidget(self.lap_pick)
        bar.addSpacing(12)
        bar.addWidget(QtWidgets.QLabel("Reference"))
        self.ref_pick = QtWidgets.QComboBox()
        self.ref_pick.setMinimumContentsLength(24)
        self.ref_pick.setToolTip("Everything is compared against this lap")
        self.ref_pick.activated.connect(lambda i: self.set_reference(self.ref_pick.itemData(i)))
        bar.addWidget(self.ref_pick)
        bar.addSpacing(12)
        self.info = QtWidgets.QLabel("")
        self.info.setStyleSheet(f"color:{INK_DIM};")
        bar.addWidget(self.info, 1)
        self.btn_video = QtWidgets.QToolButton()
        self.btn_video.setText("Video")
        self.btn_video.setCheckable(True)
        self.btn_video.setToolTip("Show the video beside this tab (V)")
        self.btn_video.toggled.connect(self._set_video_visible)
        bar.addWidget(self.btn_video)
        cl.addLayout(bar)

        # ---- tabs | video
        self.split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_menu)
        self.pages = [OverviewPage(self), LapsPage(self), CornersPage(self), EnginePage(self),
                      TrackPage(self)]
        for p in self.pages:
            self.tabs.addTab(p, p.title.replace("&", "&&"))
        self.tabs.currentChanged.connect(self._tab_changed)
        self.split.addWidget(self.tabs)
        self.videop = VideoPanel()
        self.player = self.videop.player
        self.videop.compare_changed.connect(self._ref_toggled)
        self.videop.mode_changed.connect(self._ref_mode)
        i = self.videop.mode.findData(settings().value("analysis/refmode", "spot"))
        self.videop.mode.setCurrentIndex(max(0, i))
        self.split.addWidget(self.videop)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        state = settings().value("analysis/split")
        if state is None or not self.split.restoreState(state):
            self.split.setSizes([900, 520])
        self.split.splitterMoved.connect(
            lambda *_: settings().setValue("analysis/split", self.split.saveState()))
        cl.addWidget(self.split, 1)
        self.setCentralWidget(central)
        self.videop.chk_ref.setChecked(settings().value("analysis/refvideo", False, type=bool))
        self.tabs.setCurrentIndex(int(settings().value("analysis/tab", 0)))
        self._tab_changed(self.tabs.currentIndex())

    # ------------------------------------------------------------ helpers
    def x(self, metres):
        return np.asarray(metres) * FT if self.use_mph else np.asarray(metres)

    def from_x(self, x: float) -> float:
        return x / FT if self.use_mph else x

    def dist_unit(self) -> str:
        return "ft" if self.use_mph else "m"

    def speed_unit(self) -> str:
        return "mph" if self.use_mph else "km/h"

    def spd(self, kmh):
        return np.asarray(kmh) * (MPH if self.use_mph else 1.0)

    def lap(self, key) -> analysis.LapData | None:
        return next((ld for ld in self.a.laps if ld.key == key), None)

    def focus_lap(self) -> analysis.LapData | None:
        return self.lap(self.focus) if self.focus else None

    def label(self, ld) -> str:
        return ld.label(self.a.sessions)

    def name(self, ld) -> str:
        """'Lap 5 – 2:19.806' (with the session name when there are several)."""
        return f"{self.label(ld)}  –  {fmt_lap(ld.time)}"

    def color(self, key) -> str:
        if key in self.compare:
            return SERIES[self.compare.index(key)]
        ld = self.lap(key)
        return REF_COLOR if ld is not None and ld is self.a.ref else SERIES[0]

    def status(self, text: str, ms: int = 8000):
        self.statusBar().showMessage(text, ms)

    # ============================================================== state ===
    def lap_focused(self, key):
        """A lap was picked (lap list, top bar, lap-time chart)."""
        if key is None or self.lap(key) is None:
            return
        self.focus = key
        if key not in self.compare and self.lap(key) is not self.a.ref:
            if len(self.compare) >= MAX_COMPARE:
                self.compare.pop(0)
            self.compare.append(key)
        self._later(self._laps_changed)

    def lap_checked(self, key, on: bool):
        ld = self.lap(key)
        if ld is not None and ld is not self.a.ref:   # the reference is always shown
            if on and key not in self.compare:
                if len(self.compare) >= MAX_COMPARE:
                    self.compare.pop(0)
                self.compare.append(key)
            elif not on and key in self.compare:
                self.compare.remove(key)
        self._later(self._laps_changed)

    def _later(self, fn):
        """Run after the click that caused it has finished (a list must never
        be rebuilt from inside its own click handler)."""
        if not self._pending:
            self._pending = True

            def run():
                self._pending = False
                fn()
            QtCore.QTimer.singleShot(0, run)

    def set_reference(self, key):
        ld = self.lap(key)
        if ld is None or not ld.timed or ld is self.a.ref:
            return
        self.a.set_reference(ld)
        self.compare = [k for k in self.compare if k != key]
        self.refresh()

    def goto_turn(self, n: int):
        """Open the Corners & grip tab on turn n."""
        page = self._page(CornersPage)
        self._show_page(page)
        page.show_turn(n)

    def set_loop(self, span):
        """Loop the video over span (metres along the reference) on the focus
        lap, or stop looping (None). A new loop starts playing."""
        new = span is not None and span != self._loop
        self._loop = span
        self._apply_ab()
        if new and self._loop_vt is not None and self.videop.isVisible():
            self.videop.seek(self._loop_vt[0])
            self.videop.play()

    _loop_vt = None

    def _apply_ab(self):
        """Turn the loop into video times for the lap the video follows."""
        self._loop_vt = None
        ld = self.focus_lap()
        v = self._session_video(ld) if ld is not None else None
        if self._loop is not None and v and self.a.ref is not None:
            t0, t1 = (ld.lap.start + self.a.time_at(ld, d) - v[1] for d in self._loop)
            if t1 > t0 and t0 >= 0 and (not self._duration or t1 <= self._duration):
                self._loop_vt = (t0, t1)
        self.videop.set_ab(self._loop_vt)

    def _restart(self):
        """Home: back to the start of the looped turns, else the lap."""
        d = self._loop[0] if self._loop is not None else 0.0
        self.set_cursor(d, seek=True)

    def _set_ref_d(self, rd):
        if rd is None and self.ref_d is None:
            return
        if rd is not None and self.ref_d is not None and abs(rd - self.ref_d) < 0.2:
            return
        self.ref_d = rd
        for p in self.pages:
            p.ref_cursor(rd)

    def zoom(self, d0, d1):
        self._page(LapsPage).zoom(d0, d1)

    def _page(self, cls):
        return next(p for p in self.pages if isinstance(p, cls))

    # ============================================================ refresh ===
    def refresh(self):
        """Sessions, reference or turns changed: rebuild everything."""
        self.engine = engine.EngineData(self.a)
        self._fill_pickers()
        for p in self.pages:
            p.refresh()
        self._info()
        self._load_video()
        self._load_ref_video()
        self.set_cursor(self.cursor_d)
        has = self.a.ref is not None
        for act in (self.act_find, self.act_remove, self.act_turns):
            act.setEnabled(has)

    def _laps_changed(self):
        self._sync_pickers()
        for p in self.pages:
            p.laps_changed()
        self._load_video()
        self._load_ref_video()
        self._apply_ab()
        self.set_cursor(self.cursor_d)

    def _fill_pickers(self):
        a = self.a
        for cb in (self.lap_pick, self.ref_pick):
            cb.blockSignals(True)
            cb.clear()
        for ld in a.laps:
            txt = self.name(ld) + ("" if ld.timed else "  (slow)")
            self.lap_pick.addItem(txt, ld.key)
            if ld.timed:
                self.ref_pick.addItem(self.name(ld), ld.key)
        for cb in (self.lap_pick, self.ref_pick):
            cb.blockSignals(False)
        self._sync_pickers()

    def _sync_pickers(self):
        for cb, key in ((self.lap_pick, self.focus),
                        (self.ref_pick, self.a.ref.key if self.a.ref else None)):
            # findData compares Python tuples by identity, so match by value
            i = next((j for j in range(cb.count()) if cb.itemData(j) == key), -1)
            cb.blockSignals(True)
            cb.setCurrentIndex(i)
            cb.blockSignals(False)

    def _info(self):
        a = self.a
        if a.ref is None:
            self.info.setText("")
            return
        names = ", ".join(s.name for s in a.sessions)
        track = a.profile.name if a.profile is not None else ""
        self.info.setText(f"{track + ' · ' if track else ''}{names}")

    # ============================================================== tabs ===
    def _tab_changed(self, i):
        if i < 0:
            return
        settings().setValue("analysis/tab", i)
        page = self.tabs.widget(i)
        on = settings().value(f"analysis/video/{page.title}", page.video_default, type=bool)
        self.btn_video.blockSignals(True)
        self.btn_video.setChecked(on)
        self.btn_video.blockSignals(False)
        self.act_video.setChecked(on)
        self.videop.setVisible(on)
        # only the Corners tab loops the video
        corners = self._page(CornersPage)
        if page is corners:
            corners._apply_loop()
        elif corners.title not in self._popouts:
            self.set_loop(None)

    def _set_video_visible(self, on: bool):
        page = self.tabs.currentWidget()
        if page is not None:
            settings().setValue(f"analysis/video/{page.title}", on)
        self.act_video.setChecked(on)
        self.videop.setVisible(on)
        if not on:
            self.videop.pause()

    def _toggle_video(self):
        self.btn_video.setChecked(not self.btn_video.isChecked())

    def _tab_menu(self, pos):
        i = self.tabs.tabBar().tabAt(pos)
        if i < 0:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("Pop out into its own window", lambda: self.pop_out(self.tabs.widget(i)))
        m.exec(self.tabs.tabBar().mapToGlobal(pos))

    def _pop_current(self):
        if self.tabs.currentWidget() is not None:
            self.pop_out(self.tabs.currentWidget())

    def pop_out(self, page):
        if page.title in self._popouts or self.tabs.count() <= 1:
            return
        self.tabs.removeTab(self.tabs.indexOf(page))
        w = PopOut(page, page.title)
        geo = settings().value(f"analysis/popout/{page.title}")
        if geo is None or not w.restoreGeometry(geo):
            w.resize(1000, 700)
        w.addActions(self.video_actions)
        w.closed.connect(self._pop_closed)
        self._popouts[page.title] = w
        settings().setValue(f"analysis/popped/{page.title}", True)
        w.show()
        page.show()

    def _pop_closed(self, w: PopOut):
        page = w.page
        settings().setValue(f"analysis/popout/{page.title}", w.saveGeometry())
        if not getattr(self, "_closing", False):
            settings().setValue(f"analysis/popped/{page.title}", False)
        self._popouts.pop(page.title, None)
        idx = min(self.pages.index(page), self.tabs.count())
        self.tabs.insertTab(idx, page, page.title.replace("&", "&&"))
        page.show()

    def _show_page(self, page):
        if page.title in self._popouts:
            w = self._popouts[page.title]
            w.raise_()
            w.activateWindow()
        else:
            self.tabs.setCurrentWidget(page)

    def restore_popouts(self):
        for p in list(self.pages):
            if settings().value(f"analysis/popped/{p.title}", False, type=bool):
                self.pop_out(p)

    # ============================================================= cursor ===
    def set_cursor(self, d: float, seek: bool = False):
        a = self.a
        if a.ref is None:
            self.videop.strip.show_values("–", "–", "–", self.speed_unit(), None)
            return
        d = float(np.clip(d, 0.0, a.length))
        self.cursor_d = d
        for p in self.pages:
            p.cursor(d)
        ld = self.focus_lap() or a.ref
        t_into = a.time_at(ld, d)
        spd = float(self.spd(np.interp(d, a.grid, a.channel_on_grid(ld, "speed"))))
        dl = None if ld is a.ref else float(np.interp(d, a.grid, a.delta(ld)))
        gear, rpm = self._gear_rpm(ld, t_into)
        note = ""
        v = self.a.sessions[ld.session].video
        if not v:
            note = "No synced video for this session (File → Find video for this session…)."
        elif seek and not self._seek_lap(ld, t_into):
            note = "This point isn't in the video."
        self.videop.strip.set_accent(self.color(ld.key))
        self.videop.strip.show_values(f"{ld.lap.n}", fmt_lap(t_into, 1), f"{spd:.0f}",
                                      self.speed_unit(), dl, gear, rpm, note)

    def _gear_rpm(self, ld, t_into):
        """Readout strings for the video strip (None: the data hasn't got it)."""
        e = self.engine
        col = e.col_of("rpm") if e else None
        rpm = e.at(ld, col, t_into) if col else None
        rpm = f"{rpm:,.0f}" if rpm is not None and rpm > 0 else None
        gear = None
        if e and e.gearing and e.gears[ld.session] is not None:
            g = e.gear_at(ld, t_into)
            gear = str(g) if g else "–"
        return gear, rpm

    # ============================================================== video ===
    def _load_video(self):
        ld = self.focus_lap()
        si = ld.session if ld is not None else None
        if si == self._video_session:
            return
        self._video_session = si
        s = self.a.sessions[si] if si is not None else None
        if s is None or not s.video:
            self._duration = 0.0
            self.videop.stop()
            return
        parts, _off = s.video
        try:
            info = media.probe(parts)
            self._duration = info.duration
            self.videop.set_aspect(info.width, info.height)
        except Exception:
            self._duration = 0.0
        self.videop.load(media.mpv_url(parts))
        QtCore.QTimer.singleShot(500, self._apply_ab)
        QtCore.QTimer.singleShot(400, lambda: self.set_cursor(self.cursor_d, seek=True))

    def _session_video(self, ld):
        s = self.a.sessions[ld.session]
        return s.video if s.video and ld.session == self._video_session else None

    def _seek_lap(self, ld, t_into: float) -> bool:
        v = self._session_video(ld)
        if not v:
            return False
        vt = ld.lap.start + t_into - v[1]
        if self._duration and not (0 <= vt <= self._duration):
            return False
        self.videop.seek(vt)
        self._vt = vt
        self._ref_anchor = None
        return True

    def _tick(self):
        if self._sync_job is not None and self._sync_job["done"]:
            self._finish_find_video()
        ld = self.focus_lap()
        if ld is None:
            return
        paused, vt = self.videop.state()
        self.videop.btn_play.setText("Play" if paused else "Pause")
        self._follow_video(ld, paused, vt)
        self._follow_ref(ld, paused, vt)

    def _follow_video(self, ld, paused, vt):
        v = self._session_video(ld)
        if vt is None or not v:
            return
        # follow the video while it plays, and once more after it pauses or
        # steps, so the cursor ends on the frame that's showing
        if paused and abs(float(vt) - self._vt) < 1e-3:
            return
        if float(vt) < self._vt - 0.5:     # jumped back (loop, step): re-sync the ghost
            self._ref_anchor = None
        self._vt = float(vt)
        t_into = float(vt) + v[1] - ld.lap.start
        if 0 <= t_into <= ld.time:
            self.set_cursor(self.a.dist_at(ld, t_into))

    # ==================================================== reference video ===
    def _ref_toggled(self, on: bool):
        settings().setValue("analysis/refvideo", on)
        self._ref_session = None
        self._ref_anchor = None
        if on:
            self._load_ref_video()

    def _ref_mode(self, mode: str):
        settings().setValue("analysis/refmode", mode)
        self._ref_anchor = None

    def _load_ref_video(self):
        vp = self.videop
        if not vp.compare:
            return
        ref = self.a.ref
        if ref is None:
            vp.set_ref_note("")
            return
        s = self.a.sessions[ref.session]
        if not s.video:
            self._ref_session = None
            vp.ref_stop()
            vp.set_ref_note(f"No synced video for {s.name} (the reference lap's session).")
            return
        if self._ref_session == ref.session:
            return
        self._ref_session = ref.session
        self._ref_anchor = None
        parts, _off = s.video
        try:
            self._ref_duration = media.probe(parts).duration
        except Exception:
            self._ref_duration = 0.0
        vp.ref_load(media.mpv_url(parts))

    def _follow_ref(self, ld, paused, vt):
        """Keep the reference video in step with yours: at the same place on
        track, or (ghost) the same time since the last seek or loop start."""
        vp, a = self.videop, self.a
        ref = a.ref
        if not vp.compare or ref is None or vp.ref_video is None:
            self._set_ref_d(None)
            return
        if self._ref_session is None or self._ref_session != ref.session:
            self._set_ref_d(None)
            return
        rs = a.sessions[ref.session]
        roff = rs.video[1]
        if ld is ref:
            vp.ref_pause()
            vp.set_ref_note("This is the reference lap. Pick another lap to compare.")
            self._set_ref_d(None)
            return
        d = self.cursor_d
        spot = ref.lap.start + a.time_at(ref, d) - roff
        mine = self._session_video(ld)
        if vp.mode.currentData() == "clock" and mine and vt is not None:
            if self._ref_anchor is None:
                self._ref_anchor = (float(vt), spot)
            target = self._ref_anchor[1] + (float(vt) - self._ref_anchor[0])
        else:
            target = spot
        r_into = target + roff - ref.lap.start
        if not (0 <= target <= (self._ref_duration or 1e9)) or not (-5 <= r_into <= ref.time + 5):
            vp.ref_pause()
            vp.set_ref_note("The reference lap isn't in its video here.")
            self._set_ref_d(None)
            return
        vp.set_ref_note("")
        r_paused, rt = vp.ref_state()
        if rt is None:
            return
        if paused or not mine:
            vp.ref_pause()
            if abs(rt - target) > 0.03:
                vp.ref_seek(target)
        else:
            err = target - float(rt)
            if abs(err) > 0.6:
                vp.ref_seek(target)
                vp.ref_play(1.0)
                self._ref_i = 0.0
            else:
                # PI control on playback speed: the integral takes out the
                # steady lag from mpv's own latency
                self._ref_i = float(np.clip(self._ref_i + err * 0.05, -0.5, 0.5))
                ff = 1.0
                if vp.mode.currentData() != "clock":
                    # the reference covers this stretch at its own speed
                    vm = np.interp(d, a.grid, a.channel_on_grid(ld, "speed"))
                    vr = np.interp(d, a.grid, a.channel_on_grid(ref, "speed"))
                    ff = float(np.clip(vm / max(vr, 5.0), 0.5, 2.0))
                adj = np.clip(2.0 * err + 1.5 * self._ref_i, -0.4, 0.4)
                vp.ref_play(float(np.clip(ff * (1 + adj), 0.4, 2.5)))
        # readout for the reference
        r_into = float(np.clip(r_into, 0, ref.time))
        rd = a.dist_at(ref, r_into)
        rspd = float(self.spd(np.interp(rd, a.grid, a.channel_on_grid(ref, "speed"))))
        gap = None
        if vp.mode.currentData() == "clock":
            g = float(self.x(rd - d))
            gap = ("AHEAD" if g >= 0 else "BEHIND", f"{abs(g):.0f}", self.dist_unit())
        else:
            gap = ("VS YOU", f"{-float(np.interp(d, a.grid, a.delta(ld))):+.2f}", "s")
        vp.ref_strip.set_accent(REF_COLOR)
        rgear, rrpm = self._gear_rpm(ref, r_into)
        vp.ref_strip.show_values(f"{ref.lap.n}", fmt_lap(r_into, 1), f"{rspd:.0f}",
                                 self.speed_unit(), None, rgear, rrpm, gap=gap)
        self._set_ref_d(rd)

    # ========================================================== sessions ===
    def add_session(self, path: str, tel=None, video=None, quiet=False,
                    refresh=True) -> str | None:
        """Add a data file. Returns None on success, else why it wasn't added
        (also shown in a message unless quiet)."""
        try:
            if any(Path(s.path).resolve() == Path(path).resolve() for s in self.a.sessions):
                return "already open"
            tel = tel if tel is not None else telemetry.load(path)
            if video is None:
                s = settings()
                video = analysis.find_video(path, [
                    s.value("import/library", ""), str(Path(path).parent)])
            self.a.add(tel, path, name=session_name(path), video=video)
        except Exception as e:  # noqa: BLE001 - shown to the user
            if not quiet:
                QtWidgets.QMessageBox.warning(self, "Can't add this file",
                                              f"{Path(path).name}\n\n{e}")
            return str(e)
        if len(self.a.sessions) == 1 and self.a.origin:
            self.a.use_profile(self._track_for(*self.a.origin))
        if self.focus is None or self.lap(self.focus) is None:
            self._default_selection()
        if refresh:
            self._video_session = None
            self.refresh()
        return None

    def add_many(self, items):
        """Add (csv, video files, offset) sessions; one message for problems."""
        added, problems = 0, []
        for csv, parts, off in items:
            why = self.add_session(csv, video=(parts, off) if parts else None,
                                   quiet=True, refresh=False)
            if why is None:
                added += 1
            elif why != "already open":
                problems.append(f"{Path(csv).name}: {why}")
        self._video_session = None
        self.refresh()
        if problems:
            QtWidgets.QMessageBox.warning(self, "Some files weren't added", "\n".join(problems))
        return added

    def _default_selection(self):
        """Reference plus the latest timed lap, which the video follows."""
        timed = self.a.timed_laps()
        if not timed or self.a.ref is None:
            return
        last = max(timed, key=lambda ld: (ld.session, ld.lap.start))
        if last is self.a.ref and len(timed) > 1:
            last = sorted(timed, key=lambda ld: ld.time)[1]
        self.focus = last.key
        self.compare = [last.key] if last is not self.a.ref else []

    def _track_for(self, lat, lon):
        """The saved track for this start line; with several layouts there,
        the one used last."""
        near = [p for p in tracks.load_all()
                if float(np.hypot(*tracks.to_xy(lat, lon, p.lat0, p.lon0)[0]))
                <= tracks.SAME_TRACK_M]
        last = settings().value("analysis/track_last", "")
        return next((p for p in near if p.path == last), None) or tracks.find(lat, lon)

    def remember_track(self, p):
        if p is not None and p.path:
            settings().setValue("analysis/track_last", p.path)

    def import_card(self):
        from .importer import ImportDialog
        dlg = ImportDialog(self)
        if dlg.exec() and dlg.result:
            n = self.add_many(dlg.result)
            self.status(f"Imported: {n} session(s) added.")

    def open_day(self):
        from .importer import _pick_dir
        s = settings()
        start = s.value("analysis/day", "") or s.value("import/library", "")
        d = _pick_dir(self, "Track-day folder", start)
        if not d:
            return
        s.setValue("analysis/day", d)
        found = ingest.synced_sessions(d)
        if not found:
            QtWidgets.QMessageBox.information(
                self, "Nothing synced here",
                "No synced videos in this folder. Import from the card first, or use "
                "File → Add data file… and Find video for this session….")
            return
        self.add_many(found)

    def add_session_dialog(self):
        p = pick_file(self, "Add a data file", "csv",
                      "Telemetry (*.csv *.CSV *.txt);;All files (*)")
        if p:
            self.add_session(p)

    def remove_session(self):
        ld = self.focus_lap()
        if ld is None:
            return
        s = self.a.sessions[ld.session]
        r = QtWidgets.QMessageBox.question(self, "Remove session",
                                           f"Remove {s.name} from this analysis? "
                                           "(Nothing is deleted from disk.)")
        if r != QtWidgets.QMessageBox.Yes:
            return
        self.a.remove(ld.session)
        self.compare, self.focus = [], None
        self._video_session = None
        if self.a.sessions:
            self._default_selection()
        self.refresh()

    def edit_turns(self):
        """Track → Track turns: open the Track tab."""
        self._show_page(self._page(TrackPage))

    # ------------------------------------------------------- find video ---
    def find_video(self):
        ld = self.focus_lap()
        if ld is None or self._sync_job is not None:
            return
        si = ld.session
        s = self.a.sessions[si]
        p = pick_file(self, f"GoPro video for {s.name}", "video",
                      "Video (*.mp4 *.MP4 *.mov *.MOV);;All files (*)")
        if not p:
            return
        parts = media.find_chapters(p)
        tel = s.tel
        job = {"si": si, "parts": parts, "done": False, "res": None, "err": None}
        self._sync_job = job
        self.status(f"Syncing {Path(p).name} to {s.name} from the camera's sensors…", 0)

        def work():
            try:
                info = media.probe(parts)
                res, *_ = gosync.sync_parts([(x.path, x.start) for x in info.parts], tel)
                job["res"] = res
            except gosync.NoCameraData:
                job["err"] = ("This video has no GoPro sensor track (merged or re-encoded "
                              "files lose it). Pick the original GH01xxxx file.")
            except Exception as e:  # noqa: BLE001
                job["err"] = str(e)
            job["done"] = True
        threading.Thread(target=work, daemon=True).start()

    def _finish_find_video(self):
        job, self._sync_job = self._sync_job, None
        self.statusBar().clearMessage()
        if job["err"]:
            QtWidgets.QMessageBox.warning(self, "Couldn't sync", job["err"])
            return
        res = job["res"]
        if not res.confident:
            r = QtWidgets.QMessageBox.question(
                self, "Weak match",
                f"The best match is weak ({res.score:.0%}, next best {res.runner_up:.0%}). "
                f"The video may be from another session.\n\nUse it anyway "
                f"(offset {res.offset:+.2f} s)?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if r != QtWidgets.QMessageBox.Yes:
                return
        s = self.a.sessions[job["si"]]
        s.video = (job["parts"], res.offset)
        ingest.save_pairing(job["parts"], s.path, res.offset)
        self._video_session = None
        self.refresh()
        self.status(f"Video synced to {s.name} (offset {res.offset:+.2f} s).")

    def closeEvent(self, ev):
        tp = self._page(TrackPage)
        if tp.editor.dirty:
            r = QtWidgets.QMessageBox.question(
                self, "Unsaved track changes", "Save the changes to this track's turns?",
                QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
                | QtWidgets.QMessageBox.Cancel, QtWidgets.QMessageBox.Save)
            if r == QtWidgets.QMessageBox.Cancel or (
                    r == QtWidgets.QMessageBox.Save and not tp.editor.save()):
                ev.ignore()
                return
        self._closing = True
        self.timer.stop()
        for w in list(self._popouts.values()):
            w.close()
        self.videop.shutdown()
        super().closeEvent(ev)


def open_window(sessions, parent=None) -> AnalysisWindow:
    """Open a window with (csv path, Telemetry or None, video or None) loaded."""
    win = AnalysisWindow(parent)
    win.show()
    for path, tel, video in sessions:
        win.add_session(path, tel, video)
    return win


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raceanalysis", description="Compare laps and sessions.")
    ap.add_argument("csv", nargs="*", help="Data files from the same track")
    args = ap.parse_args(argv)
    fmt = QtGui.QSurfaceFormat()
    fmt.setProfile(QtGui.QSurfaceFormat.CompatibilityProfile)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)
    orig_qpa = os.environ.get("QT_QPA_PLATFORM")
    if preview_mode() == "native":
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["raceanalysis"])
    if orig_qpa is None:
        os.environ.pop("QT_QPA_PLATFORM", None)
    else:
        os.environ["QT_QPA_PLATFORM"] = orig_qpa
    app.setApplicationName("raceoverlay")  # share Race Overlay's settings
    app.setApplicationDisplayName("Race Analysis")
    app.setDesktopFileName("raceanalysis")
    icon = Path(__file__).resolve().parent.parent / "raceoverlay" / "icon.svg"
    if icon.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon)))
    locale.setlocale(locale.LC_NUMERIC, "C")  # libmpv refuses other locales
    win = AnalysisWindow()
    win.show()
    win.restore_popouts()
    for p in args.csv:
        if os.path.exists(p):
            win.add_session(p)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
