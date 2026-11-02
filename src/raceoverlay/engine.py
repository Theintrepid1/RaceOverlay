# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Segmented, parallel, resumable renderer.

The export is cut into ~20 s segments. Worker processes (one per core by
default) each draw the gauges for a segment and run their own ffmpeg, so
both the Python drawing and the video work spread across the CPU. Finished
segments are kept on disk; a stopped or crashed render resumes by skipping
them. At the end the segments are joined losslessly and the original audio
is added once, so there are no clicks at the joins. Intro and end cards
(raceoverlay.cards) are rendered as their own short clips with the same
encoder settings and joined on either side, with silence under them.

Only the gauges' screen areas are sent to ffmpeg (packed into a small
"atlas" image and cropped back out), not full transparent frames.

The chunks, the audio and the joined file are made in a work folder on
this computer's own disk (when there's room), and only the finished video
is copied to where it was asked for. Saving to a network share then costs
one copy, not chunks + join + faststart all going over the network. The
audio is pulled out while the gauges render. Every step after the
rendering says what it's doing (STAGE lines; STEP lines when there's a
fraction to show), so nothing sits at 100% without a word.

    python -m raceoverlay.engine job.json
"""
from __future__ import annotations

import collections
import hashlib
import json
import multiprocessing as mp
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from racecore import osutil, telemetry

from . import cards as cardmod
from . import gauges, media
from .render import OverlayContext, draw_overlay

SEGMENT_SECONDS = 20.0
TILE_PAD = 8  # px around each gauge box, for outlines/strokes


@dataclass
class Output:
    path: str
    ranges: list  # [[start, end], ...] on the (merged) video timeline


@dataclass
class Job:
    parts: list
    csv: str
    outputs: list                 # list[Output | dict]
    offset: float = 0.0
    fps: float | None = None      # None = source
    height: int | None = None     # None = source
    encoder: str = "libx264"
    quality: str = "Balanced"
    workers: int = 0              # 0 = auto
    hwdecode: bool = False
    resume: bool = True
    use_mph: bool = True
    style: str | None = None
    layout: dict | None = None
    invert_lat: bool = False
    invert_lon: bool = False
    g_rotate: float | None = None
    g_flip: bool = False
    gauge_opts: dict | None = None
    overrides: dict | None = None
    cards: dict | None = None     # raceoverlay.cards.Cards as a dict
    faststart: bool = True        # index at the front (players start sooner)

    def __post_init__(self):
        self.outputs = [o if isinstance(o, Output) else Output(**o)
                        for o in self.outputs]

    @classmethod
    def load(cls, path: str) -> "Job":
        with open(path, encoding="utf-8") as fh:
            return cls(**json.load(fh))

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=1)

    def fingerprint(self, out: Output) -> str:
        """Anything that changes the picture invalidates finished segments.
        (Cards and the audio are cached under names of their own.)"""
        d = asdict(self)
        for k in ("outputs", "workers", "resume", "cards", "faststart"):
            d.pop(k)
        d["out"] = asdict(out)
        stamps = []
        for f in list(self.parts) + [self.csv]:
            try:
                st = os.stat(f)
                stamps.append([f, st.st_size, st.st_mtime_ns])
            except OSError:
                stamps.append([f, 0, 0])
        d["stamps"] = stamps
        d["version"] = 6
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()


def default_workers() -> int:
    n = os.cpu_count() or 2
    return max(1, min(8, n // 2 if n >= 4 else n))


def output_size(info: media.MediaInfo, height: int | None) -> tuple[int, int]:
    h = height or info.height
    w = int(round(info.width * h / info.height / 2)) * 2
    return w, int(h) // 2 * 2


def _legacy_workdir(out_path: str) -> Path:
    p = Path(out_path)
    return p.with_name(f".{p.name}.parts")


def _local_workdir(out_path: str) -> Path:
    key = hashlib.sha1(os.path.abspath(out_path).encode()).hexdigest()[:16]
    return osutil.cache_dir() / "renders" / key


def workdir_for(out_path: str) -> Path:
    """This output's work folder: an existing one (on this computer first,
    then next to the output as older versions made), else where a new one
    goes (see choose_workdir)."""
    for d in (_local_workdir(out_path), _legacy_workdir(out_path)):
        if d.exists():
            return d
    return _local_workdir(out_path)


def _free(path: Path) -> int:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0


def estimate_bytes(info: media.MediaInfo, seconds: float) -> int:
    """Rough size of a render of `seconds`: the source's own bitrate."""
    total = 0
    for p in info.paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    rate = total / info.duration if info.duration else 8e6
    return int(rate * seconds)


