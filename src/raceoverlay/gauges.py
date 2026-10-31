# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Overlay gauges: style presets, a movable layout, and the drawing code.

Every gauge lives in a box whose position is stored as a fraction of the frame
(top-left x/y) plus a size multiplier, so the same layout renders identically
in the GUI preview and in a 4K export. Sizes are designed at 1080p and scale
with frame height.
"""
from __future__ import annotations

import functools
import math
import re
import os
from dataclasses import dataclass, replace

from PIL import Image, ImageDraw, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYS = ["/usr/share/fonts/TTF", "/usr/share/fonts/dejavu",
        "/usr/share/fonts/truetype/dejavu"]
_FONT_FILES = {
    "bold": ["DejaVuSans-Bold.ttf"],
    "condensed": ["DejaVuSansCondensed-BoldOblique.ttf", "DejaVuSans-Bold.ttf"],
}


@functools.lru_cache(maxsize=None)
def _font(size: int, kind: str = "bold"):
    size = max(1, int(size))
    for name in _FONT_FILES.get(kind, _FONT_FILES["bold"]):
        for d in [os.path.join(_HERE, "fonts")] + _SYS:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# ------------------------------------------------------------------ styles --

@dataclass(frozen=True)
class Style:
    name: str
    fg: tuple = (255, 255, 255, 255)
    dim: tuple = (255, 255, 255, 160)
    accent: tuple = (90, 190, 255, 255)
    throttle: tuple = (64, 220, 96, 255)
    brake: tuple = (240, 70, 60, 255)
    panel: tuple | None = None          # backing panel colour, None = none
    radius: int = 14
    outline: float = 0.0                # text outline width at 1080p
    outline_col: tuple = (0, 0, 0, 180)
    font: str = "bold"
    speed_bar: bool = False
    trail: bool = False                 # g-ball shows the last second
    ring: tuple = (255, 255, 255, 120)
    track: tuple = (255, 255, 255, 180)


STYLES: dict[str, Style] = {
    "Clean": Style("Clean", outline=2.5, outline_col=(0, 0, 0, 130)),
    "Panel": Style("Panel", panel=(12, 14, 18, 170), dim=(255, 255, 255, 150)),
    "Race": Style("Race", accent=(255, 205, 0, 255), dim=(255, 205, 0, 230),
                  panel=(0, 0, 0, 200), radius=3, font="condensed",
                  speed_bar=True, trail=True, ring=(255, 205, 0, 150),
                  track=(255, 255, 255, 210)),
    "Minimal": Style("Minimal", outline=4.0, outline_col=(0, 0, 0, 190),
                     dim=(255, 255, 255, 210), ring=(255, 255, 255, 170),
                     track=(255, 255, 255, 200)),
}
DEFAULT_STYLE = "Clean"


PANEL_RGB = (12, 14, 18)


def style_bg_percent(name: str | None) -> int:
    """The style's own background opacity, in %."""
    p = get_style(name).panel
    return int(round(p[3] / 2.55)) if p else 0


def styled(name: str | None, bg_opacity: int = -1) -> Style:
    """Style with the gauge background opacity overridden (0-100 %), or the
    style's own background when bg_opacity < 0. Styles without a background
    get a dark one when it's turned up."""
    st = get_style(name)
    if bg_opacity is None or bg_opacity < 0:
        return st
    a = int(round(max(0, min(100, bg_opacity)) * 2.55))
    if a == 0:
        return replace(st, panel=None)
    rgb = st.panel[:3] if st.panel else PANEL_RGB
    return replace(st, panel=rgb + (a,))


def get_style(name: str | None) -> Style:
    return STYLES.get(name or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])


# ------------------------------------------------------------------ layout --

# Built-in gauges (special logic, fixed look; position/size editable).
# name: (label, base width, base height at 1080p, default x, default y)
WIDGETS: dict[str, tuple[str, int, int, float, float]] = {
    "gball":  ("G-ball",         200, 230, 0.865, 0.045),
    "map":    ("Track map",      240, 240, 0.030, 0.045),
    "laptimer": ("Lap timer",    380, 215, 0.401, 0.125),
    "corner": ("Corner",         290, 100, 0.424, 0.865),
}
OFF_BY_DEFAULT = {"corner"}

# Gauge behaviour options (Look tab), saved with the layout.
DEFAULT_OPTS = {
    "delta_ref": "best_so_far",   # or "session"
    "delta_bar": True,
    "corner_mode": "live",        # or "summary"
    "corner_compare": False,
    "bg_opacity": -1,             # gauge background %, -1 = style default
}


def normalize_opts(opts: dict | None) -> dict:
    out = dict(DEFAULT_OPTS)
    for k, v in (opts or {}).items():
        if k in out and type(v) is type(out[k]):
            out[k] = v
    return out


GOOD = (70, 225, 110, 255)
BAD = (245, 80, 70, 255)
PURPLE = (190, 120, 255, 255)


