# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Intro and end cards: a few silent seconds before and after the footage.

Intro: title (event or track), date, driver and car, logos.
End: best lap, lap count and top speed of what was rendered, a thank-you
line, logos.
Behind them: the first (or last) frame of the video, blurred and darkened,
or a solid color. Each card fades in from and out to black.

The engine renders each card as its own short clip with the same encoder
settings as the footage, and joins them losslessly with the rest.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import gauges, media

FADE_S = 0.5


@dataclass
class Cards:
    intro: bool = False
    end: bool = False
    seconds: float = 4.0
    bg: str = "blur"            # "blur" (the video's frame) or "color"
    color: str = "#14171c"
    title: str = ""             # e.g. "VIR Full Course"; blank = no title line
    date: str = ""              # e.g. "September 27, 2026"
    driver: str = ""
    car: str = ""
    thanks: str = ""            # end card line, e.g. "Thanks to ..."
    logos: bool = True          # the layout's logo gauges on both cards

    @classmethod
    def from_dict(cls, d: dict | None) -> "Cards":
        d = d or {}
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        c = cls(**known)
        c.seconds = float(max(1.0, min(15.0, c.seconds)))
        return c

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def any(self) -> bool:
        return self.intro or self.end


# ------------------------------------------------------------------ numbers --

def stats(ctx, ranges, offset: float) -> dict:
    """Best lap, timed laps and top speed inside the rendered video ranges
    (video time; telemetry time = video time + offset)."""
    laps = []
    if ctx.laps is not None and ctx.laps.ok:
        for lap in ctx.laps.laps:
            if not lap.timed:
                continue
            a, b = lap.start - offset, lap.end - offset
            if any(a >= r0 - 0.5 and b <= r1 + 0.5 for r0, r1 in ranges):
                laps.append(lap)
    best = min(laps, key=lambda lap: lap.duration, default=None)
    top = None
    spd = ctx.telem.channels.get("speed")
    if spd is not None:
        t = ctx.telem.time
        m = np.zeros(len(t), bool)
        for r0, r1 in ranges:
            m |= (t >= r0 + offset) & (t <= r1 + offset)
        v = spd[m]
        v = v[np.isfinite(v)]
        if v.size:
            top = float(v.max()) * (0.621371 if ctx.use_mph else 1.0)
    return {"best": best.duration if best else None, "best_n": best.n if best else None,
            "laps": len(laps), "top": top, "unit": "mph" if ctx.use_mph else "km/h"}


def fmt_lap(sec: float) -> str:
    m, s = divmod(max(0.0, sec), 60)
    return f"{int(m)}:{s:06.3f}"


# -------------------------------------------------------------------- frames --

def grab_frame(info: media.MediaInfo, t: float, w: int, h: int) -> Image.Image | None:
    """One video frame at merged-timeline time t, scaled to w x h."""
    t = max(0.0, min(t, info.duration - 0.05))
    spans = info.spans(t, 0.05)
    if not spans:
        return None
    path, ss, _ = spans[0]
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{ss:.3f}", "-i", path, "-frames:v", "1",
             "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) < w * h * 3:
        return None
    return Image.frombytes("RGB", (w, h), out[:w * h * 3])


def _hex(c: str) -> tuple[int, int, int]:
    try:
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, TypeError, IndexError):
        return (20, 23, 28)