def choose_workdir(out: Output, info: media.MediaInfo) -> Path:
    """Local work folder if this computer has room for the chunks plus the
    joined file (about twice the video), else next to the output."""
    for d in (_local_workdir(out.path), _legacy_workdir(out.path)):
        if d.exists():
            return d
    need = 2 * estimate_bytes(info, sum(b - a for a, b in out.ranges)) + (512 << 20)
    local = _local_workdir(out.path)
    return local if _free(local.parent) >= need else _legacy_workdir(out.path)


# ---------------------------------------------------------------- planning --

@dataclass
class Segment:
    out_index: int
    index: int
    start: float     # video time of the first frame
    frames: int
    path: str

    @property
    def done(self) -> bool:
        return os.path.exists(self.path)


def plan(job: Job, fps: float) -> list[Segment]:
    """Chunks of up to SEGMENT_SECONDS, shorter for short exports so every
    worker still gets several (resume granularity stays reasonable too)."""
    segs = []
    total = sum(b - a for o in job.outputs for a, b in o.ranges)
    workers = max(1, job.workers or default_workers())
    seg_s = max(2.0, min(SEGMENT_SECONDS, total / (workers * 3)))
    n_per = max(1, int(round(seg_s * fps)))
    for oi, out in enumerate(job.outputs):
        wd = workdir_for(out.path)
        idx = 0
        for a, b in out.ranges:
            total = max(1, int(round((b - a) * fps)))
            starts = list(range(0, total, n_per))
            if len(starts) > 1 and total - starts[-1] < n_per // 2:
                starts.pop()  # fold a tiny tail into the previous segment
            for j, k in enumerate(starts):
                end = starts[j + 1] if j + 1 < len(starts) else total
                segs.append(Segment(oi, idx, a + k / fps, end - k,
                                    str(wd / f"seg_{idx:05d}.mp4")))
                idx += 1
    return segs


def prepare_workdirs(job: Job, info: media.MediaInfo | None = None) -> None:
    for out in job.outputs:
        wd = choose_workdir(out, info) if info is not None else workdir_for(out.path)
        fp = job.fingerprint(out)
        stamp = wd / "job.sha1"
        if wd.exists() and not (job.resume and stamp.exists()
                                and stamp.read_text().strip() == fp):
            shutil.rmtree(wd, ignore_errors=True)
        wd.mkdir(parents=True, exist_ok=True)
        osutil.hide_path(wd)  # ".name.parts" isn't hidden by name on Windows
        stamp.write_text(fp)
        (wd / "output.txt").write_text(out.path, encoding="utf-8")   # for cleanup


# ----------------------------------------------------------------- workers --

_W: dict = {}


def _init_worker(job_d: dict, info_d: dict, fps: float, counter):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    job = Job(**job_d)
    info = media.MediaInfo.from_dict(info_d)
    tel = telemetry.load(job.csv, overrides=job.overrides,
                         g_rotate=job.g_rotate, g_flip=job.g_flip)
    ctx = OverlayContext.build(tel, use_mph=job.use_mph, style=job.style,
                               layout=job.layout, invert_lat=job.invert_lat,
                               invert_lon=job.invert_lon,
                               opts=job.gauge_opts)
    _W.update(job=job, info=info, fps=fps, ctx=ctx, counter=counter)


def _tiles(ctx: OverlayContext, w: int, h: int):
    """Screen rectangles of the shown gauges, padded and clipped."""
    boxes = gauges.layout_boxes(ctx.layout, w, h)
    out = []
    for name in ctx.shown():
        x, y, bw, bh, _ = boxes[name]
        x0 = max(0, int(x) - TILE_PAD)
        y0 = max(0, int(y) - TILE_PAD)
        x1 = min(w, int(x + bw) + TILE_PAD + 1)
        y1 = min(h, int(y + bh) + TILE_PAD + 1)
        if x1 > x0 and y1 > y0:
            out.append((x0, y0, x1, y1))
    return out


