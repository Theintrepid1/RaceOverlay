# SPDX-License-Identifier: GPL-3.0-or-later
"""Test builds say so in the title bar; releases don't."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packaging"))
import buildlabel  # noqa: E402
import raceoverlay  # noqa: E402


def test_label_in_titles(monkeypatch):
    monkeypatch.setenv("RACEOVERLAY_BUILD_LABEL", "PR 28 · 1c9645c")
    assert raceoverlay.version_text() == f"{raceoverlay.__version__} (test build: PR 28 · 1c9645c)"
    assert raceoverlay.titled("lap.mp4", "Race Overlay") == "lap.mp4 — test build: PR 28 · 1c9645c"
    assert raceoverlay.titled("", "Race Overlay") == "Race Overlay — test build: PR 28 · 1c9645c"
    monkeypatch.setenv("RACEOVERLAY_BUILD_LABEL", "")
    assert raceoverlay.version_text() == raceoverlay.__version__
    assert raceoverlay.titled("lap.mp4", "Race Overlay") == "lap.mp4"


def test_build_script_label(tmp_path, monkeypatch):
    monkeypatch.setattr(buildlabel, "_git", lambda *a: {"--short": "abc1234"}.get(a[1], "main")
                        if a[0] == "rev-parse" else "")
    assert buildlabel.label() == ""                        # main: a release
    assert buildlabel.label("PR 7") == "PR 7 · abc1234"
    monkeypatch.setattr(buildlabel, "_git", lambda *a: "abc1234" if "--short" in a else "vbo")
    assert buildlabel.label() == "vbo · abc1234"           # any other branch
    f, text = buildlabel.write(tmp_path, "PR 9")
    assert f.read_text(encoding="utf-8").strip() == "PR 9 · abc1234" == text
