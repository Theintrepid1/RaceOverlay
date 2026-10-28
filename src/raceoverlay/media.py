# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Video sources: GoPro chapter detection, multi-file "virtual" merges,
probing, lossless merge-to-file, and hardware encoder detection."""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

# GoPro chapter names: G + H (AVC) / X (HEVC) / L (low-res proxy) / P,
# 2-digit chapter, 4-digit recording number. e.g. GH020123.MP4 = chapter 2.
CHAPTER_RE = re.compile(r"^(G[A-Z])(\d{2})(\d{4})(\.mp4)$", re.IGNORECASE)


def find_chapters(path: str) -> list[str]:
    """All chapters of the GoPro recording `path` belongs to, in order.
    Returns [path] for anything that isn't a chaptered GoPro file."""
    p = Path(path)
    m = CHAPTER_RE.match(p.name)
    if not m:
        return [str(p)]
    prefix, _, num, _ = m.groups()
    found = []
    for q in p.parent.iterdir():
        mq = CHAPTER_RE.match(q.name)
        if mq and mq.group(1).upper() == prefix.upper() and mq.group(3) == num:
            found.append((int(mq.group(2)), str(q)))
    found.sort()
    return [f for _, f in found] or [str(p)]


@dataclass
class Part:
    path: str
    start: float      # where this file begins on the merged timeline
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    has_audio: bool


@dataclass
class MediaInfo:
    parts: list[Part]
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool

    @property
    def paths(self) -> list[str]:
        return [p.path for p in self.parts]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MediaInfo":
        return cls(parts=[Part(**p) for p in d["parts"]],
                   **{k: v for k, v in d.items() if k != "parts"})

    def spans(self, start: float, dur: float) -> list[tuple[str, float, float]]:
        """(path, local start, local duration) for each file overlapping
        [start, start+dur) on the merged timeline."""
        out, end = [], start + dur
        for p in self.parts:
            a, b = max(start, p.start), min(end, p.start + p.duration)
            if b - a > 1e-6:
                out.append((p.path, a - p.start, b - a))
        return out

    def compatible(self) -> tuple[bool, str]:
        """Can the parts be joined losslessly (same codec/size/fps)?"""
        a = self.parts[0]
        for p in self.parts[1:]:
            if (p.codec, p.width, p.height) != (a.codec, a.width, a.height) \
                    or abs(p.fps - a.fps) > 0.01:
                return False, (f"{Path(p.path).name} is {p.width}x{p.height} "
                               f"{p.fps:.2f}fps {p.codec}, but "
                               f"{Path(a.path).name} is {a.width}x{a.height} "
                               f"{a.fps:.2f}fps {a.codec}")
        return True, ""


