# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Compose gauge overlays onto a source video via a single streaming ffmpeg.

Python generates one RGBA frame per output frame and pipes raw bytes into
ffmpeg, which overlays them on the decoded video — no PNG-sequence temp files.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field

import numpy as np

from . import gauges
from racecore.laps import CornerModel, LapModel
from racecore.telemetry import Telemetry

TRAIL_SECONDS = 1.0
TRAIL_POINTS = 10


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    nb_frames: int


def probe(path: str) -> VideoInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    st = data["streams"][0]
    num, den = st["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 25.0
    duration = float(st.get("duration") or data["format"]["duration"])
    nb = st.get("nb_frames")
    nb_frames = int(nb) if nb not in (None, "N/A") else int(round(duration * fps))
    return VideoInfo(int(st["width"]), int(st["height"]), fps, duration, nb_frames)


def _moving_gps(telem: Telemetry):
    lat = telem.channels["gps_lat"]
    lon = telem.channels["gps_lon"]
    ok = np.isfinite(lat) & np.isfinite(lon)
    # Only points recorded while actually moving, to drop paddock scatter.
    if telem.has("speed"):
        ok &= telem.channels["speed"] > 10.0
    return ok, lat, lon


def _build_track_map(telem: Telemetry) -> np.ndarray | None:
    """Normalized Nx2 GPS path in [0,1] screen space, true aspect, or None."""
    if not (telem.has("gps_lat") and telem.has("gps_lon")):
        return None
    ok, lat, lon = _moving_gps(telem)
    lat, lon = lat[ok], lon[ok]
    if lat.size < 10:
        return None
    x = lon * np.cos(np.radians(np.mean(lat)))  # equirectangular projection
    y = lat
    span = max(np.ptp(x), np.ptp(y)) or 1.0
    # One scale for both axes keeps the track's real shape, centred.
    x = (x - x.min()) / span + (1 - np.ptp(x) / span) / 2
    y = (y - y.min()) / span + (1 - np.ptp(y) / span) / 2
    return np.column_stack([x, 1.0 - y])  # flip y for screen coords


def _nearest_map_point(telem: Telemetry, track: np.ndarray):
    """Return a function t -> current normalized (x,y) on the track map."""
    ok, _, _ = _moving_gps(telem)
    times = telem.time[ok]

    def at(t: float):
        if times.size == 0:
            return None
        idx = int(np.clip(np.searchsorted(times, t), 0, len(track) - 1))
        return track[idx]
    return at


@dataclass
class OverlayContext:
    """Everything needed to draw one overlay frame, precomputed once.

    Shared by the batch renderer and the sync GUI's live preview so what you
    see while lining things up is exactly what gets burned into the video.
    """
    telem: Telemetry
    use_mph: bool = True
    style: gauges.Style = field(default_factory=lambda: gauges.get_style(None))
    layout: dict = field(default_factory=gauges.default_layout)
    invert_lat: bool = False
    invert_lon: bool = False
    track: np.ndarray | None = None
    cur_map: object = None
    available: set = field(default_factory=set)
    vmax: float | None = None
    opts: dict = field(default_factory=lambda: dict(gauges.DEFAULT_OPTS))
    laps: LapModel | None = None
    corners: CornerModel | None = None
    _track_cache: dict = field(default_factory=dict, repr=False)
    _range_cache: dict = field(default_factory=dict, repr=False)

    @classmethod
    def build(cls, telem: Telemetry, use_mph: bool = True,
              style: str | None = None, layout: dict | None = None,
              invert_lat: bool = False, invert_lon: bool = False,
              opts: dict | None = None):
        track = _build_track_map(telem)
        laps = LapModel(telem)
        corners = CornerModel(telem, laps)
        avail = set()
        if telem.has("lat_g") or telem.has("lon_g"):
            avail.add("gball")
        if track is not None:
            avail.add("map")
        if laps.ok:
            avail.add("laptimer")
        if corners.ok:
            avail.add("corner")
        spd = telem.channels.get("speed")
        vmax = float(np.nanmax(spd)) if spd is not None and np.isfinite(spd).any() else None
        opts_n = gauges.normalize_opts(opts)
        return cls(telem=telem, use_mph=use_mph,
                   style=gauges.styled(style, opts_n.get("bg_opacity", -1)),
                   layout=gauges.normalize_layout(layout),
                   invert_lat=invert_lat, invert_lon=invert_lon, track=track,
                   cur_map=_nearest_map_point(telem, track) if track is not None else None,
                   available=avail, vmax=vmax,
                   opts=opts_n, laps=laps, corners=corners)

    def set_style(self, name: str):
        self.style = gauges.styled(name, self.opts.get("bg_opacity", -1))
        self._track_cache.clear()

    def gauge_available(self, g: dict) -> bool:
        if g["type"] == "image":
            return gauges.load_logo(g.get("image", "")) is not None
        if g["type"] == "text":
            return bool(g.get("text"))
        if g["type"] == "delta":
            return self.laps is not None and self.laps.ok
        if g["type"] == "dash":
            return True               # shows whatever the file has
        return self.telem.has(g["src"])

    def shown(self) -> list[str]:
        """Gauges to draw, in draw order (map first, built-ins last)."""
        out = ["map"] if "map" in self.available and self.layout["map"]["on"] else []
        out += [g["id"] for g in self.layout.get("custom", [])
                if g["on"] and self.gauge_available(g)]
        out += [n for n in ("gball", "laptimer", "corner")
                if n in self.available and self.layout[n]["on"]]
        return out

    def labels(self) -> dict:
        return {n: gauges.box_label(self.layout, n) for n in
                list(gauges.WIDGETS) + [g["id"] for g in self.layout.get("custom", [])]}

    # ----- custom gauge values -----

    def _src_unit(self, src: str) -> str:
        if src.startswith("col:"):
            return self.telem.units.get(src[4:], "")
        return {"speed": "MPH" if self.use_mph else "KM/H", "throttle": "%",
                "brake": "%", "rpm": "RPM", "lat_g": "g", "lon_g": "g",
                "lap": ""}.get(src, "")

    def _to_display(self, src: str, conv: str, vals):
        """Role/column values -> what the gauge shows (array or float)."""
        if src == "speed":
            vals = vals / 1.60934 if self.use_mph else vals
        elif src in ("throttle", "brake"):
            vals = vals * 100.0
        if conv in gauges.CONVERSIONS:
            vals = gauges.CONVERSIONS[conv][2](vals)
        return vals

    def gauge_unit(self, g: dict) -> str:
        if g.get("unit", "auto") != "auto":
            return g["unit"]
        if g.get("conv") in gauges.CONVERSIONS:
            return gauges.CONVERSIONS[g["conv"]][3]
        u = self._src_unit(g["src"])
        return "" if g["src"] == "lap" else u

    def gauge_range(self, g: dict) -> tuple[float, float, int]:
        """(min, max, decimals) after auto-fill, cached per gauge settings."""
        key = (g["id"], g["src"], g.get("conv"), g.get("min"), g.get("max"),
               g.get("decimals"), self.use_mph)
        hit = self._range_cache.get(key)
        if hit:
            return hit
        src = g["src"]
        vals = self.telem.series(src)
        lo = hi = None
        if vals is not None:
            d = self._to_display(src, g.get("conv", ""), vals[np.isfinite(vals)])
            if d.size:
                lo, hi = float(np.percentile(d, 0.5)), float(np.percentile(d, 99.5))
        if src in ("throttle", "brake"):
            lo, hi = 0.0, 100.0
        elif src in ("lat_g", "lon_g"):
            lo, hi = -1.5, 1.5
        elif lo is not None:
            # A gauge pinned to a barely-moving value (coolant 199-201 F) is
            # useless: widen to at least 25% of the value's size.
            span_min = 0.25 * max(abs(lo), abs(hi))
            if hi - lo < span_min:
                mid = (hi + lo) / 2
                lo, hi = mid - span_min / 2, mid + span_min / 2
            step = gauges.nice_step(hi - lo, 5)
            starts_at_zero = src in ("speed", "rpm") or lo >= 0 and lo < (hi - lo) * 0.5
            lo = 0.0 if starts_at_zero else np.floor(lo / step) * step
            hi = np.ceil(hi / step) * step
            if hi <= lo:
                hi = lo + 1
        lo = float(g["min"]) if g.get("min") is not None else (lo if lo is not None else 0.0)
        hi = float(g["max"]) if g.get("max") is not None else (hi if hi is not None else 1.0)
        dec = int(g.get("decimals", -1))
        if dec < 0:
            if src in ("speed", "rpm", "lap", "throttle", "brake"):
                dec = 0
            elif src in ("lat_g", "lon_g"):
                dec = 2
            else:
                mag = max(abs(lo), abs(hi))
                dec = 0 if mag >= 100 else (1 if mag >= 10 else 2)
        out = (lo, hi, dec)
        if len(self._range_cache) > 200:
            self._range_cache.clear()
        self._range_cache[key] = out
        return out

    def gauge_values(self, g: dict, t: float) -> dict:
        lo, hi, dec = self.gauge_range(g)
        raw = self.telem.sample(t, g["src"])
        val = float(self._to_display(g["src"], g.get("conv", ""), raw))
        v = {"value": val, "unit": self.gauge_unit(g), "decimals": dec,
             "lo": lo, "hi": hi,
             "thousands": g["type"] == "dial" and g["src"] in ("rpm",)
             or (g["type"] == "dial" and hi >= 2000)}
        if (g["type"] == "number" and g["src"] == "speed" and self.style.speed_bar
                and self.vmax):
            v["bar_frac"] = raw / self.vmax if np.isfinite(raw) else 0.0
        return v

    # ----- dash -----

    def gear_series(self, rpm_src: str = "rpm") -> np.ndarray | None:
        """Gear per sample from RPM against speed (0 = shifting / clutch in),
        or None without RPM or a clear set of gears."""
        key = ("gear", rpm_src)
        if key in self._range_cache:
            return self._range_cache[key]
        out = None
        rpm, spd = self.telem.series(rpm_src), self.telem.series("speed")
        if rpm is not None and spd is not None and self.telem.has(rpm_src):
            from racecore import engine
            rpm = engine.smooth_holds(self.telem.time, rpm)     # OBD steps -> line
            gearing = engine.find_gearing(rpm, spd)
            if gearing:
                out = engine.clean_runs(gearing.gear(rpm, spd), self.telem.time)
        self._range_cache[key] = out
        return out

    def dash_rpm_limits(self, g: dict) -> tuple[float | None, float | None, float | None]:
        """(rpm_max, shift_lo, shift_hi): the gauge's own numbers, or from the
        file (top of the RPM seen, rounded up to 1000; lights from 80%, all
        lit at 95% of the highest RPM reached)."""
        src = g.get("rpm_src") or "rpm"
        top = None
        if self.telem.has(src):
            vals = self.telem.series(src)
            vals = vals[np.isfinite(vals)]
            if vals.size:
                top = float(np.percentile(vals, 99.8))
        rmax = g.get("rpm_max") or (float(np.ceil(top / 1000.0) * 1000.0) if top else None)
        lo = g.get("shift_lo") or (round(top * 0.80, -2) if top else None)
        hi = g.get("shift_hi") or (round(top * 0.95, -2) if top else None)
        return rmax, lo, hi

    def slot_text(self, sl: dict, t: float) -> tuple[str, str, str, bool]:
        src = sl.get("src") or ""
        if not src:
            return ("", "", "", False)
        g = dict(gauges.GAUGE_DEFAULTS, id="slot:" + src, type="number", src=src,
                 conv=sl.get("conv", ""), decimals=sl.get("decimals", -1))
        label = sl.get("label") or gauges.box_label({"custom": [g]}, g["id"]).upper()
        if not self.telem.has(src):
            return (label, "", "", False)
        val = self.gauge_values(g, t)
        return (label, gauges.fmt_value(val["value"], val["decimals"]), val["unit"], True)

    def dash_values(self, g: dict, t: float) -> dict:
        src = g.get("rpm_src") or "rpm"
        rmax, lo, hi = self.dash_rpm_limits(g)
        rpm = None
        if self.telem.has(src):
            r = self.telem.sample(t, src)
            rpm = float(r) if np.isfinite(r) else None
        gear = None
        gs = self.gear_series(src)
        if gs is not None and len(gs) == len(self.telem.time):
            i = int(np.clip(np.searchsorted(self.telem.time, t), 0, len(gs) - 1))
            gear = int(gs[i])
        spd = None
        if self.telem.has("speed"):
            sv = self.telem.sample(t, "speed")
            spd = float(sv / 1.60934 if self.use_mph else sv) if np.isfinite(sv) else None

        def pedal(role):
            if not self.telem.has(role):
                return None
            pv = self.telem.sample(t, role)
            return float(pv) if np.isfinite(pv) else 0.0

        lap = None
        if self.laps is not None and self.laps.ok:
            lp = self.laps.lap_at(t)
            lap = lp.n if lp is not None and lp.kind != "out" else None
        return {"rpm": rpm, "rpm_max": rmax, "shift_lo": lo, "shift_hi": hi,
                "flash": int(t * 8) % 2 == 0, "gear": gear,
                "center": "gear" if rpm is not None else "speed",
                "speed": spd, "speed_unit": "MPH" if self.use_mph else "KM/H",
                # the big centre number is speed without RPM: no speed slot too
                "slots": [self.slot_text(sl, t) for sl in g.get("slots") or []
                          if not (rpm is None and sl.get("src") == "speed")],
                "throttle": pedal("throttle"), "brake": pedal("brake"), "lap": lap}

    def track_img(self, box):
        x, y, w, h, k = box
        key = (int(w), int(h), self.style.name)
        img = self._track_cache.get(key)
        if img is None:
            if len(self._track_cache) > 8:
                self._track_cache.clear()
            img = gauges.track_image(self.track, int(w), int(h), k, self.style)
            self._track_cache[key] = img
        return img

    def g_at(self, t: float) -> tuple[float, float]:
        tm = self.telem
        lat = tm.sample(t, "lat_g") if "lat_g" in tm.channels else float("nan")
        lon = tm.sample(t, "lon_g") if "lon_g" in tm.channels else float("nan")
        return (-lat if self.invert_lat else lat,
                -lon if self.invert_lon else lon)


FLASH_SECONDS = 4.0


def laptimer_state(ctx: OverlayContext, t: float) -> dict | None:
    lm = ctx.laps
    lap = lm.lap_at(t)
    if lap is None:
        last = lm.laps[-1] if lm.laps else None
        if last is not None and last.timed and 0 <= t - last.end < FLASH_SECONDS:
            before = lm.best_before(last.start)
            return {"label": f"LAP {last.n}", "time": last.duration,
                    "time_col": "new_best" if before is None
                    or last.duration < before.duration else None,
                    "delta": (last.duration - before.duration) if before else None,
                    "delta_label": f"vs LAP {before.n}" if before else "",
                    "bar": ctx.opts["delta_bar"], "last": None,
                    "best": min(last.duration, before.duration) if before
                    else last.duration}
        return None
    mode = "session" if ctx.opts["delta_ref"] == "session" else "best_so_far"
    best_now = lm.best_before(t)
    s = {"label": {"out": "OUT LAP", "in": "IN LAP", "slow": f"LAP {lap.n}"}
         .get(lap.kind, f"LAP {lap.n}"),
         "time": t - lap.start, "time_col": None, "delta": None,
         "delta_label": "", "bar": ctx.opts["delta_bar"],
         "best": (lm.session_best() if mode == "session" else best_now),
         "last": None}
    idx = lm.laps.index(lap)
    prev = lm.laps[idx - 1] if idx > 0 else None
    if prev is not None and prev.kind != "out":
        s["last"] = prev.duration
    # Just crossed the line after a timed lap: show that lap's time.
    if prev is not None and prev.timed and t - lap.start < FLASH_SECONDS:
        before = lm.best_before(prev.start)
        ref = lm.session_best() if mode == "session" else before
        s["label"] = f"LAP {prev.n}"
        s["time"] = prev.duration
        if before is None or prev.duration < before.duration:
            s["time_col"] = "new_best"
        if ref is not None and ref is not prev:
            s["delta"] = prev.duration - ref.duration
            s["delta_label"] = f"vs LAP {ref.n}"
    elif lap.timed:
        ref = lm.reference(t, mode)
        if ref is not None and not (mode == "session" and ref is lap):
            s["delta"] = lm.delta(t, lap, ref)
            s["delta_label"] = f"vs LAP {ref.n}"
    if s["best"] is not None:
        s["best"] = s["best"].duration
    return s


TREND_S = 1.0      # delta bar "trend": change in delta over this long


def delta_state(ctx: OverlayContext, t: float) -> dict:
    """What the delta bar shows: the lap timer's delta and reference lap,
    plus how fast the delta is changing (for trend colouring)."""
    s = laptimer_state(ctx, t) if ctx.laps is not None and ctx.laps.ok else None
    if not s or s.get("delta") is None:
        return {"delta": None, "rate": None, "ref": None}
    d = s["delta"]
    rate = None
    lap = ctx.laps.lap_at(t)
    if lap is not None and lap.timed and t - TREND_S >= lap.start:
        p = laptimer_state(ctx, t - TREND_S)
        if p and p.get("delta") is not None and p.get("delta_label") == s.get("delta_label"):
            rate = (d - p["delta"]) / TREND_S
    ref = s.get("delta_label", "").replace("vs LAP ", "") or None
    return {"delta": d, "rate": rate, "ref": ref}


def corner_state(ctx: OverlayContext, t: float) -> dict:
    cm = ctx.corners
    s = {"entry": None, "vmin": None, "g": None, "use_mph": ctx.use_mph,
         "live": False, "cmp": None, "ref": None}
    st = cm.state(t, live=ctx.opts["corner_mode"] == "live")
    if st is None:
        return s
    p, entry, vmin, g, gdir, live = st
    s.update(entry=entry, vmin=vmin, g=g, gdir=gdir, live=live)
    if ctx.opts["corner_compare"] and p.lap is not None:
        mode = "session" if ctx.opts["delta_ref"] == "session" else "best_so_far"
        lap = ctx.laps.laps[p.lap]
        ref = ctx.laps.reference(lap.start, mode)
        rp = cm.matching(p, ref)
        if rp is not None:
            s["ref"] = ref.n
            s["cmp"] = {"entry": entry - rp.entry,
                        "vmin": None if live else vmin - rp.vmin,
                        "g": None if (live or g is None or rp.gpeak is None)
                        else g - rp.gpeak}
    return s


def draw_overlay(f: gauges.Frame, ctx: OverlayContext, t: float,
                 edit: bool = False, active: str | None = None):
    """Draw every enabled gauge for telemetry time `t` onto frame `f`."""
    st = ctx.style
    boxes = gauges.layout_boxes(ctx.layout, f.w, f.h)
    shown = ctx.shown()
    customs = {g["id"]: g for g in ctx.layout.get("custom", [])}
    delta_bar_shown = any(n in customs and customs[n]["type"] == "delta" for n in shown)
    for name in shown:
        box = boxes[name]
        if name == "map":
            gauges.draw_map(f, box, st, ctx.track_img(box), ctx.cur_map(t))
        elif name in customs:
            g = customs[name]
            if g["type"] in gauges.DATA_TYPES:
                v = ctx.gauge_values(g, t)
            elif g["type"] == "delta":
                v = delta_state(ctx, t)
            elif g["type"] == "dash":
                v = ctx.dash_values(g, t)
            else:
                v = None
            gauges.CUSTOM_DRAW[g["type"]](f, box, st, g, v)
        elif name == "gball":
            lat, lon = ctx.g_at(t)
            trail = []
            if st.trail:
                step = TRAIL_SECONDS / TRAIL_POINTS
                trail = [ctx.g_at(t - step * i) for i in range(TRAIL_POINTS, 0, -1)]
            gauges.draw_gball(f, box, st, lat, lon, trail)
        elif name == "laptimer":
            s = laptimer_state(ctx, t)
            if s is not None and delta_bar_shown:
                # the delta bar shows the gap; don't repeat it here
                s = dict(s, delta=None, delta_label="", bar=False)
            if s is not None:
                gauges.draw_laptimer(f, box, st, s)
        elif name == "corner":
            gauges.draw_corner(f, box, st, corner_state(ctx, t))
    if edit:
        gauges.draw_edit_boxes(f, boxes, shown, active, ctx.labels())


def render(source: str, telem: Telemetry, output: str, *,
           offset: float = 0.0, start: float = 0.0, out_fps: float | None = None,
           use_mph: bool = True, crf: int = 18,
           limit_seconds: float | None = None, style: str | None = None,
           layout: dict | None = None, invert_lat: bool = False,
           invert_lon: bool = False, opts: dict | None = None,
           progress=sys.stderr):
    """Render `source` + telemetry overlay to `output`.

    offset: seconds added to video time to find the matching telemetry time.
            Positive = telemetry clock is ahead of the video start.
    start:  video time to begin at (preview a mid-session lap without rendering
            from the beginning).
    out_fps: output framerate; defaults to source fps. Lower renders faster.
    """
    info = probe(source)
    fps = out_fps or info.fps

    remaining = max(0.0, info.duration - start)
    span = min(remaining, limit_seconds) if limit_seconds else remaining
    total = max(1, int(round(span * fps)))

    ff_in = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start}", "-i", source,
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{info.width}x{info.height}", "-r", f"{fps}", "-i", "pipe:0",
        "-filter_complex",
        f"[0:v]fps={fps}[v];[v][1:v]overlay=0:0:format=auto",
        "-map", "0:a?", "-c:a", "aac", "-b:a", "192k",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf),
        "-t", f"{span}", output,
    ]
    proc = subprocess.Popen(ff_in, stdin=subprocess.PIPE)

    ctx = OverlayContext.build(telem, use_mph=use_mph, style=style,
                               layout=layout, invert_lat=invert_lat,
                               invert_lon=invert_lon, opts=opts)

    try:
        for i in range(total):
            t = start + i / fps + offset
            f = gauges.Frame.new(info.width, info.height)
            draw_overlay(f, ctx, t)
            proc.stdin.write(f.img.tobytes())
            if progress and i % max(1, int(fps)) == 0:
                pct = 100.0 * i / total
                progress.write(f"\r  rendering {pct:5.1f}%  ({i}/{total} frames)")
                progress.flush()
    finally:
        if proc.stdin:
            proc.stdin.close()
        rc = proc.wait()
    if progress:
        progress.write(f"\r  rendering 100.0%  ({total}/{total} frames)\n")
    if rc != 0:
        raise RuntimeError(f"ffmpeg exited with code {rc}")
    return output
