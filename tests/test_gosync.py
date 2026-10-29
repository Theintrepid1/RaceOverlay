# SPDX-License-Identifier: GPL-3.0-or-later
"""racecore.gosync on synthetic data: a fake logger session, and a fake GoPro
clip cut from it at a known offset, tilted on its mount, with gravity and
vibration added. Run: PYTHONPATH=src python3 -m pytest tests/  (or plain
python3 tests/test_gosync.py)."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import gosync  # noqa: E402
from racecore.telemetry import Telemetry  # noqa: E402

G = 9.80665


def _session(seconds=900.0, seed=1):
    """Logger at 10 Hz with corner/brake-like g events."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 0.1)
    lon = np.zeros_like(t)
    lat = np.zeros_like(t)
    for c in rng.uniform(0, seconds, 400):
        w = rng.uniform(0.8, 3.0)
        bump = np.exp(-0.5 * ((t - c) / w) ** 2)
        lat += rng.uniform(-1.0, 1.0) * bump
        lon += rng.uniform(-0.8, 0.3) * bump
    tel = Telemetry(time=t, channels={"lon_g": lon, "lat_g": lat})
    return tel


def _clip(tel, offset, seconds, yaw_deg=37.0, tilt_deg=20.0, seed=2):
    """200 Hz camera accel (m/s^2) for logger time offset..offset+seconds."""
    rng = np.random.default_rng(seed)
    ct = np.arange(0, seconds, 0.005)
    lon = np.interp(ct + offset, tel.time, tel.channels["lon_g"])
    lat = np.interp(ct + offset, tel.time, tel.channels["lat_g"])
    car = np.column_stack([lon, lat, -np.ones_like(lon)])  # x fwd, y right, z down(-1 g)
    y = np.radians(yaw_deg)
    rz = np.array([[np.cos(y), -np.sin(y), 0], [np.sin(y), np.cos(y), 0], [0, 0, 1]])
    p = np.radians(tilt_deg)
    ry = np.array([[np.cos(p), 0, np.sin(p)], [0, 1, 0], [-np.sin(p), 0, np.cos(p)]])
    cam = car @ (ry @ rz).T
    cam += rng.normal(0, 0.15, cam.shape)  # vibration
    return ct, cam * G


def test_finds_known_offset():
    tel = _session()
    ct, acc = _clip(tel, offset=312.4, seconds=300)
    res, _, car, info = gosync.sync_streams(ct, acc, tel)
    assert abs(res.offset - 312.4) < 0.1, res  # one 10 Hz logger sample
    assert res.confident, res
    assert info["lon_fit"] > 0.9 and info["lat_fit"] > 0.9, info


def test_rejects_unrelated_session():
    tel = _session(seed=1)
    other = _session(seed=99)
    ct, acc = _clip(other, offset=200.0, seconds=300)
    res, *_ = gosync.sync_streams(ct, acc, tel)
    assert not res.confident, res


def test_mount_angle_does_not_matter():
    tel = _session()
    for yaw in (0.0, 90.0, 180.0, -135.0):
        ct, acc = _clip(tel, offset=150.0, seconds=240, yaw_deg=yaw)
        res, *_ = gosync.sync_streams(ct, acc, tel)
        assert abs(res.offset - 150.0) < 0.1, (yaw, res)


def test_clip_longer_than_session():
    tel = _session(seconds=100)
    long_tel = _session(seconds=400)
    ct, acc = _clip(long_tel, offset=0.0, seconds=300)
    res = gosync.find_offset(ct, gosync.camera_horizontal(acc), tel)
    assert not res.confident


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
