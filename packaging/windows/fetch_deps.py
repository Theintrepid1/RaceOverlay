# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Download the Windows programs Race Overlay needs into windows-bin/.

  ffmpeg.exe, ffprobe.exe   BtbN FFmpeg-Builds, win64 GPL static, same 8.1
                            series as the Linux AppImage. Includes the NVENC,
                            Intel QSV and AMD AMF encoders.
  libmpv-2.dll              shinchiro/mpv-winbuild-cmake, x86_64 baseline
                            (not the -v3 build, which needs AVX2).

Used by the run-from-source setup (windows/setup.bat) and later by the
installer build. Run it with the project's Python:

    python packaging/windows/fetch_deps.py [--dest DIR] [--force]

The mpv builds ship as .7z using the BCJ2 filter, which Python's 7z
libraries can't unpack, so this also fetches 7-Zip's own standalone
extractor (7zr.exe, from the official 7-Zip releases) and uses that.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-n8.1-latest-win64-gpl-8.1.zip")
MPV_API = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
MPV_ASSET = re.compile(r"^mpv-dev-x86_64-\d{8}-git-[0-9a-f]+\.7z$")
SEVENZR_URLS = (  # "latest" first; the pinned one is the fallback
    "https://github.com/ip7z/7zip/releases/latest/download/7zr.exe",
    "https://github.com/ip7z/7zip/releases/download/26.05/7zr.exe",
)
UA = {"User-Agent": "raceoverlay-fetch-deps"}


def _get(url: str, what: str) -> bytes:
    print(f"Downloading {what}…", flush=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf, got, last = io.BytesIO(), 0, -10
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            buf.write(chunk)
            got += len(chunk)
            pct = 100 * got // total if total else 0
            if total and pct >= last + 10:
                print(f"  {pct}%  ({got >> 20} of {total >> 20} MB)", flush=True)
                last = pct
    return buf.getvalue()


def fetch_ffmpeg(dest: Path):
    data = _get(FFMPEG_URL, "ffmpeg (about 200 MB)")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = {Path(n).name.lower(): n for n in z.namelist()}
        for exe in ("ffmpeg.exe", "ffprobe.exe"):
            if exe not in names:
                raise RuntimeError(f"{exe} not found in the ffmpeg download")
            with z.open(names[exe]) as src, open(dest / exe, "wb") as out:
                shutil.copyfileobj(src, out)
        lic = names.get("license.txt")
        if lic:
            (dest / "FFmpeg-LICENSE.txt").write_bytes(z.read(lic))


def mpv_asset_url() -> tuple[str, str]:
    req = urllib.request.Request(MPV_API, headers=dict(UA, Accept="application/vnd.github+json"))
    with urllib.request.urlopen(req, timeout=60) as r:
        rel = json.load(r)
    for a in rel.get("assets", []):
        if MPV_ASSET.match(a["name"]):
            return a["name"], a["browser_download_url"]
    raise RuntimeError("no mpv-dev-x86_64 build in shinchiro/mpv-winbuild-cmake's "
                       f"latest release ({rel.get('tag_name')})")


def _sevenzr(tmp: Path) -> list[str]:
    """Command to run a 7-Zip console extractor: one already installed
    (7z/7zr on PATH), else the standalone 7zr.exe downloaded into tmp."""
    for name in ("7zr", "7z", "7za"):
        exe = shutil.which(name)
        if exe:
            return [exe]
    last = None
    for url in SEVENZR_URLS:
        try:
            exe = tmp / "7zr.exe"
            exe.write_bytes(_get(url, "7-Zip extractor (7zr.exe, under 1 MB)"))
            return [str(exe)]
        except OSError as e:
            last = e
    raise RuntimeError(f"couldn't download 7zr.exe: {last}")


def fetch_mpv(dest: Path):
    name, url = mpv_asset_url()
    data = _get(url, f"libmpv ({name})")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        arc = tmp / name
        arc.write_bytes(data)
        out = tmp / "out"
        # e = extract without folder structure; -r searches subfolders
        cmd = _sevenzr(tmp) + ["e", str(arc), f"-o{out}", "libmpv-2.dll", "-r", "-y"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        dll = out / "libmpv-2.dll"
        if r.returncode != 0 or not dll.exists():
            raise RuntimeError(f"extracting libmpv-2.dll from {name} failed:\n"
                               + (r.stdout + r.stderr).strip()[-800:])
        shutil.copy2(dll, dest / "libmpv-2.dll")
    (dest / "mpv-VERSION.txt").write_text(name + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", type=Path, default=ROOT / "windows-bin")
    ap.add_argument("--force", action="store_true", help="download again even if present")
    a = ap.parse_args(argv)
    a.dest.mkdir(parents=True, exist_ok=True)
    have = lambda *f: all((a.dest / x).exists() for x in f)  # noqa: E731
    if a.force or not have("ffmpeg.exe", "ffprobe.exe"):
        fetch_ffmpeg(a.dest)
    else:
        print("ffmpeg already there")
    if a.force or not have("libmpv-2.dll"):
        fetch_mpv(a.dest)
    else:
        print("libmpv already there")
    print(f"Done: {a.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
