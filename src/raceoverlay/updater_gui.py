# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""The update window (both apps) and the quiet once-a-day check.

    UpdateDialog(parent, which)   "Check for updates…": what's new, install,
                                  skip, roll back, or pick another version
    auto_check(window, which)     at most once a day, in the background;
                                  opens the dialog only when there's a
                                  newer version you haven't skipped

which is "overlay" or "analysis": the app that comes back after an update.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from racecore import update

KIND_TEXT = {"appimage": "AppImage", "windows": "Windows installer",
             "source": "running from source"}


class _Bridge(QtCore.QObject):
    """Carries results from worker threads to the GUI thread."""
    checked = QtCore.Signal(object, object)          # releases | None, error | None
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal(object, object)          # result, error


def _human(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{n / 1e3:.0f} kB"


class UpdateDialog(QtWidgets.QDialog):
    def __init__(self, parent, which: str = "overlay", releases=None):
        super().__init__(parent)
        self.which = which
        self.kind = update.install_kind()
        self.current = update.current_version()
        self.rels: list[update.Release] = releases or []
        self.target: update.Release | None = None
        self._busy = False
        self._cancel = False
        self.bridge = _Bridge()
        self.bridge.checked.connect(self._on_checked)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.finished.connect(self._on_finished)
        self._after = None

        self.setWindowTitle("Updates")
        lay = QtWidgets.QVBoxLayout(self)
        self.head = QtWidgets.QLabel()
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.notes = QtWidgets.QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumHeight(170)
        self.notes.setVisible(False)
        lay.addWidget(self.notes, 1)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        row = QtWidgets.QHBoxLayout()
        self.btn_install = QtWidgets.QPushButton("Download and install")
        self.btn_install.clicked.connect(lambda: self._install(self.target))
        self.btn_skip = QtWidgets.QPushButton("Skip this version")
        self.btn_skip.clicked.connect(self._skip)
        self.btn_check = QtWidgets.QPushButton("Check again")
        self.btn_check.clicked.connect(self.check)
        self.btn_close = QtWidgets.QPushButton("Later")
        self.btn_close.clicked.connect(self._close)
        for b in (self.btn_install, self.btn_skip, self.btn_check):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        lay.addLayout(row)

        box = QtWidgets.QGroupBox("If a new version breaks something")
        bl = QtWidgets.QVBoxLayout(box)
        rrow = QtWidgets.QHBoxLayout()
        self.btn_rollback = QtWidgets.QPushButton("Roll back")
        self.btn_rollback.clicked.connect(self._rollback)
        self.rb_lbl = QtWidgets.QLabel("")
        self.rb_lbl.setWordWrap(True)
        rrow.addWidget(self.btn_rollback)
        rrow.addWidget(self.rb_lbl, 1)
        bl.addLayout(rrow)
        orow = QtWidgets.QHBoxLayout()
        orow.addWidget(QtWidgets.QLabel("Or install"))
        self.other = QtWidgets.QComboBox()
        orow.addWidget(self.other, 1)
        self.btn_other = QtWidgets.QPushButton("Install this version")
        self.btn_other.clicked.connect(
            lambda: self._install(self.other.currentData()))
        orow.addWidget(self.btn_other)
        bl.addLayout(orow)
        lay.addWidget(box)

        self.auto = QtWidgets.QCheckBox("Check for updates once a day")
        self.auto.setChecked(update.auto_enabled())
        self.auto.toggled.connect(lambda on: update.save_state(auto=bool(on)))
        lay.addWidget(self.auto)
        self.resize(560, 520)
        self._refresh()
        if not self.rels:
            QtCore.QTimer.singleShot(0, self.check)
        else:
            self._on_checked(self.rels, None)

    # ----- state -----

    def _refresh(self):
        how = KIND_TEXT.get(self.kind, self.kind)
        from raceoverlay import build_label
        lab = build_label()
        test = f"<br><small>Test build: {lab}</small>" if lab else ""
        self.head.setText(f"<b>Race Overlay {self.current}</b> &nbsp;<small>({how})</small>"
                          + test)
        prev = update.previous()
        can_install = self.kind in ("appimage", "windows") and not self._busy
        if prev:
            self.rb_lbl.setText(f"Puts back {prev['version']}, kept on this computer "
                                "(works without internet).")
            self.btn_rollback.setText(f"Roll back to {prev['version']}")
        else:
            self.rb_lbl.setText("No earlier version kept yet. One is kept each time you "
                                "update from here.")
            self.btn_rollback.setText("Roll back")
        self.btn_rollback.setEnabled(bool(prev) and can_install
                                     and prev.get("kind") == self.kind)
        self.btn_install.setVisible(self.target is not None)
        self.btn_skip.setVisible(self.target is not None)
        self.btn_install.setEnabled(can_install)
        self.btn_other.setEnabled(can_install and self.other.count() > 0)
        self.other.setEnabled(not self._busy)
        self.btn_check.setEnabled(not self._busy)
        self.btn_close.setText("Cancel" if self._busy else
                               ("Later" if self.target is not None else "Close"))

    def check(self):
        if self._busy:
            return
        self.status.setText("Checking GitHub for new versions…")
        self.btn_check.setEnabled(False)

        def work():
            try:
                rels = update.releases()
                update.mark_checked()
                self.bridge.checked.emit(rels, None)
            except update.UpdateError as e:
                self.bridge.checked.emit(None, e)
        threading.Thread(target=work, daemon=True).start()

    def _on_checked(self, rels, err):
        if err is not None:
            self.status.setText(str(err) + " You can still roll back below.")
            self._refresh()
            return
        self.rels = rels
        kind = self.kind if self.kind in ("appimage", "windows") else None
        self.target = update.newer(rels, self.current, kind)
        self.other.clear()
        for r in rels:
            if r.version != self.current and (kind is None or kind in r.assets):
                self.other.addItem(f"{r.version}  ({r.published})", r)
        if self.target is not None:
            r = self.target
            self.status.setText(f"<b>Version {r.version}</b> is out "
                                f"({r.published}). You have {self.current}.")
            self.notes.setMarkdown(r.notes or "_No release notes._")
            self.notes.setVisible(True)
        else:
            self.status.setText(f"You have the newest version ({self.current}).")
            self.notes.setVisible(False)
        if self.kind == "source":
            self.status.setText(self.status.text() + "<br><small>Running from source: "
                                f"update with git, or get a build from <a href='"
                                f"{update.RELEASES_PAGE}'>the releases page</a>.</small>")
            self.status.setOpenExternalLinks(True)
        self._refresh()

    def _skip(self):
        if self.target is not None:
            update.save_state(skip=self.target.version)
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

    # ----- install / roll back -----

    def _start(self, text: str, job, after):
        self._busy, self._cancel = True, False
        self._after = after
        self.status.setText(text)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self._refresh()

        def work():
            try:
                self.bridge.finished.emit(job(), None)
            except Exception as e:  # noqa: BLE001 - shown in the dialog
                self.bridge.finished.emit(None, e)
        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, done: int, total: int):
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
            self.bar.setFormat(f"{_human(done)} of {_human(total)}")

    def _on_finished(self, result, err):
        self._busy = False
        self.bar.setVisible(False)
        self._refresh()
        if err is not None:
            self.status.setText(f"<b>Didn't work:</b> {err}")
            return
        if self._after:
            self._after(result)

    def _progress(self, done, total):
        self.bridge.progress.emit(done, total)

    def _install(self, rel):
        if rel is None:
            return
        if self.kind == "appimage":
            self._install_appimage(rel)
        elif self.kind == "windows":
            self._install_windows(rel)

    def _install_appimage(self, rel):
        target = update.appimage_target()
        asset = rel.assets.get("appimage")
        if target is None or asset is None:
            return
        cur = self.current
        if update.appimage_writable(target):
            tmp = target.with_name(f".{target.name}.update")

            def job():
                update.download(asset, tmp, self._progress, lambda: self._cancel)
                update.apply_appimage(tmp, target, cur)
                return rel.version
            self._start(f"Downloading {rel.version}…", job, self._done_appimage)
        else:
            dl = Path(QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.DownloadLocation) or Path.home()) / asset.name

            def job():
                update.download(asset, dl, self._progress, lambda: self._cancel)
                os.chmod(dl, 0o755)
                return dl
            self._start(f"Downloading {rel.version}…", job, lambda p: self.status.setText(
                f"This copy's folder ({target.parent}) can't be written to, so the new "
                f"version was saved as <b>{p}</b>. If an app manager (like Shelly) "
                "installed it, update it there instead."))

    def _done_appimage(self, version):
        self._restart_prompt(f"Updated to {version}. The version you had is kept, so you "
                             "can roll back.")

    def _restart_prompt(self, text: str):
        self.status.setText(text)
        r = QtWidgets.QMessageBox.question(
            self, "Restart now?", text + "\n\nRestart now to use it? (If the other app "
            "is open too, restart it as well.)",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes)
        self._refresh()
        if r == QtWidgets.QMessageBox.Yes:
            update.restart_appimage(update.appimage_target(), self.which == "analysis")
            QtWidgets.QApplication.quit()

    def _install_windows(self, rel):
        asset = rel.assets.get("windows")
        if asset is None:
            return
        cur = self.current
        cur_rel = next((r for r in self.rels if r.version == cur), None)
        folder = update.windows_download_dir()

        def job():
            new = update.download(asset, folder / asset.name, self._progress,
                                  lambda: self._cancel)
            # keep this version's installer for an offline rollback
            prev = update.previous()
            if cur_rel is not None and "windows" in cur_rel.assets and not (
                    prev and prev.get("version") == cur and prev.get("kind") == "windows"):
                a = cur_rel.assets["windows"]
                old = update.download(a, folder / a.name, self._progress, lambda: self._cancel)
                update.keep_windows_installer(old, cur)
            return new
        self._start(f"Downloading {rel.version}…", job, self._run_setup)

    def _run_setup(self, setup):
        self.status.setText("Installing: the app closes and comes back when it's done.")
        QtWidgets.QApplication.processEvents()
        update.run_windows_setup(Path(setup), self.which)
        QtWidgets.QApplication.quit()

    def _rollback(self):
        prev = update.previous()
        if not prev:
            return
        r = QtWidgets.QMessageBox.question(
            self, "Roll back?", f"Go back to {prev['version']}? {self.current} is kept, so "
            "you can return to it the same way.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if r != QtWidgets.QMessageBox.Yes:
            return
        cur = self.current
        if self.kind == "appimage":
            target = update.appimage_target()
            self._start(f"Putting back {prev['version']}…",
                        lambda: update.rollback_appimage(target, cur),
                        lambda v: self._restart_prompt(f"Rolled back to {v}."))
        elif self.kind == "windows":
            folder = update.windows_download_dir()

            def job():
                return update.rollback_windows(folder, cur)
            self._start(f"Putting back {prev['version']}…", job, self._run_setup)


# ----------------------------------------------------------- daily check --

_running: dict = {}


def auto_check(window, which: str = "overlay"):
    """Once a day, in the background: if there's a newer version you haven't
    skipped, show the update window. Both apps share the timestamp, so
    only one of them asks."""
    if not update.due() or _running.get("on"):
        return
    update.mark_checked()                # claim today's check before the slow part
    _running["on"] = True
    bridge = _Bridge(window)

    def done(rels, err):
        _running["on"] = False
        if err is not None or not rels:
            return
        kind = update.install_kind()
        rel = update.newer(rels, update.current_version(),
                           kind if kind in ("appimage", "windows") else None)
        if rel is None or update.load_state().get("skip") == rel.version:
            return
        dlg = UpdateDialog(window, which, releases=rels)
        dlg.setModal(False)
        dlg.show()
        _running["dlg"] = dlg
    bridge.checked.connect(done)
    _running["bridge"] = bridge

    def work():
        try:
            bridge.checked.emit(update.releases(), None)
        except update.UpdateError as e:
            bridge.checked.emit(None, e)
    threading.Thread(target=work, daemon=True).start()
