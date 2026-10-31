# SPDX-License-Identifier: GPL-3.0-or-later
"""Intro and end cards."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from PIL import Image  # noqa: E402

from raceoverlay import cards  # noqa: E402


def test_spec_and_dates():
    c = cards.Cards.from_dict({"intro": True, "seconds": 99, "junk": 1})
    assert c.intro and not c.end and c.any and c.seconds == 15.0
    assert cards.Cards.from_dict(None).any is False
    assert cards.fmt_date(dt.date(2026, 9, 7)) == "September 7, 2026"
    assert cards.fmt_lap(139.806) == "2:19.806"


def test_card_pictures():
    spec = cards.Cards(intro=True, end=True, title="VIR Full Course",
                       date="September 27, 2026", driver="Cam", car="BRZ #117",
                       thanks="Thanks to everyone")
    frame = Image.new("RGB", (640, 360), (200, 220, 255))
    a = cards.card_image("intro", spec, 640, 360, frame)
    b = cards.card_image("end", spec, 640, 360, frame,
                         {"best": 139.8, "best_n": 5, "laps": 5, "top": 120.4, "unit": "mph"})
    for img in (a, b):
        assert img.size == (640, 360) and img.mode == "RGB"
        # darkened background, bright text in the middle
        assert max(img.getpixel((5, 5))) < 130
        assert max(max(img.crop((200, 100, 440, 260)).getextrema()[i]) for i in range(3)) > 200
    plain = cards.card_image("intro", cards.Cards(intro=True, bg="color", color="#203040"),
                             320, 180, None)
    assert plain.getpixel((3, 3)) == (32, 48, 64)


def test_render_with_cards():
    """Cards on both ends, silence under them, footage in between."""
    from raceoverlay import demo, engine, media
    with tempfile.TemporaryDirectory() as tmp:
        csv = demo.write_demo_csv(os.path.join(tmp, "d.csv"))
        vid = demo.write_demo_video(os.path.join(tmp, "d.mp4"))
        av = os.path.join(tmp, "da.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", vid, "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000", "-shortest", "-c:v", "copy",
                        "-c:a", "aac", "-ac", "2", av], check=True)
        info = media.probe([av])
        out = os.path.join(tmp, "out.mp4")
        job = engine.Job(parts=[av], csv=csv, outputs=[{"path": out, "ranges": [[0, 6]]}],
                         fps=30, height=360, quality="Small", workers=2, resume=False,
                         cards={"intro": True, "end": True, "seconds": 2,
                                "title": "Test", "bg": "blur"})
        engine.run(job, progress=open(os.devnull, "w"))
        p = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
             "-of", "json", out], capture_output=True, text=True).stdout)["streams"]
        dur = {s["codec_type"]: float(s["duration"]) for s in p}
        assert abs(dur["video"] - 10.0) < 0.1 and abs(dur["audio"] - 10.0) < 0.15

        def level(t):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(t), "-t", "0.4", "-i", out,
                                "-vn", "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True).stderr
            return float(r.split("mean_volume:")[1].split("dB")[0])
        assert level(0.5) < -80 and level(4.5) > -40 and level(9.3) < -80
        assert info.has_audio
