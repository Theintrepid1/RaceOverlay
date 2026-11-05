# SPDX-License-Identifier: GPL-3.0-or-later
"""Picture-in-picture: the second camera lands in its box, lined up by its
offset, only while it has pictures, with rounded corners."""
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from raceoverlay import gauges, media, pip  # noqa: E402

RED, BLUE = (230, 20, 20), (20, 40, 230)


def _clip(path, secs, color, fps=30):
    """A solid-colour clip with a white bar that slides across once a
    second (so the frame shows which second it's from)."""
    c = "0x%02x%02x%02x" % color
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c={c}:s=320x180:r={fps}:d={secs}", "-f", "lavfi", "-i",
                    f"color=c=white:s=20x20:r={fps}:d={secs}", "-filter_complex",
                    "[0:v][1:v]overlay=x='mod(t,1)*300':y=80",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path], check=True)
    return path


def test_source_and_gauge_settings():
    assert pip.PipSource.from_dict(None) is None
    assert pip.PipSource.from_dict({"parts": []}) is None
    s = pip.PipSource.from_dict({"parts": ["a.mp4"], "offset": "2.5"})
    assert s.offset == 2.5 and not s.synced and s.to_dict()["parts"] == ["a.mp4"]
    g = gauges.normalize_gauge({"id": "p", "type": "pip", "crop": [0.1, 2, "x"],
                                "radius": -5, "border": 99, "aspect": 0.5})
    assert g["crop"] == [0.0, 0.0, 0.0, 0.0] or g["crop"][0] == 0.1
    assert g["radius"] == 0 and g["border"] == 30
    assert gauges.base_size(g) == (480, 240)
    lay = gauges.normalize_layout({"custom": [g, dict(g, id="q")]})
    assert pip.gauge_of(lay)["id"] == "p"


def test_span_mask_and_aspect():
    info = media.MediaInfo([], 1920, 1080, 30.0, 100.0, False)
    # PiP time = main time + 5, telemetry = main time + 2
    assert pip.span(info, 5.0, 2.0) == (-3.0, 97.0)
    g = {"crop": [0.25, 0, 0.25, 0]}
    assert abs(pip.aspect_for(info, g) - 1080 / 960) < 1e-6
    m = pip.mask(100, 60, 20)
    assert m.getpixel((0, 0)) < 40 and m.getpixel((50, 30)) == 255


def test_export_puts_the_second_camera_in_its_box():
    from raceoverlay import demo, engine
    with tempfile.TemporaryDirectory() as tmp:
        csv = demo.write_demo_csv(os.path.join(tmp, "d.csv"))
        main = _clip(os.path.join(tmp, "main.mp4"), 6, RED)
        second = _clip(os.path.join(tmp, "cam2.mp4"), 3, BLUE)
        lay = gauges.default_layout()
        for g in lay["custom"]:
            g["on"] = False
        for n in gauges.WIDGETS:
            lay[n]["on"] = False
        lay["custom"].append(gauges.normalize_gauge(
            {"id": "pip", "type": "pip", "x": 0.5, "y": 0.5, "scale": 0.5,
             "aspect": 0.5625, "radius": 90, "border": 0}))   # 15 px here
        out = os.path.join(tmp, "out.mp4")
        # second camera starts 2 s into the main video and runs 3 s
        job = engine.Job(parts=[main], csv=csv, outputs=[{"path": out, "ranges": [[0, 6]]}],
                         fps=30, height=360, quality="Small", workers=2, resume=False,
                         layout=lay, pip={"parts": [second], "offset": -2.0})
        engine.run(job, progress=open(os.devnull, "w"))

        def frame(t):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", out,
                                  "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                  "-"], capture_output=True, check=True).stdout
            return Image.frombytes("RGB", (640, 360), raw)
        # box: x 320, y 180, 480*0.5*(360/1080) = 80 wide, 45 tall
        inside, corner, outside = (360, 202), (322, 182), (300, 150)

        def near(px, col):
            return all(abs(a - b) < 60 for a, b in zip(px, col))
        before, during, after = frame(1.0), frame(3.4), frame(5.6)
        assert near(before.getpixel(inside), RED)          # not started yet
        assert near(during.getpixel(inside), BLUE)         # second camera
        assert near(during.getpixel(outside), RED)
        assert near(during.getpixel(corner), RED)           # rounded corner
        assert near(after.getpixel(inside), RED)           # finished: no frozen frame
        # lined up: at main 3.4 s the second camera is at 1.4 s, so its bar is
        # 40% of the way across (it moves once a second)
        box = np.asarray(during.crop((320, 180, 400, 225)).convert("L"), float)
        col = int(np.argmax(box[20:24].mean(axis=0)))
        assert abs(col / 80 - (0.4 * 300 + 10) / 320) < 0.04


def test_preview_reader_gives_frames():
    with tempfile.TemporaryDirectory() as tmp:
        p = _clip(os.path.join(tmp, "cam2.mp4"), 3, BLUE)
        info = media.probe([p])
        r = pip.PipReader(info, width=160)
        try:
            img = None
            for _ in range(200):
                img = r.frame_at(1.0)
                if img is not None:
                    break
                import time
                time.sleep(0.02)
            assert img is not None and img.size == (160, 90)
            assert near_blue(img.getpixel((10, 10)))
            assert r.frame_at(-1.0) is None and r.frame_at(99.0) is None
        finally:
            r.close()


def near_blue(px):
    return all(abs(a - b) < 60 for a, b in zip(px, BLUE))
