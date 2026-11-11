# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""raceoverlay — burn racing telemetry gauges onto video."""
from . import gauges, render, demo, media, engine  # noqa: F401

__version__ = "1.5.0"


def build_label() -> str:
    """"PR 28 · 1c9645c" for a test build, "" for a release. Builds bundle
    it as build_label.txt (packaging/buildlabel.py);
    RACEOVERLAY_BUILD_LABEL overrides it (running from source)."""
    import os
    from pathlib import Path
    env = os.environ.get("RACEOVERLAY_BUILD_LABEL")
    if env is not None:
        return env.strip()
    try:
        return Path(__file__).with_name("build_label.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def version_text() -> str:
    """"1.4.0", or "1.4.0 (test build: PR 28 · 1c9645c)"."""
    lab = build_label()
    return f"{__version__} (test build: {lab})" if lab else __version__


def titled(title: str, app: str) -> str:
    """A window title with the test-build label on it, so a test build can't
    be mistaken for the release (Windows shows only the window's own title;
    Linux desktops add "— <app> <version>" after it)."""
    lab = build_label()
    if not lab:
        return title
    return f"{title or app} — test build: {lab}"
