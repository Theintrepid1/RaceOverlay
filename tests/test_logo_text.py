# SPDX-License-Identifier: GPL-3.0-or-later
"""Logo and text gauges on the overlay."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from PIL import Image  # noqa: E402

from raceoverlay import gauges  # noqa: E402


def _with_logo_dir(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("XDG_DATA_HOME"), os.environ.get("APPDATA")
            os.environ["XDG_DATA_HOME"] = os.environ["APPDATA"] = tmp
            try:
                fn()
            finally:
                for k, v in zip(("XDG_DATA_HOME", "APPDATA"), old):
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
    wrapper.__name__ = fn.__name__
    return wrapper


@_with_logo_dir
def test_logo_draws_at_its_aspect_with_opacity():
    d = gauges.logos_dir()
    d.mkdir(parents=True)
    Image.new("RGBA", (400, 100), (255, 0, 0, 255)).save(d / "abc.png")
    g = gauges.normalize_gauge(dict(id="c1", type="image", image="abc.png", aspect=0.25,
                                    opacity=50, x=0.1, y=0.1, scale=1.0))
    assert gauges.base_size(g) == (gauges.LOGO_WIDTH, 60)
    layout = gauges.default_layout()
    layout["custom"] = [g]
    box = gauges.layout_boxes(layout, 1920, 1080)["c1"]
    f = gauges.Frame.new(1920, 1080)
    gauges.draw_image(f, box, gauges.get_style(None), g)
    px = f.img.getpixel((int(box[0] + box[2] / 2), int(box[1] + box[3] / 2)))
    assert px[0] == 255 and 120 <= px[3] <= 135          # red, half see-through
    assert f.img.getpixel((int(box[0] + box[2] + 5), int(box[1] + 5)))[3] == 0
    # drawn partly off the frame without failing
    gauges.draw_image(f, (1850, 1050, 240, 60, 1.0), gauges.get_style(None), g)
    # missing file: nothing drawn, no error
    g2 = dict(g, image="gone.png")
    assert gauges.load_logo("gone.png") is None
    gauges.draw_image(gauges.Frame.new(100, 100), (0, 0, 50, 50, 1), gauges.get_style(None), g2)


def test_text_gauge_size_follows_text():
    short = gauges.normalize_gauge(dict(id="t", type="text", text="#117"))
    long = gauges.normalize_gauge(dict(id="t", type="text", text="Beer City Racing #117"))
    assert gauges.base_size(long)[0] > gauges.base_size(short)[0] * 2
    f = gauges.Frame.new(800, 200)
    gauges.draw_text_gauge(f, (10, 10, *gauges.base_size(long), 1.0),
                           gauges.get_style("Clean"), dict(long, color="fg"))
    assert f.img.getbbox() is not None
    assert gauges.box_label({"custom": [long]}, "t") == "Beer City Racing #117"
