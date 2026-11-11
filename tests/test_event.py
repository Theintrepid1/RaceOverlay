# SPDX-License-Identifier: GPL-3.0-or-later
"""Opening a hand-organised event folder: each GoPro recording finds its
data file, one camera is the usual case, a second camera (running at the
same time) is found or added by hand, front and rear are told apart by how
they're mounted, copies and exports are left out, and the result opens
like an import."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import day, event, ingest  # noqa: E402
from test_gosync import _clip, _session  # noqa: E402

APEX = Path(__file__).parent / "data" / "Virginia_Intl_Raceway_Full__812128378.csv"


def _event(tmp: Path, rear_name="GH010002.MP4", front_yaw=10.0, rear=True):
    """Two sessions, each with a front and a rear GoPro (the rear one turned
    round on its mount), plus an export and an edit list. Camera data is
    faked from the data files' g (no real video needed)."""
    tels, accel = {}, {}
    for i, name in enumerate(("Saturday Scored 1", "Sunday Scored 2")):
        d = tmp / name
        d.mkdir()
        tel = _session(seconds=900.0, seed=10 + i)
        data = d / f"session{i + 1}.csv"
        data.write_text(f"data for {name}")
        tels[str(data)] = tel
        cams = [("GH010001.MP4", front_yaw, 200.0 + i)]
        if rear:
            cams.append((rear_name, front_yaw + 180.0, 230.0 + i))
        for cam, yaw, off in cams:
            v = d / cam
            v.write_bytes(f"{name}/{cam}".encode())
            accel[str(v)] = _clip(tel, off, 300, yaw_deg=yaw, seed=20 + i)
        (d / "export with overlay.mp4").write_bytes(f"rendered {name}".encode())
        (d / "GH010001-llc-edl.csv").write_text(f"cut {name}")
    return tels, accel


def _scan(tmp, tels, accel):
    def load(p):
        if p in tels:
            return tels[p]
        raise ValueError("not data")
    sc = event.scan(str(tmp), load=load)
    for v in sc.videos:                       # the fake GoPro files "have sensors"
        v.has_sensors = v.parts[0] in accel
    return sc


def test_front_and_rear_found_and_exports_skipped():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _event(tmp)
        sc = _scan(tmp, tels, accel)
        assert len(sc.data) == 2
        assert any("llc-edl" in p and why == "not a data file" for p, why in sc.skipped)
        sessions = event.match(sc, read=lambda v: accel[v.parts[0]])
        assert [s.title for s in sessions] == ["Saturday Scored 1", "Sunday Scored 2"]
        for s in sessions:
            assert s.main is not None and s.second is not None and not s.extra
            assert s.main.data is s.data and s.second.data is s.data
            # the two cameras see the car's forward the opposite way round (not
            # exactly: this fake session brakes far more than it accelerates,
            # which tilts each camera's gravity estimate)
            assert event._dot(s.main.forward, s.second.forward) < -0.3
        fronts = {s.main.video.name for s in sessions}
        rears = {s.second.video.name for s in sessions}
        assert len(fronts) == 1 and len(rears) == 1 and fronts != rears   # consistent
        s = sessions[0]
        assert abs(s.main.offset - 200.0) < 0.15 and abs(s.second.offset - 230.0) < 0.15
        clips = [c for x in sessions for c in (x.main, x.second)]
        assert event.facing_clarity(clips) > 0.3


def test_names_decide_which_camera_is_the_front():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _event(tmp, rear_name="REAR GH010002.MP4")
        sessions = event.match(_scan(tmp, tels, accel), read=lambda v: accel[v.parts[0]])
        assert all(s.second.video.name.startswith("REAR") for s in sessions)
        event.swap_all(sessions)                       # one click for the whole event
        assert all(s.main.video.name.startswith("REAR") for s in sessions)


def test_apply_saves_sidecars_with_the_rear_camera_and_names():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _event(tmp)
        sessions = event.match(_scan(tmp, tels, accel), read=lambda v: accel[v.parts[0]])
        sessions[1].use = False
        items = event.apply(sessions)
        assert len(items) == 1
        csv, parts, off = items[0]
        s = sessions[0]
        sc = json.loads(ingest.sidecar_path(parts).read_text())
        assert sc["csv"] == str(Path(csv).resolve()) and sc["synced"]
        pip = sc["pip"]
        assert pip["parts"] == [str(Path(p).resolve()) for p in s.second.video.parts]
        # rear time = front time + (front offset - rear offset)
        assert abs(pip["offset"] - (s.main.offset - s.second.offset)) < 1e-3
        assert day.Notes.load(csv).title == "Saturday Scored 1"
        # Open track day now finds it inside its session folder
        found = ingest.synced_sessions(str(tmp))
        assert [Path(c).name for c, _p, _o in found] == ["session1.csv"]


