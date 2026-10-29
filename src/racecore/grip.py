# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""How much of the car's grip each lap and each turn used.

The grip envelope is what you have shown the car can do: for every direction
on the g-g diagram (pure cornering, braking, trail-braking, accelerating out),
a high percentile of the combined g reached across every timed lap you've
loaded. Grip used at any moment is the combined g divided by the envelope in
that direction, so 100% is "as hard as you've ever pushed that way".

It is relative to your own data, not the tyre's absolute limit: as you push
harder the envelope grows. Accelerating is usually engine-limited, so only
the cornering and braking side counts towards a turn's grip use.

g comes from the data logger (car-frame lon_g / lat_g; telemetry.align_g
rotates raw APEX Pro axes). Over the VIR test laps the GoPro's accelerometer,
smoothed to the same bandwidth, agreed within about 0.05 g in corners but
read ~0.1 g more under braking; the logger is used because it covers every
lap of every session.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

BINS = 36                 # 10-degree directions around the g-g diagram
PCT = 98.0                # percentile of combined g that sets the envelope
MIN_SPEED_KMH = 30.0      # below this it's pit lane / paddock
WORKING_G = 0.35          # combined g at which the car counts as working
MIN_SAMPLES = 8           # per direction, else interpolated from neighbours


def direction(lat, lon) -> np.ndarray:
    """Angle on the g-g diagram, radians in [0, 2pi): 0 = right turn,
    pi/2 = accelerating, pi = left turn, 3pi/2 = braking."""
    return np.mod(np.arctan2(lon, lat), 2 * np.pi)


@dataclass
class Envelope:
    radius: np.ndarray        # g, one per direction bin (bin k centred at (k+0.5)*step)
    samples: int              # how many samples built it

    @property
    def step(self) -> float:
        return 2 * np.pi / len(self.radius)

    def at(self, theta) -> np.ndarray:
        """Envelope radius (g) in direction theta, interpolated around the circle."""
        theta = np.mod(np.asarray(theta, float), 2 * np.pi)
        n = len(self.radius)
        centres = (np.arange(n) + 0.5) * self.step
        xs = np.concatenate([centres - 2 * np.pi, centres, centres + 2 * np.pi])
        ys = np.tile(self.radius, 3)
        return np.interp(theta, xs, ys)

    def outline(self, n: int = 180) -> np.ndarray:
        """(n+1, 2) points (lat, lon) tracing the envelope, closed."""
        th = np.linspace(0, 2 * np.pi, n + 1)
        r = self.at(th)
        return np.column_stack([r * np.cos(th), r * np.sin(th)])

    def used(self, lat, lon) -> np.ndarray:
        """Fraction of the envelope used (1.0 = on it), per sample."""
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        r = np.hypot(lat, lon)
        return r / np.maximum(self.at(direction(lat, lon)), 0.05)


def build_envelope(lat: np.ndarray, lon: np.ndarray, speed_kmh: np.ndarray | None = None,
                   bins: int = BINS, pct: float = PCT) -> Envelope:
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    ok = np.isfinite(lat) & np.isfinite(lon)
    if speed_kmh is not None:
        ok &= np.asarray(speed_kmh, float) > MIN_SPEED_KMH
    lat, lon = lat[ok], lon[ok]
    th, r = direction(lat, lon), np.hypot(lat, lon)
    k = np.minimum((th / (2 * np.pi) * bins).astype(int), bins - 1)
    rad = np.full(bins, np.nan)
    for b in range(bins):
        sel = r[k == b]
        if sel.size >= MIN_SAMPLES:
            rad[b] = np.percentile(sel, pct)
    good = np.isfinite(rad)
    if not good.any():
        raise ValueError("not enough g data to build a grip envelope")
    if not good.all():  # fill gaps around the circle from the neighbours
        idx = np.arange(bins)
        gi = idx[good]
        rad = np.interp(idx, np.concatenate([gi - bins, gi, gi + bins]),
                        np.tile(rad[good], 3))
    # light circular smoothing so one noisy direction doesn't make a notch
    rad = (np.roll(rad, 1) + 2 * rad + np.roll(rad, -1)) / 4
    return Envelope(rad, int(ok.sum()))


@dataclass
class TurnGrip:
    used: float | None        # time-weighted mean grip used while working (0-1)
    used_ref: float | None
    peak_g: float             # highest combined g in the turn's stretch
    peak_g_ref: float
    soft_s: float             # seconds working below 80% of the envelope
    soft_s_ref: float


def _working(lat, lon, lon_limit=True):
    """Samples where the car is cornering or braking hard enough to count.
    Pure acceleration is left out: it is usually limited by the engine."""
    lat, lon = np.asarray(lat), np.asarray(lon)
    w = np.hypot(lat, lon) >= WORKING_G
    if lon_limit:
        w &= ~((lon > 0.15) & (np.abs(lat) < 0.35))
    return w


def stretch_grip(env: Envelope, lat, lon, dt) -> tuple[float | None, float, float]:
    """(mean used while working, peak combined g, seconds working below 80%)
    for one stretch of track; dt = seconds each sample represents."""
    lat, lon, dt = np.asarray(lat), np.asarray(lon), np.asarray(dt)
    peak = float(np.hypot(lat, lon).max()) if len(lat) else 0.0
    w = _working(lat, lon)
    if not w.any():
        return None, peak, 0.0
    u = env.used(lat[w], lon[w])
    mean = float(np.sum(u * dt[w]) / np.sum(dt[w]))
    soft = float(np.sum(dt[w][u < 0.8]))
    return mean, peak, soft
