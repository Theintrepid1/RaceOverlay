# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Picture-in-picture: a second camera (driver-facing, rear) in a box on
the main video.

Which video and how it lines up is per recording, so it's saved in the
main video's sidecar under "pip": {"parts": [...], "offset": s, "synced":
bool}, where

    PiP video time = main video time + offset.

How the box looks (where, how big, crop, corners, border, opacity) is a
gauge of type "pip" in the layout, so it moves, resizes and snaps like any
other gauge. Only the first PiP gauge is used.

Exports: each chunk's ffmpeg command takes the PiP file(s) as extra inputs,
crops and scales them to the box, cuts the corners with a mask and lays
them on the picture before the gauges (the border is drawn with the
gauges, on top).

Preview: PipReader decodes small frames from the PiP video in the
background while the main video plays, and the overlay pastes them in.
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field

from PIL import Image, ImageDraw

from . import media

PIP_WIDTH = 480          # box width at 1080p, scale 1
PREVIEW_FPS = 12.0


@dataclass
class PipSource:
    parts: list = field(default_factory=list)
    offset: float = 0.0          # PiP video time = main video time + offset
    synced: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> "PipSource | None":
        if not d or not d.get("parts"):
            return None
        return cls([str(p) for p in d["parts"]], float(d.get("offset", 0.0)),
                   bool(d.get("synced", False)))

    def to_dict(self) -> dict:
        return asdict(self)


def gauge_of(layout: dict | None) -> dict | None:
    """The layout's PiP gauge (the first one that's switched on)."""
    return next((g for g in (layout or {}).get("custom", [])
                 if g.get("type") == "pip" and g.get("on", True)), None)


def span(info: media.MediaInfo, offset: float, main_offset: float) -> tuple[float, float]:
    """Telemetry times (= main video time + main_offset) that the PiP video
    covers."""
    lo = -offset + main_offset
    return lo, lo + info.duration


def sync_offset(pip_parts: list[tuple[str, float]], tel, main_offset: float):
    """Line the PiP camera up through the data file: sync it to the logger
    the same way the main camera is, then
        PiP time = main time + main_offset - pip_offset.
    Returns (offset, gosync result)."""
    from racecore import gosync
    res, *_ = gosync.sync_parts(pip_parts, tel)
    return main_offset - res.offset, res


# ------------------------------------------------------------------ looks --

def crop_of(g: dict) -> tuple[float, float, float, float]:
    c = g.get("crop") or (0, 0, 0, 0)
    try:
        lft, top, rgt, bot = (max(0.0, min(0.45, float(x))) for x in c)
    except (TypeError, ValueError):
        lft = top = rgt = bot = 0.0
    return lft, top, rgt, bot


def aspect_for(info: media.MediaInfo | None, g: dict) -> float:
    """Box height / width: the source's shape after the crop."""
    if info is None or not info.width:
        return 9 / 16
    lft, top, rgt, bot = crop_of(g)
    w = info.width * (1 - lft - rgt)
    h = info.height * (1 - top - bot)
    return max(0.2, min(3.0, h / max(1.0, w)))


def mask(w: int, h: int, radius: float) -> Image.Image:
    """The box's shape as an L image (255 inside), corners rounded and
    smoothed by drawing 4x bigger and shrinking."""
    s = 4
    big = Image.new("L", (max(1, w) * s, max(1, h) * s), 0)
    ImageDraw.Draw(big).rounded_rectangle([0, 0, w * s - 1, h * s - 1],
                                          radius=max(0, radius) * s, fill=255)
    return big.resize((max(1, w), max(1, h)), Image.LANCZOS)