def test_copies_are_skipped():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _event(tmp)
        (tmp / "Exports").mkdir()
        shutil.copy(APEX, tmp / "a.csv")
        shutil.copy(APEX, tmp / "Exports" / "a.csv")
        (tmp / "2016-01-14").mkdir()          # an old import's date folder
        shutil.copy(tmp / "Saturday Scored 1" / "GH010001.MP4",
                    tmp / "2016-01-14" / "GH010001.MP4")
        from racecore import telemetry
        sc = event.scan(str(tmp), load=lambda p: tels[p] if p in tels else telemetry.load(p))
        assert sum(Path(d.path).name == "a.csv" for d in sc.data) == 1
        whys = {why for _p, why in sc.skipped}
        assert "a copy of another data file" in whys and "a copy of another video" in whys
        # the copy in the named session folder is the one kept
        kept = [v.parts[0] for v in sc.videos if v.name == "GH010001.MP4"]
        assert not any("2016-01-14" in k for k in kept)


def test_unmatched_clip_left_out():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _event(tmp)
        stray = tmp / "GH019999.MP4"
        stray.write_bytes(b"stray")
        accel[str(stray)] = _clip(_session(seed=99), 100.0, 300)
        sc = _scan(tmp, tels, accel)
        sessions = event.match(sc, read=lambda v: accel[v.parts[0]])
        used = {c.video.parts[0] for s in sessions for c in (s.main, s.second) if c}
        assert str(stray) not in used
        v = next(v for v in sc.videos if v.parts[0] == str(stray))
        assert v.note == "didn't match any data file"


@pytest.mark.parametrize("name,front", [("Front cam.MP4", True), ("rear_cam.mp4", False),
                                        ("GH010001.MP4", None)])
def test_name_hints(name, front):
    f = bool(event.FRONT_WORDS.search(name))
    r = bool(event.REAR_WORDS.search(name))
    assert (f, r) == ((True, False) if front else (False, True) if front is False
                      else (False, False))


def _one_camera(tmp):
    tels, accel = _event(tmp, rear=False)
    return tels, accel


def test_one_camera_is_the_usual_case():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _one_camera(tmp)
        sessions = event.match(_scan(tmp, tels, accel), read=lambda v: accel[v.parts[0]])
        for s in sessions:
            assert s.main is not None and s.main.video.name == "GH010001.MP4"
            assert s.second is None and not s.extra
        items = event.apply(sessions)
        sc = json.loads(ingest.sidecar_path(items[0][1]).read_text())
        assert "pip" not in sc


def test_camera_stopped_and_started_is_not_a_second_camera():
    """Two recordings one after the other (recording stopped in the pits):
    the same camera, so no second camera; the best one is the video."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _one_camera(tmp)
        d = tmp / "Saturday Scored 1"
        later = d / "GH010003.MP4"
        later.write_bytes(b"after the pit stop")
        tel = tels[str(d / "session1.csv")]
        accel[str(later)] = _clip(tel, 560.0, 300, yaw_deg=10.0, seed=31)
        sessions = event.match(_scan(tmp, tels, accel), read=lambda v: accel[v.parts[0]])
        s = sessions[0]
        assert s.second is None
        assert {s.main.video.name} | {c.video.name for c in s.extra} == \
            {"GH010001.MP4", "GH010003.MP4"}
        assert event.overlap(s.main, s.extra[0]) == 0


def test_second_camera_added_by_hand():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _one_camera(tmp)
        sessions = event.match(_scan(tmp, tels, accel), read=lambda v: accel[v.parts[0]])
        s = sessions[0]
        tel = tels[s.data.path]
        cam2 = _clip(tel, 215.0, 240, yaw_deg=190.0, seed=41)
        c = event.add_second(s, ["/x/rear.MP4"], read=lambda parts: cam2)
        assert s.second is c and abs(c.offset - 215.0) < 0.15
        # a video from another session is refused
        other = _clip(_session(seed=77), 100.0, 240, seed=42)
        with pytest.raises(ValueError):
            event.add_second(sessions[1], ["/x/other.MP4"], read=lambda parts: other)
        assert sessions[1].second is None
        items = event.apply([s])
        sc = json.loads(ingest.sidecar_path(items[0][1]).read_text())
        assert sc["pip"]["parts"] == [str(Path("/x/rear.MP4").resolve())]


def test_choose_video_and_sync_by_hand():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tels, accel = _one_camera(tmp)
        sessions = event.match(_scan(tmp, tels, accel), read=lambda v: accel[v.parts[0]])
        s = sessions[0]
        old = s.main
        # picking a video that syncs from its sensors
        better = _clip(tels[s.data.path], 400.0, 200, yaw_deg=10.0, seed=51)
        c = event.sync_clip(s, ["/x/picked.MP4"], read=lambda parts: better)
        event.set_video(s, c)
        assert s.main is c and abs(c.offset - 400.0) < 0.15 and old in s.extra
        # one that can't be synced (a phone video): lined up by hand
        m = event.manual_clip(s, ["/x/phone.mp4"], 123.4, 300.0)
        event.set_video(s, m)
        assert s.main is m and m.score == 0 and m.video.note == "synced by hand"
        assert m.span == (123.4, 423.4)
        items = event.apply([s])
        assert items == [(s.data.path, ["/x/phone.mp4"], 123.4)]
