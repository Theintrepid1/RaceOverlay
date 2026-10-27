"""Entry point for the packaged (AppImage) build.

One executable does three jobs:
    raceoverlay [VIDEO]          the app
    raceoverlay --engine JOB     the render engine (started by the app)
    raceoverlay --cli ARGS...    the command-line tool

Before anything Qt or mpv related loads it sets up the bundle:
  * child programs (ffmpeg, xdg-open, file managers) get the user's normal
    library path, not the bundle's private one;
  * the bundled ffmpeg goes first on PATH (it loads the system's libva, so
    GPU encoding uses the system's own drivers);
  * the bundled libmpv is loaded; desktop libraries it needs (PulseAudio,
    VA-API, Wayland/X11 helpers) come from the system when present and from
    the bundle's fallback copies only when missing.
"""
import configparser
import ctypes
import ctypes.util
import json
import multiprocessing
import os
import sys


def _prefer_system_mpv() -> bool:
    """Setup > Preview > 'Use the system's mpv when installed' (default on).
    RACEOVERLAY_BUNDLED_MPV=1 forces the bundled one (escape hatch)."""
    if os.environ.get("RACEOVERLAY_BUNDLED_MPV"):
        return False
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    c = configparser.ConfigParser()
    try:
        c.read(os.path.join(base, "raceoverlay", "raceoverlay.conf"))
        return c.get("preview", "use_system_mpv", fallback="true").lower() != "false"
    except (configparser.Error, OSError):
        return True


def _use_mpv(soname_or_path: str, source: str):
    real_find = ctypes.util.find_library

    def find_library(name):  # python-mpv asks for 'mpv'
        return soname_or_path if name == "mpv" else real_find(name)
    ctypes.util.find_library = find_library
    os.environ["RACEOVERLAY_MPV_SOURCE"] = source


def _setup_bundle(load_mpv: bool):
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        os.environ["LD_LIBRARY_PATH"] = orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)

    ffbin = os.path.join(base, "ffmpeg", "bin")
    if os.path.isdir(ffbin) and not os.environ.get("RACEOVERLAY_SYSTEM_FFMPEG"):
        os.environ["PATH"] = ffbin + os.pathsep + os.environ.get("PATH", "")

    if not load_mpv:  # renders and the CLI never touch the player
        return
    if _prefer_system_mpv():
        try:  # the system's own mpv (distro-built, matches its drivers)
            ctypes.CDLL("libmpv.so.2", mode=ctypes.RTLD_LOCAL)
            _use_mpv("libmpv.so.2", "system")
            return
        except OSError:
            pass  # not installed, or doesn't load next to the bundle
    libdir = os.path.join(base, "mpvlib")
    mpv_so = os.path.join(libdir, "libmpv.so.2")
    if not os.path.exists(mpv_so):
        return
    fb = os.path.join(libdir, "fallback")
    try:
        with open(os.path.join(fb, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        manifest = {"direct": [], "needs": {}}

    loaded = set()
    # Test switch: pretend the system lacks these libraries.
    force_fallback = bool(os.environ.get("RACEOVERLAY_FORCE_FALLBACK"))

    def ensure(so, depth=0):
        if so in loaded or depth > 20:
            return
        if not force_fallback:
            try:  # the system's copy, if it has one
                ctypes.CDLL(so, mode=ctypes.RTLD_LOCAL)
                loaded.add(so)
                return
            except OSError:
                pass
        path = os.path.join(fb, so)
        if not os.path.exists(path):
            return
        for dep in manifest["needs"].get(so, []):
            ensure(dep, depth + 1)
        try:
            ctypes.CDLL(path, mode=ctypes.RTLD_LOCAL)
            loaded.add(so)
        except OSError:
            pass

    for so in manifest["direct"]:
        ensure(so)
    # RTLD_LOCAL throughout: libmpv's dependencies (the system FreeType,
    # via libass) must not take over symbols of other bundled libraries,
    # e.g. Pillow's own FreeType, or text rendering breaks.
    ctypes.CDLL(mpv_so, mode=ctypes.RTLD_LOCAL)
    _use_mpv(mpv_so, "bundled")


def main():
    multiprocessing.freeze_support()  # render workers re-enter here
    args = sys.argv[1:]
    _setup_bundle(load_mpv=args[:1] not in (["--engine"], ["--cli"]))
    if args[:1] == ["--engine"]:
        from raceoverlay import engine
        return engine.main(args[1:])
    if args[:1] == ["--cli"]:
        from raceoverlay import cli
        return cli.main(args[1:])
    from raceoverlay import syncgui
    return syncgui.main(args)


if __name__ == "__main__":
    sys.exit(main())