def box_px(box) -> tuple[int, int, int, int]:
    """Gauge box (floats) -> even pixel x, y, w, h for ffmpeg."""
    x, y, w, h, _k = box
    return int(round(x)), int(round(y)), max(2, int(round(w)) // 2 * 2), \
        max(2, int(round(h)) // 2 * 2)


# ---------------------------------------------------------------- export --

def segment_inputs_and_graph(info: media.MediaInfo, src: PipSource, g: dict, box,
                             seg_start: float, dur: float, fps: float, first_input: int,
                             mask_path: str, dec_args: list[str], base: str
                             ) -> tuple[list[str], list[str], str]:
    """ffmpeg inputs and filter steps that lay the PiP video on `base` for
    one chunk of the main video [seg_start, seg_start + dur). Returns
    (input args, filter steps, output label); no inputs/steps and `base`
    back when the PiP video doesn't cover this chunk."""
    t0 = seg_start + src.offset                  # PiP time at the chunk's start
    lead = max(0.0, -t0)                          # chunk starts before the PiP video
    spans = info.spans(max(0.0, t0), dur - lead)
    if not spans:
        return [], [], base
    x, y, w, h = box_px(box)
    lft, top, rgt, bot = crop_of(g)
    args = []
    for path, ss, d in spans:
        args += dec_args + ["-ss", f"{ss:.6f}", "-t", f"{d:.6f}", "-i", path]
    m = first_input + len(spans)
    args += ["-loop", "1", "-i", mask_path]
    steps = []
    for j in range(len(spans)):
        crop = ""
        if lft or top or rgt or bot:
            crop = (f"crop=iw*{1 - lft - rgt:.4f}:ih*{1 - top - bot:.4f}:"
                    f"iw*{lft:.4f}:ih*{top:.4f},")
        steps.append(f"[{first_input + j}:v]{crop}scale={w}:{h},setsar=1[pp{j}]")
    if len(spans) > 1:
        steps.append("".join(f"[pp{j}]" for j in range(len(spans)))
                     + f"concat=n={len(spans)}:v=1:a=0[ppj]")
        joined = "ppj"
    else:
        joined = "pp0"
    covered = sum(d for _p, _s, d in spans)
    steps.append(f"[{joined}]fps={fps},format=rgba,setpts=PTS-STARTPTS+{lead:.6f}/TB[ppd]")
    steps.append(f"[{m}:v]format=gray[ppm]")
    steps.append("[ppd][ppm]alphamerge[ppa]")
    op = max(10, min(100, int(g.get("opacity", 100))))
    last = "ppa"
    if op < 100:
        steps.append(f"[ppa]colorchannelmixer=aa={op / 100:.3f}[ppo]")
        last = "ppo"
    # only while the PiP video has pictures (no frozen last frame after it ends)
    steps.append(f"[{base}][{last}]overlay={x}:{y}:eof_action=pass:"
                 f"enable='between(t,{lead:.6f},{lead + covered:.6f})'[ppout]")
    return args, steps, "ppout"


# --------------------------------------------------------------- preview --

class PipReader:
    """Small frames of the PiP video for the live preview.

    A background ffmpeg decodes from wherever it's asked for, at a low frame
    rate and size, and keeps about a second of frames ahead. frame_at(t)
    gives the newest decoded frame at or before PiP time t; asking for a
    time far from what's buffered (a seek) restarts the decoder there."""

    def __init__(self, info: media.MediaInfo, width: int = 480):
        self.info = info
        self.w = max(16, int(width) // 2 * 2)
        self.h = max(16, int(round(self.w * info.height / max(1, info.width))) // 2 * 2)
        self._lock = threading.Lock()
        self._frames: list[tuple[float, Image.Image]] = []
        self._want = 0.0
        self._gen = 0
        self._proc = None
        self._thread = None
        self._stop = False
        self._last: tuple[float, Image.Image] | None = None
        self._start = -1e9

    def close(self):
        self._stop = True
        self._gen += 1
        self._kill()

    def _kill(self):
        p = self._proc
        self._proc = None
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass

    def frame_at(self, t: float) -> Image.Image | None:
        if t < 0 or t > self.info.duration:
            return None
        self._want = t
        with self._lock:
            frames = self._frames
            ok = bool(frames) and frames[0][0] - 0.2 <= t <= frames[-1][0] + 1.0
            # just restarted near here: give the decoder time to deliver
            starting = not frames and self._thread is not None and \
                self._thread.is_alive() and abs(t - self._start) < 1.0
            if ok:
                best = None
                for ft, im in frames:
                    if ft <= t + 0.5 / PREVIEW_FPS:
                        best = (ft, im)
                    else:
                        break
                if best is not None:
                    self._last = best
                # drop what's behind
                while len(self._frames) > 2 and self._frames[1][0] <= t:
                    self._frames.pop(0)
        if not ok and not starting:
            self._restart(t)
        if self._last is not None and abs(self._last[0] - t) < 1.5:
            return self._last[1]
        return None

    def _restart(self, t: float):
        self._gen += 1
        self._kill()
        with self._lock:
            self._frames = []
        gen = self._gen
        self._start = t
        self._thread = threading.Thread(target=self._run, args=(t, gen), daemon=True)
        self._thread.start()

    def _run(self, t: float, gen: int):
        size = self.w * self.h * 3
        while not self._stop and gen == self._gen:
            spans = self.info.spans(t, 1e9)
            if not spans:
                return
            path, ss, _d = spans[0]
            start_t = t
            cmd = ["ffmpeg", "-v", "error", "-ss", f"{ss:.3f}", "-i", path, "-an",
                   "-vf", f"fps={PREVIEW_FPS},scale={self.w}:{self.h}",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
            except OSError:
                return
            self._proc = proc
            i = 0
            while gen == self._gen:
                # stay about a second ahead of what's being shown
                with self._lock:
                    ahead = self._frames[-1][0] - self._want if self._frames else 0
                if ahead > 1.0:
                    time.sleep(0.02)
                    continue
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                im = Image.frombytes("RGB", (self.w, self.h), buf)
                with self._lock:
                    if gen == self._gen:
                        self._frames.append((start_t + i / PREVIEW_FPS, im))
                i += 1
            try:
                proc.kill()
            except OSError:
                pass
            # carry on into the next chapter
            t = start_t + i / PREVIEW_FPS
            if gen != self._gen or not self.info.spans(t + 0.05, 1e9):
                return