def _segment_cmd(seg: Segment, w: int, h: int, tiles, atlas_wh, hwdec: bool):
    job, info, fps = _W["job"], _W["info"], _W["fps"]
    spans = info.spans(seg.start, seg.frames / fps + 1.0 / fps)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += media.hw_global_args(job.encoder)
    dec = media.hwdec_args() if hwdec else []
    for path, ss, dur in spans:
        cmd += dec
        cmd += ["-ss", f"{ss:.6f}", "-t", f"{dur:.6f}", "-i", path]
    graph = []
    # Join first, then change frame rate once, so chapter joins don't drop
    # or duplicate frames.
    for i in range(len(spans)):
        graph.append(f"[{i}:v]scale={w}:{h},setsar=1[p{i}]")
    if len(spans) > 1:
        graph.append("".join(f"[p{i}]" for i in range(len(spans)))
                     + f"concat=n={len(spans)}:v=1:a=0[j]")
        graph.append(f"[j]fps={fps}[base]")
    else:
        graph.append(f"[p0]fps={fps}[base]")
    last = "base"
    if tiles:
        aw, ah = atlas_wh
        ai = len(spans)
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{aw}x{ah}",
                "-r", f"{fps}", "-i", "pipe:0"]
        n = len(tiles)
        if n > 1:
            graph.append(f"[{ai}:v]split={n}" + "".join(f"[s{i}]" for i in range(n)))
        else:
            graph.append(f"[{ai}:v]null[s0]")
        y = 0
        for i, (x0, y0, x1, y1) in enumerate(tiles):
            tw, th = x1 - x0, y1 - y0
            graph.append(f"[s{i}]crop={tw}:{th}:0:{y}[t{i}]")
            graph.append(f"[{last}][t{i}]overlay={x0}:{y0}:eof_action=pass[o{i}]")
            last = f"o{i}"
            y += th
    graph.append(f"[{last}]{media.encoder_filter_tail(job.encoder)}[vout]")
    threads = max(1, (os.cpu_count() or 2) // max(1, job.workers or 1))
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]",
            "-frames:v", str(seg.frames), "-an"]
    cmd += media.encoder_args(job.encoder, job.quality, threads)
    cmd += ["-f", "mp4", seg.path + ".tmp"]
    return cmd


def _render_segment(seg_d: dict) -> int:
    seg = Segment(**seg_d)
    job, info, fps, ctx = _W["job"], _W["info"], _W["fps"], _W["ctx"]
    counter = _W["counter"]
    w, h = output_size(info, job.height)
    tiles = _tiles(ctx, w, h)
    atlas_w = max([x1 - x0 for x0, _, x1, _ in tiles], default=0)
    atlas_h = sum(y1 - y0 for _, y0, _, y1 in tiles)
    canvas = gauges.Frame.new(w, h) if tiles else None
    atlas = Image.new("RGBA", (max(1, atlas_w), max(1, atlas_h)), (0, 0, 0, 0))
    clear = (0, 0, 0, 0)

    attempts = [job.hwdecode, False] if job.hwdecode else [False]
    err = ""
    for hwdec in attempts:
        cmd = _segment_cmd(seg, w, h, tiles, (atlas_w, atlas_h), hwdec)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if tiles else None,
                                stderr=subprocess.PIPE)
        sent = 0
        try:
            if tiles:
                for i in range(seg.frames):
                    t = seg.start + i / fps + job.offset
                    for (x0, y0, x1, y1) in tiles:
                        canvas.img.paste(clear, (x0, y0, x1, y1))
                    draw_overlay(canvas, ctx, t)
                    y = 0
                    for (x0, y0, x1, y1) in tiles:
                        atlas.paste(canvas.img.crop((x0, y0, x1, y1)), (0, y))
                        y += y1 - y0
                    proc.stdin.write(atlas.tobytes())
                    sent += 1
                    if sent % 10 == 0:
                        with counter.get_lock():
                            counter.value += 10
            proc.stdin and proc.stdin.close()
        except BrokenPipeError:
            pass
        err = proc.stderr.read().decode(errors="replace")
        rc = proc.wait()
        with counter.get_lock():
            counter.value += (sent % 10) if tiles else seg.frames
        if rc == 0:
            os.replace(seg.path + ".tmp", seg.path)
            return seg.frames
        with counter.get_lock():
            counter.value -= sent if tiles else seg.frames
    raise RuntimeError(f"segment {seg.index} failed: {err.strip()[-600:]}")


# --------------------------------------------------------------- finishing --