# ---------------------------------------------------------- custom gauges --
#
# A custom gauge is a dict in layout["custom"]:
#   id, type (number|bar|dial), src (role like "speed" or "col:<column>"),
#   label, unit ("auto" or text), conv (unit conversion key or ""),
#   decimals (-1 = auto), min/max (None = auto), redline (None = off),
#   orient (h|v, bars), color (accent|throttle|brake|good|bad|#rrggbb),
#   align (left|center, numbers), show_value, x, y, scale, on

GAUGE_TYPES = {"number": "Number", "bar": "Bar", "dial": "Dial",
               "image": "Logo", "text": "Text"}
DATA_TYPES = {"number", "bar", "dial"}      # the ones that show a channel
BASE_SIZE = {("number", ""): (220, 100), ("bar", "h"): (300, 64),
             ("bar", "v"): (64, 240), ("dial", ""): (220, 220)}
LOGO_WIDTH = 240          # a logo's width at 1080p, scale 1
TEXT_PX = 44              # a text gauge's font size at 1080p, scale 1

#   Logo and text gauges (no channel): image (file name in logos_dir()),
#   aspect (height / width of the image), opacity (0-100); text.
GAUGE_DEFAULTS = {"type": "number", "src": "speed", "label": "",
                  "unit": "auto", "conv": "", "decimals": -1, "min": None,
                  "max": None, "redline": None, "orient": "h",
                  "color": "accent", "align": "left", "show_value": True,
                  "x": 0.4, "y": 0.4, "scale": 1.0, "on": True,
                  "image": "", "aspect": 1.0, "opacity": 100, "text": ""}


def logos_dir():
    """Where logo images are kept: copied in when added, so a layout keeps
    working if the original file moves (and other computers can get the
    same file name in their own folder)."""
    from racecore import osutil
    return osutil.data_dir() / "logos"


def logo_path(name: str):
    return logos_dir() / name if name else None


@functools.lru_cache(maxsize=32)
def _logo(name: str, mtime: float):
    from pathlib import Path
    p = logo_path(name)
    if p is None or not Path(p).exists():
        return None
    return Image.open(p).convert("RGBA")


def load_logo(name: str):
    """The logo as RGBA, or None if its file isn't in logos_dir()."""
    p = logo_path(name)
    try:
        return _logo(name, os.path.getmtime(p)) if p else None
    except OSError:
        return None

# Unit conversions offered for a gauge: key -> (menu label, from-units,
# function, new unit)
CONVERSIONS = {
    "c_f":     ("°C → °F", {"°c", "c", "degc", "deg c", "celsius"},
                lambda v: v * 9 / 5 + 32, "°F"),
    "f_c":     ("°F → °C", {"°f", "f", "degf", "fahrenheit"},
                lambda v: (v - 32) * 5 / 9, "°C"),
    "bar_psi": ("bar → psi", {"bar"}, lambda v: v * 14.5038, "psi"),
    "psi_bar": ("psi → bar", {"psi"}, lambda v: v / 14.5038, "bar"),
    "kpa_psi": ("kPa → psi", {"kpa"}, lambda v: v * 0.145038, "psi"),
    "kmh_mph": ("km/h → mph", {"km/h", "kmh", "kph"}, lambda v: v / 1.60934, "mph"),
    "mph_kmh": ("mph → km/h", {"mph"}, lambda v: v * 1.60934, "km/h"),
    "ms_mph":  ("m/s → mph", {"m/s"}, lambda v: v * 2.23694, "mph"),
    "ms_kmh":  ("m/s → km/h", {"m/s"}, lambda v: v * 3.6, "km/h"),
    "m_ft":    ("m → ft", {"m"}, lambda v: v * 3.28084, "ft"),
    "km_mi":   ("km → mi", {"km"}, lambda v: v / 1.60934, "mi"),
}


def conversions_for(unit: str) -> list[str]:
    u = (unit or "").strip().lower()
    return [k for k, c in CONVERSIONS.items() if u in c[1]]


def _default_customs() -> list[dict]:
    def g(**kw):
        d = dict(GAUGE_DEFAULTS)
        d.update(kw)
        return d
    return [
        g(id="speed", type="number", src="speed", label="", decimals=0,
          align="left", x=0.030, y=0.815, scale=1.45),
        g(id="lap", type="number", src="lap", label="LAP", decimals=0,
          align="center", x=0.466, y=0.030, scale=0.95),
        g(id="throttle", type="bar", src="throttle", label="THR",
          orient="v", color="throttle", min=0.0, max=100.0,
          show_value=False, x=0.912, y=0.700, scale=0.92),
        g(id="brake", type="bar", src="brake", label="BRK", orient="v",
          color="brake", min=0.0, max=100.0, show_value=False, x=0.946,
          y=0.700, scale=0.92),
    ]


DEFAULT_CUSTOM_IDS = {"speed", "lap", "throttle", "brake"}


def default_layout() -> dict:
    out = {k: {"x": x, "y": y, "scale": 1.0, "on": k not in OFF_BY_DEFAULT}
           for k, (_, _, _, x, y) in WIDGETS.items()}
    out["custom"] = _default_customs()
    return out


