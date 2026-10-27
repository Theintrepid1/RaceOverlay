"""Race Overlay: line telemetry up with video, style the overlay, render.

One window. Video (mpv rendering straight into a Qt OpenGL widget) with a
live overlay preview, player controls and a speed trace on the left; tabs on
the right for Sync, Look and Setup.

    python -m raceoverlay.syncgui [VIDEO] [--csv apex.csv]
"""
from __future__ import annotations

import argparse
import ctypes
import html
import json
import locale
import os
import re
import shutil
import sys
import traceback
from pathlib import Path

import numpy as np

from . import gauges, telemetry  # noqa: E402  (Qt imported in main/below)

KMH_PER_MPH = 1.60934
TRACE_WINDOW = 60.0  # seconds of speed trace shown around the playhead
PKG_ROOT = str(Path(__file__).resolve().parents[1])  # dir containing raceoverlay/
NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs",
              "fuse.kio-fuse", "fuse.rclone", "9p", "davfs", "fuse.gvfsd-fuse"}


def _prefer_portal_dialogs():
    """Use the desktop's own file dialog (KDE/GNOME, via xdg-desktop-portal).

    pip-installed Qt can't load KDE's theme plugin, so without this you get
    Qt's bare dialog with no Network places. Must run before QApplication.
    """
    if os.environ.get("QT_QPA_PLATFORMTHEME") or not os.environ.get("XDG_CURRENT_DESKTOP"):
        return
    try:
        import PySide6
        plug = Path(PySide6.__file__).parent / "Qt" / "plugins" / "platformthemes"
        if any(plug.glob("libqxdgdesktopportal*")):
            os.environ["QT_QPA_PLATFORMTHEME"] = "xdgdesktopportal"
    except Exception:
        pass


_prefer_portal_dialogs()
# mpv can only hand VAAPI-decoded frames to OpenGL through EGL. Wayland
# sessions use EGL already; make X11 sessions use it too (Qt defaults to GLX).
os.environ.setdefault("QT_XCB_GL_INTEGRATION", "xcb_egl")

from PySide6 import QtCore, QtGui, QtOpenGLWidgets, QtWidgets  # noqa: E402
import pyqtgraph as pg  # noqa: E402

import signal  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402

from . import engine, media  # noqa: E402
from .render import OverlayContext, draw_overlay  # noqa: E402


# ---------------------------------------------------------------- storage ---

def settings() -> QtCore.QSettings:
    return QtCore.QSettings("raceoverlay", "raceoverlay")


def sync_path(video: str) -> Path:
    p = Path(video)
    return p.with_name(p.name + ".sync.json")


def load_sync(video: str) -> dict:
    p = sync_path(video)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_sync(video: str, data: dict) -> Path | None:
    p = sync_path(video)
    try:
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None  # read-only share etc.: keep working, just don't persist
    return p


def network_mounts() -> list[str]:
    """Mounted network shares (SMB/NFS/sshfs/...) plus removable media."""
    out = []
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[2] in NETWORK_FS:
                    out.append(parts[1].replace("\\040", " "))
    except OSError:
        pass
    user = os.environ.get("USER", "")
    for extra in (f"/run/media/{user}", f"/run/user/{os.getuid()}/gvfs"):
        if os.path.isdir(extra):
            out.append(extra)
    return sorted(set(out))


def pick_file(parent, title: str, key: str, filt: str) -> str | None:
    """File picker that starts where you last were and lists network mounts."""
    s = settings()
    start = s.value(f"dir/{key}", "") or s.value("dir/video", "") or str(Path.home())
    dlg = QtWidgets.QFileDialog(parent, title, start, filt)
    dlg.setFileMode(QtWidgets.QFileDialog.ExistingFile)
    # Sidebar shortcuts (used when the desktop's own dialog isn't available).
    urls = [QtCore.QUrl.fromLocalFile(p) for p in
            [str(Path.home()), start] + network_mounts() if p and os.path.isdir(p)]
    dlg.setSidebarUrls(dlg.sidebarUrls() + urls)
    if not dlg.exec():
        return None
    files = dlg.selectedFiles()
    if not files:
        return None
    s.setValue(f"dir/{key}", str(Path(files[0]).parent))
    return files[0]


# Preview decoding modes -> mpv hwdec values. Vulkan video decoding is left
# out of Auto/Direct: it corrupts frames on some NVIDIA drivers.
PREVIEW_DECODE = {
    "copy": ("Auto: GPU decode, safe copy (recommended)", "nvdec-copy,vaapi-copy"),
    "direct": ("Direct GPU (fastest, can glitch on some drivers)", "nvdec,vaapi"),
    "off": ("Off: CPU only", "no"),
    "t_nvdec_copy": ("Test: NVDEC, copy", "nvdec-copy"),
    "t_nvdec": ("Test: NVDEC, direct", "nvdec"),
    "t_vaapi_copy": ("Test: VA-API, copy", "vaapi-copy"),
    "t_vaapi": ("Test: VA-API, direct", "vaapi"),
    "t_vulkan_copy": ("Test: Vulkan, copy", "vulkan-copy"),
}
DEFAULT_PREVIEW_DECODE = "copy"


def preview_hwdec() -> str:
    key = settings().value("preview/decode", DEFAULT_PREVIEW_DECODE)
    return PREVIEW_DECODE.get(key, PREVIEW_DECODE[DEFAULT_PREVIEW_DECODE])[1]


PREVIEW_MODES = {
    "native": "Native window (recommended)",
    "embedded": "Embedded (older; try if Native misbehaves)",
}


def preview_mode() -> str:
    """'native': mpv draws into its own X11 window inside ours, exactly like
    the standalone player (newer gpu-next renderer, its own GPU interop).
    'embedded': mpv renders into Qt's OpenGL widget via the render API; on
    some NVIDIA setups that path stripes every GPU-decoded frame.
    Native needs X11 (XWayland is fine on Wayland desktops)."""
    mode = os.environ.get("RACEOVERLAY_PREVIEW") or settings().value(
        "preview/mode", "native")
    if mode == "native" and not os.environ.get("DISPLAY"):
        return "embedded"  # pure Wayland without XWayland
    return mode if mode in PREVIEW_MODES else "native"


def engine_args(job_file: str) -> list[str]:
    """Arguments to start the render engine with sys.executable."""
    if getattr(sys, "frozen", False):  # packaged build: one executable
        return ["--engine", job_file]
    return ["-m", "raceoverlay.engine", job_file]


# ------------------------------------------------- app menu (AppImage only) --

def appimage_path() -> str | None:
    p = os.environ.get("APPIMAGE")
    return p if p and os.path.exists(p) else None


def _desktop_file() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "applications" / "raceoverlay.desktop"


def menu_entry_target() -> str | None:
    """The AppImage path the app-menu entry launches, or None if none."""
    f = _desktop_file()
    if not f.exists():
        return None
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("Exec="):
            return line[5:].rsplit(" %", 1)[0].strip().strip('"')
    return None


def add_menu_entry(appimage: str):
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    icon_dir = base / "icons" / "hicolor" / "scalable" / "apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    src_icon = Path(__file__).with_name("icon.svg")
    if src_icon.exists():
        shutil.copyfile(src_icon, icon_dir / "raceoverlay.svg")
    f = _desktop_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "[Desktop Entry]\nType=Application\nName=Race Overlay\n"
        "GenericName=Telemetry Video Overlay\n"
        "Comment=Sync racing telemetry to video and render gauge overlays\n"
        f'Exec="{appimage}" %f\nIcon=raceoverlay\nTerminal=false\n'
        "Categories=AudioVideo;Video;\n"
        "MimeType=video/mp4;video/quicktime;video/x-matroska;\n"
        "StartupWMClass=raceoverlay\n", encoding="utf-8")
    for cmd in (["update-desktop-database", str(f.parent)],
                ["gtk-update-icon-cache", "-q", str(base / "icons" / "hicolor")]):
        try:
            import subprocess
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:
            pass


def remove_menu_entry():
    try:
        _desktop_file().unlink()
    except OSError:
        pass


def fmt_t(t: float) -> str:
    sign = "-" if t < 0 else ""
    m, s = divmod(abs(t), 60)
    return f"{sign}{int(m)}:{s:05.2f}"


# ------------------------------------------------------------ video widget --

