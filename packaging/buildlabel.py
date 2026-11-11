# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-build label for the AppImage and Windows builds.

A build gets a label when it isn't a release: given with --label (e.g.
"PR 28"), or worked out from git when built from any branch but main. The
commit is added either way. The label is written to build_label.txt and
bundled next to raceoverlay/__init__.py; the apps show it in their title
bars (raceoverlay.build_label()). Release builds (main, or a release tag
checked out in CI) get none.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def label(given: str | None = None) -> str:
    sha = _git("rev-parse", "--short", "HEAD")
    if not given:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch in ("", "HEAD", "main", "master") or branch.startswith("release"):
            return ""                     # a release (release-1.5: the version bump)
        given = branch
    return f"{given} · {sha}" if sha else given


def write(folder: Path, given: str | None = None) -> tuple[Path, str]:
    """build_label.txt in folder (empty for a release). Returns (file, label)."""
    text = label(given)
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "build_label.txt"
    f.write_text(text + "\n", encoding="utf-8")
    return f, text
