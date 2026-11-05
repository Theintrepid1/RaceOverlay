# SPDX-License-Identifier: GPL-3.0-or-later
"""Delta bar and dash gauges: settings survive a save, values come from the
data that's there, and drawing never fails on missing data."""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore.telemetry import Telemetry  # noqa: E402
from raceoverlay import gauges, render  # noqa: E402

LAT0, LON0 = 36.56, -79.20


def _tel(obd: bool, period=60.0, laps_n=4):
    t = np.arange(0.0, period * laps_n + 5.0, 0.1)
    ang = 2 * np.pi * (t / period) - np.pi / 2
    r = 300.0
    x, y = r * np.cos(ang), r * np.sin(ang)
    lap = np.floor((ang + np.pi / 2) / (2 * np.pi) + 0.25)
    v = 2 * np.pi * r / period * 3.6 + 20 * np.sin(t / 3)          # km/h
    ch = {"speed": v, "lap": lap, "gps_lat": LAT0 + y / 110540.0,
          "gps_lon": LON0 + x / (np.cos(np.radians(LAT0)) * 111320.0),
          "lat_g": 0.8 * np.sin(t), "lon_g": 0.5 * np.cos(t / 2)}
    raw, units = {}, {}
    if obd:
        gear = np.where(v < 75, 2, np.where(v < 105, 3, 4))
        rpm = v * np.take([0, 0, 95.0, 70.0, 55.0], gear)
        ch["rpm"] = rpm
        ch["throttle"] = np.clip(ch["lon_g"] * 2 + 0.5, 0, 1)
        raw["Oil Temp"] = np.linspace(95, 110, len(t))
        units["Oil Temp"] = "°C"
        raw["RPM"] = rpm
    return Telemetry(time=t, channels=ch, raw=raw, units=units,
                     source_columns=list(raw))


def _frame():
    img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    return gauges.Frame(img, ImageDraw.Draw(img), 1920, 1080)


def test_settings_normalize_and_round_trip():
    g = gauges.normalize_gauge({"id": "d", "type": "dash", "slots": ["speed", {"src": "rpm"}],
                                "rpm_max": "8000", "shift_lo": "", "range": 99})
    assert len(g["slots"]) == gauges.DASH_SLOTS
    assert g["slots"][0]["src"] == "speed" and g["slots"][3]["src"] == ""
    assert g["rpm_max"] == 8000.0 and g["shift_lo"] is None and g["range"] == 10.0
    lay = gauges.normalize_layout({"custom": [g, {"id": "e", "type": "delta",
                                                  "green": "up"}]})
    d = gauges.entry(lay, "e")
    assert d["type"] == "delta" and d["green"] == "left"
    assert gauges.entry(lay, "d")["slots"] == g["slots"]
    assert gauges.base_size(d) == (600, 84)


def test_delta_bar_values_and_trend():
    tel = _tel(obd=False)
    ctx = render.OverlayContext.build(tel)
    g = gauges.normalize_gauge({"id": "d", "type": "delta"})
    assert ctx.gauge_available(g)
    lap = [l for l in ctx.laps.laps if l.timed][1]
    s = render.delta_state(ctx, lap.start + 30)
    assert s["delta"] is not None and s["ref"] is not None
    assert s["rate"] is not None and abs(s["rate"]) < 1.0
    out = render.delta_state(ctx, 1.0)             # out-lap: nothing to compare
    assert out["delta"] is None
    f = _frame()
    for v in (s, out, {"delta": -12.0, "rate": 0.3, "ref": "2"}):
        for gg in (g, dict(g, green="right", trend=True)):
            gauges.draw_delta(f, (100, 100, 600, 84, 1.0), ctx.style, gg, v)


