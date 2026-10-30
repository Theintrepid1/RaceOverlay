# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Per-track profiles: official turn names and positions, and an optional
track-map image lined up with the GPS trace.

Stored as JSON, one file per track, found again by the start/finish line's
position. A profile has its own origin (its start line), so it lines up with
any session at that track.

Two places, merged by file name:
  built-in   tracks/ in the repo, shipped inside every build (read-only).
             Turns and start line only: official map images are the
             tracks' artwork, so they stay on the user's own machine.
  local      <data dir>/raceoverlay/tracks/ (osutil.data_dir()). New tracks,
             and edits to a built-in one, which then override it (same file
             name) until "Reset to built-in" removes the local copy.

The map image is placed with a similarity transform (scale, rotation,
shift) from two point pairs the user clicks: a spot on the image and the
same spot on the trace, twice. Turns are clicked on the image and snapped
to the trace, then stored as latitude/longitude.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import osutil

SAME_TRACK_M = 150.0


def tracks_dir() -> Path:
    """Local tracks (this machine's own and edited ones)."""
    return osutil.data_dir() / "tracks"


def builtin_dir() -> Path | None:
    """The tracks/ folder shipped with the app: next to src/ in a checkout,
    inside the bundle in a packaged build."""
    env = os.environ.get("RACEOVERLAY_BUILTIN_TRACKS")
    cands = [Path(env)] if env else []
    if getattr(sys, "frozen", False):
        cands.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tracks")
    cands.append(Path(__file__).resolve().parents[2] / "tracks")
    return next((c for c in cands if c.is_dir()), None)


def to_xy(lat, lon, lat0: float, lon0: float) -> np.ndarray:
    """Degrees to local metres (east, north) around (lat0, lon0)."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    return np.column_stack([np.atleast_1d((lon - lon0) * np.cos(np.radians(lat0)) * 111320.0),
                            np.atleast_1d((lat - lat0) * 110540.0)])


def to_latlon(xy, lat0: float, lon0: float) -> np.ndarray:
    xy = np.atleast_2d(np.asarray(xy, float))
    return np.column_stack([lat0 + xy[:, 1] / 110540.0,
                            lon0 + xy[:, 0] / (np.cos(np.radians(lat0)) * 111320.0)])


# ------------------------------------------------------------- transforms ---
# Image pixel (x right, y down) -> complex z = x - i*y (so up is up), then
# world = a*z + b with complex a (scale + rotation) and b (shift), in metres.

def similarity(img_pts, world_pts) -> tuple[complex, complex]:
    """Transform taking two image points onto two world points."""
    (x1, y1), (x2, y2) = img_pts
    (u1, v1), (u2, v2) = world_pts
    z1, z2 = complex(x1, -y1), complex(x2, -y2)
    w1, w2 = complex(u1, v1), complex(u2, v2)
    if abs(z2 - z1) < 1e-9:
        raise ValueError("pick two different points on the image")
    a = (w2 - w1) / (z2 - z1)
    return a, w1 - a * z1


def apply(a: complex, b: complex, img_xy) -> np.ndarray:
    p = np.atleast_2d(np.asarray(img_xy, float))
    w = a * (p[:, 0] - 1j * p[:, 1]) + b
    return np.column_stack([w.real, w.imag])


def invert(a: complex, b: complex, world_xy) -> np.ndarray:
    p = np.atleast_2d(np.asarray(world_xy, float))
    z = ((p[:, 0] + 1j * p[:, 1]) - b) / a
    return np.column_stack([z.real, -z.imag])


def fit_box(img_w: int, img_h: int, world_xy: np.ndarray) -> tuple[complex, complex]:
    """A first placement: the image scaled to the trace's bounding box, centred."""
    lo, hi = world_xy.min(0), world_xy.max(0)
    s = max((hi - lo)[0] / img_w, (hi - lo)[1] / img_h) * 1.1
    a = complex(s, 0)
    centre = complex(*((lo + hi) / 2))
    return a, centre - a * complex(img_w / 2, -img_h / 2)