def _run_ffmpeg_progress(cmd: list[str], duration: float, report,
                         at_end=None) -> tuple[int, str]:
    """Run ffmpeg, calling report(fraction 0..1) as it goes (via -progress).
    at_end() is called once when the last frame has been written but
    ffmpeg is still busy (the faststart rewrite happens then)."""
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    ended = False
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration > 0:
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            f = max(0.0, min(1.0, us / 1e6 / duration))
            report(f)
            if f >= 0.999 and not ended and at_end:
                ended = True
                at_end()
        elif line.startswith("progress=end") and not ended and at_end:
            ended = True
            at_end()
    err = proc.stderr.read()
    return proc.wait(), err


def _make_cards(job: Job, info: media.MediaInfo, out: Output, fps: float,
                ctx: OverlayContext | None) -> tuple[str | None, str | None]:
    """Intro and end card clips for one output (kept in its work folder, so
    a resumed render doesn't redo them)."""
    spec = cardmod.Cards.from_dict(job.cards)
    if not spec.any:
        return None, None
    wd = workdir_for(out.path)
    w, h = output_size(info, job.height)
    paths = []
    for kind, t in (("intro", out.ranges[0][0]), ("end", out.ranges[-1][1] - 0.1)):
        if not getattr(spec, kind):
            paths.append(None)
            continue
        key = hashlib.sha1(json.dumps([kind, spec.to_dict(), out.ranges, w, h, fps,
                                       job.encoder, job.quality, job.layout],
                                      sort_keys=True).encode()).hexdigest()[:12]
        dest = wd / f"card_{kind}_{key}.mp4"
        if not dest.exists():
            frame = cardmod.grab_frame(info, t, w, h) if spec.bg == "blur" else None
            st = cardmod.stats(ctx, out.ranges, job.offset) if (kind == "end" and ctx) else None
            img = cardmod.card_image(kind, spec, w, h, frame, st, job.layout)
            cardmod.encode(img, spec.seconds, fps, str(dest), job.encoder, job.quality)
        paths.append(str(dest))
    return paths[0], paths[1]


def _card_secs(job: Job, cards: tuple[str | None, str | None]) -> tuple[float, float]:
    spec = cardmod.Cards.from_dict(job.cards)
    return (spec.seconds if cards[0] else 0.0, spec.seconds if cards[1] else 0.0)


def _audio_cmd(info: media.MediaInfo, out: Output, pre: float, post: float,
               dest: str) -> list[str]:
    """The output's soundtrack: its ranges of the source audio joined, with
    silence under the cards (fading in after the intro, out before the end)."""
    acmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    n = 0
    for a, b in out.ranges:
        for path, ss, dur in info.spans(a, b - a):
            acmd += ["-ss", f"{ss:.6f}", "-t", f"{dur:.6f}", "-i", path]
            n += 1
    if pre or post:
        fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
        parts = [f"[{i}:a]{fmt}[r{i}]" for i in range(n)]
        body = "".join(f"[r{i}]" for i in range(n))
        body_s = sum(b - a for a, b in out.ranges)
        fades = []
        if pre:
            fades.append(f"afade=t=in:d={cardmod.FADE_S}")
        if post:
            fades.append(f"afade=t=out:st={max(0.0, body_s - cardmod.FADE_S):.3f}"
                         f":d={cardmod.FADE_S}")
        parts.append(f"{body}concat=n={n}:v=0:a=1," + ",".join(fades) + "[body]")
        for name, secs in (("pre", pre), ("post", post)):
            if secs:
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{secs:.3f}[{name}]")
        seq = (["[pre]"] if pre else []) + ["[body]"] + (["[post]"] if post else [])
        parts.append("".join(seq) + f"concat=n={len(seq)}:v=0:a=1[a]")
        graph = ";".join(parts)
    else:
        graph = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
    return acmd + ["-filter_complex", graph, "-map", "[a]", "-c:a", "aac",
                   "-b:a", "192k", dest]


def _audio_path(job: Job, out: Output, pre: float, post: float) -> Path:
    key = hashlib.sha1(json.dumps([out.ranges, pre, post, job.parts],
                                  sort_keys=True).encode()).hexdigest()[:12]
    return workdir_for(out.path) / f"audio_{key}.m4a"


