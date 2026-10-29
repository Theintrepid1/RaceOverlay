# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Command-line entry point for raceoverlay (the app uses the same engine)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from racecore import telemetry

from . import demo, engine, gauges, media


def _parse_overrides(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--map expects channel=Column, got '{p}'")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _apply_sync_file(args):
    """Fill unset args from a sync sidecar (<video>.sync.json); flags win."""
    sync = {}
    if args.sync:
        with open(args.sync, encoding="utf-8") as fh:
            sync = json.load(fh)
    if not args.video and sync.get("parts"):
        args.video = sync["parts"]
    if args.csv is None:
        args.csv = sync.get("csv")
    if args.offset is None:
        args.offset = float(sync.get("offset", 0.0))
    if args.g_rotate is None and sync.get("g_rotate") is not None:
        args.g_rotate = float(sync["g_rotate"])
    if args.g_flip is None:
        args.g_flip = bool(sync.get("g_flip", False))
    if args.style is None:
        args.style = sync.get("style")
    if args.invert_lat is None:
        args.invert_lat = bool(sync.get("invert_lat", False))
    if args.invert_lon is None:
        args.invert_lon = bool(sync.get("invert_lon", False))
    args.layout = sync.get("layout")
    args.gauge_opts = sync.get("gauge_opts")
    args.sync_overrides = sync.get("overrides") or {}
    if not args.kmh and sync.get("use_mph") is False:
        args.kmh = True


def _lap_ranges(tel, laps: list[int], offset: float, pad: float, dur: float):
    lap = tel.channels.get("lap")
    if lap is None:
        raise SystemExit("--lap needs a lap channel in the CSV")
    out = []
    for n in laps:
        idx = [i for i, v in enumerate(lap) if round(v) == n]
        if not idx:
            raise SystemExit(f"No lap {n} in the data")
        a = tel.time[idx[0]] - offset - pad
        nxt = idx[-1] + 1
        b = (tel.time[nxt] if nxt < len(tel.time) else tel.time[-1]) - offset + pad
        a, b = max(0.0, a), min(dur, b)
        if b <= a:
            raise SystemExit(f"Lap {n} isn't inside the video at this offset")
        out.append((n, [a, b]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="raceoverlay",
        description="Burn racing telemetry gauges onto a video.")
    ap.add_argument("--video", nargs="+",
                    help="Source video, or several files to treat as one "
                         "(e.g. GoPro chapters, in order)")
    ap.add_argument("--chapters", action="store_true",
                    help="Auto-add the other GoPro chapters of --video")
    ap.add_argument("--csv", help="Telemetry CSV file")
    ap.add_argument("-o", "--output", default="overlay_out.mp4",
                    help="Output path (with --lap --split: used as a name prefix)")
    ap.add_argument("--sync", metavar="FILE",
                    help="Load settings saved by the app (<video>.sync.json)")
    ap.add_argument("--offset", type=float, default=None,
                    help="Seconds to shift data vs video (+ = data ahead)")
    ap.add_argument("--start", type=float, default=0.0, metavar="SEC",
                    help="Begin at this video time")
    ap.add_argument("--limit", type=float, metavar="SEC",
                    help="Only render N seconds")
    ap.add_argument("--lap", type=int, action="append", metavar="N",
                    help="Export lap N (repeatable) instead of the whole video")
    ap.add_argument("--split", action="store_true",
                    help="With --lap: one file per lap (default: joined)")
    ap.add_argument("--pad", type=float, default=3.0,
                    help="Seconds before/after each lap (default 3)")
    ap.add_argument("--fps", type=float, metavar="F",
                    help="Output framerate (default: source, capped at 60)")
    ap.add_argument("--height", type=int, metavar="PX",
                    help="Output height, e.g. 1080, 720 (default: source)")
    ap.add_argument("--quality", choices=list(media.QUALITY),
                    default="Balanced")
    ap.add_argument("--encoder", help="ffmpeg encoder (default: best detected)")
    ap.add_argument("--workers", type=int, default=0,
                    help="Parallel workers (default: auto)")
    ap.add_argument("--no-resume", action="store_true",
                    help="Start over instead of reusing finished segments")
    ap.add_argument("--g-rotate", type=float, default=None, metavar="DEG",
                    help="Raw ax/ay sensor angle (default: auto-detect)")
    ap.add_argument("--g-flip", action="store_true", default=None,
                    help="Mirror the raw lateral axis (with --g-rotate)")
    ap.add_argument("--style", choices=sorted(gauges.STYLES))
    ap.add_argument("--invert-lat", action="store_true", default=None,
                    help="G-ball: flip left/right")
    ap.add_argument("--invert-lon", action="store_true", default=None,
                    help="G-ball: flip up/down")
    ap.add_argument("--map", dest="maps", action="append", metavar="chan=Col",
                    help="Override column mapping, e.g. --map speed='GPS Speed'")
    ap.add_argument("--kmh", action="store_true", help="Show km/h")
    ap.add_argument("--inspect", action="store_true",
                    help="Print detected channel mapping for --csv and exit")
    ap.add_argument("--encoders", action="store_true",
                    help="List encoders that work on this machine and exit")
    ap.add_argument("--demo", action="store_true",
                    help="Render a sample with synthetic data")
    args = ap.parse_args(argv)
    _apply_sync_file(args)

    if args.encoders:
        hw = media.detect_hw(force=True)
        for e in hw["encoders"]:
            print(f"{e:12s} {media.ENCODERS[e][0]}")
        print(f"hardware decode: {'yes' if hw['hwdecode'] else 'no'}")
        return 0

    if args.demo:
        tmp = tempfile.mkdtemp(prefix="raceoverlay_demo_")
        args.csv = demo.write_demo_csv(os.path.join(tmp, "demo.csv"))
        args.video = [demo.write_demo_video(os.path.join(tmp, "demo.mp4"))]
        print(f"Demo inputs in {tmp}")

    if not args.csv:
        ap.error("--csv is required (or --sync / --demo)")
    overrides = dict(getattr(args, "sync_overrides", {}) or {})
    overrides.update(_parse_overrides(args.maps or []))
    tel = telemetry.load(args.csv, overrides=overrides, g_rotate=args.g_rotate,
                         g_flip=bool(args.g_flip))
    print(telemetry.describe(tel))
    if tel.g_source == "raw":
        a = tel.g_align
        fit = (f", fit {a['lon_fit']:.0%} accel / {a['lat_fit']:.0%} cornering"
               if a.get("lon_fit") is not None and a.get("lat_fit") is not None
               else "")
        print(f"  g-sensor angle {a['angle']:+.0f} deg"
              f"{' mirrored' if a['flip'] else ''}"
              f" ({'auto-detected' if a.get('auto') else 'set'}{fit})")
    if args.inspect:
        return 0
    if not args.video:
        ap.error("--video is required to render (or use --inspect)")

    parts = args.video
    if args.chapters and len(parts) == 1:
        parts = media.find_chapters(parts[0])
        print(f"Chapters: {', '.join(os.path.basename(p) for p in parts)}")
    info = media.probe(parts)
    hw = media.detect_hw()
    encoder = args.encoder or media.default_encoder(hw["encoders"])
    fps = args.fps or min(info.fps, 60.0)

    if args.lap:
        laps = _lap_ranges(tel, args.lap, args.offset, args.pad, info.duration)
        if args.split:
            stem, ext = os.path.splitext(args.output)
            outputs = [{"path": f"{stem}_lap{n}{ext or '.mp4'}", "ranges": [r]}
                       for n, r in laps]
        else:
            outputs = [{"path": args.output, "ranges": [r for _, r in laps]}]
    else:
        end = info.duration if args.limit is None else min(
            info.duration, args.start + args.limit)
        outputs = [{"path": args.output, "ranges": [[args.start, end]]}]

    job = engine.Job(
        parts=parts, csv=args.csv, outputs=outputs, offset=args.offset,
        fps=fps, height=args.height, encoder=encoder, quality=args.quality,
        workers=args.workers, hwdecode=hw["hwdecode"],
        resume=not args.no_resume, use_mph=not args.kmh, style=args.style,
        layout=args.layout, invert_lat=bool(args.invert_lat),
        invert_lon=bool(args.invert_lon),
        g_rotate=tel.g_align.get("angle") if tel.g_source == "raw" else None,
        g_flip=bool(tel.g_align.get("flip")) if tel.g_source == "raw" else False,
        gauge_opts=args.gauge_opts, overrides=overrides or None)
    try:
        os.setpgrp()
    except OSError:
        pass
    for p in engine.run(job):
        print(f"\nWrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
