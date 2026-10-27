"""Bundle setup for the packaged (AppImage) build.

Runs as a PyInstaller runtime hook: before PyInstaller's own hooks import
Qt, so the system mpv (when used) loads before any bundled library can
claim its dependencies' names.

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
import struct
import subprocess
import sys


def _prefer_system_mpv() -> bool:
    """Opt-in only (RACEOVERLAY_SYSTEM_MPV=1). The built-in player is the
    tested default: mixing a distro libmpv's libraries with the bundle's
    X11/Qt libraries crashed at startup on CachyOS (v0.9.6)."""
    return os.environ.get("RACEOVERLAY_SYSTEM_MPV") == "1"


# ---------------------------------------------------------------- system mpv
# The bundle's launcher puts its own library folder (Ubuntu 24.04 copies of
# glib, systemd, xkbcommon, zstd, ...) first on the loader's search path, and
# that can't be undone once the process is running.  Loading the system's
# libmpv by name would pull those older copies in as its dependencies and
# fail on a newer distro.  So: ask the system's own loader (clean env) where
# every dependency lives, then load them leaves-first by absolute path.

def _elf_needed(path):
    """DT_NEEDED sonames of a 64-bit little-endian ELF (enough for x86-64)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return []
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    secs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
            for i in range(shnum)]
    out = []
    for sh in secs:
        if sh[1] != 6:  # SHT_DYNAMIC
            continue
        strtab = secs[sh[6]]
        stroff = strtab[4]
        for off in range(sh[4], sh[4] + sh[5], 16):
            tag, val = struct.unpack_from("<qQ", data, off)
            if tag == 0:
                break
            if tag == 1:  # DT_NEEDED
                end = data.index(b"\0", stroff + val)
                out.append(data[stroff + val:end].decode())
    return out


def _clean_env():
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    return env


def _system_libmpv():
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True,
                             env=_clean_env(), timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
        for d in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/lib64"):
            if os.path.exists(os.path.join(d, "libmpv.so.2")):
                return os.path.join(d, "libmpv.so.2")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("libmpv.so.2 ") and "x86-64" in line and "=>" in line:
            return line.split("=>", 1)[1].strip()
    return None


def _ldd_map(path):
    """soname -> absolute path, as the system resolves them with no bundle."""
    try:
        out = subprocess.run(["ldd", path], capture_output=True, text=True,
                             env=_clean_env(), timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(f"ldd failed: {e}")
    m, missing = {}, []
    for line in out.splitlines():
        if "=>" not in line:
            continue
        name, rest = (x.strip() for x in line.split("=>", 1))
        if rest.startswith("not found"):
            missing.append(name)
        elif rest.startswith("/"):
            m[name] = rest.split(" (")[0].strip()
    if missing:
        raise OSError("system mpv is missing " + ", ".join(missing))
    return m


def _load_system_mpv():
    """Returns the loaded libmpv path, or raises OSError with the reason."""
    mpv = _system_libmpv()
    if not mpv:
        raise OSError("mpv isn't installed")
    paths = _ldd_map(mpv)
    skip = ("libc.so", "libm.so", "libdl.so", "libpthread.so", "librt.so",
            "ld-linux", "libgcc_s.so", "libstdc++.so")
    done = set()

    def load(path, depth=0):
        if path in done or depth > 40:
            return
        done.add(path)
        for so in _elf_needed(path):
            dep = paths.get(so)
            if dep and not so.startswith(skip):
                load(dep, depth + 1)
        ctypes.CDLL(path, mode=ctypes.RTLD_LOCAL)

    load(mpv)
    return mpv


def _guard_file():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "raceoverlay", "system-mpv-starting")


def _disable_system_mpv():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    conf = os.path.join(base, "raceoverlay", "raceoverlay.conf")
    c = configparser.ConfigParser()
    c.optionxform = str
    try:
        c.read(conf)
        if not c.has_section("preview"):
            c.add_section("preview")
        c.set("preview", "use_system_mpv", "false")
        os.makedirs(os.path.dirname(conf), exist_ok=True)
        with open(conf, "w") as fh:
            c.write(fh)
    except (OSError, configparser.Error):
        pass


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
        guard = _guard_file()
        if os.path.exists(guard):
            # The last launch with the system mpv never reached the window.
            os.environ["RACEOVERLAY_MPV_NOTE"] = (
                "system mpv skipped: the last start with it didn't finish")
            try:
                os.remove(guard)
            except OSError:
                pass
        else:
            try:  # the system's own mpv (distro-built, matches its drivers)
                path = _load_system_mpv()
                try:
                    os.makedirs(os.path.dirname(guard), exist_ok=True)
                    open(guard, "w").close()  # syncgui removes it once shown
                    os.environ["RACEOVERLAY_MPV_GUARD"] = guard
                except OSError:
                    pass
                _use_mpv(path, "system")
                return
            except Exception as e:  # not installed, or won't load
                os.environ["RACEOVERLAY_MPV_NOTE"] = f"system mpv not used: {e}"
                print("raceoverlay:", os.environ["RACEOVERLAY_MPV_NOTE"],
                      file=sys.stderr)
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



def _boot():
    args = sys.argv[1:]
    skip_mpv = args[:1] in (["--engine"], ["--cli"]) or any(
        a.startswith("--multiprocessing") for a in args)
    _setup_bundle(load_mpv=not skip_mpv)


_boot()