def start_audio(job: Job, info: media.MediaInfo, out: Output,
                pre: float, post: float) -> subprocess.Popen | None:
    """Pull this output's audio out in the background (while the gauges
    render). None when there's no audio or it's already there."""
    if not info.has_audio:
        return None
    dest = _audio_path(job, out, pre, post)
    if dest.exists():
        return None
    tmp = str(dest) + ".tmp.m4a"
    cmd = _audio_cmd(info, out, pre, post, tmp)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    proc._dest = (tmp, str(dest))       # type: ignore[attr-defined]
    return proc


def _finish_audio(proc) -> bool:
    if proc is None:
        return True
    proc.stderr.read()
    rc = proc.wait()
    tmp, dest = proc._dest
    if rc == 0:
        os.replace(tmp, dest)
        return True
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def _same_disk(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _deliver(src: Path, dest: str, stage, step) -> None:
    """Put the finished file where it was asked for: a rename on the same
    disk, else one copy (with progress) to a temporary name, then a rename."""
    d = Path(dest)
    d.parent.mkdir(parents=True, exist_ok=True)
    if _same_disk(src, d.parent):
        os.replace(src, d)
        return
    stage("copy", f"Copying to {d.parent}")
    total = max(1, src.stat().st_size)
    tmp = d.with_name(f".{d.name}.copying")
    done, t0, last = 0, time.time(), 0.0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                buf = fi.read(16 << 20)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                now = time.time()
                if now - last >= 0.25 or done == total:
                    last = now
                    rate = done / max(1e-6, now - t0) / 1e6
                    step(done / total, f"{done / 1e6:,.0f} of {total / 1e6:,.0f} MB, "
                                       f"{rate:.0f} MB/s")
        if tmp.stat().st_size != total:
            raise OSError("the copy came out the wrong size")
        os.replace(tmp, d)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    src.unlink()


def _finalize(job: Job, info: media.MediaInfo, out: Output,
              segs: list[Segment], stage, step,
              cards: tuple[str | None, str | None] = (None, None),
              audio_proc=None) -> None:
    wd = workdir_for(out.path)
    lst = wd / "segments.txt"
    intro, end = cards
    pre, post = _card_secs(job, cards)
    total_s = sum(b - a for a, b in out.ranges) + pre + post
    media.concat_list(([intro] if intro else []) + [s.path for s in segs]
                      + ([end] if end else []), str(lst))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(lst)]
    if info.has_audio:
        aud = _audio_path(job, out, pre, post)
        if audio_proc is not None and audio_proc.poll() is None:
            stage("audio", "Collecting the audio (it was started with the render)")
        ok = _finish_audio(audio_proc) if audio_proc is not None else aud.exists()
        if not ok or not aud.exists():          # not started, or it failed: do it now
            stage("audio", "Collecting the audio")
            tmp = str(aud) + ".tmp.m4a"
            rc, _ = _run_ffmpeg_progress(_audio_cmd(info, out, pre, post, tmp), total_s,
                                         lambda f: step(f, ""))
            if rc == 0:
                os.replace(tmp, aud)
        if aud.exists():
            cmd += ["-i", str(aud), "-map", "0:v", "-map", "1:a"]
        else:
            cmd += ["-map", "0:v"]
    else:
        cmd += ["-map", "0:v"]
    final = wd / ("final" + Path(out.path).suffix)
    cmd += ["-c", "copy"]
    if job.faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += [str(final)]
    stage("join", "Writing the final file")
    rc, err = _run_ffmpeg_progress(
        cmd, total_s, lambda f: step(f, ""),
        at_end=(lambda: stage("faststart", "Optimizing for playback (moving the "
                                           "index to the front)"))
        if job.faststart else None)
    if rc != 0:
        raise RuntimeError(f"joining segments failed: {err.strip()[-600:]}")
    _deliver(final, out.path, stage, step)
    stage("cleanup", "Cleaning up the work files")
    shutil.rmtree(wd, ignore_errors=True)


# --------------------------------------------------------------------- run --

def _fmt_eta(s: float) -> str:
    if s != s or s == float("inf"):
        return "--:--"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60}:{s % 60:02d}"