def snap(path_xy: np.ndarray, pt) -> int:
    """Index of the trace point nearest to pt."""
    d = ((path_xy - np.asarray(pt, float)) ** 2).sum(1)
    return int(np.argmin(d))


# ------------------------------------------------------------ turn names ---
# "T4", "4", "Turn 4", "T4A", "4b": optional prefix, number, optional letter.
_NAME_RE = re.compile(r"(.*?)(\d+)([A-Za-z]?)")


def name_after(name: str) -> str | None:
    """The name that follows `name`: a split turn's next letter (4A -> 4B,
    T7b -> T7c), else the next number (T4 -> T5, Turn 9 -> Turn 10). None
    for names without a number ("Oak Tree")."""
    m = _NAME_RE.fullmatch(name.strip())
    if not m:
        return None
    prefix, num, letter = m.groups()
    if letter:
        if letter in "zZ":
            return f"{prefix}{int(num) + 1}"
        return f"{prefix}{num}{chr(ord(letter) + 1)}"
    return f"{prefix}{int(num) + 1}"


# --------------------------------------------------------------- profiles ---

@dataclass
class Turn:
    name: str
    lat: float
    lon: float


@dataclass
class TrackProfile:
    name: str
    lat0: float                # start/finish line: the profile's origin
    lon0: float
    turns: list[Turn] = field(default_factory=list)
    image: str | None = None   # copy of the map image, inside tracks_dir()
    transform: tuple[float, float, float, float] | None = None  # a.re, a.im, b.re, b.im
    path: str | None = None    # where it was loaded from / saved to
    builtin: bool = False      # loaded from the shipped tracks/ folder
    edited: bool = False       # local copy overriding a built-in one
    # sector lines, as points on the track (lat, lon); n lines make n + 1
    # sectors. None of them: the analysis splits the lap in thirds.
    sectors: list[tuple[float, float]] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable id across machines and installs: the file name."""
        return Path(self.path).stem if self.path else ""

    def ab(self) -> tuple[complex, complex] | None:
        if not self.transform:
            return None
        ar, ai, br, bi = self.transform
        return complex(ar, ai), complex(br, bi)

    def set_ab(self, a: complex, b: complex):
        self.transform = (a.real, a.imag, b.real, b.imag)

    def sectors_xy(self, lat0: float, lon0: float) -> np.ndarray:
        """Sector line positions in another frame."""
        if not self.sectors:
            return np.zeros((0, 2))
        return to_xy([p[0] for p in self.sectors], [p[1] for p in self.sectors], lat0, lon0)

    def turns_xy(self, lat0: float, lon0: float) -> np.ndarray:
        """Turn positions in another frame (e.g. an analysis' origin)."""
        if not self.turns:
            return np.zeros((0, 2))
        return to_xy([t.lat for t in self.turns], [t.lon for t in self.turns], lat0, lon0)

    def next_name(self) -> str:
        """What the next placed turn is called: carries on from the last one
        (T4 -> T5, 4A -> 4B, T7b -> T7c), or T1 for the first."""
        if not self.turns:
            return "T1"
        nxt = name_after(self.turns[-1].name)
        if nxt:
            return nxt
        nums = [int(m.group(2)) for t in self.turns
                if (m := _NAME_RE.fullmatch(t.name.strip()))]
        return f"T{max(nums) + 1 if nums else len(self.turns) + 1}"

    # ----------------------------------------------------------- storage
    def to_dict(self, share: bool = False) -> dict:
        """share=True: for the built-in library, without the map image."""
        img = Path(self.image).name if self.image and not share else None
        return {"name": self.name, "start": [self.lat0, self.lon0],
                "turns": [{"name": t.name, "lat": t.lat, "lon": t.lon} for t in self.turns],
                "sectors": [{"lat": la, "lon": lo} for la, lo in self.sectors],
                "image": img,
                "transform": list(self.transform) if self.transform and img else None}

    def export(self, dest: str | Path) -> Path:
        """Write a shareable copy (no image) for adding to the built-in library."""
        dest = Path(dest)
        dest.write_text(json.dumps(self.to_dict(share=True), indent=2) + "\n",
                        encoding="utf-8")
        return dest

    def file_stem(self) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_").lower() or "track"

    @classmethod
    def from_dict(cls, d: dict, path: str | None = None) -> "TrackProfile":
        img = d.get("image")
        if img and path:
            img = str(Path(path).parent / img)
        return cls(d.get("name", "Track"), float(d["start"][0]), float(d["start"][1]),
                   [Turn(t["name"], float(t["lat"]), float(t["lon"])) for t in d.get("turns", [])],
                   img, tuple(d["transform"]) if d.get("transform") else None,
                   sectors=[(float(q["lat"]), float(q["lon"])) for q in d.get("sectors", [])],
                   path=path)

    def save(self, folder: Path | None = None) -> Path:
        folder = Path(folder) if folder else tracks_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stem = self.file_stem()
        if self.builtin and self.path:  # never write into the shipped library: override it locally
            self.path = str(folder / Path(self.path).name)
            self.builtin, self.edited = False, True
        dest = Path(self.path) if self.path else folder / f"{stem}.json"
        n = 2
        while not self.path and dest.exists() and not self._same_place(dest):
            dest = folder / f"{stem}_{n}.json"  # another track with the same name
            n += 1
        img = Path(self.image) if self.image else None
        if img and img.exists() and (img.parent != dest.parent or img.stem != dest.stem):
            # keep our own copy so the profile still works if the original moves
            # (or, for a track saved as a copy of another, if that one is deleted)
            copy = dest.parent / f"{dest.stem}{Path(self.image).suffix.lower()}"
            shutil.copyfile(self.image, copy)
            self.image = str(copy)
        dest.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        self.path = str(dest)
        return dest


def _same_place_impl(self, other: Path) -> bool:
    try:
        d = json.loads(other.read_text(encoding="utf-8"))
        lat, lon = d["start"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return float(np.hypot(*to_xy(lat, lon, self.lat0, self.lon0)[0])) <= SAME_TRACK_M


TrackProfile._same_place = _same_place_impl


def _read_folder(folder: Path | None) -> dict[str, TrackProfile]:
    out = {}
    for f in sorted(folder.glob("*.json")) if folder and folder.is_dir() else []:
        try:
            out[f.stem] = TrackProfile.from_dict(json.loads(f.read_text(encoding="utf-8")), str(f))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def load_all(folder: Path | None = None) -> list[TrackProfile]:
    """Every track: built-in ones, with local copies of the same file name
    taking their place. Passing a folder reads just that folder."""
    if folder is not None:
        return list(_read_folder(Path(folder)).values())
    merged = {}
    for k, p in _read_folder(builtin_dir()).items():
        p.builtin = True
        merged[k] = p
    for k, p in _read_folder(tracks_dir()).items():
        p.edited = k in merged
        merged[k] = p
    return [merged[k] for k in sorted(merged)]


def reset_to_builtin(p: TrackProfile) -> TrackProfile | None:
    """Delete the local copy that overrides a built-in track; returns the
    built-in one (None if there isn't one, and nothing is deleted)."""
    if not p.edited or not p.path:
        return None
    base = _read_folder(builtin_dir()).get(p.key)
    if base is None:
        return None
    Path(p.path).unlink(missing_ok=True)
    base.builtin = True
    return base


def find(lat: float, lon: float, folder: Path | None = None) -> TrackProfile | None:
    """The saved profile whose start line is within SAME_TRACK_M of (lat, lon)."""
    best = None
    for p in load_all(folder):
        d = float(np.hypot(*to_xy(lat, lon, p.lat0, p.lon0)[0]))
        if d <= SAME_TRACK_M and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


def name_from_file(path: str) -> str:
    """'Virginia Intl Raceway Full' from an APEX Pro export name (any of its
    three naming styles, see racecore.ingest)."""
    name = Path(path).name
    m = (re.search(r"[\s_]from[\s_]+(.+?)[\s_]+on[\s_]+\d", name)
         or re.match(r"^(.+?)__\d{5,}\.csv$", name, re.IGNORECASE))
    if not m:
        return "Track"
    return re.sub(r"\s+", " ", m.group(1).replace("_", " ")).strip()