def test_dash_with_and_without_engine_data():
    g = gauges.normalize_gauge({"id": "d", "type": "dash", "text": "#117",
                                "slots": [{"src": "speed"}, {"src": "col:Oil Temp",
                                                             "conv": "c_f"},
                                          {"src": "lat_g", "label": "LAT"}, {"src": ""}]})
    # APEX only: speed in the middle, no speed slot twice, no RPM
    ctx = render.OverlayContext.build(_tel(obd=False))
    v = ctx.dash_values(g, 100.0)
    assert v["center"] == "speed" and v["rpm"] is None and v["gear"] is None
    assert [sl[0] for sl in v["slots"]][:2] == ["OIL TEMP", "LAT"]
    assert v["slots"][0][3] is False                   # oil not in this file
    assert v["throttle"] is None and v["brake"] is None
    # with OBD: gear from RPM/speed, auto shift lights, converted slot
    ctx = render.OverlayContext.build(_tel(obd=True))
    rmax, lo, hi = ctx.dash_rpm_limits(g)
    assert rmax and lo < hi <= rmax
    seen = set()
    for t in np.arange(70.0, 130.0, 0.5):
        v = ctx.dash_values(g, float(t))
        seen.add(v["gear"])
    assert len(seen - {0, None}) >= 2          # numbered from the lowest gear seen
    v = ctx.dash_values(g, 100.0)
    assert v["center"] == "gear" and v["throttle"] is not None
    oil = v["slots"][1]
    assert oil[0] == "OIL TEMP" and oil[2] == "°F" and 200 < float(oil[1]) < 235
    own = ctx.dash_rpm_limits(dict(g, rpm_max=9000.0, shift_lo=6000.0, shift_hi=7000.0))
    assert own == (9000.0, 6000.0, 7000.0)
    f = _frame()
    for c in (ctx, render.OverlayContext.build(_tel(obd=False))):
        gauges.draw_dash(f, (100, 100, 660, 330, 1.0), c.style, g, c.dash_values(g, 100.0))
        gauges.draw_dash(f, (100, 100, 660, 330, 1.0), c.style, dict(g, pedals=False),
                         c.dash_values(g, 100.0))
    gauges.draw_dash(f, (0, 0, 660, 330, 1.0), ctx.style, g,
                     {"rpm": 9999.0, "rpm_max": 8000.0, "shift_lo": 6000.0,
                      "shift_hi": 7000.0, "flash": True, "gear": 0, "center": "gear",
                      "slots": [], "throttle": 1.0, "brake": None, "lap": None})


def test_full_overlay_draws_both():
    tel = _tel(obd=True)
    lay = gauges.default_layout()
    lay["custom"] += [gauges.normalize_gauge({"id": "d1", "type": "delta"}),
                      gauges.normalize_gauge({"id": "d2", "type": "dash", "y": 0.6})]
    ctx = render.OverlayContext.build(tel, layout=lay)
    assert {"d1", "d2"} <= set(ctx.shown())
    f = _frame()
    render.draw_overlay(f, ctx, 130.0)
    a = np.asarray(f.img)[..., 3]
    assert a[650:1000, 600:1300].max() > 0       # the dash drew something


def test_delta_colour_blends_and_lap_timer_drops_its_delta():
    reds, greens = gauges.good_bad(-5), gauges.good_bad(5)
    assert reds[:3] == gauges.BAD[:3] and greens[:3] == gauges.GOOD[:3]
    mid = gauges.good_bad(0)                      # a straight mix, no yellow stop
    assert mid[:3] == tuple(round((a + b) / 2) for a, b in zip(gauges.BAD[:3],
                                                                gauges.GOOD[:3]))
    # a little ahead is greener than a little behind, both in between
    a, b = gauges.good_bad(0.3), gauges.good_bad(-0.3)
    assert a[1] > b[1] and a[0] < b[0]
    tel = _tel(obd=False)
    drawn = {}
    real = gauges.draw_laptimer
    gauges.draw_laptimer = lambda f, box, st, s: drawn.setdefault("s", s)
    try:
        lay = gauges.default_layout()
        ctx = render.OverlayContext.build(tel, layout=lay)
        lap = [lp for lp in ctx.laps.laps if lp.timed][1]
        render.draw_overlay(_frame(), ctx, lap.start + 30)
        assert drawn.pop("s")["delta"] is not None
        lay["custom"].append(gauges.normalize_gauge({"id": "d1", "type": "delta"}))
        ctx = render.OverlayContext.build(tel, layout=lay)
        render.draw_overlay(_frame(), ctx, lap.start + 30)
        s = drawn.pop("s")
        assert s["delta"] is None and not s["bar"] and s["time"] > 0
    finally:
        gauges.draw_laptimer = real


def test_snap_to_centre_edges_and_corners():
    W, H = 1920, 1080
    m = gauges.SNAP_MARGIN * H
    # near the middle: centred both ways
    x, y, gl = gauges.snap(655, 391, 600, 300, W, H, tol=12)
    assert (x, y) == (660, 390) and set(gl) == {("v", 960), ("h", 540)}
    # near the bottom-right inset corner
    x, y, gl = gauges.snap(W - m - 200 + 5, H - m - 100 - 7, 200, 100, W, H, tol=12)
    assert (x, y) == (W - m - 200, H - m - 100)
    # flush to the top-left corner
    assert gauges.snap(3, 4, 200, 100, W, H, tol=12)[:2] == (0, 0)
    # far from everything: left alone, no guides
    assert gauges.snap(400, 200, 200, 100, W, H, tol=12) == (400, 200, [])
