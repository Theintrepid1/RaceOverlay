# SPDX-License-Identifier: GPL-3.0-or-later
"""racecore.ingest on a fake SD card in a temp folder."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from racecore import ingest  # noqa: E402
from test_gosync import _clip, _session  # noqa: E402


def _card(tmp):
    root = Path(tmp) / "card"
    d = root / "DCIM" / "100GOPRO"
    d.mkdir(parents=True)
    for name, n in [("GH010009.MP4", 3000), ("GH020009.MP4", 2000),
                    ("GH010010.MP4", 1000), ("GL010009.LRV", 10),
                    ("GOPR0001.JPG", 5), ("notes.txt", 1)]:
        (d / name).write_bytes(os.urandom(n))
    (root / "DCIM" / "101GOPRO").mkdir()
    (root / "DCIM" / "101GOPRO" / "GH030009.MP4").write_bytes(os.urandom(500))
    return str(root)


def test_card_and_clips():
    with tempfile.TemporaryDirectory() as tmp:
        root = _card(tmp)
        assert ingest.is_card(root)
        assert not ingest.is_card(tmp)
        clips = ingest.scan_clips(root, probe=False)
        by = {c.number: c for c in clips}
        assert set(by) == {"0009", "0010"}
        assert [Path(p).name for p in by["0009"].chapters] == \
            ["GH010009.MP4", "GH020009.MP4", "GH030009.MP4"]  # across folders
        assert by["0009"].size == 5500


def test_import_copies_skips_and_cancels():
    with tempfile.TemporaryDirectory() as tmp:
        root = _card(tmp)
        lib = os.path.join(tmp, "lib")
        clips = ingest.scan_clips(root, lib, probe=False)
        clip = next(c for c in clips if c.number == "0009")
        clip.recorded = dt.datetime(2026, 9, 27, 10, 14)
        assert not clip.imported

        # cancel straight away: nothing left behind, no .part file
        try:
            ingest.import_clips([clip], lib, cancelled=lambda: True)
            raise AssertionError("expected InterruptedError")
        except InterruptedError:
            pass
        day = Path(lib) / "2026-09-27"
        assert not any(day.glob("*")) if day.exists() else True

        seen = []
        out = ingest.import_clips([clip], lib, progress=lambda d, t, f: seen.append((d, t)))
        assert [Path(p).name for p in out[0]] == \
            ["GH010009.MP4", "GH020009.MP4", "GH030009.MP4"]
        assert all(Path(p).parent == day for p in out[0])
        for src, dst in zip(clip.chapters, out[0]):
            assert Path(src).read_bytes() == Path(dst).read_bytes()
        assert seen[-1] == (5500, 5500)
        assert all(Path(p).exists() for p in clip.chapters)  # card untouched

        # second import copies nothing
        mtimes = [Path(p).stat().st_mtime_ns for p in out[0]]
        ingest.import_clips([clip], lib)
        assert mtimes == [Path(p).stat().st_mtime_ns for p in out[0]]


def test_log_dates_and_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        names = ["APEX_Pro_Data_CSV_from_Virginia_Intl_Raceway_Full_on_09.27_1.2026.csv",
                 "APEX_Pro_Data_CSV_from_Virginia_Intl_Raceway_Full_on_09.27_2.2026.csv",
                 "APEX_Pro_Data_CSV_from_CMP_on_10.12_1.2026.csv", "other.csv", "x.txt"]
        for n in names:
            Path(tmp, n).write_text("Time\n0\n")
        logs = ingest.find_logs(tmp)
        assert len(logs) == 4
        info = {Path(lg.path).name: lg for lg in logs}
        lg = info[names[1]]
        assert lg.date == dt.date(2026, 9, 27) and lg.session == 2
        clip = ingest.Clip("0009", "GH", ["x"], 0, dt.datetime(2026, 9, 27, 10, 0))
        # other.csv has no date in its name, so it's always worth trying
        assert {Path(c.path).name for c in ingest.candidates(clip, logs)} == \
            set(names[:2]) | {"other.csv"}
        clip.recorded = None
        assert len(ingest.candidates(clip, logs)) == 4


def test_pair_picks_the_log_that_syncs():
    right, wrong = _session(seed=1), _session(seed=7)
    t, acc = _clip(right, offset=222.0, seconds=300)
    tels = {"a.csv": wrong, "b.csv": right}
    logs = [ingest.Log("a.csv", dt.date(2026, 9, 27)), ingest.Log("b.csv", dt.date(2026, 9, 27))]
    clip = ingest.Clip("0009", "GH", ["x"], 0, dt.datetime(2026, 9, 27, 10, 0))
    p = ingest.pair(clip, [], logs, load=tels.__getitem__, accel=(t, acc))
    assert p.log is not None and p.log.path == "b.csv", p
    assert p.confident and abs(p.offset - 222.0) < 0.1
    assert len(p.tried) == 2

    # only the wrong log available: no pairing claimed
    p = ingest.pair(clip, [], logs[:1], load=tels.__getitem__, accel=(t, acc))
    assert p.log is None and not p.confident


if __name__ == "__main__":
    np.seterr(all="ignore")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_camera_clock_fix():
    """The HERO's clock reset to 2016: shift every clip by one amount,
    file them under the right day, and remember it for the Day tab."""
    from racecore import day, gpmf
    with tempfile.TemporaryDirectory() as tmp:
        root = _card(tmp)
        lib = os.path.join(tmp, "lib")
        clips = ingest.scan_clips(root, lib, probe=False)
        cam = [dt.datetime(2016, 1, 1, 0, 12), dt.datetime(2016, 1, 1, 0, 41)]
        for c, t in zip(clips, cam):
            c.recorded = t
        assert ingest.clock_looks_wrong(clips, today=dt.date(2026, 9, 28))
        ok = [ingest.Clip("1", "GH", ["x"], 0, dt.datetime(2026, 9, 27, 10, 0))]
        assert not ingest.clock_looks_wrong(ok, today=dt.date(2026, 9, 28))

        # GPS: clip 0 has no fix, clip 1 does (time in UTC; camera is local)
        real1 = dt.datetime(2026, 9, 27, 10, 42, 5)
        utc = dt.timezone.utc

        def fake_gps(path, max_s=None):
            if path == clips[1].chapters[0]:
                return 30.0, (real1 + dt.timedelta(seconds=30)).replace(tzinfo=utc)
            return None
        real = gpmf.gps_time
        gpmf.gps_time = fake_gps
        try:
            secs, src = ingest.gps_clock_fix(sorted(clips, key=lambda c: c.recorded),
                                             local_tz=utc)
        finally:
            gpmf.gps_time = real
        assert src is clips[1]
        assert abs(secs - (real1 - cam[1]).total_seconds()) < 1e-6

        ingest.fix_clock(clips, secs)
        assert clips[1].recorded == real1
        assert clips[1].recorded - clips[0].recorded == cam[1] - cam[0]   # gaps kept
        ingest.fix_clock(clips, secs)                                      # not twice
        assert clips[1].recorded == real1

        out = ingest.import_clips(clips, lib)
        for c, paths in zip(clips, out):
            assert all(Path(p).parent.name == "2026-09-27" for p in paths)
            assert abs(Path(paths[0]).stat().st_mtime - c.recorded.timestamp()) < 2
            rec, fix = ingest.recorded_for(paths)
            assert rec == c.recorded.replace(microsecond=0) and abs(fix - secs) < 0.1
        # the Day tab's start time uses the corrected time (no GPS in these files)
        start, src_kind, _ = day.find_start((out[1], 5.0), 36.5, -79.2)
        assert src_kind == "fixed" and start == "2026-09-27 10:42"   # 10:42:05 - 5 s
        # scanning again (camera times, 2016) still sees them as imported
        again = ingest.scan_clips(root, lib, probe=False)
        assert all(c.imported for c in again if c.name in {x.name for x in clips})
