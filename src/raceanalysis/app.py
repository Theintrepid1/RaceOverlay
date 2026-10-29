# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""RaceAnalysis window: lap table, delta and speed traces against distance,
track map, time lost per corner, and the video following the cursor.

Opened from Race Overlay ("Analyze laps…"), or on its own:
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

from racecore import analysis, gosync, ingest, telemetry, tracks
from raceoverlay import media
from raceoverlay.syncgui import (MpvNativeWidget, MpvWidget, _btn, _note,
                                 pick_file, preview_mode, settings)

# Series colours (fixed order, dark surface); reference = neutral grey.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500"]
REF_COLOR = "#b5b4ad"
GAIN, LOSS, NEUTRAL = "#199e70", "#e66767", "#6b6a64"
CURSOR = "#f2f1ec"
MAX_COMPARE = len(SERIES)
FT = 3.28084


def session_name(path: str) -> str:
    """'Session 2 · Sep 27' for APEX Pro exports, else the file name."""
    lg = ingest.log_info(path) if Path(path).exists() else None
    if lg and lg.session and ingest.APEX_DATE_RE.search(Path(path).name):
        return f"Session {lg.session} · {lg.date:%b %d}"
    return Path(path).stem


def fmt_lap(s: float) -> str:
    m = int(s // 60)
    return f"{m}:{s - 60 * m:06.3f}" if m else f"{s:.3f}"


def fmt_gap(s: float) -> str:
    return f"{s:+.2f}"


class AnalysisWindow(QtWidgets.QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Race Analysis")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.a = analysis.Analysis()
        self.use_mph = settings().value("use_mph", True, type=bool)
        self.compare: list[tuple[int, int]] = []   # lap keys shown, in colour order
        self.focus: tuple[int, int] | None = None  # lap the video and corners follow
        self.cursor_d = 0.0                        # metres along the reference
        self._video_session: int | None = None
        self._vt = 0.0
        self._duration = 0.0
        self._following = False
        self._sync_job: dict | None = None
        avail = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1680, int(avail.width() * 0.95)),
                    min(1000, int(avail.height() * 0.92)))
        self._build_ui()
        self._summary()  # welcome text until a session is added
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    # ================================================================ UI ===
    def _build_ui(self):
        pg.setConfigOptions(antialias=True)
        outer = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        # ---- left: video over track map
        left = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        vbox = QtWidgets.QWidget()
        vl = QtWidgets.QVBoxLayout(vbox)
        vl.setContentsMargins(0, 0, 0, 0)
        native = (preview_mode() == "native"
                  and QtGui.QGuiApplication.platformName() == "xcb")
        self.video = MpvNativeWidget() if native else MpvWidget()
        self.video.setMinimumSize(320, 180)
        self.player = self.video.player
        vl.addWidget(self.video, 1)
        crow = QtWidgets.QHBoxLayout()
        self.btn_play = _btn("Play", self._toggle_play)
        crow.addWidget(_btn("-2s", lambda: self._step(-2.0)))
        crow.addWidget(self.btn_play)
        crow.addWidget(_btn("+2s", lambda: self._step(2.0)))
        self.video_lbl = _note("")
        crow.addWidget(self.video_lbl, 1)
        vl.addLayout(crow)
        left.addWidget(vbox)
        self.map = pg.PlotWidget()
        self.map.setAspectLocked(True)
        self.map.hideAxis("left")
        self.map.hideAxis("bottom")
        self.map.setMenuEnabled(False)
        self.map.scene().sigMouseClicked.connect(self._map_clicked)
        self.gg = pg.PlotWidget()
        self.gg.setAspectLocked(True)
        self.gg.setMenuEnabled(False)
        for ax in ("left", "bottom"):
            self.gg.getAxis(ax).enableAutoSIPrefix(False)
        self.gg.setLabel("bottom", "Lateral g (right +)")
        self.gg.setLabel("left", "Brake – / accel + (g)")
        self.gg_turn: int | None = None   # turn highlighted on the g-g diagram
        self.left_tabs = QtWidgets.QTabWidget()
        self.left_tabs.addTab(self.map, "Track map")
        self.left_tabs.addTab(self.gg, "g-g (grip)")
        left.addWidget(self.left_tabs)
        left.setSizes([420, 420])
        outer.addWidget(left)

        # ---- right: tools, tables, traces
        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
        tools = QtWidgets.QHBoxLayout()
        tools.addWidget(_btn("Import from GoPro card…", self._import_card,
                             "Copy new recordings off the card into a folder per "
                             "track day, match each to its data file and sync it"))
        tools.addWidget(_btn("Open track day…", self._open_day,
                             "Load every synced session in a track-day folder"))
        tools.addWidget(_btn("Add data file…", self._add_session_dialog,
                             "Add a data file from the same track"))
        tools.addWidget(_btn("Find video…", self._find_video,
                             "Pick the GoPro video for the selected session and "
                             "sync it from the camera's sensors"))
        tools.addWidget(_btn("Remove session", self._remove_session))
        tools.addWidget(_btn("Use as reference", self._set_reference,
                             "Compare everything against the selected lap"))
        tools.addWidget(_btn("Track turns…", self._edit_turns,
                             "Name the turns like the official track map"))
        tools.addStretch(1)
        rl.addLayout(tools)
        self.summary = _note("")
        rl.addWidget(self.summary)

        rsplit = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        tables = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.lap_tree = QtWidgets.QTreeWidget()
        self.lap_tree.setHeaderLabels(["Lap", "Gap", "Grip", "S1", "S2", "S3"])
        self.lap_tree.headerItem().setToolTip(
            2, "Grip used while cornering and braking, against the best you've shown "
               "the car can do (100%)")
        self.lap_tree.setRootIsDecorated(True)
        self.lap_tree.setUniformRowHeights(True)
        self.lap_tree.header().setStretchLastSection(False)
        self.lap_tree.itemChanged.connect(self._lap_checked)
        self.lap_tree.currentItemChanged.connect(self._lap_selected)
        # clicking the row you're already on brings it back if it was unticked
        self.lap_tree.itemClicked.connect(
            lambda it, col: col > 0 and self._lap_selected(it, None))
        tables.addWidget(self.lap_tree)
        cbox = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(cbox)
        cl.setContentsMargins(0, 0, 0, 0)
        self.corner_lbl = _note("")
        cl.addWidget(self.corner_lbl)
        self.corner_tbl = QtWidgets.QTableWidget(0, 6)
        self.corner_tbl.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.corner_tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.corner_tbl.verticalHeader().setVisible(False)
        self.corner_tbl.cellClicked.connect(self._corner_clicked)
        cl.addWidget(self.corner_tbl, 1)
        tables.addWidget(cbox)
        tables.setSizes([520, 520])
        rsplit.addWidget(tables)

        self.traces = pg.GraphicsLayoutWidget()
        self.p_delta = self.traces.addPlot(row=0, col=0)
        self.p_speed = self.traces.addPlot(row=1, col=0)
        self.p_long = self.traces.addPlot(row=2, col=0)
        self.p_grip = self.traces.addPlot(row=3, col=0)
        self.plots = [self.p_delta, self.p_speed, self.p_long, self.p_grip]
        for p in self.plots:
            p.showGrid(x=True, y=True, alpha=0.15)
            for ax in ("left", "bottom"):
                p.getAxis(ax).enableAutoSIPrefix(False)  # g, not mg
            p.setMenuEnabled(False)
            p.getAxis("left").setWidth(52)
            if p is not self.p_delta:
                p.setXLink(self.p_delta)
        self.p_delta.setLabel("left", "Delta vs ref (s)")
        self.p_long.setLabel("left", "Brake – / accel + (g)")
        self.p_grip.setLabel("left", "Grip used (%)")
        self.cursors = []
        for p in self.plots:
            ln = pg.InfiniteLine(angle=90, movable=True,
                                 pen=pg.mkPen(CURSOR, width=1))
            ln.sigDragged.connect(lambda l: self.set_cursor(self._from_x(l.value())))
            p.addItem(ln, ignoreBounds=True)
            self.cursors.append(ln)
        self.traces.scene().sigMouseClicked.connect(self._traces_clicked)
        rsplit.addWidget(self.traces)
        rsplit.setSizes([300, 560])
        rl.addWidget(rsplit, 1)
        outer.addWidget(right)
        outer.setSizes([560, 1100])
        self.setCentralWidget(outer)

    # ============================================================ helpers ===
    def _x(self, metres):
        return np.asarray(metres) * FT if self.use_mph else np.asarray(metres)

    def _from_x(self, x: float) -> float:
        return x / FT if self.use_mph else x

    def _dist_unit(self) -> str:
        return "ft" if self.use_mph else "m"

    def _spd(self, kmh):
        return np.asarray(kmh) * (0.621371 if self.use_mph else 1.0)

    def _name(self, ld) -> str:
        """'Lap 5 – 2:19.806' (with the session name when there are several)."""
        return f"{ld.label(self.a.sessions)}  –  {fmt_lap(ld.time)}"

    def _lap(self, key) -> analysis.LapData | None:
        return next((ld for ld in self.a.laps if ld.key == key), None)

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
            self.a.use_profile(tracks.find(*self.a.origin))
        if self.focus is None or self._lap(self.focus) is None:
            self._default_selection()
        if refresh:
            self._refresh()
        return None

    def _add_many(self, items: list[tuple[str, list[str] | None, float | None]]):
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
        self._refresh()
        if problems:
            QtWidgets.QMessageBox.warning(self, "Some files weren't added",
                                          "\n".join(problems))
        return added

    def _import_card(self):
        from .importer import ImportDialog
        dlg = ImportDialog(self)
        if dlg.exec() and dlg.result:
            n = self._add_many(dlg.result)
            self.summary.setText(self.summary.text() +
                                 f" &nbsp; <i>Imported: {n} session(s) added.</i>")

    def _open_day(self):
        from .importer import _pick_dir
        s = settings()
        lib = s.value("import/library", "")
        start = s.value("analysis/day", "") or lib
        d = _pick_dir(self, "Track-day folder", start)
        if not d:
            return
        s.setValue("analysis/day", d)
        found = ingest.synced_sessions(d)
        if not found:
            QtWidgets.QMessageBox.information(
                self, "Nothing synced here",
                "No synced videos in this folder. Import from the card first, or "
                "use Add data file… and Find video….")
            return
        self._add_many(found)

    def _edit_turns(self):
        if self.a.ref is None:
            return
        from .turns import TurnsDialog
        dlg = TurnsDialog(self, self.a, self.a.profile)
        if dlg.exec():
            self.a.use_profile(dlg.profile)
            self._refresh()

    # ------------------------------------------------------- find video ---
    def _selected_session(self) -> int | None:
        it = self.lap_tree.currentItem()
        if it is None:
            return None
        data = it.data(0, QtCore.Qt.UserRole)
        return data[1] if data[0] == "session" else data[1][0]

    def _find_video(self):
        si = self._selected_session()
        if si is None or self._sync_job is not None:
            return
        s = self.a.sessions[si]
        p = pick_file(self, f"GoPro video for {s.name}", "video",
                      "Video (*.mp4 *.MP4 *.mov *.MOV);;All files (*)")
        if not p:
            return
        parts = media.find_chapters(p)
        tel = s.tel
        job = {"si": si, "parts": parts, "done": False, "res": None, "err": None}
        self._sync_job = job
        self.summary.setText(f"Syncing {Path(p).name} to {s.name} from the camera's "
                             "sensors…")

        def work():
            try:
                info = media.probe(parts)
                res, *_ = gosync.sync_parts([(x.path, x.start) for x in info.parts], tel)
                job["res"] = res
            except gosync.NoCameraData:
                job["err"] = ("This video has no GoPro sensor track (merged or "
                              "re-encoded files lose it). Pick the original "
                              "GH01xxxx file.")
            except Exception as e:  # noqa: BLE001
                job["err"] = str(e)
            job["done"] = True
        threading.Thread(target=work, daemon=True).start()

    def _finish_find_video(self):
        job, self._sync_job = self._sync_job, None
        if job["err"]:
            QtWidgets.QMessageBox.warning(self, "Couldn't sync", job["err"])
            self._summary()
            return
        res = job["res"]
        if not res.confident:
            r = QtWidgets.QMessageBox.question(
                self, "Weak match",
                f"The best match is weak ({res.score:.0%}, next best "
                f"{res.runner_up:.0%}). The video may be from another session.\n\n"
                f"Use it anyway (offset {res.offset:+.2f} s)?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if r != QtWidgets.QMessageBox.Yes:
                self._summary()
                return
        s = self.a.sessions[job["si"]]
        s.video = (job["parts"], res.offset)
        ingest.save_pairing(job["parts"], s.path, res.offset)
        self._video_session = None
        self._refresh()

    def _add_session_dialog(self):
        p = pick_file(self, "Add a data file", "csv",
                      "Telemetry (*.csv *.CSV *.txt);;All files (*)")
        if p:
            self.add_session(p)

    def _remove_session(self):
        it = self.lap_tree.currentItem()
        if it is None or not self.a.sessions:
            return
        data = it.data(0, QtCore.Qt.UserRole)
        si = data[1] if data[0] == "session" else data[1][0]
        self.a.remove(si)
        self.compare, self.focus = [], None
        self._video_session = None
        if self.a.sessions:
            self._default_selection()
        self._refresh()

    def _default_selection(self):
        """Reference plus the latest timed lap, which the video follows."""
        timed = self.a.timed_laps()
        if not timed or self.a.ref is None:
            return
        last = max(timed, key=lambda ld: (ld.session, ld.lap.start))
        if last is self.a.ref and len(timed) > 1:
            last = sorted(timed, key=lambda ld: ld.time)[1]
        self.focus = last.key
        self.compare = [last.key]

    def _set_reference(self):
        ld = self._lap(self.focus) if self.focus else None
        if ld is None or not ld.timed:
            return
        self.a.set_reference(ld)
        self.compare = [k for k in self.compare if k != ld.key]
        self._refresh()

    # ============================================================ refresh ===
    def _refresh(self):
        self._fill_laps()
        self._draw_traces()
        self._draw_map()
        self._draw_gg()
        self._fill_corners()
        self._summary()
        self._load_video()
        self.set_cursor(self.cursor_d)

    def _summary(self):
        a = self.a
        if a.ref is None:
            self.summary.setText(
                "<b>At the track:</b> Import from GoPro card… copies the new "
                "recordings and matches them to your data files. "
                "<b>Later:</b> Open track day… loads a day you imported before.")
            return
        bs = a.best_sectors()
        parts = " + ".join(f"S{k + 1} lap {ld.lap.n}" for k, (_, ld) in enumerate(bs))
        txt = (f"<b>Reference:</b> {self._name(a.ref)}"
               f" &nbsp; <b>Theoretical best:</b> {fmt_lap(a.theoretical_best())}"
               f" <small>({parts})</small>")
        self.summary.setText(txt)

    def _fill_laps(self):
        a = self.a
        tree = self.lap_tree
        tree.blockSignals(True)
        tree.clear()
        if a.ref is None:
            tree.blockSignals(False)
            return
        best = a.ref.time
        bsec = [t for t, _ in a.best_sectors()]
        purple = QtGui.QColor("#b38cff")
        for si, s in enumerate(a.sessions):
            vid = "video" if s.video else "no video"
            top = QtWidgets.QTreeWidgetItem([f"{s.name}  ({vid})"])
            top.setData(0, QtCore.Qt.UserRole, ("session", si))
            top.setFirstColumnSpanned(True)
            tree.addTopLevelItem(top)
            for ld in [x for x in a.laps if x.session == si]:
                row = [f"Lap {ld.lap.n}  –  {fmt_lap(ld.time)}",
                       "" if ld is a.ref else (fmt_gap(ld.time - best) if ld.timed else "slow")]
                lg = a.lap_grip(ld)
                row += [f"{lg * 100:.0f}%" if lg is not None else ""]
                row += [f"{t:.2f}" for t in ld.sectors]
                it = QtWidgets.QTreeWidgetItem(row)
                it.setData(0, QtCore.Qt.UserRole, ("lap", ld.key))
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(0, QtCore.Qt.Unchecked)  # set in _restyle_laps
                for c in range(1, 6):
                    it.setTextAlignment(c, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if ld is a.ref:
                    f = it.font(0)
                    f.setBold(True)
                    for c in range(6):
                        it.setFont(c, f)
                    it.setText(1, "ref")
                for k, t in enumerate(ld.sectors):
                    if ld.timed and abs(t - bsec[k]) < 1e-9:
                        it.setForeground(3 + k, purple)
                if not ld.timed:
                    for c in range(6):
                        it.setForeground(c, QtGui.QColor(NEUTRAL))
                top.addChild(it)
            top.setExpanded(True)
        for c in range(6):
            tree.resizeColumnToContents(c)
        tree.blockSignals(False)
        self._restyle_laps()

    def _lap_items(self):
        tree = self.lap_tree
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            for j in range(top.childCount()):
                yield top.child(j)

    def _restyle_laps(self):
        """Check marks, colours and the current row, on the existing rows.
        Never clears the tree, so it is safe to call after a click on it."""
        a, tree = self.a, self.lap_tree
        tree.blockSignals(True)
        default = tree.palette().color(QtGui.QPalette.Text)
        for it in self._lap_items():
            key = it.data(0, QtCore.Qt.UserRole)[1]
            ld = self._lap(key)
            if ld is None:
                continue
            shown = key in self.compare or ld is a.ref
            want = QtCore.Qt.Checked if shown else QtCore.Qt.Unchecked
            if it.checkState(0) != want:
                it.setCheckState(0, want)
            if key in self.compare:
                it.setForeground(0, QtGui.QColor(SERIES[self.compare.index(key)]))
            elif ld.timed:
                it.setForeground(0, default)
            if key == self.focus and tree.currentItem() is not it:
                tree.setCurrentItem(it)
        tree.blockSignals(False)

    def _lap_checked(self, it, col):
        data = it.data(0, QtCore.Qt.UserRole)
        if not data or data[0] != "lap":
            return
        key = data[1]
        ld = self._lap(key)
        checked = it.checkState(0) == QtCore.Qt.Checked
        if ld is not self.a.ref:  # the reference is always shown
            if checked and key not in self.compare:
                if len(self.compare) >= MAX_COMPARE:
                    self.compare.pop(0)
                self.compare.append(key)
            elif not checked and key in self.compare:
                self.compare.remove(key)
        # Qt is still inside this click: update after it returns
        QtCore.QTimer.singleShot(0, self._compare_changed)

    def _compare_changed(self):
        self._restyle_laps()
        self._draw_traces()
        self._draw_map()
        self._draw_gg()

    def _lap_selected(self, it, _prev):
        if it is None:
            return
        data = it.data(0, QtCore.Qt.UserRole)
        if not data or data[0] != "lap":
            return
        self.focus = data[1]
        if self.focus not in self.compare and self._lap(self.focus) is not self.a.ref:
            if len(self.compare) >= MAX_COMPARE:
                self.compare.pop(0)
            self.compare.append(self.focus)
        QtCore.QTimer.singleShot(0, self._focus_changed)

    def _focus_changed(self):
        self._restyle_laps()
        self._draw_traces()
        self._draw_map()
        self._draw_gg()
        self._fill_corners()
        self._load_video()
        self.set_cursor(self.cursor_d)

    # ============================================================= traces ===
    def _draw_traces(self):
        for p, cur in zip(self.plots, self.cursors):
            p.clear()
            p.addItem(cur, ignoreBounds=True)
        a = self.a
        if a.ref is None:
            return
        x = self._x(a.grid)
        unit = "mph" if self.use_mph else "km/h"
        self.p_speed.setLabel("left", f"Speed ({unit})")
        self.p_grip.setLabel("bottom", f"Distance ({self._dist_unit()})")
        # corner bands, shaded every other one, named on the speed trace
        for c in a.corners:
            if c.n % 2 == 0:
                for p in self.plots:
                    band = pg.LinearRegionItem(values=(self._x(c.start_d), self._x(c.end_d)),
                                               movable=False,
                                               brush=pg.mkBrush(255, 255, 255, 12),
                                               pen=pg.mkPen(None))
                    band.setZValue(-10)
                    p.addItem(band, ignoreBounds=True)
        rp = pg.mkPen(REF_COLOR, width=1.6)
        if self.p_delta.legend is None:
            self.p_delta.addLegend(offset=(8, 4), labelTextColor="#d8d7d0",
                                   brush=pg.mkBrush(20, 20, 20, 200))
        self.p_delta.legend.clear()
        self.p_delta.plot([0, float(x[-1])], [0, 0], pen=rp,
                          name=f"Ref: {self._name(a.ref)}")
        self.p_speed.plot(x, self._spd(a.channel_on_grid(a.ref, "speed")), pen=rp)
        g = a.channel_on_grid(a.ref, "lon_g")
        if g is not None:
            self.p_long.plot(x, g, pen=rp)
        self.p_grip.addLine(y=100, pen=pg.mkPen("#6b6a64", style=QtCore.Qt.DashLine))
        u = a.grip_on_grid(a.ref)
        if u is not None:
            self.p_grip.plot(x, u * 100, pen=rp, connect="finite")
        for i, key in enumerate(self.compare):
            ld = self._lap(key)
            if ld is None:
                continue
            pen = pg.mkPen(SERIES[i], width=1.6 if key == self.focus else 1.1)
            self.p_delta.plot(x, a.delta(ld), pen=pen, name=self._name(ld))
            self.p_speed.plot(x, self._spd(a.channel_on_grid(ld, "speed")), pen=pen)
            g = a.channel_on_grid(ld, "lon_g")
            if g is not None:
                self.p_long.plot(x, g, pen=pen)
            u = a.grip_on_grid(ld)
            if u is not None:
                self.p_grip.plot(x, u * 100, pen=pen, connect="finite")
        self.p_grip.setYRange(0, 115, padding=0)
        vr = self.p_speed.viewRange()[1]
        for c in a.corners:
            t = pg.TextItem(c.name, color="#d8d7d0", anchor=(0.5, 0))
            t.setPos(float(self._x(c.apex_d)), vr[1])
            self.p_speed.addItem(t, ignoreBounds=True)
        self.p_delta.setXRange(0, float(x[-1]), padding=0.01)

    def _traces_clicked(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            return
        pos = ev.scenePos()
        for p in self.plots:
            vb = p.getViewBox()
            if vb.sceneBoundingRect().contains(pos):
                self.set_cursor(self._from_x(vb.mapSceneToView(pos).x()), seek=True)
                return

    # ================================================================ map ===
    def _draw_map(self):
        self.map.clear()
        a = self.a
        if a.ref is None:
            return
        ref = a.ref
        self.map.plot(ref.xy[:, 0], ref.xy[:, 1], pen=pg.mkPen("#3a3a37", width=7))
        ld = self._lap(self.focus) if self.focus else None
        if ld is not None and ld is not a.ref:
            # colour the reference line by where this lap gained or lost time
            rate = np.gradient(a.delta(ld)) / analysis.STEP_M * 100  # s per 100 m
            gx = np.interp(a.grid, a._ref_s, ref.xy[:, 0])
            gy = np.interp(a.grid, a._ref_s, ref.xy[:, 1])
            k = np.ones(15) / 15
            rate = np.convolve(rate, k, "same")
            cols = np.where(rate > 0.02, 1, np.where(rate < -0.02, -1, 0))
            for val, col in ((1, LOSS), (-1, GAIN)):
                m = cols == val
                self.map.plot(gx[m], gy[m], pen=None, symbol="o", symbolSize=4,
                              symbolPen=None, symbolBrush=pg.mkBrush(col))
        else:
            self.map.plot(ref.xy[:, 0], ref.xy[:, 1], pen=pg.mkPen(REF_COLOR, width=2))
        for c in a.corners:
            px = float(np.interp(c.apex_d, a._ref_s, ref.xy[:, 0]))
            py = float(np.interp(c.apex_d, a._ref_s, ref.xy[:, 1]))
            t = pg.TextItem(c.name, color="#d8d7d0", anchor=(0.5, 1.2))
            t.setPos(px, py)
            self.map.addItem(t)
        sx, sy = ref.xy[0]
        self.map.plot([sx], [sy], pen=None, symbol="s", symbolSize=9,
                      symbolBrush=pg.mkBrush("#f2f1ec"))
        col = SERIES[self.compare.index(self.focus)] \
            if self.focus in self.compare else REF_COLOR
        self.marker = self.map.plot([sx], [sy], pen=None, symbol="o", symbolSize=13,
                                    symbolPen=pg.mkPen("#0b0b0b", width=2),
                                    symbolBrush=pg.mkBrush(col))

    # ================================================================ g-g ===
    def _draw_gg(self):
        self.gg.clear()
        self.gg_marker = None
        a = self.a
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
        for txt, x, y, anc in (("Braking", 0, -1.55, (0.5, 0)), ("Accel", 0, 1.55, (0.5, 1)),
                               ("Left", -1.55, 0, (0, 1)), ("Right", 1.55, 0, (1, 1))):
            t = pg.TextItem(txt, color="#8a8983", anchor=anc)
            t.setPos(x, y)
            self.gg.addItem(t)
        if a.envelope is not None:
            o = a.envelope.outline()
            self.gg.plot(o[:, 0], o[:, 1], pen=pg.mkPen(REF_COLOR, width=2,
                                                        style=QtCore.Qt.DashLine))
        ld = self._lap(self.focus) if self.focus else a.ref
        c = next((c for c in a.corners if c.n == self.gg_turn), None)
        for lap, col, size in ((a.ref, REF_COLOR, 3),
                               (ld if ld is not a.ref else None, None, 4)):
            if lap is None or lap.lat_g is None or lap.lon_g is None:
                continue
            col = col or (SERIES[self.compare.index(lap.key)]
                          if lap.key in self.compare else SERIES[0])
            fast = lap.v > 30
            if c is None:
                sel = fast
                alpha = 70 if lap is a.ref and ld is not a.ref else 170
                self.gg.plot(lap.lat_g[sel], lap.lon_g[sel], pen=None, symbol="o",
                             symbolSize=size, symbolPen=None,
                             symbolBrush=pg.mkBrush(QtGui.QColor(col).red(),
                                                    QtGui.QColor(col).green(),
                                                    QtGui.QColor(col).blue(), alpha))
            else:
                inside = fast & (lap.d >= c.start_d) & (lap.d <= c.end_d)
                self.gg.plot(lap.lat_g[fast & ~inside], lap.lon_g[fast & ~inside], pen=None,
                             symbol="o", symbolSize=3, symbolPen=None,
                             symbolBrush=pg.mkBrush(120, 120, 115, 40))
                self.gg.plot(lap.lat_g[inside], lap.lon_g[inside],
                             pen=pg.mkPen(col, width=1.2), symbol="o", symbolSize=size + 2,
                             symbolPen=None, symbolBrush=pg.mkBrush(col))
        if c is not None:
            t = pg.TextItem(f"{c.name}: this lap vs reference (grey)", color="#d8d7d0",
                            anchor=(0, 0))
            t.setPos(-1.6, 1.6)
            self.gg.addItem(t)
        self.gg_marker = self.gg.plot([0], [0], pen=None, symbol="o", symbolSize=13,
                                      symbolPen=pg.mkPen("#0b0b0b", width=2),
                                      symbolBrush=pg.mkBrush(CURSOR))
        self.gg.setRange(xRange=(-1.6, 1.6), yRange=(-1.6, 1.6), padding=0.02)

    def _map_clicked(self, ev):
        a = self.a
        if a.ref is None or ev.button() != QtCore.Qt.LeftButton:
            return
        vb = self.map.getViewBox()
        p = vb.mapSceneToView(ev.scenePos())
        d2 = (a.ref.xy[:, 0] - p.x()) ** 2 + (a.ref.xy[:, 1] - p.y()) ** 2
        i = int(np.argmin(d2))
        self.set_cursor(float(a._ref_s[i]), seek=True)

    # ============================================================ corners ===
    def _fill_corners(self):
        t = self.corner_tbl
        t.setRowCount(0)
        ld = self._lap(self.focus) if self.focus else None
        a = self.a
        u = self._dist_unit()
        su = "mph" if self.use_mph else "km/h"
        t.setHorizontalHeaderLabels(["Corner", "Time vs ref", "Grip used", f"Min speed ({su})",
                                     f"Braked ({u} vs ref)", "Best anyone"])
        if ld is None or a.ref is None:
            self.corner_lbl.setText("")
            return
        if ld is a.ref:
            self.corner_lbl.setText("Pick a lap other than the reference to see "
                                    "where it gained or lost time.")
            return
        res = sorted(a.corner_results(ld), key=lambda r: -r.lost)
        total = ld.time - a.ref.time
        self.corner_lbl.setText(
            f"<b>{self._name(ld)}</b> vs reference: {fmt_gap(total)} s. "
            + (f"Turns from the {self.a.profile.name} map; "
               if self.a.profile is not None and self.a.profile.turns else
               "Corners found from speed (C1 = first after the line; Track turns… "
               "names them like the track map); ")
            + "each owns the track up to the next one.")
        t.setRowCount(len(res))
        for r, cr in enumerate(res):
            c = cr.corner
            lr = c.name + (f" ({c.direction})" if c.direction else "")
            if cr.brake_d is not None and cr.brake_d_ref is not None:
                diff = self._x(cr.brake_d - cr.brake_d_ref)
                brake = (f"{abs(diff):.0f} later" if diff > 0 else
                         f"{abs(diff):.0f} earlier" if diff < 0 else "same")
            else:
                brake = "no hard braking"
            g = cr.grip
            gtxt = (f"{g.used * 100:.0f}% / {g.used_ref * 100:.0f}%"
                    if g is not None and g.used is not None and g.used_ref is not None else "")
            cells = [lr, fmt_gap(cr.lost), gtxt,
                     f"{self._spd(cr.vmin):.0f} / {self._spd(cr.vmin_ref):.0f}",
                     brake, fmt_gap(cr.best_lost)]
            for col, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                it.setData(QtCore.Qt.UserRole, c.n)
                if col == 1:
                    it.setForeground(QtGui.QColor(LOSS if cr.lost > 0.05 else
                                                  GAIN if cr.lost < -0.05 else NEUTRAL))
                if col == 2 and g is not None and g.used is not None and g.used_ref is not None:
                    it.setForeground(QtGui.QColor(LOSS if g.used < g.used_ref - 0.05 else
                                                  GAIN if g.used > g.used_ref + 0.05 else NEUTRAL))
                    it.setToolTip(f"Grip used while cornering/braking: this lap "
                                  f"{g.used:.0%}, reference {g.used_ref:.0%}. Peak "
                                  f"{g.peak_g:.2f} g vs {g.peak_g_ref:.2f} g; below 80% "
                                  f"for {g.soft_s:.1f} s vs {g.soft_s_ref:.1f} s.")
                if col > 0:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                t.setItem(r, col, it)
        t.resizeColumnsToContents()

    def _corner_clicked(self, row, _col):
        it = self.corner_tbl.item(row, 0)
        if it is None:
            return
        n = it.data(QtCore.Qt.UserRole)
        c = next((c for c in self.a.corners if c.n == n), None)
        if c is None:
            return
        # clicking the turn already shown on the g-g goes back to the whole lap
        self.gg_turn = None if self.gg_turn == n else n
        self._draw_gg()
        if self.gg_turn is None:
            self.p_delta.setXRange(0, float(self._x(self.a.length)), padding=0.01)
            return
        # land on the approach so the video shows the braking zone
        start = max(c.start_d, c.apex_d - 250.0)
        self.set_cursor(start, seek=True)
        for p in self.plots[:1]:
            p.setXRange(float(self._x(c.start_d)), float(self._x(c.end_d)), padding=0.02)

    # ============================================================= cursor ===
    def set_cursor(self, d: float, seek: bool = False):
        a = self.a
        if a.ref is None:
            return
        d = float(np.clip(d, 0.0, a.length))
        self.cursor_d = d
        x = float(self._x(d))
        for ln in self.cursors:
            ln.blockSignals(True)
            ln.setValue(x)
            ln.blockSignals(False)
        ld = self._lap(self.focus) if self.focus else a.ref
        if ld is None:
            return
        # the marker sits where the followed lap actually was at this point
        k = int(np.searchsorted(ld.d, d))
        k = min(k, len(ld.xy) - 1)
        if hasattr(self, "marker"):
            self.marker.setData([ld.xy[k, 0]], [ld.xy[k, 1]])
        if getattr(self, "gg_marker", None) is not None and ld.lat_g is not None \
                and ld.lon_g is not None:
            self.gg_marker.setData([float(np.interp(d, a.grid, a.channel_on_grid(ld, "lat_g")))],
                                   [float(np.interp(d, a.grid, a.channel_on_grid(ld, "lon_g")))])
        t_into = a.time_at(ld, d)
        spd = self._spd(np.interp(d, a.grid, a.channel_on_grid(ld, "speed")))
        dl = float(np.interp(d, a.grid, a.delta(ld))) if ld is not a.ref else 0.0
        self.video_lbl.setText(
            f"{self._name(ld)} · at {fmt_lap(t_into)} · {spd:.0f} "
            f"{'mph' if self.use_mph else 'km/h'} · {fmt_gap(dl)} s vs ref")
        if seek:
            self._seek_lap(ld, t_into)

    # ============================================================== video ===
    def _load_video(self):
        ld = self._lap(self.focus) if self.focus else None
        si = ld.session if ld is not None else None
        if si == self._video_session:
            return
        self._video_session = si
        s = self.a.sessions[si] if si is not None else None
        if s is None or not s.video:
            self._duration = 0.0
            try:
                self.player.command("stop")
            except Exception:
                pass
            if s is not None:
                self.video_lbl.setText("No synced video for this session. Sync it in "
                                       "Race Overlay first (Import or Auto-sync).")
            return
        parts, _off = s.video
        try:
            self._duration = media.probe(parts).duration
        except Exception:
            self._duration = 0.0
        self.video.load(media.mpv_url(parts))
        QtCore.QTimer.singleShot(400, lambda: self.set_cursor(self.cursor_d, seek=True))

    def _session_video(self, ld):
        s = self.a.sessions[ld.session]
        return s.video if s.video and ld.session == self._video_session else None

    def _seek_lap(self, ld, t_into: float):
        v = self._session_video(ld)
        if not v:
            return
        vt = ld.lap.start + t_into - v[1]
        if self._duration and not (0 <= vt <= self._duration):
            self.video_lbl.setText(self.video_lbl.text() + "  (not in the video)")
            return
        try:
            self.player.command("seek", f"{max(0.0, vt):.3f}", "absolute+exact")
            self._vt = vt
        except Exception:
            pass

    def _toggle_play(self):
        try:
            self.player.pause = not self.player.pause
        except Exception:
            pass

    def _step(self, dt: float):
        try:
            self.player.command("seek", f"{dt:.3f}", "relative+exact")
        except Exception:
            pass

    def _tick(self):
        """While the video plays, move the cursor with it."""
        if self._sync_job is not None and self._sync_job["done"]:
            self._finish_find_video()
        ld = self._lap(self.focus) if self.focus else None
        if ld is None:
            return
        try:
            paused = bool(self.player.pause)
            vt = self.player.time_pos
        except Exception:
            return
        self.btn_play.setText("Play" if paused else "Pause")
        v = self._session_video(ld)
        if vt is None or not v:
            return
        # follow the video while it plays, plus once more after it pauses or
        # steps, so the cursor ends on the frame that's showing
        if paused and abs(float(vt) - self._vt) < 1e-3:
            return
        self._vt = float(vt)
        t_into = float(vt) + v[1] - ld.lap.start
        if 0 <= t_into <= ld.time:
            self.set_cursor(self.a.dist_at(ld, t_into))

    def closeEvent(self, ev):
        self.timer.stop()
        try:
            self.video.shutdown()
        except Exception:
            pass
        super().closeEvent(ev)


def open_window(sessions: list[tuple[str, object, tuple | None]], parent=None) -> AnalysisWindow:
    """Open an analysis window with (csv path, Telemetry or None, video or None)
    sessions already loaded. Used by Race Overlay's "Analyze laps…"."""
    win = AnalysisWindow(parent)
    win.show()
    for path, tel, video in sessions:
        win.add_session(path, tel, video)
    return win


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raceanalysis",
                                 description="Compare laps and sessions.")
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
    for p in args.csv:
        if os.path.exists(p):
            win.add_session(p)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