def normalize_gauge(g: dict) -> dict:
    out = dict(GAUGE_DEFAULTS)
    out.update({k: v for k, v in g.items() if k in GAUGE_DEFAULTS or k == "id"})
    if out["type"] not in GAUGE_TYPES:
        out["type"] = "number"
    for f in ("x", "y", "scale", "aspect"):
        out[f] = float(out[f])
    out["opacity"] = int(max(0, min(100, out["opacity"])))
    out["on"] = bool(out["on"])
    return out


def normalize_layout(layout: dict | None) -> dict:
    """Fill in missing gauges/fields so old or partial layouts still work.
    Layouts from before custom gauges keep their speed/lap/pedal positions."""
    layout = layout or {}
    out = default_layout()
    for k, v in layout.items():
        if k in WIDGETS and isinstance(v, dict):
            for f in ("x", "y", "scale"):
                if isinstance(v.get(f), (int, float)):
                    out[k][f] = float(v[f])
            if "on" in v:
                out[k]["on"] = bool(v["on"])
    if isinstance(layout.get("custom"), list):
        out["custom"] = [normalize_gauge(g) for g in layout["custom"]
                         if isinstance(g, dict) and g.get("id")]
    else:  # migrate v0.7 built-ins
        cust = {g["id"]: g for g in out["custom"]}
        old = layout.get("speed")
        if isinstance(old, dict):
            cust["speed"].update(x=old.get("x", 0.03), y=old.get("y", 0.815),
                                 scale=1.45 * old.get("scale", 1.0),
                                 on=old.get("on", True))
        old = layout.get("lap")
        if isinstance(old, dict):
            cust["lap"].update(x=old.get("x", 0.466), y=old.get("y", 0.03),
                               scale=0.95 * old.get("scale", 1.0),
                               on=old.get("on", True))
        old = layout.get("pedals")
        if isinstance(old, dict):
            sc = old.get("scale", 1.0)
            for gid, dx in (("throttle", 0.0), ("brake", 0.034 * sc)):
                cust[gid].update(x=old.get("x", 0.915) + dx,
                                 y=old.get("y", 0.70), scale=0.92 * sc,
                                 on=old.get("on", True))
    return out


def new_gauge_id(layout: dict) -> str:
    used = {g["id"] for g in layout.get("custom", [])}
    i = 1
    while f"c{i}" in used:
        i += 1
    return f"c{i}"


def entry(layout: dict, name: str) -> dict | None:
    """The position/size dict for a built-in name or custom gauge id."""
    if name in WIDGETS:
        return layout.get(name)
    return next((g for g in layout.get("custom", []) if g["id"] == name), None)


def base_size(g: dict) -> tuple[int, int]:
    if g["type"] == "image":
        return LOGO_WIDTH, max(1, int(round(LOGO_WIDTH * float(g.get("aspect") or 1.0))))
    if g["type"] == "text":
        f = _font(TEXT_PX, "bold")
        l, t, r, b = f.getbbox(g.get("text") or " ")
        pad = 2 * 10
        return max(40, int(r - min(0, l)) + pad), int(TEXT_PX * 1.3) + pad // 2
    key = (g["type"], g.get("orient", "h") if g["type"] == "bar" else "")
    return BASE_SIZE.get(key, (220, 100))


def layout_boxes(layout: dict, w: int, h: int) -> dict[str, tuple]:
    """Pixel boxes (x, y, w, h, k) per gauge; k = px per 1080p unit."""
    s = h / 1080.0
    boxes = {}
    items = [(n, layout[n], WIDGETS[n][1], WIDGETS[n][2]) for n in WIDGETS]
    for g in layout.get("custom", []):
        bw, bh = base_size(g)
        items.append((g["id"], g, bw, bh))
    for name, L, bw, bh in items:
        k = s * L["scale"]
        bw_px, bh_px = bw * k, bh * k
        x = min(max(0.0, L["x"] * w), max(0.0, w - bw_px))
        y = min(max(0.0, L["y"] * h), max(0.0, h - bh_px))
        boxes[name] = (x, y, bw_px, bh_px, k)
    return boxes


def box_label(layout: dict, name: str) -> str:
    if name in WIDGETS:
        return WIDGETS[name][0]
    g = entry(layout, name)
    if g is None:
        return name
    if g["type"] == "text":
        return g.get("text") or "Text"
    if g["type"] == "image":
        return g.get("label") or "Logo"
    return g.get("label") or g.get("src", "").replace("col:", "") or name


# ------------------------------------------------------------------ frame ---

@dataclass
class Frame:
    """A single overlay frame being drawn."""
    img: Image.Image
    draw: ImageDraw.ImageDraw
    w: int
    h: int

    @classmethod
    def new(cls, w: int, h: int) -> "Frame":
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        return cls(img=img, draw=ImageDraw.Draw(img), w=w, h=h)


