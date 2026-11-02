# SPDX-License-Identifier: GPL-3.0-or-later
"""Export file names, and where the render's work files go."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from raceoverlay import engine, media, naming  # noqa: E402


def test_names():
    b = naming.base(dt.date(2026, 9, 27), "VIR Full Course", 1, "GH010123")
    assert b == "2026-09-27 VIR S1"
    assert naming.whole(b) == "2026-09-27 VIR S1.mp4"
    assert naming.lap(b, 5, 139.806) == "2026-09-27 VIR S1 lap5 2'19.806.mp4"
    assert naming.laps(b, [6, 2, 3, 4]) == "2026-09-27 VIR S1 laps 2-4,6.mp4"
    assert naming.laps(b, [2, 4, 6]) == "2026-09-27 VIR S1 laps 2,4,6.mp4"
    assert naming.laps(b, [3]) == "2026-09-27 VIR S1 lap 3.mp4"
    assert naming.short_track("Carolina Motorsports Park") == "Carolina Motorsports Park"
    assert naming.fmt_laptime(59.9996) == "1'00.000"
    # nothing known: the video's name stands in
    assert naming.base(None, "", None, "GH010123") == "GH010123"
    assert naming.base(None, "", 2, "GH010123") == "GH010123 S2"
    assert "/" not in naming.safe("x/y") and ":" not in naming.safe("x:y")


def test_work_folder_is_local_unless_short_of_space():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cache")
        src = Path(tmp, "src.mp4")
        src.write_bytes(b"x" * 1000)
        part = media.Part(str(src), 0.0, 100.0, 1920, 1080, 60.0, "h264", True)
        info = media.MediaInfo(parts=[part], width=1920, height=1080, fps=60.0,
                               duration=100.0, has_audio=True)
        out = engine.Output(os.path.join(tmp, "nas", "out.mp4"), [[0, 100]])
        local = engine.choose_workdir(out, info)
        assert local == engine._local_workdir(out.path)
        real = engine._free
        engine._free = lambda p: 10          # no room here: next to the output
        try:
            assert engine.choose_workdir(out, info) == engine._legacy_workdir(out.path)
            engine._legacy_workdir(out.path).mkdir(parents=True)   # an old render to resume
            engine._free = real
            assert engine.choose_workdir(out, info) == engine._legacy_workdir(out.path)
            assert engine.workdir_for(out.path) == engine._legacy_workdir(out.path)
        finally:
            engine._free = real
        os.environ.pop("XDG_CACHE_HOME")


def test_cards_and_faststart_dont_throw_away_rendered_chunks():
    j = dict(parts=["a.mp4"], csv="a.csv", outputs=[{"path": "o.mp4", "ranges": [[0, 1]]}])
    a = engine.Job(**j)
    b = engine.Job(**j, cards={"intro": True}, faststart=False)
    assert a.fingerprint(a.outputs[0]) == b.fingerprint(b.outputs[0])
