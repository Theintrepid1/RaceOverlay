# SPDX-License-Identifier: GPL-3.0-or-later
"""Math channels: formulas over the file's channels, safely evaluated,
from one library shared by both apps."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import mathch  # noqa: E402
from racecore.mathch import FormulaError, MathChannel, MissingInput  # noqa: E402
from racecore.telemetry import Telemetry  # noqa: E402


def _tel():
    return _varying(_flat())


def _flat():
    t = np.arange(0, 10, 0.1)
    n = len(t)
    return Telemetry(time=t, channels={
        "speed": np.full(n, 160.934),                       # 100 mph
        "throttle": np.where(t < 5, 0.0, 0.5),              # 0-1 inside, % in formulas
        "brake": np.where((t > 4) & (t < 6), 0.3, 0.0),
        "lat_g": np.full(n, 0.6), "lon_g": np.full(n, -0.8)},
        source_columns=["Oil Temp (F)", "Water Temp"],
        raw={"Oil Temp (F)": np.full(n, 230.0), "Water Temp": np.full(n, 190.0)},
        units={"Oil Temp (F)": "F"})


def _varying(tel):
    """Real channels move; a perfectly flat one counts as no data. Nudge
    each fixture channel at its last sample only."""
    for d in (tel.channels, tel.raw):
        for k in d:
            d[k] = d[k].astype(float)
            d[k][-1] += 1e-3
    return tel


@pytest.fixture
def lib(tmp_path, monkeypatch):
    p = tmp_path / "math_channels.json"
    monkeypatch.setattr(mathch, "library_path", lambda: p)

    def save(*chans):
        mathch.save_library(list(chans))
    return save


def ev(formula, tel=None, chans=()):
    return mathch.evaluate(tel or _tel(), MathChannel("x", formula), list(chans))


def test_arithmetic_names_and_columns():
    assert ev("hypot(lat_g, lon_g)")[0] == pytest.approx(1.0)
    assert ev("speed_mph")[0] == pytest.approx(100.0)
    assert ev("speed_ms")[0] == pytest.approx(44.704, abs=1e-3)
    assert ev("oil_temp_f - water_temp")[0] == 40.0
    assert ev('col("Oil Temp (F)") / 10  # a comment')[0] == 23.0
    assert ev("throttle / max(throttle)")[-1] == 1.0
    assert ev("max(throttle)")[0] == pytest.approx(50.1)
    v = ev("if(brake > 5 and throttle > 10, 1, 0)")       # on both pedals 5-6 s
    t = _tel().time
    assert v[(t > 5.05) & (t < 5.95)].min() == 1 and v[t < 4].max() == 0 and v[t > 6.05].max() == 0
    assert ev("1 if throttle > 10 else 0")[-1] == 1
    assert np.isnan(ev("if(lat_g > 1, 1, nan)")).all()
    assert ev("2 ** 3 + 0 * time")[0] == 8 and len(ev("5")) == 100   # constants fill the file


def test_time_functions():
    t = _tel()
    d = ev("integral(speed_ms)", t)
    v = t.channels["speed"] / 3.6
    assert d[-1] == pytest.approx(float(np.sum(np.diff(t.time) * (v[1:] + v[:-1]) / 2)))
    r = ev("rate(time * 3)", t)
    assert r[10] == pytest.approx(3.0)
    s = ev("smooth(throttle, 1)", t)
    i = int(np.argmin(abs(t.time - 5.0)))
    assert 15 < s[i] < 35                                  # the step is ramped over ~1 s
    assert ev("smooth(throttle, seconds=1)", t)[i] == pytest.approx(s[i])
    p = ev("prev(throttle, 1)", t)
    assert p[int(np.argmin(abs(t.time - 5.5)))] == 0 and p[-1] == pytest.approx(50)


def test_math_channels_use_each_other_but_not_in_a_loop():
    a = MathChannel("Total g", "hypot(lat_g, lon_g)")
    assert ev("total_g * 2", chans=[a])[0] == pytest.approx(2.0)
    assert ev('math("Total g") + 1', chans=[a])[0] == pytest.approx(2.0)
    loop = [MathChannel("A", "b + 1"), MathChannel("B", "a + 1")]
    with pytest.raises(FormulaError, match="loop"):
        mathch.evaluate(_tel(), loop[0], loop)


@pytest.mark.parametrize("bad", [
    "__import__('os').system('true')", "speed.__class__", "[1, 2]", "lambda: 1",
    "open('x')", "speed[0]", "eval('1')", "x := 1", "speed @ speed", "{'a': 1}",
    "smooth(speed, window=3)", "1 +"])
def test_anything_but_arithmetic_is_refused(bad):
    with pytest.raises(FormulaError):
        ev(bad)


def test_missing_channel_is_not_an_error_in_the_formula():
    with pytest.raises(MissingInput):
        ev("rpm / 1000")                                    # this file has no rpm
    t = _tel()
    t.channels["rpm"] = np.zeros(len(t.time))               # an rpm column, no OBD link
    with pytest.raises(MissingInput, match="no data"):
        ev("rpm / 1000", t)
    assert mathch.inputs("rpm / 1000 + col('Oil') + math('X') + if(a, b, nan)") == \
        {"rpm", "col:Oil", "math:X", "a", "b"}


def test_library_and_telemetry_keys(lib):
    lib(MathChannel("Overlap", "if(brake > 5 and throttle > 10, 1, 0)"),
        MathChannel("Revs", "rpm / 1000", "krpm"),
        MathChannel("Off one", "speed", on=False))
    t = _tel()
    assert [m.name for m in mathch.available(t)] == ["Overlap"]     # no rpm; one off
    assert t.has("math:Overlap") and not t.has("math:Revs")
    assert t.sample(5.5, "math:Overlap") == 1.0
    assert mathch.unit("math:Revs") == "krpm"
    # editing the library is picked up straight away
    lib(MathChannel("Overlap", "throttle"))
    assert t.sample(5.5, "math:Overlap") == pytest.approx(50.0)
    assert mathch.ident("Oil Temp (F)") == "oil_temp_f"


def test_presets_all_parse():
    for p in mathch.PRESETS:
        mathch.parse(p.formula)
    t = _tel()
    for name in ("Brake/throttle overlap", "Combined g", "Corner radius", "Distance",
                 "Throttle rate", "Power estimate"):
        m = next(p for p in mathch.PRESETS if p.name == name)
        assert mathch.evaluate(t, m, []).shape == t.time.shape
