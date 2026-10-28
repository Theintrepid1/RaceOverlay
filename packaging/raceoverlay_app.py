# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Entry point for the packaged (AppImage) build.

One executable does three jobs:
    raceoverlay [VIDEO]          the app
    raceoverlay --engine JOB     the render engine (started by the app)
    raceoverlay --cli ARGS...    the command-line tool

Library and player setup happens earlier, in raceoverlay_boot.py (a
PyInstaller runtime hook that runs before Qt is imported).
"""
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()  # render workers re-enter here
    args = sys.argv[1:]
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
