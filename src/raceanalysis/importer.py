# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Import from GoPro card: copy recordings into a folder per track day and
pair each with its data file (racecore.ingest does the work)."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import threading
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from racecore import ingest
from raceoverlay import media

from . import fields
from .common import INK_DIM, LOSS
from raceoverlay.syncgui import _btn, _note, fmt_t, settings


def _pick_dir(parent, title: str, start: str) -> str | None:
    d = QtWidgets.QFileDialog.getExistingDirectory(
        parent, title, start if start and os.path.isdir(start) else str(Path.home()))
    return d or None


def _fmt_size(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    return f"{n / 1e6:.0f} MB" if n >= 1e7 else f"{n / 1e6:.1f} MB"


class ImportDialog(QtWidgets.QDialog):
    """Copy recordings off a GoPro card and pair each with its logger file.

    Work runs on a thread; a timer moves its results onto the UI. Every
    confident pairing is saved next to the video (the same .sync.json Race
    Overlay uses, so it opens the video already synced). On "Add to
    analysis", `self.result` = [(csv, video files, offset), ...] for the
    matched recordings.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Import from GoPro card")
        self.resize(640, 560)
        s = settings()
        self.result = None
        self._job: dict | None = None
        self._pairs: list = []
        lay = QtWidgets.QVBoxLayout(self)

        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(QtWidgets.QLabel("Card"))
        self.card_pick = QtWidgets.QComboBox()
        self.card_pick.currentIndexChanged.connect(self._scan)
        crow.addWidget(self.card_pick, 1)
        crow.addWidget(_btn("Refresh", self._find_cards))
        crow.addWidget(_btn("Folder…", self._choose_card,
                            "Use a folder instead (a copied card, a card reader "
                            "that didn't show up)"))
        lay.addLayout(crow)

        lay.addWidget(_note("Recordings (newest first). Chapters stay as "
                            "separate files so the GoPro sensor track is kept.",
                            small=True))
        self.lst = QtWidgets.QListWidget()
        self.lst.itemChanged.connect(lambda _it: self._clock_changed())
        lay.addWidget(self.lst, 1)

        # ---- camera clock: the HERO forgets the date with its battery out
        self._clips: list = []
        self._gps: dict | None = None       # background GPS-time lookup
        self._gps_fix: tuple | None = None  # (seconds, clip) from GPS
        self._exact: float | None = None    # GPS fix to the second, until edited
        cbox = QtWidgets.QGroupBox("Camera clock")
        cl = QtWidgets.QVBoxLayout(cbox)
        self.clock_msg = _note("", small=True)
        self.clock_msg.setWordWrap(True)
        cl.addWidget(self.clock_msg)
        row = QtWidgets.QHBoxLayout()
        self.fix_chk = QtWidgets.QCheckBox("Correct it: the first ticked recording started at")
        self.fix_chk.toggled.connect(lambda _on: self._clock_changed())
        row.addWidget(self.fix_chk)
        self.fix_when = fields.StartFields()
        self.fix_when.setToolTip("Date and time (24-hour, track time) the first ticked "
                                 "recording really started. Every ticked recording moves by "
                                 "the same amount, so their order and gaps stay the same.")
        self.fix_when.edited.connect(self._when_edited)
        row.addWidget(self.fix_when)
        self.btn_gps = _btn("Use GPS time", self._use_gps,
                            "Fill in the time from the GoPro's GPS (needs GPS on and a fix)")
        self.btn_gps.setEnabled(False)
        row.addWidget(self.btn_gps)
        row.addStretch(1)
        cl.addLayout(row)
        self.shift_lbl = _note("", small=True)
        cl.addWidget(self.shift_lbl)
        lay.addWidget(cbox)

        form = QtWidgets.QFormLayout()
        self.lib_edit = QtWidgets.QLineEdit(s.value(
            "import/library", str(Path.home() / "Videos" / "Track Days")))
        self.log_edit = QtWidgets.QLineEdit(s.value(
            "import/logs", s.value("dir/csv", "") or str(Path.home() / "Downloads")))
        for edit, title, key in ((self.lib_edit, "Copy videos to", "lib"),
                                 (self.log_edit, "Data files in", "logs")):
            row = QtWidgets.QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(_btn("…", lambda _=False, e=edit, t=title: self._browse(e, t)))
            form.addRow(title, row)
        self.lib_edit.editingFinished.connect(self._scan)
        lay.addLayout(form)
        lay.addWidget(_note("Videos go into a folder per day (e.g. 2026-09-27). "
                            "Nothing on the card is changed. Data files (APEX "
                            "Pro CSVs) are matched to each video by syncing "
                            "against them.", small=True))

        self.bar = QtWidgets.QProgressBar()
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.msg = _note("")
        lay.addWidget(self.msg)

        brow = QtWidgets.QHBoxLayout()
        self.btn_import = _btn("Import and match", self._start_import, big=True)
        self.btn_open = _btn("Add to analysis", self._open, big=True)
        self.btn_open.setVisible(False)
        self.btn_cancel = _btn("Cancel", self._cancel)
        self.btn_cancel.setVisible(False)
        close = _btn("Close", self.reject)
        brow.addWidget(self.btn_import, 1)
        brow.addWidget(self.btn_open, 1)
        brow.addWidget(self.btn_cancel)
        brow.addWidget(close)
        lay.addLayout(brow)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(100)
        self._find_cards()

    # ---------------------------------------------------------------- cards
    def _find_cards(self):
        self.card_pick.blockSignals(True)
        self.card_pick.clear()
        for c in ingest.find_cards():
            self.card_pick.addItem(c.label, c.root)
        last = settings().value("import/card", "")
        if last and os.path.isdir(last) and self.card_pick.findData(last) < 0 \
                and ingest.is_card(last):
            self.card_pick.addItem(Path(last).name, last)
        self.card_pick.blockSignals(False)
        if self.card_pick.count() == 0:
            self.lst.clear()
            self.msg.setText("No GoPro card found. Put the card in a reader "
                             "(it should open as a drive), then press Refresh, "
                             "or pick a folder.")
        else:
            self._scan()

    def _choose_card(self):
        d = _pick_dir(self, "Folder with GoPro files", settings().value("import/card", ""))
        if not d:
            return
        if self.card_pick.findData(d) < 0:
            self.card_pick.addItem(Path(d).name or d, d)
        self.card_pick.setCurrentIndex(self.card_pick.findData(d))
        self._scan()

    def _browse(self, edit, title):
        d = _pick_dir(self, title, edit.text())
        if d:
            edit.setText(d)
            self._scan()

    def _scan(self):
        root = self.card_pick.currentData()
        if not root or self._busy():
            return
        self.lst.clear()
        self.msg.setText("Reading the card…")
        lib = self.lib_edit.text().strip()
        self._run("scan", lambda job: ingest.scan_clips(root, lib or None))

    # ----------------------------------------------------------------- work
    def _busy(self) -> bool:
        return self._job is not None and not self._job.get("done")

    def _run(self, kind, fn):
        job = {"kind": kind, "done": False, "value": None, "error": None,
               "progress": None, "cancel": False}
        self._job = job

        def work():
            try:
                job["value"] = fn(job)
            except InterruptedError:
                job["error"] = "cancelled"
            except Exception as e:  # noqa: BLE001 - shown to the user
                job["error"] = str(e)
            job["done"] = True
        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        if self._job:
            self._job["cancel"] = True
            self.msg.setText("Stopping…")

    def _poll(self):
        g = self._gps
        if g is not None and g["done"]:
            self._gps = None
            self._gps_done(g["value"])
        job = self._job
        if job is None:
            return
        p = job.get("progress")
        if p and not job["done"]:
            done, total, text = p
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * done / max(total, 1)))
            self.msg.setText(text)
        if not job["done"]:
            return
        self._job = None
        self.bar.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_import.setEnabled(True)
        if job["error"]:
            self.msg.setText("Cancelled. Files copied so far are kept."
                             if job["error"] == "cancelled" else
                             f"Stopped: {job['error']}")
            return
        if job["kind"] == "scan":
            self._show_clips(job["value"])
        else:
            self._show_pairs(job["value"])

    def _show_clips(self, clips):
        self._clips = list(clips)
        self.lst.blockSignals(True)
        self.lst.clear()
        for c in clips:
            it = QtWidgets.QListWidgetItem("")
            it.setData(QtCore.Qt.UserRole, c)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Unchecked if c.imported else QtCore.Qt.Checked)
            it.setToolTip("\n".join(c.chapters))
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        n = sum(1 for c in clips if not c.imported)
        self.msg.setText(f"{len(clips)} recordings, {n} new." if clips else
                         "No GoPro recordings on this card.")
        self._gps_fix, self._exact = None, None
        self.btn_gps.setEnabled(False)
        wrong = ingest.clock_looks_wrong(clips)
        self.fix_chk.blockSignals(True)
        self.fix_chk.setChecked(wrong)
        self.fix_chk.blockSignals(False)
        first = self._first()
        if wrong and first is not None:
            # until GPS answers: today's date, keeping the camera's time of day
            today = dt.date.today()
            self.fix_when.setText(f"{today:%Y-%m-%d} {first.recorded:%H:%M}")
        elif first is not None:
            self.fix_when.setText(f"{first.recorded:%Y-%m-%d %H:%M}")
        else:
            self.fix_when.setText("")
        self._start_gps(wrong)
        self._clock_changed()

    # ------------------------------------------------------------ clock fix
    def _ticked(self) -> list:
        return [self.lst.item(i).data(QtCore.Qt.UserRole) for i in range(self.lst.count())
                if self.lst.item(i).checkState() == QtCore.Qt.Checked]

    def _first(self):
        """The oldest ticked recording with a camera time (the one the
        corrected start time is typed for)."""
        dated = [c for c in self._ticked() if c.recorded is not None]
        return min(dated, key=lambda c: c.recorded - dt.timedelta(seconds=c.clock_fix),
                   default=None)

    @staticmethod
    def _camera_time(c) -> dt.datetime:
        return c.recorded - dt.timedelta(seconds=c.clock_fix)

    def _start_gps(self, wrong: bool):
        """Look for the GoPro's GPS time in the background (only when the
        clock looks wrong: it reads part of each video)."""
        if not wrong:
            self.clock_msg.setText("The camera's dates look right. Tick \"Correct it\" "
                                   "if its clock was off anyway.")
            return
        clips = sorted([c for c in self._ticked() if c.recorded is not None],
                       key=self._camera_time)
        self.clock_msg.setText(
            f"<span style='color:{LOSS}'><b>The camera's clock looks wrong</b></span> "
            f"(it says {self._camera_time(clips[0]):%b %d, %Y}). "
            "GoPros reset to 2016 when the battery comes out. Looking for the GPS time…"
            if clips else "The camera's clock looks wrong.")
        job = {"done": False, "value": None}
        self._gps = job

        def work():
            try:
                job["value"] = ingest.gps_clock_fix(clips)
            except Exception:  # noqa: BLE001
                job["value"] = None
            job["done"] = True
        threading.Thread(target=work, daemon=True).start()

    def _gps_done(self, res):
        if res is None:
            self.clock_msg.setText(
                f"<span style='color:{LOSS}'><b>The camera's clock looks wrong</b></span> "
                "and there's no GPS time in these recordings (GPS off, or no fix yet). "
                "Type when the first ticked recording started.")
            return
        secs, clip = res
        self._gps_fix = res
        self.btn_gps.setEnabled(True)
        real = self._camera_time(clip) + dt.timedelta(seconds=secs)
        self.clock_msg.setText(
            f"<span style='color:{LOSS}'><b>The camera's clock looks wrong.</b></span> "
            f"Its GPS says {clip.name} started {real:%a %b %d, %Y  %H:%M} "
            "(this computer's time zone), so that's filled in below.")
        self._use_gps()

    def _use_gps(self):
        if self._gps_fix is None:
            return
        secs, _clip = self._gps_fix
        first = self._first()
        if first is None:
            return
        target = self._camera_time(first) + dt.timedelta(seconds=secs)
        self.fix_when.setText(target.strftime("%Y-%m-%d %H:%M"))
        self._exact = secs
        self.fix_chk.setChecked(True)
        self._clock_changed()

    def _when_edited(self):
        self._exact = None          # typed: to the minute
        if self.fix_when.text():
            self.fix_chk.setChecked(True)
        self._clock_changed()

    def _shift(self) -> float:
        """Seconds to add to every ticked recording's camera time."""
        if not self.fix_chk.isChecked():
            return 0.0
        if self._exact is not None:
            return self._exact
        first, when = self._first(), self.fix_when.text()
        if first is None or not when:
            return 0.0
        target = dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
        cam = self._camera_time(first).replace(second=0, microsecond=0)
        return (target - cam).total_seconds()

    def _clock_changed(self):
        """Apply the shift to the ticked recordings (unticked ones go back to
        the camera's time) and show the corrected dates."""
        secs = self._shift()
        ticked = {id(c) for c in self._ticked()}
        for c in self._clips:
            ingest.fix_clock([c], secs if id(c) in ticked else 0.0)
        self.lst.blockSignals(True)
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            c = it.data(QtCore.Qt.UserRole)
            if c.recorded is None:
                when = "date unknown"
            elif c.clock_fix:
                when = (f"{c.recorded:%a %b %d  %H:%M}  "
                        f"(camera said {self._camera_time(c):%b %d %Y})")
            else:
                when = c.recorded.strftime("%a %b %d  %H:%M")
            dur = fmt_t(c.duration).split(".")[0] if c.duration else "?"
            parts = f"{len(c.chapters)} files" if len(c.chapters) > 1 else "1 file"
            tag = "   ✓ already imported" if c.imported else ""
            it.setText(f"{c.name}   {when}   {dur}   {parts}, {_fmt_size(c.size)}{tag}")
        self.lst.blockSignals(False)
        if self.fix_chk.isChecked() and not self.fix_when.complete():
            self.shift_lbl.setText("Finish the date and time to correct the clock.")
        elif secs:
            d = abs(secs)
            days, rest = divmod(d, 86400)
            h, rest = divmod(rest, 3600)
            m = rest / 60
            amount = (f"{int(days):,} days " if days else "") + f"{int(h)} h {m:.0f} min"
            self.shift_lbl.setText(
                f"Every ticked recording moves {'forward' if secs > 0 else 'back'} {amount}. "
                "They're filed under the corrected day, and their files get the corrected "
                "time. The card itself isn't changed.")
        else:
            self.shift_lbl.setText("")
        self.shift_lbl.setStyleSheet(f"color:{INK_DIM};")

    def _start_import(self):
        if self._busy():
            return
        clips = self._ticked()
        if self.fix_chk.isChecked() and not self.fix_when.text():
            self.msg.setText("Fill in when the first recording started, or untick "
                             "\"Correct it\".")
            return
        self._clock_changed()           # the corrected times go with the copy
        lib, logdir = self.lib_edit.text().strip(), self.log_edit.text().strip()
        if not clips:
            self.msg.setText("Tick at least one recording.")
            return
        if not lib:
            self.msg.setText("Pick where to copy the videos.")
            return
        need = sum(c.size for c in clips)
        try:
            Path(lib).mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(lib).free
        except OSError as e:
            self.msg.setText(f"Can't use {lib}: {e}")
            return
        if need > free:
            self.msg.setText(f"Not enough space in {lib}: need {_fmt_size(need)}, "
                             f"{_fmt_size(free)} free.")
            return
        s = settings()
        s.setValue("import/library", lib)
        s.setValue("import/logs", logdir)
        s.setValue("import/card", self.card_pick.currentData() or "")
        self.btn_import.setEnabled(False)
        self.btn_cancel.setVisible(True)
        self.btn_open.setVisible(False)
        self.bar.setVisible(True)

        def work(job):
            def prog(done, total, name):
                job["progress"] = (done, total, f"Copying {name}  "
                                   f"({_fmt_size(done)} of {_fmt_size(total)})")
            stop = lambda: job["cancel"]  # noqa: E731
            copied = ingest.import_clips(clips, lib, progress=prog, cancelled=stop)
            logs = ingest.find_logs(logdir) if logdir else []
            out = []
            for i, (clip, paths) in enumerate(zip(clips, copied)):
                job["progress"] = (i, len(clips),
                                   f"Matching {clip.name} to a data file…")
                info = media.probe(paths)
                parts = [(p.path, p.start) for p in info.parts]
                pr = ingest.pair(clip, parts, logs, cancelled=stop)
                if pr.log and pr.confident:
                    ingest.save_pairing(paths, pr.log.path, pr.offset)
                out.append((paths, pr))
            return out
        self._run("import", work)

    def _show_pairs(self, results):
        self._pairs = results
        self.lst.clear()
        for paths, pr in results:
            if pr.log:
                txt = (f"{pr.clip.name}  →  {Path(pr.log.path).name}\n"
                       f"      match {pr.score:.0%}, offset {pr.offset:+.2f} s")
            elif pr.score is not None:
                txt = (f"{pr.clip.name}  →  no confident match\n"
                       f"      best {pr.score:.0%}; open a data file and sync by hand")
            else:
                txt = f"{pr.clip.name}  →  {pr.note}"
            it = QtWidgets.QListWidgetItem(txt)
            it.setData(QtCore.Qt.UserRole, (paths, pr))
            it.setToolTip("\n".join(paths))
            self.lst.addItem(it)
        if results:
            self.lst.setCurrentRow(0)
        ok = sum(1 for _, pr in results if pr.log)
        self.msg.setText(f"Imported {len(results)} recording(s); matched {ok} "
                         "to a data file and saved the sync (Race Overlay opens "
                         "them synced).")
        self.btn_open.setVisible(ok > 0)
        self.btn_import.setVisible(False)

    def _open(self):
        """Every matched recording; one video per data file (the best match
        when a session was recorded in more than one clip)."""
        best: dict[str, tuple] = {}
        for paths, pr in self._pairs:
            if pr.log and pr.confident:
                cur = best.get(pr.log.path)
                if cur is None or pr.score > cur[2]:
                    best[pr.log.path] = (paths, pr.offset, pr.score)
        self.result = [(csv, v[0], v[1]) for csv, v in best.items()]
        self.accept()