def _render_all(job: Job, info: media.MediaInfo, fps: float, pending, already: int,
                total: int, progress, stage) -> None:
    """Draw the gauges for every unfinished segment, in parallel."""
    if not pending:
        return
    ctx = mp.get_context("spawn")
    counter = ctx.Value("q", 0)
    hist = collections.deque(maxlen=20)
    finishing = [False]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=min(job.workers, len(pending)),
                             mp_context=ctx, initializer=_init_worker,
                             initargs=(asdict(job), info.to_dict(), fps,
                                       counter)) as ex:
        futs = [ex.submit(_render_segment, asdict(s)) for s in pending]
        while True:
            done, not_done = wait(futs, timeout=0.5,
                                  return_when=FIRST_EXCEPTION)
            for f in done:
                if f.exception():
                    for g in not_done:
                        g.cancel()
                    raise f.exception()
            now = time.time()
            n = already + counter.value
            hist.append((now, n))
            rate = 0.0
            if len(hist) >= 2 and hist[-1][0] > hist[0][0]:
                rate = (hist[-1][1] - hist[0][1]) / (hist[-1][0] - hist[0][0])
            eta = (total - n) / rate if rate > 0 else float("inf")
            if n >= total and not_done and not finishing[0]:
                finishing[0] = True
                stage("finishing", "Finishing the last chunks")
            progress.write(f"PROGRESS {n} {total} {rate:.1f} "
                           f"{rate / fps:.3f} {eta:.0f}\n")
            if progress.isatty():
                progress.write(f"\r  {100 * n / total:5.1f}%  {rate:6.1f} fps  "
                               f"{rate / fps:4.2f}x realtime  "
                               f"{_fmt_eta(eta)} left ")
            progress.flush()
            if not not_done:
                break
    progress.write(f"INFO rendered in {time.time() - t0:.1f}s\n")


def run(job: Job, progress=sys.stderr) -> list[str]:
    info = media.probe(job.parts)
    fps = min(job.fps or info.fps, info.fps)
    if job.workers <= 0:
        job.workers = default_workers()
    prepare_workdirs(job, info)
    segs = plan(job, fps)
    pending = [s for s in segs if not s.done]
    total = sum(s.frames for s in segs)
    already = total - sum(s.frames for s in pending)
    w, h = output_size(info, job.height)
    progress.write(f"INFO {w}x{h} {fps:.3f}fps {job.encoder} "
                   f"workers={job.workers} segments={len(segs)} "
                   f"resumed={len(segs) - len(pending)} "
                   f"work={workdir_for(job.outputs[0].path)}\n")
    progress.flush()

    def stage(key: str, text: str):
        progress.write(f"STAGE {key}\t{text}\n")
        if progress.isatty():
            progress.write(f"\n  {text}…\n")
        progress.flush()

    def step_for(oi: int, n_out: int):
        last = [-1.0]

        def step(f: float, detail: str = ""):
            if f - last[0] >= 0.005 or f >= 1.0 or detail:
                last[0] = f
                files = f"\tfile {oi + 1} of {n_out}" if n_out > 1 else "\t"
                progress.write(f"STEP {f:.4f}{files}\t{detail}\n")
                if progress.isatty():
                    progress.write(f"\r  {100 * f:5.1f}%  {detail}      ")
                progress.flush()
        return step

    # the audio needs no picture: start pulling it out now, alongside the render
    spec = cardmod.Cards.from_dict(job.cards)
    audio = [start_audio(job, info, o, spec.seconds if spec.intro else 0.0,
                         spec.seconds if spec.end else 0.0)
             for o in job.outputs]

    try:
        _render_all(job, info, fps, pending, already, total, progress, stage)
    except BaseException:
        for p in audio:                 # don't leave audio jobs running
            if p is not None and p.poll() is None:
                p.kill()
        raise

    card_ctx = None
    if spec.any:
        stage("cards", "Making the title cards")
        tel = telemetry.load(job.csv, overrides=job.overrides,
                             g_rotate=job.g_rotate, g_flip=job.g_flip)
        card_ctx = OverlayContext.build(tel, use_mph=job.use_mph, style=job.style,
                                        layout=job.layout, opts=job.gauge_opts)
    n_out = len(job.outputs)
    for oi, out in enumerate(job.outputs):
        made = _make_cards(job, info, out, fps, card_ctx) if card_ctx else (None, None)
        _finalize(job, info, out, [s for s in segs if s.out_index == oi],
                  stage, step_for(oi, n_out), made, audio[oi])
    progress.write(f"PROGRESS {total} {total} 0 0 0\nDONE\n")
    progress.flush()
    return [o.path for o in job.outputs]


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    osutil.new_process_group()  # lets the GUI pause/stop us and our children
    job = Job.load(args[0])
    try:
        for p in run(job):
            print(f"Wrote {p}")
    except Exception as e:
        sys.stderr.write(f"ERROR {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