def _probe_one(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
         "-of", "json", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def probe(paths: list[str] | str) -> MediaInfo:
    """Probe one file or several (treated as one continuous video)."""
    if isinstance(paths, str):
        paths = [paths]
    parts, t = [], 0.0
    for path in paths:
        d = _probe_one(path)
        v = next((s for s in d["streams"] if s.get("codec_type") == "video"), None)
        if v is None:
            raise ValueError(f"No video stream in {path}")
        num, den = v["r_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else 30.0
        dur = float(d["format"]["duration"])
        has_a = any(s.get("codec_type") == "audio" for s in d["streams"])
        parts.append(Part(path, t, dur, int(v["width"]), int(v["height"]), fps,
                          v.get("codec_name", "?"), has_a))
        t += dur
    a = parts[0]
    return MediaInfo(parts, a.width, a.height, a.fps, t,
                     all(p.has_audio for p in parts))


def mpv_url(paths: list[str]) -> str:
    """Single file -> its path; several -> an mpv EDL that plays them as one
    seamless timeline (nothing written to disk)."""
    if len(paths) == 1:
        return paths[0]
    segs = [f"%{len(p.encode('utf-8'))}%{p}" for p in paths]
    return "edl://" + ";".join(segs)


def concat_list(paths: list[str], dest: str):
    def esc(p):
        return p.replace("'", "'\\''")
    with open(dest, "w", encoding="utf-8") as fh:
        for p in paths:
            fh.write(f"file '{esc(os.path.abspath(p))}'\n")


def merged_name(paths: list[str]) -> str:
    """GH010123.MP4 + GH020123.MP4 -> GH000123_merged.mp4"""
    p = Path(paths[0])
    m = CHAPTER_RE.match(p.name)
    stem = f"{m.group(1)}00{m.group(3)}" if m else p.stem
    return str(p.with_name(f"{stem}_merged.mp4"))


def save_merged_cmd(paths: list[str], out: str, list_file: str) -> list[str]:
    """ffmpeg command for a lossless (stream-copy) join."""
    concat_list(paths, list_file)
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-movflags", "+faststart", out]


# ------------------------------------------------------- encoder detection --

def render_node() -> str | None:
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else None


# name -> (label, codec family, kind)
ENCODERS = {
    "libx264":    ("H.264 · CPU (x264)", "h264", "cpu"),
    "h264_vaapi": ("H.264 · GPU (VAAPI / Intel Quick Sync, AMD)", "h264", "vaapi"),
    "h264_qsv":   ("H.264 · GPU (Intel QSV)", "h264", "qsv"),
    "h264_nvenc": ("H.264 · GPU (NVIDIA NVENC)", "h264", "nvenc"),
    "h264_amf":   ("H.264 · GPU (AMD AMF)", "h264", "amf"),
    "libx265":    ("H.265 · CPU (x265)", "hevc", "cpu"),
    "hevc_vaapi": ("H.265 · GPU (VAAPI)", "hevc", "vaapi"),
    "hevc_qsv":   ("H.265 · GPU (Intel QSV)", "hevc", "qsv"),
    "hevc_nvenc": ("H.265 · GPU (NVIDIA NVENC)", "hevc", "nvenc"),
    "hevc_amf":   ("H.265 · GPU (AMD AMF)", "hevc", "amf"),
}

# Quality presets -> per-encoder-kind quantizer.
QUALITY = {
    "High":     {"cpu_h264": 18, "cpu_hevc": 22, "hw": 20},
    "Balanced": {"cpu_h264": 21, "cpu_hevc": 25, "hw": 24},
    "Small":    {"cpu_h264": 25, "cpu_hevc": 29, "hw": 28},
}


def hw_global_args(encoder: str) -> list[str]:
    kind = ENCODERS.get(encoder, ("", "", "cpu"))[2]
    node = render_node()
    if kind == "vaapi" and node:
        return ["-init_hw_device", f"vaapi=va:{node}", "-filter_hw_device", "va"]
    if kind == "qsv":
        return ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
    return []


def encoder_filter_tail(encoder: str) -> str:
    """Filter appended to the final video chain before encoding."""
    kind = ENCODERS.get(encoder, ("", "", "cpu"))[2]
    if kind == "vaapi":
        return "format=nv12,hwupload"
    if kind == "qsv":
        return "format=nv12"
    return "format=yuv420p"


def encoder_args(encoder: str, quality: str = "Balanced",
                 threads: int | None = None) -> list[str]:
    label, fam, kind = ENCODERS.get(encoder, ENCODERS["libx264"])
    q = QUALITY.get(quality, QUALITY["Balanced"])
    if kind == "cpu":
        crf = q["cpu_h264"] if fam == "h264" else q["cpu_hevc"]
        a = ["-c:v", encoder, "-preset", "veryfast", "-crf", str(crf)]
        if fam == "hevc":
            a += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
        if threads:
            a += ["-threads", str(threads)]
        return a
    qp = str(q["hw"])
    if kind == "vaapi":
        a = ["-c:v", encoder, "-rc_mode", "CQP", "-qp", qp]
    elif kind == "qsv":
        a = ["-c:v", encoder, "-global_quality", qp, "-preset", "faster"]
    elif kind == "nvenc":
        a = ["-c:v", encoder, "-preset", "p4", "-rc", "vbr", "-cq", qp,
             "-b:v", "0"]
    else:  # amf
        a = ["-c:v", encoder, "-quality", "speed", "-rc", "cqp",
             "-qp_i", qp, "-qp_p", qp]
    if fam == "hevc":
        a += ["-tag:v", "hvc1"]
    return a


def _cache_file() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "raceoverlay" / "hw.json"


def _ffmpeg_id() -> str:
    try:
        exe = subprocess.run(["which", "ffmpeg"], capture_output=True,
                             text=True).stdout.strip()
        st = os.stat(exe)
        return f"{exe}:{st.st_mtime_ns}:{render_node()}"
    except OSError:
        return "?"


def _try(cmd: list[str]) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def detect_hw(force: bool = False) -> dict:
    """Which encoders actually work here (a 5-frame test encode each), and
    whether VAAPI hardware decoding works. Cached per ffmpeg build."""
    cache = _cache_file()
    fid = _ffmpeg_id()
    if not force and cache.exists():
        try:
            d = json.loads(cache.read_text())
            if d.get("id") == fid:
                return d
        except (OSError, ValueError):
            pass
    try:
        listed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                capture_output=True, text=True).stdout
    except OSError:
        listed = ""
    works = []
    for enc in ENCODERS:
        if f" {enc} " not in listed:
            continue
        cmd = (["ffmpeg", "-hide_banner", "-v", "error"] + hw_global_args(enc)
               + ["-f", "lavfi", "-i", "color=black:s=320x240:r=30",
                  "-frames:v", "5", "-vf", encoder_filter_tail(enc)]
               + encoder_args(enc) + ["-f", "null", "-"])
        if _try(cmd):
            works.append(enc)
    hwdec = False
    node = render_node()
    if node:
        tmp = cache.parent / "probe.mp4"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        if _try(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                 "testsrc=s=320x240:r=30", "-frames:v", "10", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", str(tmp)]):
            hwdec = _try(["ffmpeg", "-v", "error", "-hwaccel", "vaapi",
                          "-hwaccel_device", node, "-i", str(tmp),
                          "-f", "null", "-"])
        tmp.unlink(missing_ok=True)
    d = {"id": fid, "encoders": works or ["libx264"], "hwdecode": hwdec}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(d))
    except OSError:
        pass
    return d


def default_encoder(encoders: list[str]) -> str:
    """Prefer a GPU H.264 encoder (frees the CPU for drawing gauges)."""
    for enc in ("h264_vaapi", "h264_nvenc", "h264_qsv", "h264_amf"):
        if enc in encoders:
            return enc
    return "libx264"
