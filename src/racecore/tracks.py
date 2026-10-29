# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Per-track profiles: official turn names and positions, and an optional
track-map image lined up with the GPS trace.

Stored as JSON in <data dir>/raceoverlay/tracks/, one file per track, found
again by the start/finish line's position. A profile has its own origin
(its start line), so it lines up with any session at that track.

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
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SAME_TRACK_M = 150.0


def tracks_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "raceoverlay" / "tracks"


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

    def ab(self) -> tuple[complex, complex] | None:
        if not self.transform:
            return None
        ar, ai, br, bi = self.transform
        return complex(ar, ai), complex(br, bi)

    def set_ab(self, a: complex, b: complex):
        self.transform = (a.real, a.imag, b.real, b.imag)

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
    def to_dict(self) -> dict:
        return {"name": self.name, "start": [self.lat0, self.lon0],
                "turns": [{"name": t.name, "lat": t.lat, "lon": t.lon} for t in self.turns],
                "image": Path(self.image).name if self.image else None,
                "transform": list(self.transform) if self.transform else None}

    @classmethod
    def from_dict(cls, d: dict, path: str | None = None) -> "TrackProfile":
        img = d.get("image")
        if img and path:
            img = str(Path(path).parent / img)
        return cls(d.get("name", "Track"), float(d["start"][0]), float(d["start"][1]),
                   [Turn(t["name"], float(t["lat"]), float(t["lon"])) for t in d.get("turns", [])],
                   img, tuple(d["transform"]) if d.get("transform") else None, path)

    def save(self, folder: Path | None = None) -> Path:
        folder = Path(folder) if folder else tracks_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_").lower() or "track"
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


def load_all(folder: Path | None = None) -> list[TrackProfile]:
    folder = Path(folder) if folder else tracks_dir()
    out = []
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            out.append(TrackProfile.from_dict(json.loads(f.read_text(encoding="utf-8")), str(f)))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def find(lat: float, lon: float, folder: Path | None = None) -> TrackProfile | None:
    """The saved profile whose start line is within SAME_TRACK_M of (lat, lon)."""
    best = None
    for p in load_all(folder):
        d = float(np.hypot(*to_xy(lat, lon, p.lat0, p.lon0)[0]))
        if d <= SAME_TRACK_M and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


def name_from_file(path: str) -> str:
    """'Virginia Intl Raceway Full' from an APEX Pro export name."""
    m = re.search(r"_from_(.+?)_on_", Path(path).name)
    return m.group(1).replace("_", " ") if m else "Track"
