# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""File → Open event folder…: tie a hand-organised event together.

Scans the folder (racecore.event), syncs every GoPro recording to the data
file it fits, and shows the result for review: one row per session with its
video, and a second camera when one was running at the same time (or added
here by hand). Open saves the pairings next to the videos and opens the
sessions; next time Open track day opens the same folder at once.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from racecore import event

COLS = ["", "Session", "Data file", "Video", "Second camera", "Match"]
SECOND_COL = 4


class _Bridge(QtCore.QObject):
    progress = QtCore.Signal(str)
    done = QtCore.Signal(object, object, object)       # scan, sessions, error
    added = QtCore.Signal(int, object)                 # row, error (None = ok)


class EventDialog(QtWidgets.QDialog):
    def __init__(self, parent, folder: str):
        super().__init__(parent)
        self.folder = folder
        self.setWindowTitle(f"Open event — {Path(folder).name}")
        self.sc = None
        self.sessions: list[event.Session] = []
        self.result = None
        self._cancel = False
        self._busy = True
        self.bridge = _Bridge()
        self.bridge.progress.connect(self._on_progress)
        self.bridge.done.connect(self._on_done)
        self.bridge.added.connect(self._on_added)

        lay = QtWidgets.QVBoxLayout(self)
        self.status = QtWidgets.QLabel("Looking through the folder…")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)
        lay.addWidget(self.bar)
        self.table = QtWidgets.QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked
                                   | QtWidgets.QAbstractItemView.EditKeyPressed)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table.itemChanged.connect(self._item_changed)
        lay.addWidget(self.table, 1)
        row = QtWidgets.QHBoxLayout()
        self.btn_add2 = QtWidgets.QPushButton("Add second camera…")
        self.btn_add2.setToolTip("Pick another GoPro recording from the same session (a "
                                 "rear or in-car camera); it's synced from its sensors and "
                                 "shown picture-in-picture in Race Overlay")
        self.btn_add2.clicked.connect(self._add_second)
        self.btn_rm2 = QtWidgets.QPushButton("Remove second camera")
        self.btn_rm2.clicked.connect(self._remove_second)
        self.btn_swap = QtWidgets.QPushButton("Swap video and second camera everywhere")
        self.btn_swap.setToolTip("With two cameras, which one is the main video is told by "
                                 "which way it faces; that's a guess unless a file or "
                                 "folder name says front or rear")
        self.btn_swap.clicked.connect(self._swap_all)
        self.btn_swap_one = QtWidgets.QPushButton("Swap for this session")
        self.btn_swap_one.clicked.connect(self._swap_one)
        for b in (self.btn_add2, self.btn_rm2, self.btn_swap, self.btn_swap_one):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.table.itemSelectionChanged.connect(self._refresh_buttons)
        self.left_out = QtWidgets.QTextBrowser()
        self.left_out.setMaximumHeight(130)
        self.left_out.setVisible(False)
        lay.addWidget(self.left_out)
        bb = QtWidgets.QDialogButtonBox()
        self.btn_open = bb.addButton("Open these sessions", QtWidgets.QDialogButtonBox.AcceptRole)
        self.btn_cancel = bb.addButton(QtWidgets.QDialogButtonBox.Cancel)
        self.btn_open.clicked.connect(self._open)
        self.btn_cancel.clicked.connect(self._close)
        lay.addWidget(bb)
        self.resize(1000, 560)
        self._refresh_buttons()
        threading.Thread(target=self._work, daemon=True).start()

    # ----- background -----

    def _work(self):
        try:
            sc = event.scan(self.folder, progress=self.bridge.progress.emit,
                            cancelled=lambda: self._cancel)
            sessions = event.match(sc, progress=self.bridge.progress.emit,
                                   cancelled=lambda: self._cancel)
            self.bridge.done.emit(sc, sessions, None)
        except Exception as e:  # noqa: BLE001 - shown in the dialog
            self.bridge.done.emit(None, None, e)

    def _on_progress(self, text: str):
        self.status.setText(text + "…")

    def _on_done(self, sc, sessions, err):
        self._busy = False
        self.bar.setVisible(False)
        if self._cancel:
            self.reject()
            return
        if err is not None:
            self.status.setText(f"<b>Couldn't read this folder:</b> {err}")
            self._refresh_buttons()
            return
        self.sc, self.sessions = sc, sessions
        self._status_text()
        self._fill()
        self._fill_left_out()
        self._refresh_buttons()

    # ----- table -----

    def _status_text(self):
        sessions = self.sessions
        if not sessions:
            self.status.setText("No data files with g data in this folder.")
            return
        with_video = sum(1 for s in sessions if s.main)
        seconds = sum(1 for s in sessions if s.second)
        text = f"{len(sessions)} session(s), {with_video} with a video"
        if seconds:
            text += (f", {seconds} with a second camera (it ran at the same time; "
                     "check which one should be the main video)")
        self.status.setText(text + ". Untick anything you don't want, then Open. "
                            "Double-click a name to change it.")

    def _fill(self):
        t = self.table
        t.setColumnHidden(SECOND_COL, not any(s.second for s in self.sessions))
        t.blockSignals(True)
        t.setRowCount(len(self.sessions))
        for r, s in enumerate(self.sessions):
            use = QtWidgets.QTableWidgetItem()
            use.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            use.setCheckState(QtCore.Qt.Checked if s.use else QtCore.Qt.Unchecked)
            t.setItem(r, 0, use)
            title = QtWidgets.QTableWidgetItem(s.title)
            t.setItem(r, 1, title)
            for col, text, tip in ((2, Path(s.data.path).name, s.data.path),
                                   (3, *self._cam(s.main)), (4, *self._cam(s.second))):
                it = QtWidgets.QTableWidgetItem(text)
                it.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
                it.setToolTip(tip)
                t.setItem(r, col, it)
            scores = [c.score for c in (s.main, s.second) if c]
            m = QtWidgets.QTableWidgetItem(
                " / ".join(f"{x:.0%}" for x in scores) if scores else "no video")
            m.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            m.setToolTip("How well each camera's motion sensors match the data file")
            t.setItem(r, 5, m)
        t.blockSignals(False)

    @staticmethod
    def _cam(c) -> tuple[str, str]:
        if c is None:
            return "—", ""
        name = c.video.name + (f" +{len(c.video.parts) - 1}" if len(c.video.parts) > 1 else "")
        return name, "\n".join(c.video.parts) + f"\noffset {c.offset:+.2f} s"

    def _fill_left_out(self):
        if self.sc is None:
            return
        used = {c.video.parts[0] for s in self.sessions for c in (s.main, s.second) if c}
        root = Path(self.folder)

        def rel(p: str) -> str:
            try:
                return Path(p).relative_to(root).as_posix()
            except ValueError:
                return Path(p).name
        lines = []
        for s in self.sessions:
            for c in s.extra:
                lines.append(f"{rel(c.video.parts[0])}: also fits {Path(s.data.path).name} "
                             "(another recording from that session); not used")
        for v in self.sc.videos:
            if v.parts[0] not in used and v.note:
                lines.append(f"{rel(v.parts[0])}: {v.note}")
        for p, why in self.sc.skipped:
            if why.startswith("a copy") or Path(p).suffix.lower() in event.DATA_EXT:
                lines.append(f"{rel(p)}: {why}")
        seen, uniq = set(), []
        for x in lines:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        if uniq:
            self.left_out.setPlainText("Left out:\n" + "\n".join(uniq))
            self.left_out.setVisible(True)

    def _item_changed(self, it):
        r = it.row()
        if not (0 <= r < len(self.sessions)):
            return
        if it.column() == 0:
            self.sessions[r].use = it.checkState() == QtCore.Qt.Checked
            self._refresh_buttons()
        elif it.column() == 1:
            self.sessions[r].title = it.text().strip()

    def _swap_all(self):
        event.swap_all(self.sessions)
        self._fill()

    def _row(self):
        r = self.table.currentRow()
        return (r, self.sessions[r]) if 0 <= r < len(self.sessions) else (r, None)

    def _swap_one(self):
        r, s = self._row()
        if s is not None and s.second is not None:
            s.main, s.second = s.second, s.main
            self._fill()
            self.table.selectRow(r)

    def _add_second(self):
        from raceoverlay import media
        from raceoverlay.syncgui import pick_file
        r, s = self._row()
        if s is None:
            return
        p = pick_file(self, f"Second camera for {s.title}", "video",
                      "Videos (*.mp4 *.MP4 *.mov *.MOV);;All files (*)")
        if not p:
            return
        parts = media.find_chapters(p)
        self._busy = True
        self.bar.setVisible(True)
        self.status.setText(f"Syncing {Path(p).name} to {s.title}…")
        self._refresh_buttons()

        def work():
            try:
                event.add_second(s, parts)
                self.bridge.added.emit(r, None)
            except Exception as e:  # noqa: BLE001 - shown in the dialog
                self.bridge.added.emit(r, e)
        threading.Thread(target=work, daemon=True).start()

    def _on_added(self, r, err):
        self._busy = False
        self.bar.setVisible(False)
        if err is not None:
            self.status.setText(f"<b>Not added:</b> {err}. (Videos without a GoPro sensor "
                                "track can be added in Race Overlay's Second camera section "
                                "and lined up by hand.)")
        else:
            self._status_text()
        self._fill()
        self.table.selectRow(r)
        self._refresh_buttons()

    def _remove_second(self):
        r, s = self._row()
        if s is not None and s.second is not None:
            s.second = None
            self._fill()
            self._status_text()
            self.table.selectRow(r)

    def _refresh_buttons(self):
        have = any(s.use for s in self.sessions)
        two = any(s.second for s in self.sessions)
        _r, sel = self._row()
        self.btn_open.setEnabled(not self._busy and have)
        self.btn_add2.setEnabled(not self._busy and sel is not None)
        self.btn_rm2.setVisible(two)
        self.btn_rm2.setEnabled(not self._busy and sel is not None and sel.second is not None)
        for b in (self.btn_swap, self.btn_swap_one):
            b.setVisible(two)
        self.btn_swap.setEnabled(not self._busy)
        self.btn_swap_one.setEnabled(not self._busy and sel is not None
                                     and sel.second is not None)
        self.btn_cancel.setText("Stop" if self._busy else "Cancel")

    # ----- finish -----

    def _open(self):
        self.result = event.apply(self.sessions)
        self.accept()

    def _close(self):
        if self._busy:
            self._cancel = True
            self.status.setText("Stopping…")
            return
        self.reject()

    def closeEvent(self, e):
        if self._busy:
            self._cancel = True
            e.ignore()
            return
        super().closeEvent(e)
