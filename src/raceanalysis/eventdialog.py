# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""File → Open event folder…: tie a hand-organised event together.

Scans the folder (racecore.event), syncs every GoPro recording to the data
file it fits, and shows the result for review: one row per session with
its front and rear cameras. Open saves the pairings next to the videos and
opens the sessions; next time Open track day opens the same folder at once.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from racecore import event

COLS = ["", "Session", "Data file", "Front camera", "Rear camera", "Match"]


class _Bridge(QtCore.QObject):
    progress = QtCore.Signal(str)
    done = QtCore.Signal(object, object, object)       # scan, sessions, error


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
        self.btn_swap = QtWidgets.QPushButton("Swap front and rear for every session")
        self.btn_swap.setToolTip("The cameras are told apart by which way they face; "
                                 "which way is the front is a guess unless a file or "
                                 "folder name says so")
        self.btn_swap.clicked.connect(self._swap_all)
        self.btn_swap_one = QtWidgets.QPushButton("Swap for this session")
        self.btn_swap_one.clicked.connect(self._swap_one)
        row.addWidget(self.btn_swap)
        row.addWidget(self.btn_swap_one)
        row.addStretch(1)
        lay.addLayout(row)
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
        n_vid = sum(1 for s in sessions for c in (s.front, s.rear) if c)
        if not sessions:
            self.status.setText("No data files with g data in this folder.")
        else:
            self.status.setText(
                f"{len(sessions)} session(s), {n_vid} camera recording(s) matched. Check "
                "the front and rear cameras, untick anything you don't want, then Open. "
                "Double-click a name to change it.")
        self._fill()
        self._fill_left_out()
        self._refresh_buttons()

    # ----- table -----

    def _fill(self):
        t = self.table
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
                                   (3, *self._cam(s.front)), (4, *self._cam(s.rear))):
                it = QtWidgets.QTableWidgetItem(text)
                it.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
                it.setToolTip(tip)
                t.setItem(r, col, it)
            scores = [c.score for c in (s.front, s.rear) if c]
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
        used = {c.video.parts[0] for s in self.sessions for c in (s.front, s.rear) if c}
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
                             "(a third camera, or another copy); not used")
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

    def _swap_one(self):
        r = self.table.currentRow()
        if 0 <= r < len(self.sessions):
            s = self.sessions[r]
            if s.rear is not None:
                s.front, s.rear = s.rear, s.front
                self._fill()
                self.table.selectRow(r)

    def _refresh_buttons(self):
        have = any(s.use for s in self.sessions)
        self.btn_open.setEnabled(not self._busy and have)
        self.btn_swap.setEnabled(not self._busy and any(s.rear for s in self.sessions))
        self.btn_swap_one.setEnabled(not self._busy and any(s.rear for s in self.sessions))
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
