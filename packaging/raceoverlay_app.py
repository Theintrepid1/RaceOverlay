# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Entry point for the packaged builds (AppImage, Windows installer).

One executable does three jobs:
    raceoverlay [VIDEO]          the app
    raceoverlay --engine JOB     the render engine (started by the app)
    raceoverlay --cli ARGS...    the command-line tool
    raceoverlay --analysis [CSV] Race Analysis (lap comparison)
    raceoverlay --selftest OUT   check the bundle (libmpv, ffmpeg, encoders),
                                 results to the file OUT; used by the CI build

Library and player setup happens earlier, in raceoverlay_boot.py (a
PyInstaller runtime hook that runs before Qt is imported); on Windows in
windows/boot_win.py.
"""
import multiprocessing
import sys


def selftest(out: str) -> int:
    """Load libmpv, run ffmpeg/ffprobe, detect encoders, find the built-in
    tracks. Writes a report to `out`; exit code 0 only if all of it works."""
    import json
    import shutil
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            lines.append(f"ok   {name}: {fn()}")
        except Exception as e:  # noqa: BLE001 - reporting every failure
            ok = False
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")

    def mpv_check():
        import mpv
        p = mpv.MPV(vo="null", ao="null")
        v = p.mpv_version
        p.terminate()
        return v
    check("libmpv", mpv_check)
    check("ffmpeg", lambda: shutil.which("ffmpeg") or 1 / 0)
    check("ffprobe", lambda: shutil.which("ffprobe") or 1 / 0)

    def enc():
        from raceoverlay import media
        return media.detect_hw(force=True)
    check("encoders", lambda: json.dumps(enc()))

    def trk():
        from racecore import tracks
        found = [p.name for p in tracks.load_all() if p.builtin]
        return ", ".join(found) if found else 1 / 0
    check("built-in tracks", trk)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0 if ok else 3


def main():
    multiprocessing.freeze_support()  # render workers re-enter here
    args = sys.argv[1:]
    if args[:1] == ["--engine"]:
        from raceoverlay import engine
        return engine.main(args[1:])
    if args[:1] == ["--cli"]:
        from raceoverlay import cli
        return cli.main(args[1:])
    if args[:1] == ["--selftest"]:
        return selftest(args[1] if len(args) > 1 else "selftest.txt")
    if args[:1] == ["--analysis"]:
        from raceanalysis import app
        return app.main(args[1:])
    from raceoverlay import syncgui
    return syncgui.main(args)


if __name__ == "__main__":
    sys.exit(main())