def _panel(f: Frame, box, st: Style):
    if st.panel:
        x, y, w, h, k = box
        f.draw.rounded_rectangle([x, y, x + w, y + h],
                                 radius=max(1, int(st.radius * k)),
                                 fill=st.panel)


def _text(f: Frame, xy, s: str, font, fill, st: Style, k: float, anchor="la"):
    # Outline thins out on small text so labels don't turn into blobs.
    size = getattr(font, "size", 40 * k)
    sw = int(round(st.outline * k * (0.45 + 0.55 * min(1.0, size / (80 * k)))))
    if st.outline and sw > 0:
        f.draw.text(xy, s, font=font, fill=fill, anchor=anchor,
                    stroke_width=sw, stroke_fill=st.outline_col)
    else:
        f.draw.text(xy, s, font=font, fill=fill, anchor=anchor)


def _ok(v) -> bool:
    return v is not None and math.isfinite(v)


# ------------------------------------------------------------------ gauges --

# Pillow draws circles and lines without antialiasing, so the round gauges
# (g-ball, track map) are drawn this many times larger and shrunk down.
SUPERSAMPLE = 3


class _Fine:
    """A supersampled drawing layer over one box of a frame. Draw with
    coordinates in frame pixels; finish() shrinks it and lays it on top."""

    def __init__(self, f: "Frame", x: float, y: float, w: float, h: float):
        self.f = f
        self.x0, self.y0 = int(math.floor(x)), int(math.floor(y))
        self.w = max(1, int(math.ceil(x + w)) - self.x0)
        self.h = max(1, int(math.ceil(y + h)) - self.y0)
        s = SUPERSAMPLE
        self.img = Image.new("RGBA", (self.w * s, self.h * s), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    def _xy(self, pts):
        s = SUPERSAMPLE
        return [((px - self.x0) * s, (py - self.y0) * s) for px, py in pts]

    def ellipse(self, cx, cy, rx, ry=None, **kw):
        ry = rx if ry is None else ry
        (a, b), (c, d) = self._xy([(cx - rx, cy - ry), (cx + rx, cy + ry)])
        if "width" in kw:
            kw["width"] = max(1, int(round(kw["width"] * SUPERSAMPLE)))
        self.d.ellipse([a, b, c, d], **kw)

    def line(self, pts, width=1.0, **kw):
        self.d.line(self._xy(pts), width=max(1, int(round(width * SUPERSAMPLE))), **kw)

    def finish(self):
        small = self.img.resize((self.w, self.h), Image.BOX)
        # keep to the frame (a gauge dragged partly off the edge)
        sx, sy = max(0, -self.x0), max(0, -self.y0)
        ex = min(self.w, self.f.w - self.x0)
        ey = min(self.h, self.f.h - self.y0)
        if ex <= sx or ey <= sy:
            return
        self.f.img.alpha_composite(small, (self.x0 + sx, self.y0 + sy), (sx, sy, ex, ey))


@functools.lru_cache(maxsize=16)
def _gball_rings(w: int, h: int, k: float, ring, outline: float, outline_col):
    """The g-ball's rings and crosshair, drawn once per size and style
    (smoothly, via supersampling) and reused every frame."""
    f = Frame.new(w, h)
    label_h = 34 * k
    r = min(w, h - label_h) / 2 - 12 * k
    cx, cy = w / 2, (h - label_h) / 2 + 4 * k
    lw = max(1.0, 3 * k)
    fine = _Fine(f, 0, 0, w, h)
    if outline:  # dark edge so the ring survives sky/concrete/white walls
        e = max(1.0, outline * k * 0.6)
        fine.ellipse(cx, cy, r + e, outline=outline_col, width=lw + 2 * e)
        fine.ellipse(cx, cy, r / 2 + e, outline=outline_col, width=max(1.0, 2 * k) + 2 * e)
    fine.ellipse(cx, cy, r, outline=ring, width=lw)
    fine.ellipse(cx, cy, r / 2, outline=ring, width=max(1.0, 2 * k))
    faint = ring[:3] + (ring[3] // 2,)
    cw = max(1.0, 1.2 * k)
    fine.line([(cx - r, cy), (cx + r, cy)], fill=faint, width=cw)
    fine.line([(cx, cy - r), (cx, cy + r)], fill=faint, width=cw)
    fine.finish()
    return f.img


def _dot(f: Frame, px: float, py: float, d: float, **kw):
    """One smooth dot (only its own few pixels are supersampled)."""
    pad = d + 2 + kw.get("width", 0)
    fine = _Fine(f, px - pad, py - pad, 2 * pad, 2 * pad)
    fine.ellipse(px, py, d, **kw)
    fine.finish()


def draw_gball(f: Frame, box, st: Style, lat, lon, trail=(), g_range=1.5):
    """lat: + = dot right. lon: + = dot up. Caller applies any inversion."""
    if not _ok(lat) and not _ok(lon):
        return
    x, y, w, h, k = box
    _panel(f, box, st)
    lat = lat if _ok(lat) else 0.0
    lon = lon if _ok(lon) else 0.0
    label_h = 34 * k
    r = min(w, h - label_h) / 2 - 12 * k
    x0, y0 = int(x), int(y)
    cx = x0 + int(w) / 2
    cy = y0 + (int(h) - label_h) / 2 + 4 * k
    rings = _gball_rings(int(w), int(h), round(k, 4), tuple(st.ring), st.outline,
                         tuple(st.outline_col) if st.outline_col else None)
    sx, sy = max(0, -x0), max(0, -y0)
    ex, ey = min(rings.width, f.w - x0), min(rings.height, f.h - y0)
    if ex > sx and ey > sy:
        f.img.alpha_composite(rings, (x0 + sx, y0 + sy), (sx, sy, ex, ey))

    def pos(la, lo):
        return (cx + max(-1.0, min(1.0, la / g_range)) * r,
                cy - max(-1.0, min(1.0, lo / g_range)) * r)

    n = len(trail)
    for i, (la, lo) in enumerate(trail):  # oldest first
        if not (_ok(la) and _ok(lo)):
            continue
        px, py = pos(la, lo)
        a = int(40 + 140 * (i + 1) / (n + 1))
        _dot(f, px, py, (3 + 3 * (i + 1) / (n + 1)) * k, fill=st.accent[:3] + (a,))
    px, py = pos(lat, lon)
    _dot(f, px, py, 10 * k, fill=st.accent, outline=(0, 0, 0, 160), width=max(1.0, 2 * k))
    _text(f, (cx, y + h - 8 * k), f"{math.hypot(lat, lon):.2f} g",
          _font(22 * k, st.font), st.fg, st, k, anchor="ms")


def track_image(path_xy, w: int, h: int, k: float, st: Style) -> Image.Image:
    """Pre-render the track outline once per size/style (drawing ~10k
    points every frame is wasted work). Drawn larger and shrunk so the
    line is smooth."""
    s = SUPERSAMPLE
    W, H = max(1, int(w)), max(1, int(h))
    img = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inset = 18 * k
    pts = [((inset + p[0] * (w - 2 * inset)) * s, (inset + p[1] * (h - 2 * inset)) * s)
           for p in path_xy]
    width = max(2.0, 3 * k)
    if st.outline:
        ow = width + 2 * max(1.0, k * 2)
        d.line(pts, fill=st.outline_col, width=max(1, int(round(ow * s))), joint="curve")
    d.line(pts, fill=st.track, width=max(1, int(round(width * s))), joint="curve")
    return img.resize((W, H), Image.LANCZOS)


def draw_map(f: Frame, box, st: Style, track_img: Image.Image, cur_xy):
    x, y, w, h, k = box
    _panel(f, box, st)
    f.img.alpha_composite(track_img, (int(x), int(y)))
    if cur_xy is not None:
        inset = 18 * k
        cx = x + inset + cur_xy[0] * (w - 2 * inset)
        cy = y + inset + cur_xy[1] * (h - 2 * inset)
        _dot(f, cx, cy, 9 * k, fill=st.accent, outline=(0, 0, 0, 200),
             width=max(1.0, 2 * k))


def draw_edit_boxes(f: Frame, boxes: dict, names, active: str | None = None,
                    labels: dict | None = None):
    """Outline each movable gauge with its name (layout edit mode)."""
    for name in names:
        x, y, w, h, k = boxes[name]
        hot = name == active
        col = (255, 216, 74, 255) if hot else (255, 216, 74, 170)
        f.draw.rectangle([x, y, x + w, y + h], outline=col,
                         width=3 if hot else 2)
        label = labels.get(name, name) if labels else WIDGETS.get(name, (name,))[0]
        font = _font(max(12, 16 * f.h / 1080))
        # Label sits just above the box (inside it if there's no room).
        ly = y - 4 if y > font.size + 6 else y + h + font.size + 4
        f.draw.text((x + 2, ly), label, font=font, fill=col, anchor="ls",
                    stroke_width=2, stroke_fill=(0, 0, 0, 200))


# ------------------------------------------------------- lap timer / corner --

def fmt_lap(sec: float) -> str:
    if sec is None or not math.isfinite(sec):
        return "-:--.-"
    sec = max(0.0, sec)
    m, s_ = divmod(sec, 60)
    return f"{int(m)}:{s_:04.1f}"


def draw_laptimer(f: Frame, box, st: Style, s: dict):
    """s: label, time, time_col ("new_best"/"slower"/None), delta, delta_label,
    last, best, bar."""
    x, y, w, h, k = box
    _panel(f, box, st)
    pad = 16 * k
    small = _font(21 * k, st.font)
    big = _font(64 * k, st.font)
    mid = _font(34 * k, st.font)
    _text(f, (x + pad, y + pad), s["label"], small, st.dim, st, k, anchor="la")
    if s.get("delta_label"):
        _text(f, (x + w - pad, y + pad), s["delta_label"], small, st.dim, st,
              k, anchor="ra")
    col = {"new_best": PURPLE}.get(s.get("time_col"), st.fg)
    _text(f, (x + w / 2, y + pad + 22 * k + 62 * k), fmt_lap(s["time"]), big,
          col, st, k, anchor="ms")
    dy = y + pad + 22 * k + 62 * k + 8 * k
    d = s.get("delta")
    if d is not None and math.isfinite(d):
        dcol = GOOD if d < 0 else BAD
        _text(f, (x + w / 2, dy + 32 * k), f"{d:+.2f}", mid, dcol, st, k,
              anchor="ms")
        if s.get("bar"):
            by0, by1 = dy + 40 * k, dy + 48 * k
            bx0, bx1 = x + pad, x + w - pad
            cx = (bx0 + bx1) / 2
            f.draw.rectangle([bx0, by0, bx1, by1], fill=(255, 255, 255, 45))
            frac = max(-1.0, min(1.0, d / 2.0))  # full bar = 2 s
            end = cx + frac * (bx1 - bx0) / 2
            f.draw.rectangle([min(cx, end), by0, max(cx, end), by1], fill=dcol)
            f.draw.line([cx, by0 - 3 * k, cx, by1 + 3 * k], fill=st.fg,
                        width=max(1, int(2 * k)))
    by = y + h - pad
    if s.get("last") is not None:
        _text(f, (x + pad, by), "LAST " + fmt_lap(s["last"]), small, st.dim,
              st, k, anchor="ls")
    if s.get("best") is not None:
        _text(f, (x + w - pad, by), "BEST " + fmt_lap(s["best"]), small,
              st.dim, st, k, anchor="rs")


def draw_corner(f: Frame, box, st: Style, s: dict):
    """s: entry, vmin, g (km/h, km/h, g; None = no corner yet), use_mph,
    live (bool, still in the corner), cmp (differences vs reference or None),
    ref (lap number compared against or None)."""
    x, y, w, h, k = box
    _panel(f, box, st)
    pad = 10 * k
    small = _font(15 * k, st.font)
    big = _font(32 * k, st.font)
    tiny = _font(15 * k, st.font)
    conv = (1 / 1.60934) if s["use_mph"] else 1.0
    cols = [("ENTRY", s.get("entry"), "{:.0f}", "entry"),
            ("MIN", s.get("vmin"), "{:.0f}", "vmin"),
            ("PEAK G", s.get("g"), "{:.2f}", "g")]
    if s.get("g") is None and s.get("entry") is not None:
        cols = cols[:2]
    cw = (w - 2 * pad) / len(cols)
    cmp = s.get("cmp") or {}
    val_col = st.fg if not s.get("live") else st.accent[:3] + (255,)
    for i, (lbl, val, fmt, key) in enumerate(cols):
        cx = x + pad + cw * (i + 0.5)
        if key == "g" and val is not None and s.get("gdir"):
            lbl = "◀ LEFT G" if s["gdir"] == "L" else "RIGHT G ▶"
        _text(f, (cx, y + pad), lbl, small, st.dim, st, k, anchor="ma")
        if val is None:
            txt = "--"
        else:
            txt = fmt.format(val * (conv if key != "g" else 1.0))
        vy = y + pad + 17 * k + 32 * k
        _text(f, (cx, vy), txt, big, val_col, st, k, anchor="ms")
        dv = cmp.get(key)
        if dv is not None:
            dv = dv * (conv if key != "g" else 1.0)
            txt = ("{:+.2f}" if key == "g" else "{:+.0f}").format(dv)
            col = st.dim if abs(dv) < (0.005 if key == "g" else 0.5) else \
                (GOOD if dv > 0 else BAD)
            _text(f, (cx, y + h - 8 * k), txt, tiny, col, st, k, anchor="ms")
    unit = "MPH" if s["use_mph"] else "KM/H"
    foot = unit + (f" · vs LAP {s['ref']}" if s.get("ref") else "")
    if not cmp:
        _text(f, (x + w / 2, y + h - 8 * k), foot, tiny, st.dim, st, k,
              anchor="ms")


# ----------------------------------------------------------- custom drawing --

def _color(st: Style, spec: str):
    named = {"accent": st.accent, "throttle": st.throttle, "brake": st.brake,
             "good": GOOD, "bad": BAD, "fg": st.fg}
    if spec in named:
        return named[spec]
    if isinstance(spec, str) and spec.startswith("#") and len(spec) == 7:
        try:
            return tuple(int(spec[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
        except ValueError:
            pass
    return st.accent


def fmt_value(v, decimals: int) -> str:
    if v is None or not math.isfinite(v):
        return "--"
    return f"{v:.{max(0, decimals)}f}"


def nice_step(span: float, target: int = 6) -> float:
    if span <= 0 or not math.isfinite(span):
        return 1.0
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def draw_number(f: Frame, box, st: Style, g: dict, v: dict):
    """v: value, unit, decimals, lo, hi, bar_frac (Race-style speed bar)."""
    x, y, w, h, k = box
    _panel(f, box, st)
    pad = 14 * k
    label = g.get("label") or ""
    vf = _font((52 if label else 62) * k, st.font)
    lf = _font(20 * k, st.font)
    uf = _font(24 * k, st.font)
    txt = fmt_value(v["value"], v["decimals"])
    unit = v["unit"]
    bar = v.get("bar_frac") is not None
    base = y + h - pad - (16 * k if bar else 0)
    if g.get("align") == "center":
        if label:
            _text(f, (x + w / 2, y + 10 * k), label, lf, st.dim, st, k,
                  anchor="ma")
        tw = f.draw.textlength(txt, font=vf)
        uw = (f.draw.textlength(unit, font=uf) + 8 * k) if unit else 0
        x0 = x + (w - tw - uw) / 2
        _text(f, (x0, base), txt, vf, st.fg, st, k, anchor="ls")
        if unit:
            _text(f, (x0 + tw + 8 * k, base), unit, uf, st.dim, st, k,
                  anchor="ls")
    else:
        if label:
            _text(f, (x + pad, y + 10 * k), label, lf, st.dim, st, k,
                  anchor="la")
        # Right-align digits to a fixed width so the number doesn't jitter.
        lo, hi = v.get("lo"), v.get("hi")
        widest = max((fmt_value(t, v["decimals"]) for t in (lo, hi)
                      if t is not None), key=len, default=txt)
        tmpl = re.sub(r"\d", "8", widest if len(widest) >= len(txt) else txt)
        right = x + pad + f.draw.textlength(tmpl, font=vf)
        _text(f, (right, base), txt, vf, st.fg, st, k, anchor="rs")
        if unit:
            _text(f, (right + 10 * k, base), unit, uf, st.dim, st, k,
                  anchor="ls")
    if bar:
        bx0, bx1 = x + pad, x + w - pad
        by1 = y + h - pad
        by0 = by1 - 8 * k
        f.draw.rectangle([bx0, by0, bx1, by1], fill=(255, 255, 255, 50))
        fr = max(0.0, min(1.0, v["bar_frac"]))
        if fr > 0:
            f.draw.rectangle([bx0, by0, bx0 + (bx1 - bx0) * fr, by1],
                             fill=st.accent)


def draw_bar(f: Frame, box, st: Style, g: dict, v: dict):
    x, y, w, h, k = box
    _panel(f, box, st)
    lo, hi, val = v["lo"], v["hi"], v["value"]
    frac = 0.0 if not _ok(val) or hi == lo else max(0.0, min(1.0, (val - lo) / (hi - lo)))
    col = _color(st, g.get("color", "accent"))
    red = g.get("redline")
    if red is not None and _ok(val) and val >= red:
        col = BAD
    lf = _font(17 * k, st.font)
    vf = _font(20 * k, st.font)
    r = max(1, int(5 * k))
    label = g.get("label") or ""
    txt = fmt_value(val, v["decimals"]) + (f" {v['unit']}" if v["unit"] else "")
    if g.get("orient") == "v":
        pad = 8 * k
        y0 = y + pad + (24 * k if g.get("show_value") else 0)
        y1 = y + h - pad - (22 * k if label else 0)
        x0, x1 = x + pad, x + w - pad
        f.draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=(0, 0, 0, 110))
        fh = (y1 - y0) * frac
        if fh > 1:
            f.draw.rounded_rectangle([x0, y1 - fh, x1, y1], radius=r, fill=col)
        if red is not None and hi != lo:
            ry = y1 - (y1 - y0) * max(0.0, min(1.0, (red - lo) / (hi - lo)))
            f.draw.line([x0, ry, x1, ry], fill=BAD, width=max(1, int(2 * k)))
        if label:
            _text(f, (x + w / 2, y + h - pad), label, lf, st.dim, st, k,
                  anchor="ms")
        if g.get("show_value"):
            _text(f, (x + w / 2, y + pad), fmt_value(val, v["decimals"]), vf,
                  st.fg, st, k, anchor="ma")
    else:
        pad = 10 * k
        top = y + pad
        if label:
            _text(f, (x + pad, top), label, lf, st.dim, st, k, anchor="la")
        if g.get("show_value"):
            _text(f, (x + w - pad, top), txt, vf, st.fg, st, k, anchor="ra")
        y0 = y + h - pad - 18 * k
        y1 = y + h - pad
        x0, x1 = x + pad, x + w - pad
        f.draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=(0, 0, 0, 110))
        fw = (x1 - x0) * frac
        if fw > 1:
            f.draw.rounded_rectangle([x0, y0, x0 + fw, y1], radius=r, fill=col)
        if red is not None and hi != lo:
            rx = x0 + (x1 - x0) * max(0.0, min(1.0, (red - lo) / (hi - lo)))
            f.draw.line([rx, y0, rx, y1], fill=BAD, width=max(1, int(2 * k)))


def draw_dial(f: Frame, box, st: Style, g: dict, v: dict):
    x, y, w, h, k = box
    _panel(f, box, st)
    lo, hi, val = v["lo"], v["hi"], v["value"]
    cx, cy = x + w / 2, y + h / 2 + 4 * k
    r = min(w, h) / 2 - 12 * k
    a0, sweep = 135.0, 270.0

    def ang(val_):
        fr = 0.0 if hi == lo else max(0.0, min(1.0, (val_ - lo) / (hi - lo)))
        return a0 + sweep * fr

    bb = [cx - r, cy - r, cx + r, cy + r]
    tw = max(2, int(9 * k))
    f.draw.arc(bb, a0, a0 + sweep, fill=(0, 0, 0, 120) if not st.panel
               else (255, 255, 255, 40), width=tw)
    red = g.get("redline")
    if red is not None and red < hi:
        f.draw.arc(bb, ang(red), a0 + sweep, fill=BAD, width=tw)
    col = _color(st, g.get("color", "accent"))
    if _ok(val):
        a = ang(val)
        if a > a0 + 0.5:
            f.draw.arc([cx - r + tw * 1.2, cy - r + tw * 1.2, cx + r - tw * 1.2,
                        cy + r - tw * 1.2], a0, a,
                       fill=col[:3] + (150,), width=max(1, int(4 * k)))
    # ticks + labels
    step = nice_step(hi - lo, 6)
    div = 1000.0 if v.get("thousands") else 1.0
    tf = _font(15 * k, st.font)
    t = math.ceil(lo / step) * step
    n = 0
    while t <= hi + 1e-9 and n < 30:
        a = math.radians(ang(t))
        r0, r1 = r - tw / 2 - 2 * k, r - tw / 2 - 12 * k
        f.draw.line([cx + r0 * math.cos(a), cy + r0 * math.sin(a),
                     cx + r1 * math.cos(a), cy + r1 * math.sin(a)],
                    fill=st.fg, width=max(1, int(2 * k)))
        rl = r - tw / 2 - 26 * k
        lab = t / div
        lab_s = f"{lab:.0f}" if abs(lab - round(lab)) < 1e-6 else f"{lab:g}"
        _text(f, (cx + rl * math.cos(a), cy + rl * math.sin(a)), lab_s, tf,
              st.dim, st, k, anchor="mm")
        t += step
        n += 1
    # needle
    if _ok(val):
        a = math.radians(ang(val))
        L = r - tw / 2 - 6 * k
        f.draw.line([cx, cy, cx + L * math.cos(a), cy + L * math.sin(a)],
                    fill=col, width=max(2, int(4 * k)))
    hub = 7 * k
    f.draw.ellipse([cx - hub, cy - hub, cx + hub, cy + hub], fill=st.fg)
    vf = _font(30 * k, st.font)
    sf = _font(14 * k, st.font)
    _text(f, (cx, cy + r * 0.62), fmt_value(val, v["decimals"]), vf, st.fg,
          st, k, anchor="ms")
    sub = " ".join(p for p in (g.get("label") or "", v["unit"],
                               "x1000" if v.get("thousands") else "") if p)
    if sub:
        _text(f, (cx, cy + r * 0.62 + 6 * k), sub, sf, st.dim, st, k,
              anchor="ma")


@functools.lru_cache(maxsize=16)
def _logo_sized(name: str, w: int, h: int, opacity: int, mtime: float):
    src = _logo(name, mtime)
    if src is None:
        return None
    img = src.resize((max(1, w), max(1, h)), Image.LANCZOS)
    if opacity < 100:
        a = img.getchannel("A").point(lambda v: v * opacity // 100)
        img.putalpha(a)
    return img


def draw_image(f: Frame, box, st: Style, g: dict, _v=None):
    """A logo: drawn as is (no gauge background), at its own aspect."""
    x, y, w, h, _k = box
    p = logo_path(g.get("image", ""))
    try:
        mt = os.path.getmtime(p) if p else 0.0
    except OSError:
        return
    img = _logo_sized(g["image"], int(round(w)), int(round(h)), int(g.get("opacity", 100)), mt)
    if img is None:
        return
    x0, y0 = int(round(x)), int(round(y))
    sx, sy = max(0, -x0), max(0, -y0)
    ex, ey = min(img.width, f.w - x0), min(img.height, f.h - y0)
    if ex > sx and ey > sy:
        f.img.alpha_composite(img, (x0 + sx, y0 + sy), (sx, sy, ex, ey))


def draw_text_gauge(f: Frame, box, st: Style, g: dict, _v=None):
    """Plain text (team name, car number): the style's font and outline."""
    x, y, w, h, k = box
    text = g.get("text") or ""
    if not text:
        return
    _panel(f, box, st)
    col = _color(st, g.get("color") or "fg")
    _text(f, (x + w / 2, y + h / 2), text, _font(TEXT_PX * k, st.font), col, st, k,
          anchor="mm")


CUSTOM_DRAW = {"number": draw_number, "bar": draw_bar, "dial": draw_dial,
               "image": draw_image, "text": draw_text_gauge}