class MpvWidget(QtOpenGLWidgets.QOpenGLWidget):
    """mpv drawing into this widget via the libmpv render API."""

    frame_ready = QtCore.Signal()
    pressed = QtCore.Signal(float, float, object)   # device px, modifiers
    dragged = QtCore.Signal(float, float)
    released = QtCore.Signal(float, float)
    wheeled = QtCore.Signal(float, float, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        import mpv
        self._mpv = mpv
        extra = {}
        lvl = os.environ.get("RACEOVERLAY_MPV_LOG")  # e.g. "v" or "warn"
        if lvl:
            extra = {"loglevel": lvl, "log_handler": lambda l, c, m:
                     sys.stderr.write(f"[mpv {l}] {c}: {m}")}
        self.player = mpv.MPV(
            vo="libmpv", hwdec=preview_hwdec(), keep_open="yes", hr_seek="yes",
            pause=True, input_default_bindings=False, input_vo_keyboard=False,
            **extra)
        try:  # only exists when libmpv was built with Lua (system mpv)
            self.player["osc"] = False
        except Exception:
            pass
        self.ctx = None
        self._pending: str | None = None
        self.frame_ready.connect(self.update, QtCore.Qt.QueuedConnection)
        self.setMinimumSize(480, 270)

    def initializeGL(self):
        def get_proc_address(_ctx, name):
            glctx = QtGui.QOpenGLContext.currentContext()
            if glctx is None:
                return 0
            addr = glctx.getProcAddress(name)
            return int(addr) if addr else 0

        self._gpa = self._mpv.MpvGlGetProcAddressFn(get_proc_address)
        self.ctx = self._mpv.MpvRenderContext(
            self.player, "opengl",
            opengl_init_params={"get_proc_address": self._gpa})
        self.ctx.update_cb = self.frame_ready.emit
        if self._pending:
            self.player.play(self._pending)
            self._pending = None

    def load(self, path: str):
        """Open a file. Deferred until the GL context exists, because mpv's
        libmpv video output fails (and drops the file) without one."""
        if self.ctx is None:
            self._pending = path
        else:
            self.player.play(path)

    def paintGL(self):
        if self.ctx is None:
            return
        r = self.devicePixelRatioF()
        self.ctx.render(flip_y=True, opengl_fbo={
            "w": int(self.width() * r), "h": int(self.height() * r),
            "fbo": self.defaultFramebufferObject()})

    def _dev(self, ev):
        p = ev.position()
        r = self.devicePixelRatioF()
        return p.x() * r, p.y() * r

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            x, y = self._dev(ev)
            self.pressed.emit(x, y, ev.modifiers())

    def mouseMoveEvent(self, ev):
        if ev.buttons() & QtCore.Qt.LeftButton:
            self.dragged.emit(*self._dev(ev))

    def mouseReleaseEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self.released.emit(*self._dev(ev))

    def wheelEvent(self, ev):
        x, y = self._dev(ev)
        self.wheeled.emit(x, y, ev.angleDelta().y())

    def shutdown(self):
        if self.ctx is not None:
            self.makeCurrent()
            self.ctx.free()
            self.ctx = None
            self.doneCurrent()
        self.player.terminate()


class MpvNativeWidget(QtWidgets.QWidget):
    """mpv drawing into its own native (X11) child window, like standalone
    mpv. Mouse and keyboard events fall through to Qt (mpv is told to ignore
    them), so gauge dragging and shortcuts work the same as embedded."""

    pressed = QtCore.Signal(float, float, object)
    dragged = QtCore.Signal(float, float)
    released = QtCore.Signal(float, float)
    wheeled = QtCore.Signal(float, float, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        import mpv
        self.setAttribute(QtCore.Qt.WA_NativeWindow)
        self.setAttribute(QtCore.Qt.WA_DontCreateNativeAncestors)
        self.setAutoFillBackground(True)  # black until mpv's window covers it
        pal = self.palette()
        pal.setColor(QtGui.QPalette.Window, QtCore.Qt.black)
        self.setPalette(pal)
        extra = {}
        lvl = os.environ.get("RACEOVERLAY_MPV_LOG")
        if lvl:
            extra = {"loglevel": lvl, "log_handler": lambda l, c, m:
                     sys.stderr.write(f"[mpv {l}] {c}: {m}")}
        if os.environ.get("RACEOVERLAY_GPU_SW"):  # headless testing only
            extra["gpu_sw"] = True
        self.player = mpv.MPV(
            # X11 only: on a Wayland desktop mpv would otherwise pick a
            # Wayland context, ignore the window id and open its own window.
            vo="gpu-next,gpu", gpu_context="x11egl", hwdec=preview_hwdec(),
            keep_open="yes",
            hr_seek="yes", pause=True, input_default_bindings=False,
            input_vo_keyboard=False, input_cursor=False, cursor_autohide="no",
            **extra)
        try:
            self.player["osc"] = False
        except Exception:
            pass
        self._wid_set = False
        self.setMinimumSize(480, 270)

    def _attach(self):
        if not self._wid_set:
            self.player["wid"] = int(self.winId())
            self._wid_set = True

    def load(self, path: str):
        self._attach()
        self.player.play(path)

    def paintEvent(self, ev):  # nothing playing yet: plain black, not junk
        QtGui.QPainter(self).fillRect(ev.rect(), QtCore.Qt.black)

    def _dev(self, ev):
        p = ev.position()
        r = self.devicePixelRatioF()
        return p.x() * r, p.y() * r

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            x, y = self._dev(ev)
            self.pressed.emit(x, y, ev.modifiers())

    def mouseMoveEvent(self, ev):
        if ev.buttons() & QtCore.Qt.LeftButton:
            self.dragged.emit(*self._dev(ev))

    def mouseReleaseEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self.released.emit(*self._dev(ev))

    def wheelEvent(self, ev):
        x, y = self._dev(ev)
        self.wheeled.emit(x, y, ev.angleDelta().y())

    def shutdown(self):
        self.player.terminate()


class MpvOverlay:
    """Push a PIL RGBA image to mpv's OSD as a premultiplied BGRA overlay."""

    def __init__(self, player):
        self.player = player
        self.oid = player.allocate_overlay_id()
        self._buf = None
        self._shape = None
        self.visible = False

    def show(self, img, x: int, y: int):
        a = np.asarray(img, dtype=np.uint16)
        alpha = a[..., 3:4]
        rgb = (a[..., :3] * alpha + 127) // 255  # mpv wants premultiplied
        bgra = np.concatenate([rgb[..., ::-1], alpha], axis=2).astype(np.uint8)
        h, w = bgra.shape[:2]
        if self._shape != (h, w):
            self._buf = ctypes.create_string_buffer(w * h * 4)
            self._shape = (h, w)
        ctypes.memmove(self._buf, bgra.tobytes(), w * h * 4)
        self.player.overlay_add(self.oid, int(x), int(y),
                                "&" + str(ctypes.addressof(self._buf)),
                                0, "bgra", w, h, w * 4)
        self.visible = True

    def hide(self):
        if self.visible:
            try:
                self.player.overlay_remove(self.oid)
            except Exception:
                pass
            self.visible = False


# ----------------------------------------------------------- UI helpers -----

def _btn(text, slot, tip="", big=False):
    b = QtWidgets.QPushButton(text)
    b.setFocusPolicy(QtCore.Qt.NoFocus)
    b.clicked.connect(slot)
    if tip:
        b.setToolTip(tip)
    if big:
        b.setMinimumHeight(38)
        f = b.font()
        f.setBold(True)
        b.setFont(f)
    return b


def _group(title: str):
    box = QtWidgets.QGroupBox(title)
    f = box.font()
    f.setBold(True)
    box.setFont(f)
    lay = QtWidgets.QVBoxLayout(box)
    return box, lay


def _note(text: str, small=False) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(f"<small>{text}</small>" if small else text)
    lbl.setWordWrap(True)
    f = lbl.font()
    f.setBold(False)
    lbl.setFont(f)
    return lbl


def _check(text, checked, slot, tip=""):
    c = QtWidgets.QCheckBox(text)
    c.setChecked(checked)
    c.setFocusPolicy(QtCore.Qt.NoFocus)
    c.toggled.connect(slot)
    if tip:
        c.setToolTip(tip)
    f = c.font()
    f.setBold(False)
    c.setFont(f)
    return c


def _tab_page() -> tuple[QtWidgets.QScrollArea, QtWidgets.QVBoxLayout]:
    page = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(page)
    scroll = QtWidgets.QScrollArea()
    scroll.setWidget(page)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
    return scroll, lay


# ------------------------------------------------------------- window -------

def src_label(tel, src: str) -> str:
    if src.startswith("col:"):
        return src[4:]
    return telemetry.ROLE_LABELS.get(src, src)


class GaugeDialog(QtWidgets.QDialog):
    """Create or edit one data gauge, with a preview of the chosen channel."""

    COLORS = [("Accent (style color)", "accent"), ("Green", "throttle"),
              ("Red", "brake"), ("White", "fg")]

    def __init__(self, parent, ctx, g: dict):
        super().__init__(parent)
        self.setWindowTitle("Data gauge")
        self.resize(560, 640)
        self.ctx, self.tel = ctx, ctx.telem
        self.g = dict(g)
        lay = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        lay.addLayout(form)

        self.type_cb = QtWidgets.QComboBox()
        for k, v in gauges.GAUGE_TYPES.items():
            self.type_cb.addItem(v, k)
        self.type_cb.setCurrentIndex(self.type_cb.findData(self.g["type"]))
        form.addRow("Type", self.type_cb)

        self.src_cb = QtWidgets.QComboBox()
        self.src_cb.setMaxVisibleItems(25)
        model = self.src_cb.model()
        for r in ("speed", "throttle", "brake", "rpm", "lap", "lat_g", "lon_g"):
            col = self.tel.mapping.get(r)
            text = telemetry.ROLE_LABELS[r] + (f"  (from {col})" if col else "")
            self.src_cb.addItem(text, r)
            if not self.tel.has(r):
                model.item(self.src_cb.count() - 1).setEnabled(False)
        self.src_cb.insertSeparator(self.src_cb.count())
        for c in self.tel.source_columns:
            u = self.tel.units.get(c, "")
            live = self.tel.has("col:" + c)
            self.src_cb.addItem(c + (f"  [{u}]" if u else "")
                                + ("" if live else "  (constant)"), "col:" + c)
            if not live:
                model.item(self.src_cb.count() - 1).setEnabled(False)
        i = self.src_cb.findData(self.g["src"])
        if i < 0:  # gauge from another file: keep its source selectable
            self.src_cb.addItem(f"{src_label(self.tel, self.g['src'])}  (not in this file)",
                                self.g["src"])
            i = self.src_cb.count() - 1
        self.src_cb.setCurrentIndex(i)
        form.addRow("Channel", self.src_cb)

        self.label_ed = QtWidgets.QLineEdit(self.g.get("label", ""))
        self.label_ed.setPlaceholderText("e.g. OIL")
        form.addRow("Label", self.label_ed)

        self.conv_cb = QtWidgets.QComboBox()
        form.addRow("Convert", self.conv_cb)
        self.unit_ed = QtWidgets.QLineEdit("" if self.g.get("unit", "auto") == "auto"
                                           else self.g["unit"])
        form.addRow("Unit text", self.unit_ed)

        self.dec_sp = QtWidgets.QSpinBox()
        self.dec_sp.setRange(-1, 4)
        self.dec_sp.setSpecialValueText("Auto")
        self.dec_sp.setValue(int(self.g.get("decimals", -1)))
        form.addRow("Decimals", self.dec_sp)

        def num_edit(v):
            e = QtWidgets.QLineEdit("" if v is None else f"{v:g}")
            e.setValidator(QtGui.QDoubleValidator())
            return e
        self.min_ed = num_edit(self.g.get("min"))
        self.max_ed = num_edit(self.g.get("max"))
        self.red_ed = num_edit(self.g.get("redline"))
        self.red_ed.setPlaceholderText("off")
        rng = QtWidgets.QHBoxLayout()
        rng.addWidget(self.min_ed)
        rng.addWidget(QtWidgets.QLabel("to"))
        rng.addWidget(self.max_ed)
        self.range_row = QtWidgets.QWidget()
        self.range_row.setLayout(rng)
        rng.setContentsMargins(0, 0, 0, 0)
        form.addRow("Range", self.range_row)
        form.addRow("Redline / warn at", self.red_ed)

        self.orient_cb = QtWidgets.QComboBox()
        self.orient_cb.addItem("Horizontal", "h")
        self.orient_cb.addItem("Vertical", "v")
        self.orient_cb.setCurrentIndex(max(0, self.orient_cb.findData(self.g.get("orient", "h"))))
        form.addRow("Bar direction", self.orient_cb)
        self.align_cb = QtWidgets.QComboBox()
        self.align_cb.addItem("Left", "left")
        self.align_cb.addItem("Centered", "center")
        self.align_cb.setCurrentIndex(max(0, self.align_cb.findData(self.g.get("align", "left"))))
        form.addRow("Alignment", self.align_cb)
        self.showval = QtWidgets.QCheckBox("Show the number on the bar")
        self.showval.setChecked(bool(self.g.get("show_value", True)))
        form.addRow("", self.showval)

        crow = QtWidgets.QHBoxLayout()
        self.color_cb = QtWidgets.QComboBox()
        for t, v in self.COLORS:
            self.color_cb.addItem(t, v)
        col = self.g.get("color", "accent")
        if self.color_cb.findData(col) < 0:
            self.color_cb.addItem(f"Custom {col}", col)
        self.color_cb.setCurrentIndex(self.color_cb.findData(col))
        pick = QtWidgets.QPushButton("Pick…")
        pick.clicked.connect(self._pick_color)
        crow.addWidget(self.color_cb, 1)
        crow.addWidget(pick)
        self.color_row = QtWidgets.QWidget()
        self.color_row.setLayout(crow)
        crow.setContentsMargins(0, 0, 0, 0)
        form.addRow("Color", self.color_row)
        self.form = form

        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(150)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.plot.setLabel("bottom", "data time (s)")
        self.curve = self.plot.plot(pen=pg.mkPen("#5ab4ff", width=1.2),
                                    connect="finite")
        self.lines = []
        lay.addWidget(self.plot, 1)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok
                                        | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.type_cb.currentIndexChanged.connect(self._type_changed)
        self.src_cb.currentIndexChanged.connect(self._src_changed)
        for w in (self.min_ed, self.max_ed, self.red_ed):
            w.editingFinished.connect(self._update_preview)
        self.conv_cb.currentIndexChanged.connect(self._update_preview)
        self._fill_conv(self.g.get("conv", ""))
        self._type_changed()

    def _row_visible(self, widget, on: bool):
        self.form.setRowVisible(widget, on)

    def _type_changed(self):
        t = self.type_cb.currentData()
        self._row_visible(self.orient_cb, t == "bar")
        self._row_visible(self.showval, t == "bar")
        self._row_visible(self.align_cb, t == "number")
        self._row_visible(self.range_row, t in ("bar", "dial"))
        self._row_visible(self.red_ed, t in ("bar", "dial"))
        self._row_visible(self.color_row, t in ("bar", "dial"))
        self._update_preview()

    def _src_unit(self) -> str:
        src = self.src_cb.currentData() or ""
        return self.tel.units.get(src[4:], "") if src.startswith("col:") else \
            {"speed": "km/h"}.get(src, "")

    def _fill_conv(self, want: str = ""):
        self.conv_cb.blockSignals(True)
        self.conv_cb.clear()
        self.conv_cb.addItem("None", "")
        src = self.src_cb.currentData() or ""
        if src.startswith("col:"):
            for k in gauges.conversions_for(self._src_unit()):
                self.conv_cb.addItem(gauges.CONVERSIONS[k][0], k)
        self.conv_cb.setCurrentIndex(max(0, self.conv_cb.findData(want)))
        self.conv_cb.setEnabled(self.conv_cb.count() > 1)
        self.conv_cb.blockSignals(False)

    def _src_changed(self):
        self._fill_conv("")
        self.min_ed.clear()
        self.max_ed.clear()
        self._update_preview()

    def _pick_color(self):
        c = QtWidgets.QColorDialog.getColor(parent=self)
        if c.isValid():
            hx = c.name()
            i = self.color_cb.findData(hx)
            if i < 0:
                self.color_cb.addItem(f"Custom {hx}", hx)
                i = self.color_cb.count() - 1
            self.color_cb.setCurrentIndex(i)

    @staticmethod
    def _num(e):
        try:
            return float(e.text()) if e.text().strip() else None
        except ValueError:
            return None

    def result_gauge(self) -> dict:
        g = dict(self.g)
        g.update(type=self.type_cb.currentData(), src=self.src_cb.currentData(),
                 label=self.label_ed.text().strip(),
                 unit=self.unit_ed.text().strip() or "auto",
                 conv=self.conv_cb.currentData() or "",
                 decimals=self.dec_sp.value(), min=self._num(self.min_ed),
                 max=self._num(self.max_ed), redline=self._num(self.red_ed),
                 orient=self.orient_cb.currentData(),
                 align=self.align_cb.currentData(),
                 show_value=self.showval.isChecked(),
                 color=self.color_cb.currentData())
        return gauges.normalize_gauge(g)

    def _update_preview(self):
        g = self.result_gauge()
        g["id"] = "__preview__"
        series = self.tel.series(g["src"])
        for ln in self.lines:
            self.plot.removeItem(ln)
        self.lines = []
        if series is None:
            self.curve.setData([], [])
            self.info.setText("This channel isn't in the current file.")
            return
        disp = self.ctx._to_display(g["src"], g["conv"], series)
        self.curve.setData(self.tel.time, disp)
        lo, hi, dec = self.ctx.gauge_range(g)
        unit = self.ctx.gauge_unit(g)
        fin = disp[np.isfinite(disp)]
        if fin.size:
            self.info.setText(
                f"In this file: {fin.min():.{dec}f} to {fin.max():.{dec}f} {unit}. "
                + (f"Gauge range: {lo:g} to {hi:g}" + (" (auto)" if
                   g["min"] is None and g["max"] is None else "")
                   if g["type"] != "number" else ""))
        if g["type"] != "number":
            for v, col in ((lo, (255, 255, 255, 120)), (hi, (255, 255, 255, 120)),
                           (g.get("redline"), (245, 80, 70, 200))):
                if v is not None:
                    ln = pg.InfiniteLine(pos=v, angle=0,
                                         pen=pg.mkPen(col, style=QtCore.Qt.DashLine))
                    self.plot.addItem(ln)
                    self.lines.append(ln)


class MergeDialog(QtWidgets.QDialog):
    """Pick several video files and put them in order."""

    def __init__(self, parent, start: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Merge video files")
        self.resize(520, 380)
        self.files: list[str] = []
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel(
            "Files play back to back in this order. Drag to reorder."))
        self.lst = QtWidgets.QListWidget()
        self.lst.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        for p in start:
            self._add(p)
        lay.addWidget(self.lst, 1)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add files…")
        add.clicked.connect(self._add_files)
        rm = QtWidgets.QPushButton("Remove")
        rm.clicked.connect(lambda: [self.lst.takeItem(self.lst.row(i))
                                    for i in self.lst.selectedItems()])
        srt = QtWidgets.QPushButton("Sort by name")
        srt.clicked.connect(self._sort)
        for b in (add, rm, srt):
            row.addWidget(b)
        lay.addLayout(row)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok
                                        | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _add(self, p):
        it = QtWidgets.QListWidgetItem(Path(p).name)
        it.setData(QtCore.Qt.UserRole, p)
        it.setToolTip(p)
        self.lst.addItem(it)

    def _add_files(self):
        s = settings()
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add videos", s.value("dir/video", str(Path.home())),
            "Video (*.mp4 *.MP4 *.mov *.MOV *.mkv *.MKV)")
        for f in files:
            self._add(f)
        if files:
            s.setValue("dir/video", str(Path(files[0]).parent))

    def _sort(self):
        ps = sorted((self.lst.item(i).data(QtCore.Qt.UserRole)
                     for i in range(self.lst.count())),
                    key=lambda p: (media.CHAPTER_RE.sub(r"\3\2", Path(p).name)))
        self.lst.clear()
        for p in ps:
            self._add(p)

    def _ok(self):
        self.files = [self.lst.item(i).data(QtCore.Qt.UserRole)
                      for i in range(self.lst.count())]
        self.accept()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        st = settings()
        self.video_path: str | None = None
        self.csv_path: str | None = None
        self.info = None
        self.frame_dt = 1 / 30
        self.offset = 0.0
        self.synced = False
        self.telem = None
        self.ctx: OverlayContext | None = None
        self.laps: list[tuple[int, float, float]] = []
        # Look settings: global defaults, overridden per video by its sidecar.
        self.use_mph = st.value("use_mph", True, type=bool)
        self.style_name = st.value("style", gauges.DEFAULT_STYLE)
        self.invert_lat = st.value("invert_lat", False, type=bool)
        self.invert_lon = st.value("invert_lon", False, type=bool)
        try:
            self.layout_ = gauges.normalize_layout(json.loads(st.value("layout", "{}")))
        except ValueError:
            self.layout_ = gauges.default_layout()
        try:
            self.gauge_opts = gauges.normalize_opts(json.loads(st.value("gauge_opts", "{}")))
        except ValueError:
            self.gauge_opts = gauges.normalize_opts(None)
        self.g_manual: tuple[float, bool] | None = None  # None = auto
        self.overrides: dict[str, str] = {}  # role -> column ("" = unused)
        self.proc = None
        self.parts: list[str] = []
        self._paused = False
        self._job_outputs: list[str] = []
        self._job_preview = False
        self._proc_buf = ""
        self._hw = None
        self._hw_applied = False
        threading.Thread(target=self._detect_hw, daemon=True).start()
        self._proc_err = ""
        self._ovl_key = None
        self._layout_rev = 0
        self._vt_cache = 0.0
        self._slider_held = False
        self._drag = None  # (name, dx, dy) while moving a gauge
        self._hover = None

        self.setWindowTitle("")  # Qt adds "Race Overlay <version>"
        self.resize(1520, 960)
        self._build_ui()
        self.player = self.video.player
        self.overlay = MpvOverlay(self.player)
        self._bind_keys()
        self._refresh_all()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    # ================================================================ UI ===

    def _build_ui(self):
        native = (preview_mode() == "native"
                  and QtGui.QGuiApplication.platformName() == "xcb")
        self.video = MpvNativeWidget() if native else MpvWidget()
        self.video.pressed.connect(self._video_pressed)
        self.video.dragged.connect(self._video_dragged)
        self.video.released.connect(self._video_released)
        self.video.wheeled.connect(self._video_wheel)

        self.empty_lbl = QtWidgets.QLabel(
            "Open a video and a data file in the panel on the right  →",
            self.video)
        self.empty_lbl.setStyleSheet("color:#bbb; font-size:16px;")
        self.empty_lbl.setAlignment(QtCore.Qt.AlignCenter)

        self.btn_play = _btn("Play", self._toggle_play)
        self.btn_play.setMinimumWidth(80)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setFocusPolicy(QtCore.Qt.NoFocus)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_slider_held", True))
        self.slider.sliderReleased.connect(self._slider_released)
        self.slider.sliderMoved.connect(
            lambda ms: self._seek_abs(ms / 1000.0, exact=False))
        self.time_lbl = QtWidgets.QLabel()
        self.time_lbl.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.FixedFont))
        transport = QtWidgets.QHBoxLayout()
        for w in (_btn("-5s", lambda: self._seek_rel(-5)),
                  _btn("< frame", lambda: self._cmd("frame_back_step")),
                  self.btn_play,
                  _btn("frame >", lambda: self._cmd("frame_step")),
                  _btn("+5s", lambda: self._seek_rel(5))):
            transport.addWidget(w)
        transport.addWidget(self.slider, 1)
        transport.addWidget(self.time_lbl)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setFixedHeight(160)
        self.plot.setFocusPolicy(QtCore.Qt.NoFocus)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "video time (s)")
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.span = pg.LinearRegionItem((0, 1), movable=False,
                                        brush=(90, 190, 255, 25),
                                        pen=pg.mkPen(None))
        self.span.setZValue(-10)
        self.plot.addItem(self.span)
        self.speed_curve = self.plot.plot(pen=pg.mkPen("#5ab4ff", width=1.5),
                                          connect="finite")
        self.playhead = pg.InfiniteLine(angle=90, movable=False,
                                        pen=pg.mkPen("#ffd84a", width=2))
        self.playhead.setZValue(20)
        self.plot.addItem(self.playhead)
        self.lap_lines: list = []
        self.plot.scene().sigMouseClicked.connect(self._plot_clicked)
        self.plot.setToolTip("Click: jump the video here\nCtrl+click: snap this "
                             "point of the data to the current video frame")

        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.video, 1)
        lv.addLayout(transport)
        lv.addWidget(self.plot)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setFixedWidth(390)
        self.tabs.addTab(self._build_sync_tab(), "Sync")
        self.tabs.addTab(self._build_export_tab(), "Export")
        self.tabs.addTab(self._build_look_tab(), "Look")
        self.tabs.addTab(self._build_setup_tab(), "Setup")
        self.tabs.currentChanged.connect(lambda _: self._invalidate_overlay())

        central = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(central)
        h.addWidget(left, 1)
        h.addWidget(self.tabs)
        self.setCentralWidget(central)

    def _build_sync_tab(self):
        page, pv = _tab_page()

        files, fl = _group("Files")
        grid = QtWidgets.QGridLayout()
        self.video_lbl = _note("")
        self.csv_lbl = _note("")
        grid.addWidget(_note("Video"), 0, 0)
        grid.addWidget(self.video_lbl, 0, 1)
        grid.addWidget(_btn("Open…", self._choose_video), 0, 2)
        grid.addWidget(_note("Data"), 1, 0)
        grid.addWidget(self.csv_lbl, 1, 1)
        grid.addWidget(_btn("Open…", self._choose_csv), 1, 2)
        grid.setColumnStretch(1, 1)
        fl.addLayout(grid)
        mrow = QtWidgets.QHBoxLayout()
        mrow.addWidget(_btn("Merge files…", self._merge_dialog,
                            "Play several video files as one (GoPro chapters, "
                            "split sessions)"))
        self.btn_save_merged = _btn("Save merged file", self._save_merged,
                                    "Write the joined video as one MP4 "
                                    "(lossless, no re-encode)")
        mrow.addWidget(self.btn_save_merged)
        fl.addLayout(mrow)
        pv.addWidget(files)

        self.state_lbl = QtWidgets.QLabel()
        self.state_lbl.setWordWrap(True)
        pv.addWidget(self.state_lbl)

        box1, l1 = _group("1 · Line it up")
        l1.addWidget(_note(
            "Scrub the video to where the car crosses the start/finish line "
            "and pause. Pick which lap is starting, then click Sync here."))
        self.lap_pick = QtWidgets.QComboBox()
        self.lap_pick.setFocusPolicy(QtCore.Qt.ClickFocus)
        l1.addWidget(self.lap_pick)
        l1.addWidget(_btn("Sync here", self._sync_to_lap,
                          "This video frame = the moment that lap started",
                          big=True))
        l1.addWidget(_note("No line crossing on camera? Pause at a braking "
                           "point and Ctrl+click the matching speed drop in "
                           "the trace.", small=True))
        pv.addWidget(box1)

        box2, l2 = _group("2 · Fine-tune")
        l2.addWidget(_note(
            "Play through a braking zone. If the gauges react before the car "
            "does, press Later. If they lag, press Earlier."))
        grid = QtWidgets.QGridLayout()
        steps = [("1s", 1.0), ("0.1s", 0.1), ("1 frame", None)]
        grid.addWidget(_note("Earlier"), 0, 0)
        grid.addWidget(_note("Later"), 1, 0)
        for i, (lbl, d) in enumerate(steps):
            grid.addWidget(_btn(f"- {lbl}", lambda _=False, d=d: self._nudge(d, +1),
                                "Gauges react sooner"), 0, 1 + i)
            grid.addWidget(_btn(f"+ {lbl}", lambda _=False, d=d: self._nudge(d, -1),
                                "Gauges react later"), 1, 1 + i)
        l2.addLayout(grid)
        orow = QtWidgets.QHBoxLayout()
        orow.addWidget(_note("Offset"))
        self.offset_spin = QtWidgets.QDoubleSpinBox()
        self.offset_spin.setRange(-100000.0, 100000.0)
        self.offset_spin.setDecimals(3)
        self.offset_spin.setSingleStep(0.1)
        self.offset_spin.setSuffix(" s")
        self.offset_spin.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.offset_spin.setToolTip("Data time = video time + offset")
        self.offset_spin.valueChanged.connect(
            lambda v: self._set_offset(v, from_spin=True))
        orow.addWidget(self.offset_spin, 1)
        l2.addLayout(orow)
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(_note("Check lap"))
        self.lap_jump = QtWidgets.QComboBox()
        self.lap_jump.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.lap_jump.activated.connect(self._jump_to_lap)
        crow.addWidget(self.lap_jump, 1)
        l2.addLayout(crow)
        pv.addWidget(box2)

        self.status = _note("")
        pv.addWidget(self.status)
        pv.addWidget(_btn("Next: Export  →", lambda: self.tabs.setCurrentIndex(1),
                          "Choose what to render and how", big=True))
        pv.addStretch(1)
        return page

    def _build_export_tab(self):
        page, pv = _tab_page()

        what, wl = _group("What to render")
        self.rb_whole = QtWidgets.QRadioButton("Whole video")
        self.rb_laps = QtWidgets.QRadioButton("Selected laps")
        self.rb_whole.setChecked(True)
        for rb in (self.rb_whole, self.rb_laps):
            rb.setFocusPolicy(QtCore.Qt.NoFocus)
            rb.toggled.connect(lambda _: self._export_changed())
            wl.addWidget(rb)
        self.lap_list = QtWidgets.QListWidget()
        self.lap_list.setMaximumHeight(150)
        self.lap_list.setFocusPolicy(QtCore.Qt.NoFocus)
        self.lap_list.itemChanged.connect(lambda _: self._export_changed())
        wl.addWidget(self.lap_list)
        self.rb_split = QtWidgets.QRadioButton("One file per lap")
        self.rb_join = QtWidgets.QRadioButton("All selected laps in one file")
        self.rb_split.setChecked(True)
        grp = QtWidgets.QButtonGroup(page)
        for rb in (self.rb_split, self.rb_join):
            grp.addButton(rb)
            rb.setFocusPolicy(QtCore.Qt.NoFocus)
            rb.toggled.connect(lambda _: self._export_changed())
            wl.addWidget(rb)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(_note("Extra before/after each lap"))
        self.pad_spin = QtWidgets.QDoubleSpinBox()
        self.pad_spin.setRange(0.0, 30.0)
        self.pad_spin.setSingleStep(0.5)
        self.pad_spin.setSuffix(" s")
        self.pad_spin.setValue(float(settings().value("export/pad", 3.0)))
        self.pad_spin.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.pad_spin.valueChanged.connect(lambda _: self._export_changed())
        prow.addWidget(self.pad_spin)
        wl.addLayout(prow)
        pv.addWidget(what)

        fmt, fml = _group("Format")
        form = QtWidgets.QFormLayout()
        self.res_pick = QtWidgets.QComboBox()
        self.fps_pick = QtWidgets.QComboBox()
        self.q_pick = QtWidgets.QComboBox()
        self.q_pick.addItems(list(media.QUALITY))
        self.q_pick.setCurrentText(settings().value("export/quality", "Balanced"))
        self.enc_pick = QtWidgets.QComboBox()
        self.enc_pick.addItem("Detecting…", "libx264")
        self.workers_spin = QtWidgets.QSpinBox()
        self.workers_spin.setRange(1, max(1, os.cpu_count() or 1))
        self.workers_spin.setValue(int(settings().value(
            "export/workers", engine.default_workers())))
        self.workers_spin.setToolTip("Video chunks rendered at the same time. "
                                     "About one per CPU core is fastest.")
        for w in (self.res_pick, self.fps_pick, self.q_pick, self.enc_pick,
                  self.workers_spin):
            w.setFocusPolicy(QtCore.Qt.ClickFocus)
        for w in (self.res_pick, self.fps_pick, self.q_pick, self.enc_pick):
            w.currentIndexChanged.connect(lambda _: self._export_changed())
        self.workers_spin.valueChanged.connect(lambda _: self._export_changed())
        form.addRow(_note("Resolution"), self.res_pick)
        form.addRow(_note("Frame rate"), self.fps_pick)
        form.addRow(_note("Quality"), self.q_pick)
        form.addRow(_note("Encoder"), self.enc_pick)
        form.addRow(_note("Parallel jobs"), self.workers_spin)
        fml.addLayout(form)
        pv.addWidget(fmt)

        dest, dl = _group("Save to")
        self.out_lbl = _note("")
        dl.addWidget(self.out_lbl)
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(_btn("Change…", self._choose_out_dir))
        drow.addWidget(_btn("Next to video", self._out_next_to_video))
        dl.addLayout(drow)
        pv.addWidget(dest)

        go, gl = _group("Render")
        self.btn_preview = _btn("Preview 20s from playhead", self._render_preview,
                                "Quick 720p/30fps clip from just before the "
                                "playhead; opens when done")
        self.btn_full = _btn("Render", self._render_full, big=True)
        crow = QtWidgets.QHBoxLayout()
        self.btn_pause = _btn("Pause", self._pause_render)
        self.btn_stop = _btn("Stop", self._stop_render,
                             "Stops now. Finished chunks are kept, so Render "
                             "picks up where it left off.")
        crow.addWidget(self.btn_pause)
        crow.addWidget(self.btn_stop)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.stats_lbl = QtWidgets.QLabel("")
        self.stats_lbl.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.FixedFont))
        self.export_status = _note("")
        self.btn_open_out = _btn("Open output folder", self._open_out_dir)
        for w in (self.btn_preview, self.btn_full):
            gl.addWidget(w)
        gl.addLayout(crow)
        for w in (self.progress, self.stats_lbl, self.export_status,
                  self.btn_open_out):
            gl.addWidget(w)
        pv.addWidget(go)
        pv.addStretch(1)
        self._set_render_buttons()
        return page

    def _build_look_tab(self):
        page, pv = _tab_page()

        sbox, sl = _group("Style")
        self.style_pick = QtWidgets.QComboBox()
        self.style_pick.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.style_pick.addItems(list(gauges.STYLES))
        self.style_pick.setCurrentText(self.style_name)
        self.style_pick.currentTextChanged.connect(self._style_changed)
        sl.addWidget(self.style_pick)
        brow = QtWidgets.QHBoxLayout()
        brow.addWidget(_note("Background"))
        self.bg_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.bg_slider.setRange(0, 100)
        self.bg_slider.setFocusPolicy(QtCore.Qt.NoFocus)
        self.bg_slider.setToolTip("How see-through the boxes behind the gauges "
                                  "are. 0% = no background.")
        self.bg_slider.valueChanged.connect(self._bg_changed)
        brow.addWidget(self.bg_slider, 1)
        self.bg_lbl = QtWidgets.QLabel()
        self.bg_lbl.setMinimumWidth(78)
        brow.addWidget(self.bg_lbl)
        sl.addLayout(brow)
        self.bg_reset = _btn("Use the style's background", self._bg_default)
        sl.addWidget(self.bg_reset)
        self._sync_bg_widgets()
        sl.addWidget(_note("Clean: outlined text, no backing. Panel: dark "
                           "boxes. Race: yellow, speed bar, g trail. "
                           "Minimal: heavy outlines only.", small=True))
        pv.addWidget(sbox)

        gbox, gl = _group("Gauges")
        self.edit_btn = QtWidgets.QPushButton("Move && resize gauges")
        self.edit_btn.setCheckable(True)
        self.edit_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        self.edit_btn.setMinimumHeight(36)
        self.edit_btn.toggled.connect(self._edit_toggled)
        gl.addWidget(self.edit_btn)
        gl.addWidget(_note("While on: drag gauges on the video, scroll the "
                           "mouse wheel over one to resize it.", small=True))
        gl.addWidget(_note("<b>Data gauges</b> (pick any channel):"))
        self.custom_list = QtWidgets.QListWidget()
        self.custom_list.setMinimumHeight(130)
        self.custom_list.setFocusPolicy(QtCore.Qt.NoFocus)
        self.custom_list.itemChanged.connect(self._custom_item_changed)
        self.custom_list.itemDoubleClicked.connect(lambda _: self._edit_gauge())
        gl.addWidget(self.custom_list)
        crow = QtWidgets.QGridLayout()
        for i, (text, slot) in enumerate((("Add…", self._add_gauge),
                                          ("Edit…", self._edit_gauge),
                                          ("Copy", self._dup_gauge),
                                          ("Delete", self._del_gauge))):
            crow.addWidget(_btn(text, slot), i // 2, i % 2)
        gl.addLayout(crow)
        gl.addWidget(_note("<b>Built-in gauges:</b>"))
        self.gauge_checks = {}
        for name, (label, *_rest) in gauges.WIDGETS.items():
            c = _check(label, self.layout_[name]["on"],
                       lambda on, n=name: self._gauge_toggled(n, on))
            self.gauge_checks[name] = c
            gl.addWidget(c)
        gl.addWidget(_note("Gauges whose data isn't in the current file are "
                           "greyed out and skipped automatically.", small=True))
        gl.addWidget(_btn("Reset positions && sizes", self._reset_layout))
        pv.addWidget(gbox)

        bbox, bl = _group("G-ball")
        self.inv_lat_box = _check("Flip left / right", self.invert_lat,
                                  self._inv_lat, "Default: dot moves toward "
                                  "the inside of the turn")
        self.inv_lon_box = _check("Flip up / down", self.invert_lon,
                                  self._inv_lon, "Default: dot goes up under "
                                  "acceleration, down under braking")
        bl.addWidget(self.inv_lat_box)
        bl.addWidget(self.inv_lon_box)
        pv.addWidget(bbox)

        lbox, ll = _group("Lap timer")
        lrow = QtWidgets.QHBoxLayout()
        lrow.addWidget(_note("Delta compared to"))
        self.delta_ref_pick = QtWidgets.QComboBox()
        self.delta_ref_pick.addItem("Best lap so far", "best_so_far")
        self.delta_ref_pick.addItem("Best lap of the session", "session")
        self.delta_ref_pick.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.delta_ref_pick.setToolTip(
            "Best so far: only laps already driven at that point in the video "
            "(no spoilers). Session: your fastest lap, all session.")
        self.delta_ref_pick.currentIndexChanged.connect(
            lambda _: self._opt("delta_ref", self.delta_ref_pick.currentData()))
        lrow.addWidget(self.delta_ref_pick, 1)
        ll.addLayout(lrow)
        self.delta_bar_box = _check("Delta bar", True,
                                    lambda on: self._opt("delta_bar", on))
        ll.addWidget(self.delta_bar_box)
        ll.addWidget(_note("Out-lap and in-lap show a running clock only. "
                           "After each lap its time shows for a few seconds, "
                           "purple if it's a new best.", small=True))
        pv.addWidget(lbox)

        cbox, cl = _group("Corner")
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(_note("Show"))
        self.corner_mode_pick = QtWidgets.QComboBox()
        self.corner_mode_pick.addItem("Live (fills in through the corner)", "live")
        self.corner_mode_pick.addItem("Summary (updates after each corner)", "summary")
        self.corner_mode_pick.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.corner_mode_pick.currentIndexChanged.connect(
            lambda _: self._opt("corner_mode", self.corner_mode_pick.currentData()))
        crow.addWidget(self.corner_mode_pick, 1)
        cl.addLayout(crow)
        self.corner_cmp_box = _check(
            "Compare to the delta lap", False,
            lambda on: self._opt("corner_compare", on),
            "Shows +/- vs the same corner on the lap the delta uses")
        cl.addWidget(self.corner_cmp_box)
        self.corner_info = _note("", small=True)
        cl.addWidget(self.corner_info)
        pv.addWidget(cbox)

        obox, ol = _group("Display")
        self.kmh_box = _check("Speed in km/h", not self.use_mph,
                              self._units_changed)
        self.ovl_box = _check("Show overlay on video", True,
                              lambda _: self._invalidate_overlay())
        ol.addWidget(self.kmh_box)
        ol.addWidget(self.ovl_box)
        pv.addWidget(obox)
        pv.addStretch(1)
        return page

    def _build_setup_tab(self):
        page, pv = _tab_page()
        dbox, dl = _group("Data file")
        self.device_lbl = _note("<i>No data file loaded.</i>")
        dl.addWidget(self.device_lbl)
        dl.addWidget(_note("Which column feeds each built-in channel. Auto "
                           "picks the best match; g-force columns are checked "
                           "against GPS and their sign fixed if needed.",
                           small=True))
        self.role_form = QtWidgets.QFormLayout()
        self.role_combos: dict[str, QtWidgets.QComboBox] = {}
        for role in telemetry.USER_ROLES:
            cb = QtWidgets.QComboBox()
            cb.setFocusPolicy(QtCore.Qt.ClickFocus)
            cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(12)
            cb.activated.connect(lambda _i, r=role: self._role_changed(r))
            self.role_combos[role] = cb
            self.role_form.addRow(_note(telemetry.ROLE_LABELS[role]), cb)
        dl.addLayout(self.role_form)
        self.role_info = _note("", small=True)
        dl.addWidget(self.role_info)
        dl.addWidget(_btn("Reset all to Auto", self._reset_roles))
        pv.addWidget(dbox)

        abox, al = _group("G-sensor alignment")
        al.addWidget(_note(
            "Only matters for loggers that export raw accelerometer axes "
            "(APEX Pro does). The sensor's axes rarely point straight down "
            "the car, so they're rotated to line up with it. Auto compares "
            "them with GPS speed and heading changes; a fit near 100% means "
            "it's right."))
        self.align_lbl = _note("")
        al.addWidget(self.align_lbl)
        al.addWidget(_btn("Auto-detect", self._auto_align))
        mrow = QtWidgets.QHBoxLayout()
        mrow.addWidget(_note("Manual angle"))
        self.rot_spin = QtWidgets.QDoubleSpinBox()
        self.rot_spin.setRange(-180.0, 180.0)
        self.rot_spin.setDecimals(1)
        self.rot_spin.setSingleStep(5.0)
        self.rot_spin.setSuffix(" deg")
        self.rot_spin.setFocusPolicy(QtCore.Qt.ClickFocus)
        mrow.addWidget(self.rot_spin, 1)
        mrow.addWidget(_btn("Apply", self._manual_align))
        al.addLayout(mrow)
        self.mirror_box = _check("Mirror lateral axis", False, lambda _: None)
        al.addWidget(self.mirror_box)
        pv.addWidget(abox)

        if appimage_path():
            mbox, ml = _group("App menu")
            self.menu_lbl = _note("")
            ml.addWidget(self.menu_lbl)
            mrow = QtWidgets.QHBoxLayout()
            mrow.addWidget(_btn("Add to app menu", self._menu_add))
            mrow.addWidget(_btn("Remove", self._menu_remove))
            ml.addLayout(mrow)
            pv.addWidget(mbox)
            self._menu_refresh()

        from . import __version__
        vbox, vl = _group("Preview")
        vl.addWidget(_note("How the preview player decodes video. Doesn't "
                           "affect rendered videos.", small=True))
        self.decode_pick = QtWidgets.QComboBox()
        self.decode_pick.setFocusPolicy(QtCore.Qt.ClickFocus)
        for key, (label, _v) in PREVIEW_DECODE.items():
            if key == "t_nvdec_copy":
                self.decode_pick.insertSeparator(self.decode_pick.count())
            self.decode_pick.addItem(label, key)
        cur = settings().value("preview/decode", DEFAULT_PREVIEW_DECODE)
        self.decode_pick.setCurrentIndex(max(0, self.decode_pick.findData(cur)))
        self.decode_pick.currentIndexChanged.connect(self._decode_changed)
        vl.addWidget(self.decode_pick)
        mrow = QtWidgets.QHBoxLayout()
        mrow.addWidget(QtWidgets.QLabel("Player window:"))
        self.mode_pick = QtWidgets.QComboBox()
        self.mode_pick.setFocusPolicy(QtCore.Qt.ClickFocus)
        for key, label in PREVIEW_MODES.items():
            self.mode_pick.addItem(label, key)
        self.mode_pick.setCurrentIndex(max(0, self.mode_pick.findData(
            settings().value("preview/mode", "native"))))
        self.mode_pick.currentIndexChanged.connect(
            lambda _i: (settings().setValue("preview/mode",
                                            self.mode_pick.currentData()),
                        self.status.setText("Player window change applies "
                                            "after a restart.")))
        self.mode_pick.setToolTip(
            "Native: mpv draws in its own window, like the standalone mpv "
            "player (fixes striped GPU preview on some NVIDIA setups).\n"
            "Embedded: the older way. Restart to apply.")
        mrow.addWidget(self.mode_pick, 1)
        vl.addLayout(mrow)
        self.decode_lbl = _note("", small=True)
        vl.addWidget(self.decode_lbl)
        vl.addWidget(_note("If the preview shows stripes or blocky glitches, "
                           "try the Test options to find a GPU decoder that "
                           "works on your machine, or use Off. If a Test "
                           "option can't start, it quietly uses the CPU "
                           "(check the line above).", small=True))
        pv.addWidget(vbox)

        abox, al2 = _group("About")
        al2.addWidget(_note(f"Race Overlay {__version__}", small=True))
        pv.addWidget(abox)

        kbox, kl = _group("Keys")
        kl.addWidget(_note(
            "Space play/pause · click video play/pause<br>"
            "← → 1 s (Shift 5 s) · Ctrl+← → one frame (also , .)<br>"
            "Ctrl+Shift+← → gauges earlier / later by one frame<br>"
            "[ ] gauges later / earlier 0.1 s<br>Ctrl+O open video · Ctrl+S save"))
        pv.addWidget(kbox)
        pv.addStretch(1)
        return page

    def _bind_keys(self):
        def sc(seq, fn):
            s = QtGui.QShortcut(QtGui.QKeySequence(seq), self)
            s.setContext(QtCore.Qt.WindowShortcut)
            s.activated.connect(fn)

        sc("Space", self._toggle_play)
        sc("Left", lambda: self._seek_rel(-1.0))
        sc("Right", lambda: self._seek_rel(1.0))
        sc("Shift+Left", lambda: self._seek_rel(-5.0))
        sc("Shift+Right", lambda: self._seek_rel(5.0))
        sc(",", lambda: self._cmd("frame_back_step"))
        sc(".", lambda: self._cmd("frame_step"))
        sc("[", lambda: self._nudge(0.1, -1))
        sc("]", lambda: self._nudge(0.1, +1))
        sc("Ctrl+Left", lambda: self._cmd("frame_back_step"))
        sc("Ctrl+Right", lambda: self._cmd("frame_step"))
        sc("Ctrl+Shift+Left", lambda: self._nudge(None, +1))   # gauges earlier
        sc("Ctrl+Shift+Right", lambda: self._nudge(None, -1))  # gauges later
        sc("Ctrl+S", lambda: self._save(quiet=False))
        sc("Ctrl+O", self._choose_video)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.empty_lbl.setGeometry(self.video.rect())

    # ============================================================ files ===

    def _choose_video(self):
        p = pick_file(self, "Open video", "video",
                      "Video (*.mp4 *.MP4 *.mov *.MOV *.mkv *.MKV);;All files (*)")
        if p:
            self.open_video(p)

    def _choose_csv(self):
        p = pick_file(self, "Open telemetry CSV", "csv",
                      "Telemetry (*.csv *.CSV *.txt);;All files (*)")
        if p:
            self.open_csv(p)

    def open_video(self, path: str, parts: list[str] | None = None):
        if parts is None:
            parts = self._resolve_parts(path)
            if parts is None:
                return
        try:
            info = media.probe(parts)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Can't read video",
                                           f"{path}\n\n{e}")
            return
        settings().setValue("dir/video", str(Path(parts[0]).parent))
        self.parts = list(parts)
        path = self.parts[0]  # the sidecar lives next to the first file
        self.video_path, self.info = path, info
        self.frame_dt = 1.0 / info.fps
        self.slider.setRange(0, max(1, int(info.duration * 1000)))
        self.span.setRegion((0, info.duration))
        self.video.load(media.mpv_url(self.parts))
        extra = f" (+{len(parts) - 1} more)" if len(parts) > 1 else ""
        self.setWindowTitle(f"{Path(path).name}{extra}")
        self._fill_format_pickers()

        saved = load_sync(path)
        self.offset = float(saved.get("offset", 0.0))
        self.synced = bool(saved.get("synced", "offset" in saved))
        if "style" in saved:
            self.style_name = saved["style"]
        if "layout" in saved:
            self.layout_ = gauges.normalize_layout(saved["layout"])
        if "gauge_opts" in saved:
            self.gauge_opts = gauges.normalize_opts(saved["gauge_opts"])
        self.overrides = dict(saved.get("overrides") or {})
        for key in ("invert_lat", "invert_lon", "use_mph"):
            if key in saved:
                setattr(self, key, bool(saved[key]))
        self.g_manual = None
        if saved.get("g_rotate") is not None and not saved.get("g_auto", True):
            self.g_manual = (float(saved["g_rotate"]), bool(saved.get("g_flip")))
        self._sync_look_widgets()

        csv = saved.get("csv")
        if csv and os.path.exists(csv):
            self.open_csv(csv, keep_sync=True)
        else:
            self._refresh_all()

    def _resolve_parts(self, path: str) -> list[str] | None:
        """Decide which files make up the video: a saved merge, the GoPro
        chapters of this recording (asks), or just the one file."""
        chapters = media.find_chapters(path)
        saved = load_sync(chapters[0]) if chapters else {}
        sp = saved.get("parts")
        if sp and len(sp) > 1 and all(os.path.exists(p) for p in sp) \
                and os.path.abspath(path) in [os.path.abspath(p) for p in sp]:
            return sp
        if len(chapters) > 1:
            try:
                total = media.probe(chapters).duration
                tl = f" ({fmt_t(total).split('.')[0]} total)"
            except Exception:
                tl = ""
            r = QtWidgets.QMessageBox.question(
                self, "GoPro recording in parts",
                f"This recording is split into {len(chapters)} files{tl}:\n\n"
                + "\n".join(Path(c).name for c in chapters)
                + "\n\nLoad them as one video?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
                | QtWidgets.QMessageBox.Cancel, QtWidgets.QMessageBox.Yes)
            if r == QtWidgets.QMessageBox.Cancel:
                return None
            if r == QtWidgets.QMessageBox.Yes:
                return chapters
        return [path]

    def _merge_dialog(self):
        dlg = MergeDialog(self, self.parts)
        if dlg.exec() and dlg.files:
            info = media.probe(dlg.files)
            ok, why = info.compatible()
            if not ok:
                QtWidgets.QMessageBox.information(
                    self, "Different formats",
                    "These files can play and render together, but can't be "
                    f"saved as one merged file without re-encoding.\n\n{why}")
            self.open_video(dlg.files[0], parts=dlg.files)
            self._save(quiet=True)

    def _save_merged(self):
        if len(self.parts) < 2:
            self._set_status("Only one file is open; nothing to merge.")
            return
        ok, why = media.probe(self.parts).compatible()
        if not ok:
            QtWidgets.QMessageBox.warning(self, "Can't merge losslessly", why)
            return
        out, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save merged video", media.merged_name(self.parts),
            "MP4 (*.mp4)")
        if not out:
            return
        lst = Path(tempfile.gettempdir()) / "raceoverlay_merge.txt"
        cmd = media.save_merged_cmd(self.parts, out, str(lst))
        proc = QtCore.QProcess(self)
        self.btn_save_merged.setEnabled(False)
        self._set_status(f"Saving {Path(out).name}… (copying, no re-encode)")

        def done(code, _s):
            self.btn_save_merged.setEnabled(True)
            self._set_status(f"Saved {Path(out).name}" if code == 0 else
                             "Merge failed: " + bytes(proc.readAllStandardError())
                             .decode(errors="replace")[-300:])
        proc.finished.connect(done)
        proc.start(cmd[0], cmd[1:])
        self._merge_proc = proc

    def open_csv(self, path: str, keep_sync: bool = False):
        try:
            g_rot, g_flip = self.g_manual if self.g_manual else (None, False)
            if not keep_sync and path != self.csv_path:
                self.overrides = {}  # a different file: mappings don't carry over
            try:
                tel = telemetry.load(path, overrides=self.overrides,
                                     g_rotate=g_rot, g_flip=g_flip)
            except ValueError:
                if not self.overrides:
                    raise
                self.overrides = {}  # stale mapping (column gone): back to auto
                tel = telemetry.load(path, g_rotate=g_rot, g_flip=g_flip)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Can't read data file",
                                           f"{path}\n\n{e}")
            return
        settings().setValue("dir/csv", str(Path(path).parent))
        if not keep_sync and path != self.csv_path:
            self.synced = False  # different log, old offset means nothing
        self.csv_path, self.telem = path, tel
        self._rebuild_ctx()
        self._build_laps()
        self._refresh_all()
        if self.video_path and not keep_sync:
            self._save(quiet=True)

    def _rebuild_ctx(self):
        if self.telem is None:
            self.ctx = None
            return
        self.ctx = OverlayContext.build(
            self.telem, use_mph=self.use_mph, style=self.style_name,
            layout=self.layout_, invert_lat=self.invert_lat,
            invert_lon=self.invert_lon, opts=self.gauge_opts)
        self.gauge_opts = self.ctx.opts  # share one dict so edits apply live
        self.layout_ = self.ctx.layout  # share one dict so edits apply live
        self._invalidate_overlay()

    def _sync_look_widgets(self):
        self.style_pick.blockSignals(True)
        self.style_pick.setCurrentText(self.style_name)
        self.style_pick.blockSignals(False)
        self._sync_bg_widgets()
        for name, c in self.gauge_checks.items():
            c.blockSignals(True)
            c.setChecked(self.layout_[name]["on"])
            c.blockSignals(False)
        self._fill_custom_list()
        o = self.gauge_opts
        for combo, val in ((self.delta_ref_pick, o["delta_ref"]),
                           (self.corner_mode_pick, o["corner_mode"])):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(val)))
            combo.blockSignals(False)
        for box, val in ((self.delta_bar_box, o["delta_bar"]),
                         (self.corner_cmp_box, o["corner_compare"])):
            box.blockSignals(True)
            box.setChecked(val)
            box.blockSignals(False)
        for box, val in ((self.inv_lat_box, self.invert_lat),
                         (self.inv_lon_box, self.invert_lon),
                         (self.kmh_box, not self.use_mph)):
            box.blockSignals(True)
            box.setChecked(val)
            box.blockSignals(False)

    # ======================================================= refreshes ===

    def _refresh_all(self):
        def elide(p):
            return Path(p).name if p else "<i>none</i>"
        vtxt = elide(self.video_path)
        if len(self.parts) > 1:
            vtxt += f" <small>+{len(self.parts) - 1} more</small>"
        self.video_lbl.setText(vtxt)
        self.video_lbl.setToolTip("\n".join(self.parts))
        self.btn_save_merged.setEnabled(len(self.parts) > 1)
        self.csv_lbl.setText(elide(self.csv_path))
        self.csv_lbl.setToolTip(self.csv_path or "")
        self.empty_lbl.setVisible(self.video_path is None)
        self._fill_lap_list()
        self._set_render_buttons()
        if self.ctx and self.ctx.corners and self.ctx.corners.ok:
            cm = self.ctx.corners
            per = {}
            for p in cm.passes:
                if p.lap is not None:
                    per[p.lap] = per.get(p.lap, 0) + 1
            sb = self.ctx.laps.session_best() if self.ctx.laps.ok else None
            extra = ""
            if sb is not None:
                extra = f", {per.get(self.ctx.laps.laps.index(sb), 0)} on your fastest lap"
            self.corner_info.setText(
                f"{len(cm.passes)} corners in this session{extra}. A corner is "
                "anywhere you slow by 5+ mph; flat-out sweepers don't count.")
        elif self.ctx:
            self.corner_info.setText("Needs a speed channel.")
        avail = self.ctx.available if self.ctx else set(gauges.WIDGETS)
        for name, c in self.gauge_checks.items():
            c.setEnabled(name in avail)
            c.setToolTip("" if name in avail else "No data for this in the log")
        self._fill_custom_list()
        self._fill_data_panel()
        self._set_offset(self.offset, from_spin=False, quiet=True)
        self._update_state()
        self._update_align_label()

    def _update_state(self):
        base = "padding:8px; border-radius:6px; font-weight:bold; color:white;"
        if self.video_path is None or self.telem is None:
            need = [n for n, v in (("a video", self.video_path),
                                   ("a data file", self.telem)) if v is None]
            self.state_lbl.setText("Open " + " and ".join(need) + " above.")
            self.state_lbl.setStyleSheet(base + "background:#444;")
        elif self.synced:
            self.state_lbl.setText(f"Synced  (offset {self.offset:+.2f}s)")
            self.state_lbl.setStyleSheet(base + "background:#1f5130;")
        else:
            self.state_lbl.setText("Not synced yet. Start with step 1.")
            self.state_lbl.setStyleSheet(base + "background:#6b4a10;")

    def _update_align_label(self):
        t = self.telem
        if t is None:
            self.align_lbl.setText("<i>No data file loaded.</i>")
        elif t.g_source == "named":
            self.align_lbl.setText("This log has lateral/longitudinal g "
                                   "columns already. Nothing to align.")
        elif t.g_source != "raw":
            self.align_lbl.setText("This log has no accelerometer data.")
        else:
            a = t.g_align
            fits = ""
            if a.get("lon_fit") is not None:
                fits = f"<br>Fit: {a['lon_fit']:.0%} braking/accel"
                if a.get("lat_fit") is not None:
                    fits += f", {a['lat_fit']:.0%} cornering"
            how = "auto" if a.get("auto") else "manual"
            mir = ", mirrored" if a.get("flip") else ""
            self.align_lbl.setText(f"<b>{a['angle']:+.0f}°{mir}</b> ({how}){fits}")
            self.rot_spin.setValue(float(a["angle"]))
            self.mirror_box.setChecked(bool(a.get("flip")))

    # ============================================================ laps ===

    def _build_laps(self):
        self.laps = []
        for line in self.lap_lines:
            self.plot.removeItem(line)
        self.lap_lines = []
        self.lap_pick.clear()
        self.lap_jump.clear()
        lm = self.ctx.laps if self.ctx else None
        if lm is not None and lm.ok:
            rows = [(l.n, l.start, l.duration) for l in lm.laps]
            # laps[1:] are line crossings. A file that starts at the line
            # (one-lap export) gets a placeholder so that still holds.
            self.laps = rows if lm.laps[0].kind == "out" else [(None, 0.0, 0.0)] + rows
        crossings = self.laps[1:]  # lap 0 starts at logger power-up
        timed = [d for _, _, d in crossings[:-1]]  # last one is the in-lap
        best = min(timed) if timed else None
        if not crossings:
            self.lap_pick.addItem("(no lap crossings in this log)")
        for n, start, dur in crossings:
            m, sec = divmod(dur, 60)
            tag = "   * best" if best is not None and dur == best else ""
            text = f"Lap {n}  ({int(m)}:{sec:04.1f}){tag}"
            self.lap_pick.addItem(text)
            self.lap_jump.addItem(text)
            line = pg.InfiniteLine(
                angle=90, movable=False,
                pen=pg.mkPen((255, 255, 255, 90), style=QtCore.Qt.DashLine),
                label=f"L{n}", labelOpts={"position": 0.9,
                                          "color": (255, 255, 255, 170)})
            self.plot.addItem(line)
            self.lap_lines.append(line)

    def _redraw_trace(self):
        if self.telem is None:
            self.speed_curve.setData([], [])
            return
        spd = self.telem.channels.get("speed")
        if spd is not None:
            y = spd / KMH_PER_MPH if self.use_mph else spd
            self.speed_curve.setData(self.telem.time - self.offset, y)
            self.plot.setLabel("left", "mph" if self.use_mph else "km/h")
        for line, (_, start, _) in zip(self.lap_lines, self.laps[1:]):
            line.setValue(start - self.offset)

    # ============================================================ sync ===

    def _ready(self, need_sync_data=True) -> bool:
        if self.video_path is None or (need_sync_data and self.telem is None):
            self._set_status("Open a video and a data file first.")
            self.tabs.setCurrentIndex(0)
            return False
        return True

    def _set_offset(self, value: float, from_spin: bool, quiet=False):
        self.offset = float(value)
        if not from_spin:
            self.offset_spin.blockSignals(True)
            self.offset_spin.setValue(self.offset)
            self.offset_spin.blockSignals(False)
        self._redraw_trace()
        self._invalidate_overlay()
        if self.synced and not quiet:
            self._update_state()
        if hasattr(self, "lap_list"):
            self._fill_lap_list()
            self._set_render_buttons()

    def _mark_synced(self, msg: str):
        self.synced = True
        self._fill_lap_list()
        self._update_state()
        self._save(quiet=True)
        self._set_status(msg)

    def _sync_to_lap(self):
        if not self._ready():
            return
        idx = self.lap_pick.currentIndex()
        crossings = self.laps[1:]
        if not (0 <= idx < len(crossings)):
            self._set_status("This log has no lap crossings. Ctrl+click the "
                             "speed trace instead.")
            return
        n, start, _ = crossings[idx]
        self._set_offset(start - self._vt(), from_spin=False)
        self._mark_synced(f"Synced to the start of lap {n}. Play a braking "
                          "zone and fine-tune if needed.")

    def _nudge(self, amount: float | None, sign: int):
        """sign +1 = gauges earlier (offset up), -1 = later."""
        d = self.frame_dt if amount is None else amount
        self._set_offset(self.offset + sign * d, from_spin=False)
        if self.synced:
            self._save(quiet=True)

    def _plot_clicked(self, ev):
        if ev.button() != QtCore.Qt.LeftButton or self.video_path is None:
            return
        vb = self.plot.getPlotItem().vb
        pos = ev.scenePos()
        if not vb.sceneBoundingRect().contains(pos):
            return
        x = vb.mapSceneToView(pos).x()
        if ev.modifiers() & QtCore.Qt.ControlModifier and self.telem is not None:
            self._set_offset(x + self.offset - self._vt(), from_spin=False)
            self._mark_synced("Snapped that point of the trace to this frame.")
        else:
            self._seek_abs(x)
        ev.accept()

    def _jump_to_lap(self, idx: int):
        crossings = self.laps[1:]
        if self.info and 0 <= idx < len(crossings):
            target = crossings[idx][1] - self.offset
            if not 0 <= target <= self.info.duration:
                self._set_status(f"Lap {crossings[idx][0]} isn't in this "
                                 "video at the current offset.")
                return
            self._seek_abs(target)

    # ============================================================ look ===

    def _save_look_prefs(self):
        s = settings()
        s.setValue("style", self.style_name)
        s.setValue("layout", json.dumps(self.layout_))
        s.setValue("gauge_opts", json.dumps(self.gauge_opts))
        s.setValue("invert_lat", self.invert_lat)
        s.setValue("invert_lon", self.invert_lon)
        s.setValue("use_mph", self.use_mph)
        if self.video_path:
            self._save(quiet=True)

    # ----- data gauges -----

    def _fill_custom_list(self):
        if not hasattr(self, "custom_list"):
            return
        cur = self.custom_list.currentRow()
        self.custom_list.blockSignals(True)
        self.custom_list.clear()
        for g in self.layout_.get("custom", []):
            name = g.get("label") or src_label(self.telem, g["src"])
            text = f"{name}  ·  {gauges.GAUGE_TYPES[g['type']]}"
            ok = self.ctx is None or self.ctx.gauge_available(g)
            if not ok:
                text += "   (no data)"
            it = QtWidgets.QListWidgetItem(text)
            it.setData(QtCore.Qt.UserRole, g["id"])
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if g["on"] else QtCore.Qt.Unchecked)
            if not ok:
                it.setForeground(QtGui.QBrush(QtGui.QColor(140, 140, 140)))
                it.setToolTip(f"'{src_label(self.telem, g['src'])}' isn't in "
                              "this file, so this gauge is skipped.")
            self.custom_list.addItem(it)
        self.custom_list.blockSignals(False)
        if 0 <= cur < self.custom_list.count():
            self.custom_list.setCurrentRow(cur)

    def _selected_gauge(self) -> dict | None:
        it = self.custom_list.currentItem()
        return gauges.entry(self.layout_, it.data(QtCore.Qt.UserRole)) if it else None

    def _custom_item_changed(self, it):
        g = gauges.entry(self.layout_, it.data(QtCore.Qt.UserRole))
        if g is not None:
            g["on"] = it.checkState() == QtCore.Qt.Checked
            self._layout_changed()

    def _need_data(self) -> bool:
        if self.telem is None:
            QtWidgets.QMessageBox.information(
                self, "Open a data file first",
                "Gauges are set up against a data file's channels. Open one "
                "on the Sync tab.")
            return False
        return True

    def _add_gauge(self):
        if not self._need_data():
            return
        src = next((r for r in ("rpm", "speed") if self.telem.has(r)), "speed")
        g = gauges.normalize_gauge(dict(id=gauges.new_gauge_id(self.layout_),
                                        type="number", src=src, x=0.42, y=0.42))
        dlg = GaugeDialog(self, self.ctx, g)
        if dlg.exec():
            self.layout_["custom"].append(dlg.result_gauge())
            self._layout_changed()
            self._fill_custom_list()
            self.custom_list.setCurrentRow(self.custom_list.count() - 1)

    def _edit_gauge(self):
        g = self._selected_gauge()
        if g is None or not self._need_data():
            return
        dlg = GaugeDialog(self, self.ctx, g)
        if dlg.exec():
            g.update(dlg.result_gauge())
            if self.ctx:
                self.ctx._range_cache.clear()
            self._layout_changed()
            self._fill_custom_list()

    def _dup_gauge(self):
        g = self._selected_gauge()
        if g is None:
            return
        c = dict(g)
        c["id"] = gauges.new_gauge_id(self.layout_)
        c["x"] = min(0.9, g["x"] + 0.03)
        c["y"] = min(0.9, g["y"] + 0.03)
        self.layout_["custom"].append(c)
        self._layout_changed()
        self._fill_custom_list()
        self.custom_list.setCurrentRow(self.custom_list.count() - 1)

    def _del_gauge(self):
        g = self._selected_gauge()
        if g is None:
            return
        self.layout_["custom"] = [x for x in self.layout_["custom"] if x is not g]
        if self.ctx:
            self.ctx.layout = self.layout_
        self._layout_changed()
        self._fill_custom_list()

    # ----- data file / channel roles -----

    def _fill_data_panel(self):
        if not hasattr(self, "role_combos"):
            return
        t = self.telem
        if t is None:
            self.device_lbl.setText("<i>No data file loaded.</i>")
            for cb in self.role_combos.values():
                cb.clear()
                cb.setEnabled(False)
            self.role_info.setText("")
            return
        name = "APEX Pro (CSV)" if t.device == "apex" else \
            telemetry.DEVICE_NAMES.get(t.device, t.device)
        meta = [t.meta.get(k) for k in ("Venue", "Vehicle", "User", "Date")]
        meta = " · ".join(m for m in meta if m)
        varying = len(t.columns_varying())
        self.device_lbl.setText(
            f"<b>{name}</b><br>{len(t.source_columns)} channels "
            f"({varying} with data), {t.duration / 60:.1f} min"
            + (f"<br>{meta}" if meta else ""))
        cols = t.source_columns
        for role, cb in self.role_combos.items():
            cb.blockSignals(True)
            cb.clear()
            cb.setEnabled(True)
            auto_col = None if role in self.overrides else t.mapping.get(role)
            if role in ("lat_g", "lon_g") and t.g_source == "raw" and not auto_col:
                auto_col = "sensor axes, see below"
            cb.addItem(f"Auto ({auto_col})" if auto_col else
                       ("Auto" if role in self.overrides else "Auto (not found)"), None)
            cb.addItem("Don't use", "")
            for c in cols:
                u = t.units.get(c, "")
                live = t.has("col:" + c)
                cb.addItem(f"{c}" + (f" [{u}]" if u else "")
                           + ("" if live else "  (constant)"), c)
            want = self.overrides.get(role, None)
            idx = cb.findData(want) if want is not None else 0
            cb.setCurrentIndex(max(0, idx))
            cb.blockSignals(False)
        notes = []
        for role in ("lat_g", "lon_g"):
            c = t.checks.get(role)
            if c and c["verified"]:
                notes.append(f"{telemetry.ROLE_LABELS[role]}: {c['col']} matches "
                             f"GPS {c['corr']:.0%}"
                             + (", sign flipped" if c["flipped"] else ""))
            elif c:
                notes.append(f"{telemetry.ROLE_LABELS[role]}: {c['col']} "
                             "(couldn't check against GPS)")
        if t.g_source == "raw":
            notes.append("G-force from raw sensor axes; see G-sensor alignment.")
        if t.meta.get("single_lap"):
            n, d = t.meta["single_lap"]
            notes.append(f"Single-lap file: lap {n}, {gauges.fmt_lap(d)}.")
        self.role_info.setText("<br>".join(notes))

    def _role_changed(self, role: str):
        cb = self.role_combos[role]
        val = cb.currentData()
        if val is None:
            self.overrides.pop(role, None)
        else:
            self.overrides[role] = val
        if self.csv_path:
            self.open_csv(self.csv_path, keep_sync=True)
            self._save(quiet=True)

    def _reset_roles(self):
        self.overrides = {}
        if self.csv_path:
            self.open_csv(self.csv_path, keep_sync=True)
            self._save(quiet=True)

    def _sync_bg_widgets(self):
        v = self.gauge_opts.get("bg_opacity", -1)
        shown = gauges.style_bg_percent(self.style_name) if v < 0 else v
        self.bg_slider.blockSignals(True)
        self.bg_slider.setValue(shown)
        self.bg_slider.blockSignals(False)
        self.bg_lbl.setText(f"{shown}%" + (" (style)" if v < 0 else ""))
        self.bg_reset.setEnabled(v >= 0)

    def _bg_changed(self, v: int):
        self._opt("bg_opacity", int(v))
        self._sync_bg_widgets()

    def _bg_default(self):
        self._opt("bg_opacity", -1)
        self._sync_bg_widgets()

    # ----- app menu -----

    def _menu_refresh(self):
        if not hasattr(self, "menu_lbl"):
            return
        tgt = menu_entry_target()
        if tgt and os.path.abspath(tgt) == os.path.abspath(appimage_path() or ""):
            self.menu_lbl.setText("Race Overlay is in your app menu.")
        elif tgt:
            self.menu_lbl.setText("The app-menu entry points at another copy:\n" + tgt)
        else:
            self.menu_lbl.setText("Not in your app menu.")

    def _menu_add(self):
        p = appimage_path()
        if p:
            add_menu_entry(p)
            settings().setValue("integration/asked", True)
        self._menu_refresh()

    def _menu_remove(self):
        remove_menu_entry()
        self._menu_refresh()

    def offer_menu_entry(self):
        """First run of an AppImage: offer to add it to the app menu. If the
        AppImage was moved since, quietly repoint the existing entry."""
        p = appimage_path()
        if not p:
            return
        tgt = menu_entry_target()
        if tgt:
            if os.path.abspath(tgt) != os.path.abspath(p) and not os.path.exists(tgt):
                add_menu_entry(p)  # the old copy is gone: follow the move
            return
        s = settings()
        if s.value("integration/asked", False, type=bool):
            return
        s.setValue("integration/asked", True)
        r = QtWidgets.QMessageBox.question(
            self, "Add to app menu?",
            "Add Race Overlay to your applications menu, so you can start it "
            "like any other app (and use Open With on videos)?\n\n"
            "You can change this later on the Setup tab.")
        if r == QtWidgets.QMessageBox.Yes:
            add_menu_entry(p)
        self._menu_refresh()

    def _decode_changed(self, _i=None):
        key = self.decode_pick.currentData()
        settings().setValue("preview/decode", key)
        try:
            self.player["hwdec"] = PREVIEW_DECODE[key][1]
            if self.video_path:  # re-open the decoder at the current frame
                self._seek_abs(self._vt())
        except Exception:
            pass

    def _update_decode_label(self):
        if not hasattr(self, "decode_lbl"):
            return
        try:
            cur = self.player.hwdec_current or "no"
        except Exception:
            cur = "?"
        name = {"no": "CPU (software)", "?": "unknown"}.get(cur, cur)
        try:
            ver = str(self.player.mpv_version).replace("mpv ", "")
        except Exception:
            ver = "?"
        src = os.environ.get("RACEOVERLAY_MPV_SOURCE", "system")
        player = f"{'system' if src == 'system' else 'built-in'} mpv {ver}"
        try:
            vo = self.player.current_vo
        except Exception:
            vo = None
        player += f", {'native window' if isinstance(self.video, MpvNativeWidget) else 'embedded'}"
        if vo and vo != "libmpv":
            player += f" ({vo})"
        note = os.environ.get("RACEOVERLAY_MPV_NOTE")
        if note and src != "system":
            player += f"<br><i>{html.escape(note)}</i>"
        self.decode_lbl.setText((f"Decoding with: <b>{name}</b><br>" if self.video_path
                                 else "") + f"Player: {player}")

    def _opt(self, key: str, value):
        self.gauge_opts[key] = value
        if self.ctx:
            self.ctx.opts[key] = value
            if key == "bg_opacity":
                self.ctx.set_style(self.style_name)
        self._layout_rev += 1
        self._invalidate_overlay()
        self._save_look_prefs()

    def _style_changed(self, name: str):
        self.style_name = name
        if self.ctx:
            self.ctx.set_style(name)
        self._sync_bg_widgets()
        self._invalidate_overlay()
        self._save_look_prefs()

    def _gauge_toggled(self, name: str, on: bool):
        self.layout_[name]["on"] = on
        self._layout_changed()

    def _reset_layout(self):
        fresh = gauges.default_layout()
        for name in gauges.WIDGETS:
            fresh[name]["on"] = self.layout_[name]["on"]
            self.layout_[name].update(fresh[name])
        defaults = {g["id"]: g for g in fresh["custom"]}
        for i, g in enumerate(self.layout_["custom"]):
            if g["id"] in defaults:
                d = defaults[g["id"]]
                g.update(x=d["x"], y=d["y"], scale=d["scale"])
            else:  # stack your own gauges down the middle-left
                g.update(x=0.18, y=min(0.85, 0.30 + 0.12 * i), scale=1.0)
        self._layout_changed()

    def _layout_changed(self):
        self._layout_rev += 1
        self._invalidate_overlay()
        self._save_look_prefs()

    def _inv_lat(self, on):
        self.invert_lat = on
        if self.ctx:
            self.ctx.invert_lat = on
        self._invalidate_overlay()
        self._save_look_prefs()

    def _inv_lon(self, on):
        self.invert_lon = on
        if self.ctx:
            self.ctx.invert_lon = on
        self._invalidate_overlay()
        self._save_look_prefs()

    def _units_changed(self, kmh: bool):
        self.use_mph = not kmh
        if self.ctx:
            self.ctx.use_mph = self.use_mph
        self._redraw_trace()
        self._invalidate_overlay()
        self._save_look_prefs()

    def _edit_toggled(self, on: bool):
        self.video.setCursor(QtCore.Qt.OpenHandCursor if on
                             else QtCore.Qt.ArrowCursor)
        if on:
            self.ovl_box.setChecked(True)
            try:
                self.player.pause = True
            except Exception:
                pass
        self._invalidate_overlay()

    # ----- dragging gauges on the video -----

    def _frame_geom(self):
        """(x0, y0, w, h) of the video picture inside the widget, device px."""
        try:
            d = self.player.osd_dimensions or {}
        except Exception:
            return None
        w = int(d.get("w", 0)) - int(d.get("ml", 0)) - int(d.get("mr", 0))
        h = int(d.get("h", 0)) - int(d.get("mt", 0)) - int(d.get("mb", 0))
        if w <= 16 or h <= 16:
            return None
        return int(d.get("ml", 0)), int(d.get("mt", 0)), w, h

    def _hit(self, x, y):
        g = self._frame_geom()
        if g is None or self.ctx is None:
            return None, None
        x0, y0, w, h = g
        fx, fy = x - x0, y - y0
        boxes = gauges.layout_boxes(self.layout_, w, h)
        for name in reversed(self.ctx.shown()):  # topmost drawn last
            bx, by, bw, bh, _ = boxes[name]
            if bx <= fx <= bx + bw and by <= fy <= by + bh:
                return name, (fx - bx, fy - by, g)
        return None, (0, 0, g)

    def _video_pressed(self, x, y, mods):
        if not self.edit_btn.isChecked():
            if self.video_path:
                self._toggle_play()
            return
        name, info = self._hit(x, y)
        if name:
            self._drag = (name, info[0], info[1])
            self._hover = name
            self.video.setCursor(QtCore.Qt.ClosedHandCursor)
            self._invalidate_overlay()

    def _video_dragged(self, x, y):
        if not self._drag:
            return
        g = self._frame_geom()
        if g is None:
            return
        name, dx, dy = self._drag
        x0, y0, w, h = g
        L = gauges.entry(self.layout_, name)
        L["x"] = max(0.0, min(1.0, (x - x0 - dx) / w))
        L["y"] = max(0.0, min(1.0, (y - y0 - dy) / h))
        self._layout_rev += 1
        self._invalidate_overlay()

    def _video_released(self, x, y):
        if self._drag:
            self._drag = None
            self.video.setCursor(QtCore.Qt.OpenHandCursor)
            self._layout_changed()

    def _video_wheel(self, x, y, delta):
        if not self.edit_btn.isChecked() or delta == 0:
            return
        name, _ = self._hit(x, y)
        if name:
            L = gauges.entry(self.layout_, name)
            L["scale"] = max(0.4, min(3.0, L["scale"] * (1.1 if delta > 0 else 1 / 1.1)))
            self._hover = name
            self._layout_changed()

    # ========================================================= alignment ===

    def _auto_align(self):
        if self.telem is None or self.telem.g_source != "raw":
            self._update_align_label()
            return
        telemetry.align_g(self.telem, None)
        self.g_manual = None
        self._after_align()

    def _manual_align(self):
        if self.telem is None or self.telem.g_source != "raw":
            return
        self.g_manual = (self.rot_spin.value(), self.mirror_box.isChecked())
        telemetry.align_g(self.telem, *self.g_manual)
        self._after_align()

    def _after_align(self):
        self._rebuild_ctx()
        self._update_align_label()
        if self.video_path:
            self._save(quiet=True)

    # ========================================================= playback ===

    def _vt(self) -> float:
        try:
            t = self.player.time_pos
        except Exception:
            t = None
        if t is not None:
            self._vt_cache = float(t)
        return self._vt_cache

    def _cmd(self, *args):
        if self.video_path is None:
            return
        try:
            self.player.command(*args)
        except Exception:
            pass

    def _seek_abs(self, t: float, exact: bool = True):
        if self.info is None:
            return
        t = max(0.0, min(self.info.duration - self.frame_dt, t))
        self._cmd("seek", f"{t:.4f}",
                  "absolute+exact" if exact else "absolute+keyframes")
        self._vt_cache = t

    def _seek_rel(self, d: float):
        self._seek_abs(self._vt() + d)

    def _slider_released(self):
        self._slider_held = False
        self._seek_abs(self.slider.value() / 1000.0)

    def _toggle_play(self):
        if self.video_path is None:
            return
        try:
            self.player.pause = not self.player.pause
        except Exception:
            pass

    # ========================================================= periodic ===

    def _tick(self):
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        if self._tick_n % 20 == 0:  # about once a second
            self._update_decode_label()
        if self._hw is not None and not self._hw_applied:
            self._apply_hw()
        if self.info is None:
            self.time_lbl.setText("")
            return
        v = self._vt()
        try:
            paused = bool(self.player.pause)
        except Exception:
            paused = True
        self.btn_play.setText("Play" if paused else "Pause")
        if not self._slider_held:
            self.slider.blockSignals(True)
            self.slider.setValue(int(v * 1000))
            self.slider.blockSignals(False)
        self.time_lbl.setText(f"{fmt_t(v)} / {fmt_t(self.info.duration)}")
        self.playhead.setValue(v)
        self.plot.setXRange(v - TRACE_WINDOW / 2, v + TRACE_WINDOW / 2,
                            padding=0)
        self._update_overlay(v)

    def _invalidate_overlay(self):
        self._ovl_key = None

    def _update_overlay(self, v: float):
        if not self.ovl_box.isChecked() or self.ctx is None:
            self.overlay.hide()
            return
        g = self._frame_geom()
        if g is None:
            return
        x0, y0, w, h = g
        edit = self.edit_btn.isChecked()
        key = (round(v, 3), round(self.offset, 4), g, self._layout_rev, edit,
               self._hover, self.style_name, self.use_mph, self.invert_lat,
               self.invert_lon, id(self.ctx))
        if key == self._ovl_key:
            return
        self._ovl_key = key
        f = gauges.Frame.new(w, h)
        draw_overlay(f, self.ctx, v + self.offset, edit=edit,
                     active=self._drag[0] if self._drag else None)
        self.overlay.show(f.img, x0, y0)

    # ==================================================== save / render ===

    def _sync_data(self) -> dict:
        a = self.telem.g_align if self.telem is not None else {}
        return {"video": str(Path(self.video_path).resolve()),
                "parts": [str(Path(p).resolve()) for p in self.parts],
                "csv": str(Path(self.csv_path).resolve()) if self.csv_path else None,
                "offset": round(self.offset, 4),
                "synced": self.synced,
                "g_rotate": a.get("angle"),
                "g_flip": bool(a.get("flip", False)),
                "g_auto": bool(a.get("auto", True)),
                "style": self.style_name,
                "layout": self.layout_,
                "invert_lat": self.invert_lat,
                "invert_lon": self.invert_lon,
                "gauge_opts": self.gauge_opts,
                "overrides": self.overrides,
                "use_mph": self.use_mph}

    def _save(self, quiet: bool = False):
        if self.video_path is None:
            return None
        p = save_sync(self.video_path, self._sync_data())
        if not quiet:
            self._set_status(f"Saved {p.name}" if p else
                             "Couldn't save next to the video (read-only?).")
        return p

    # ---------------------------------------------------------- export ---

    def _detect_hw(self):
        try:
            self._hw = media.detect_hw()
        except Exception:
            self._hw = {"encoders": ["libx264"], "hwdecode": False}

    def _apply_hw(self):
        self._hw_applied = True
        saved = settings().value("export/encoder", "")
        self.enc_pick.blockSignals(True)
        self.enc_pick.clear()
        for e in self._hw["encoders"]:
            self.enc_pick.addItem(media.ENCODERS[e][0], e)
        pick = saved if saved in self._hw["encoders"] else \
            media.default_encoder(self._hw["encoders"])
        self.enc_pick.setCurrentIndex(max(0, self.enc_pick.findData(pick)))
        self.enc_pick.blockSignals(False)
        self._export_changed()

    def _fill_format_pickers(self):
        if self.info is None:
            return
        info, st = self.info, settings()
        self.res_pick.blockSignals(True)
        self.res_pick.clear()
        self.res_pick.addItem(f"Source ({info.width}×{info.height})", 0)
        for hgt in (2160, 1440, 1080, 720):
            if hgt < info.height:
                self.res_pick.addItem(f"{hgt}p", hgt)
        i = self.res_pick.findData(int(st.value("export/height", 0)))
        self.res_pick.setCurrentIndex(max(0, i))
        self.res_pick.blockSignals(False)

        self.fps_pick.blockSignals(True)
        self.fps_pick.clear()
        self.fps_pick.addItem(f"Source ({info.fps:.2f})", 0.0)
        for f in (60, 30, 24):
            if f < info.fps - 0.5:
                self.fps_pick.addItem(f"{f}", float(f))
        want = float(st.value("export/fps", -1))
        if want < 0:  # first time: cap high-speed sources at 60
            want = 60.0 if info.fps > 61 else 0.0
        self.fps_pick.setCurrentIndex(max(0, self.fps_pick.findData(want)))
        self.fps_pick.blockSignals(False)
        self._export_changed()

    def _fill_lap_list(self):
        checked = {self.lap_list.item(i).data(QtCore.Qt.UserRole)
                   for i in range(self.lap_list.count())
                   if self.lap_list.item(i).checkState() == QtCore.Qt.Checked}
        self.lap_list.blockSignals(True)
        self.lap_list.clear()
        for n, rng, note in self._lap_video_ranges():
            m, sec = divmod(next(d for k, _, d in self.laps if k == n), 60)
            it = QtWidgets.QListWidgetItem(f"Lap {n}  {int(m)}:{sec:04.1f}{note}")
            it.setData(QtCore.Qt.UserRole, n)
            if rng is None:
                it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEnabled)
                it.setCheckState(QtCore.Qt.Unchecked)
            else:
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(QtCore.Qt.Checked if n in checked
                                 else QtCore.Qt.Unchecked)
            self.lap_list.addItem(it)
        self.lap_list.blockSignals(False)

    def _lap_video_ranges(self):
        """[(lap, [start, end] on the video or None, note)] for real laps."""
        out = []
        if self.info is None or not self.laps:
            return out
        pad = self.pad_spin.value() if hasattr(self, "pad_spin") else 3.0
        dur = self.info.duration
        crossings = self.laps[1:]
        best = min((d for _, _, d in crossings[:-1]), default=None)
        for n, start, d in crossings:
            a = start - self.offset - pad
            b = start + d - self.offset + pad
            ca, cb = max(0.0, a), min(dur, b)
            tag = "  ★ best" if best is not None and d == best else ""
            if not self.synced:
                out.append((n, None, "  (sync first)"))
            elif cb - ca < 1.0:
                out.append((n, None, "  (not in video)"))
            elif ca > a + 0.05 or cb < b - 0.05:
                out.append((n, [ca, cb], tag + "  (partly in video)"))
            else:
                out.append((n, [ca, cb], tag))
        return out

    def _out_dir(self) -> Path:
        d = settings().value("export/dir", "")
        if d and os.path.isdir(d):
            return Path(d)
        return Path(self.parts[0]).parent if self.parts else Path.home()

    def _stem(self) -> str:
        if len(self.parts) > 1:
            return Path(media.merged_name(self.parts)).stem.replace("_merged", "")
        return Path(self.parts[0]).stem

    def _planned_outputs(self) -> list[dict]:
        if self.info is None:
            return []
        d, stem = self._out_dir(), self._stem()
        if self.rb_whole.isChecked():
            return [{"path": str(d / f"{stem}_overlay.mp4"),
                     "ranges": [[0.0, self.info.duration]]}]
        chosen = {self.lap_list.item(i).data(QtCore.Qt.UserRole)
                  for i in range(self.lap_list.count())
                  if self.lap_list.item(i).checkState() == QtCore.Qt.Checked}
        laps = [(n, r) for n, r, _ in self._lap_video_ranges()
                if r is not None and n in chosen]
        if not laps:
            return []
        durs = {n: dd for n, _, dd in self.laps}
        if self.rb_split.isChecked():
            outs = []
            for n, r in laps:
                m, sec = divmod(durs[n], 60)
                outs.append({"path": str(d / f"{stem}_lap{n}_{int(m)}m{sec:04.1f}.mp4"),
                             "ranges": [r]})
            return outs
        merged = []
        for _, r in sorted(laps, key=lambda x: x[1][0]):
            if merged and r[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], r[1])
            else:
                merged.append(list(r))
        ns = [n for n, _ in laps]
        name = f"{stem}_laps_{ns[0]}-{ns[-1]}" if len(ns) > 2 else \
            f"{stem}_laps_{'_'.join(map(str, ns))}"
        return [{"path": str(d / f"{name}.mp4"), "ranges": merged}]

    def _export_changed(self):
        if not hasattr(self, "rb_laps"):
            return
        st = settings()
        st.setValue("export/pad", self.pad_spin.value())
        st.setValue("export/quality", self.q_pick.currentText())
        st.setValue("export/workers", self.workers_spin.value())
        if self.enc_pick.currentData() and self._hw_applied:
            st.setValue("export/encoder", self.enc_pick.currentData())
        if self.res_pick.count():
            st.setValue("export/height", self.res_pick.currentData())
        if self.fps_pick.count():
            st.setValue("export/fps", self.fps_pick.currentData())
        laps_mode = self.rb_laps.isChecked()
        for w in (self.lap_list, self.rb_split, self.rb_join, self.pad_spin):
            w.setEnabled(laps_mode)
        if laps_mode and self.sender() is self.pad_spin:
            self._fill_lap_list()
        self.out_lbl.setText(str(self._out_dir()))
        self._set_render_buttons()

    def _set_render_buttons(self):
        if not hasattr(self, "btn_full"):
            return
        running = self.proc is not None
        ready = self.video_path is not None and self.telem is not None
        outs = self._planned_outputs() if ready else []
        self.btn_preview.setEnabled(ready and not running)
        self.btn_full.setEnabled(ready and not running and bool(outs))
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.btn_pause.setText("Resume" if self._paused else "Pause")
        resumable = any(engine.workdir_for(o["path"]).exists() for o in outs)
        n = len(outs)
        label = "Resume render" if resumable else "Render"
        if n > 1:
            label += f" ({n} files)"
        if running:
            label = "Rendering…"
        self.btn_full.setText(label)
        self.btn_full.setToolTip("\n".join(Path(o["path"]).name for o in outs)
                                 or "Pick at least one lap")

    def _choose_out_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Save renders to", str(self._out_dir()))
        if d:
            settings().setValue("export/dir", d)
            self._export_changed()

    def _out_next_to_video(self):
        settings().setValue("export/dir", "")
        self._export_changed()

    def _open_out_dir(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self._out_dir())))

    def _make_job(self, outputs: list[dict], preview: bool) -> engine.Job:
        a = self.telem.g_align if self.telem.g_source == "raw" else {}
        hwdec = bool(self._hw and self._hw.get("hwdecode"))
        if preview:
            fps = min(30.0, self.info.fps)
            height = min(720, self.info.height)
            quality, workers = "Small", engine.default_workers()
        else:
            fps = self.fps_pick.currentData() or None
            height = self.res_pick.currentData() or None
            quality, workers = self.q_pick.currentText(), self.workers_spin.value()
        return engine.Job(
            parts=self.parts, csv=self.csv_path, outputs=outputs,
            offset=self.offset, fps=fps, height=height,
            encoder=self.enc_pick.currentData() or "libx264", quality=quality,
            workers=workers, hwdecode=hwdec, resume=not preview,
            use_mph=self.use_mph, style=self.style_name, layout=self.layout_,
            invert_lat=self.invert_lat, invert_lon=self.invert_lon,
            g_rotate=a.get("angle"), g_flip=bool(a.get("flip", False)),
            gauge_opts=self.gauge_opts, overrides=self.overrides or None)

    def _render_preview(self):
        if not self._ready():
            return
        start = max(0.0, self._vt() - 2.0)
        end = min(self.info.duration, start + 20.0)
        cache = Path(QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.CacheLocation)) / "preview"
        cache.mkdir(parents=True, exist_ok=True)
        out = str(cache / f"{self._stem()}_preview.mp4")
        self._start_job(self._make_job([{"path": out, "ranges": [[start, end]]}],
                                       True), preview=True)

    def _render_full(self):
        if not self._ready():
            return
        if not self.synced:
            r = QtWidgets.QMessageBox.question(
                self, "Not synced",
                "You haven't synced yet, so the gauges won't match the video. "
                "Render anyway?")
            if r != QtWidgets.QMessageBox.Yes:
                return
        outs = self._planned_outputs()
        d = self._out_dir()
        if not os.access(d, os.W_OK):
            QtWidgets.QMessageBox.information(
                self, "Folder is read-only",
                f"Can't write to {d}. Pick another folder under Save to.")
            self._choose_out_dir()
            return
        clash = [o["path"] for o in outs if os.path.exists(o["path"])]
        if clash:
            r = QtWidgets.QMessageBox.question(
                self, "Replace?", "Already exists:\n"
                + "\n".join(Path(c).name for c in clash) + "\n\nReplace?")
            if r != QtWidgets.QMessageBox.Yes:
                return
        self._save(quiet=True)
        self._start_job(self._make_job(outs, False), preview=False)

    def _start_job(self, job: engine.Job, preview: bool):
        if self.proc is not None:
            return
        jf = Path(tempfile.gettempdir()) / f"raceoverlay_job_{os.getpid()}.json"
        job.save(str(jf))
        self.proc = QtCore.QProcess(self)
        env = QtCore.QProcessEnvironment.systemEnvironment()
        pp = env.value("PYTHONPATH", "")
        env.insert("PYTHONPATH", PKG_ROOT + (os.pathsep + pp if pp else ""))
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardError.connect(self._render_progress)
        self.proc.finished.connect(lambda code, _s: self._render_done(code))
        self._job_outputs = [o.path for o in job.outputs]
        self._job_preview = preview
        self._proc_buf = ""
        self._proc_err = ""
        self._paused = False
        self.progress.setValue(0)
        self.stats_lbl.setText("starting…")
        self.export_status.setText(("Preview: " if preview else "Rendering ")
                                   + ", ".join(Path(p).name for p in self._job_outputs))
        self.proc.start(sys.executable, engine_args(str(jf)))
        self._set_render_buttons()
        self.tabs.setCurrentIndex(1)

    def _render_progress(self):
        chunk = bytes(self.proc.readAllStandardError()).decode(errors="replace")
        self._proc_buf += chunk
        *lines, self._proc_buf = self._proc_buf.split("\n")
        for line in lines:
            if line.startswith("PROGRESS"):
                try:
                    _, n, total, rate, rt, eta = line.split()
                    n, total = int(n), max(1, int(total))
                    rate, rt, eta = float(rate), float(rt), float(eta)
                except ValueError:
                    continue
                self.progress.setValue(int(1000 * n / total))
                if not self._paused:
                    self.stats_lbl.setText(
                        f"{100 * n / total:5.1f}%  {rate:5.0f} fps  "
                        f"{rt:4.2f}× realtime\n{engine._fmt_eta(eta)} left")
            elif line.startswith("INFO"):
                m = re.search(r"resumed=(\d+)", line)
                if m and int(m.group(1)) > 0:
                    self.export_status.setText(
                        self.export_status.text()
                        + f"\nResuming: {m.group(1)} chunks already done.")
            elif line.startswith("STAGE joining"):
                self.progress.setValue(0)
                self.stats_lbl.setText("Joining chunks + adding audio…")
            elif line.startswith("JOIN"):
                try:
                    _, frac, i, n = line.split()
                    frac = float(frac)
                except ValueError:
                    continue
                self.progress.setValue(int(1000 * frac))
                files = f"  (file {i} of {n})" if int(n) > 1 else ""
                self.stats_lbl.setText(
                    f"Joining chunks + adding audio…  {100 * frac:3.0f}%{files}")
            elif line.startswith("ERROR"):
                self._proc_err = line[6:]
            elif line.strip():
                self._proc_err = (self._proc_err + "\n" + line)[-1500:]

    def _render_done(self, code: int):
        self.proc = None
        self._paused = False
        outs, preview = self._job_outputs, self._job_preview
        if code == 0:
            self.progress.setValue(1000)
            self.stats_lbl.setText("")
            self.export_status.setText("Done: " + ", ".join(Path(p).name for p in outs))
            if preview and outs:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(outs[0]))
        elif code in (-15, 143, 15) or self._stopping:
            self.export_status.setText(
                "Stopped. Finished chunks were kept; press Render to continue."
                if not preview else "Preview stopped.")
        else:
            tail = self._proc_err.strip().splitlines()[-3:]
            self.export_status.setText("Render failed:\n" + "\n".join(tail))
        self._stopping = False
        self._set_render_buttons()

    def _signal_job(self, sig):
        if self.proc is None:
            return
        pid = int(self.proc.processId())
        try:
            os.killpg(pid, sig)  # engine runs as its own process group
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def _pause_render(self):
        if self.proc is None:
            return
        self._paused = not self._paused
        self._signal_job(signal.SIGSTOP if self._paused else signal.SIGCONT)
        if self._paused:
            self.stats_lbl.setText("Paused")
        self._set_render_buttons()

    _stopping = False

    def _stop_render(self):
        if self.proc is not None:
            self._stopping = True
            self._signal_job(signal.SIGTERM)
            self._signal_job(signal.SIGCONT)  # a paused job must wake to exit

    def _set_status(self, text: str):
        self.status.setText(text)

    def closeEvent(self, ev):
        self.timer.stop()
        if self.proc is not None:
            r = QtWidgets.QMessageBox.question(
                self, "Render running",
                "A render is running. Stop it and quit? Finished chunks are "
                "kept, so you can resume later.")
            if r != QtWidgets.QMessageBox.Yes:
                ev.ignore()
                return
            self._stop_render()
            self.proc.waitForFinished(5000)
        self.overlay.hide()
        self.video.shutdown()
        super().closeEvent(ev)