def background(spec: Cards, frame: Image.Image | None, w: int, h: int) -> Image.Image:
    if spec.bg == "blur" and frame is not None:
        img = frame.convert("RGB").resize((w, h))
        img = img.filter(ImageFilter.GaussianBlur(radius=max(4, h // 40)))
        return Image.blend(img, Image.new("RGB", (w, h), (0, 0, 0)), 0.55)
    return Image.new("RGB", (w, h), _hex(spec.color))


def _logos(layout: dict | None) -> list[Image.Image]:
    out = []
    for g in (layout or {}).get("custom", []):
        if g.get("type") == "image" and g.get("on", True):
            img = gauges.load_logo(g.get("image", ""))
            if img is not None:
                out.append(img)
    return out


def _text(d: ImageDraw.ImageDraw, xy, s: str, px: float, fill):
    f = gauges._font(int(px), "bold")
    d.text(xy, s, font=f, fill=fill, anchor="mm",
           stroke_width=max(1, int(px / 22)), stroke_fill=(0, 0, 0, 160))


def _logo_row(img: Image.Image, logos, cy: float, max_h: float):
    """Logos side by side, centred on cy, each at most max_h tall."""
    if not logos:
        return
    w = img.width
    gap = max_h * 0.5
    sized = []
    for lg in logos:
        s = min(max_h / lg.height, (w * 0.25) / lg.width)
        sized.append(lg.resize((max(1, int(lg.width * s)), max(1, int(lg.height * s))),
                               Image.LANCZOS))
    total = sum(s.width for s in sized) + gap * (len(sized) - 1)
    if total > w * 0.9:            # too many: shrink them all to fit
        k = w * 0.9 / total
        sized = [s.resize((max(1, int(s.width * k)), max(1, int(s.height * k))),
                          Image.LANCZOS) for s in sized]
        gap *= k
        total = w * 0.9
    x = (w - total) / 2
    for s in sized:
        img.alpha_composite(s, (int(x), int(cy - s.height / 2)))
        x += s.width + gap


def card_image(kind: str, spec: Cards, w: int, h: int, frame: Image.Image | None,
               st: dict | None = None, layout: dict | None = None) -> Image.Image:
    """The intro ("intro") or end ("end") card as an RGB picture."""
    img = background(spec, frame, w, h).convert("RGBA")
    d = ImageDraw.Draw(img)
    k = h / 1080.0
    white, dim, accent = (245, 245, 240, 255), (200, 200, 195, 255), (255, 216, 74, 255)
    logos = _logos(layout) if spec.logos else []
    lines: list[tuple[str, float, tuple]] = []
    if kind == "intro":
        if spec.title:
            lines.append((spec.title, 96, white))
        if spec.date:
            lines.append((spec.date, 44, dim))
        who = "  ·  ".join(x for x in (spec.driver, spec.car) if x)
        if who:
            lines.append((who, 56, accent))
    else:
        st = st or {}
        if st.get("best") is not None:
            lines.append(("BEST LAP", 40, dim))
            lines.append((fmt_lap(st["best"]), 120, accent))
            if st.get("best_n"):
                lines.append((f"Lap {st['best_n']}", 40, dim))
        bits = []
        if st.get("laps"):
            bits.append(f"{st['laps']} lap{'s' if st['laps'] != 1 else ''}")
        if st.get("top") is not None:
            bits.append(f"top speed {st['top']:.0f} {st.get('unit', '')}")
        if bits:
            lines.append(("  ·  ".join(bits), 48, white))
        if spec.thanks:
            lines.append((spec.thanks, 44, white))
    if not lines and not logos:
        return img.convert("RGB")
    # stack the lines (and logos) around the middle of the frame
    gaps = [px * 0.45 for _, px, _ in lines]
    logo_h = 150 * k if logos else 0
    heights = [px * k for _, px, _ in lines]
    total = sum(heights) + sum(g * k for g in gaps[:-1]) + (logo_h + 70 * k if logos else 0)
    y = (h - total) / 2
    for (s, px, col), hh, g in zip(lines, heights, gaps):
        size = px * k
        f = gauges._font(int(size), "bold")
        while f.getlength(s) > w * 0.9 and size > 12:       # long line: shrink it
            size *= 0.92
            f = gauges._font(int(size), "bold")
        _text(d, (w / 2, y + hh / 2), s, size, col)
        y += hh + g * k
    if logos:
        _logo_row(img, logos, y + 40 * k + logo_h / 2, logo_h)
    return img.convert("RGB")


# ------------------------------------------------------------------- encode --

def encode(img: Image.Image, seconds: float, fps: float, out: str, encoder: str,
           quality: str, threads: int | None = None) -> None:
    """A still card as a short silent clip that fades in and out, encoded
    like the footage so the two can be joined without re-encoding."""
    w, h = img.size
    raw = img.convert("RGB").tobytes()
    fade = min(FADE_S, seconds / 4)
    frames = max(1, int(round(seconds * fps)))
    vf = (f"fps={fps},fade=t=in:st=0:d={fade:.3f},"
          f"fade=t=out:st={max(0.0, seconds - fade):.3f}:d={fade:.3f},"
          f"{media.encoder_filter_tail(encoder)}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += media.hw_global_args(encoder)
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", f"{fps}",
            "-i", "pipe:0", "-vf", vf, "-frames:v", str(frames), "-an"]
    cmd += media.encoder_args(encoder, quality, threads)
    cmd += ["-f", "mp4", out + ".tmp"]
    # the same frame, once per output frame (a raw pipe can't be looped)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(frames + 2):
            proc.stdin.write(raw)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"card failed: {err.strip()[-400:]}")
    os.replace(out + ".tmp", out)


def card_seconds(spec: Cards, kind: str) -> float:
    return spec.seconds if getattr(spec, kind) else 0.0


def fmt_date(d) -> str:
    """'September 27, 2026' (no leading zero, any platform)."""
    return f"{d:%B} {d.day}, {d.year}"