# ---------------------------------------------------------------- main ------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="raceoverlay-sync",
        description="Line up telemetry with video, style it, render.")
    ap.add_argument("video_pos", nargs="?", metavar="VIDEO",
                    help="Video to open (lets the desktop launcher pass a file)")
    ap.add_argument("--video", help="Source video (.mp4/.mov)")
    ap.add_argument("--csv", help="Telemetry CSV (default: from saved sync)")
    args = ap.parse_args(argv)

    fmt = QtGui.QSurfaceFormat()
    fmt.setProfile(QtGui.QSurfaceFormat.CompatibilityProfile)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)

    # The native mpv window needs Qt on X11 (XWayland on Wayland desktops),
    # even when the session sets QT_QPA_PLATFORM=wayland: under Wayland mpv
    # can't embed and opens a separate top-level window instead.
    orig_qpa = os.environ.get("QT_QPA_PLATFORM")
    if preview_mode() == "native":
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    app = QtWidgets.QApplication(["raceoverlay"])
    if orig_qpa is None:  # programs we launch get the session's setting
        os.environ.pop("QT_QPA_PLATFORM", None)
    else:
        os.environ["QT_QPA_PLATFORM"] = orig_qpa
    app.setApplicationName("raceoverlay")
    from . import __version__
    app.setApplicationDisplayName(f"Race Overlay {__version__}")
    app.setDesktopFileName("raceoverlay")
    icon = Path(__file__).with_name("icon.svg")
    if icon.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon)))
    locale.setlocale(locale.LC_NUMERIC, "C")  # libmpv refuses other locales

    def fail(title: str, text: str) -> int:
        QtWidgets.QMessageBox.critical(None, title, text)
        return 1

    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        return fail("ffmpeg not found",
                    f"Missing: {', '.join(missing)}\n\nInstall it with:\n"
                    "sudo pacman -S ffmpeg")
    try:
        import mpv  # noqa: F401
    except (ImportError, OSError) as e:
        return fail("mpv not available",
                    f"{e}\n\nInstall libmpv with:\nsudo pacman -S mpv\n"
                    "and the Python binding with:\n"
                    ".venv/bin/pip install python-mpv")

    def excepthook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        sys.stderr.write(text)
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Critical,
                                    "Race Overlay error", str(value))
        box.setDetailedText(text)
        box.exec()
    sys.excepthook = excepthook

    win = MainWindow()
    win.show()
    QtCore.QTimer.singleShot(400, win.offer_menu_entry)

    def started_ok():  # the system mpv got us to a working window
        guard = os.environ.pop("RACEOVERLAY_MPV_GUARD", None)
        if guard:
            try:
                os.remove(guard)
            except OSError:
                pass
    QtCore.QTimer.singleShot(3000, started_ok)
    app.aboutToQuit.connect(started_ok)  # a quick normal quit isn't a crash
    video = args.video or args.video_pos
    if video:
        if os.path.exists(video):
            win.open_video(video)
        else:
            fail("Video not found", video)
    if args.csv and os.path.exists(args.csv):
        win.open_csv(args.csv)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
