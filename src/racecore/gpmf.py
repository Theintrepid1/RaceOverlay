# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Minimal GoPro GPMF telemetry reader.

Extracts the `gpmd` data track with ffmpeg and decodes the KLV payloads to get
timestamped sensor streams (ACCL, GYRO, GPS5/GPS9 when present). Used to
auto-sync GoPro footage against an external logger.
"""
from __future__ import annotations

import json
import struct
import subprocess
from dataclasses import dataclass

import numpy as np

# GPMF type char -> (struct format, byte size)
_TYPES = {
    "b": ("b", 1), "B": ("B", 1), "s": ("h", 2), "S": ("H", 2),
    "l": ("i", 4), "L": ("I", 4), "f": ("f", 4), "d": ("d", 8),
    "j": ("q", 8), "J": ("Q", 8),
}


@dataclass
class SensorStream:
    name: str               # FourCC, e.g. "GYRO"
    time: np.ndarray        # seconds from video start, per sample
    values: np.ndarray      # (N, axes), scaled to SI units
    orin: str = ""          # axis-order string from ORIN, if present


def _gpmd_stream_index(path: str) -> int | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_tag_string", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    for st in json.loads(out)["streams"]:
        if st.get("codec_tag_string") == "gpmd":
            return int(st["index"])
    return None


def _packets(path: str, index: int) -> list[tuple[float, float, bytes]]:
    """(start_s, duration_s, payload) for each gpmd packet."""
    meta = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", str(index),
         "-show_entries", "packet=pts_time,duration_time,size", "-of", "json",
         path], capture_output=True, text=True, check=True).stdout
    pkts = json.loads(meta)["packets"]
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{index}",
         "-c", "copy", "-f", "data", "-"],
        capture_output=True, check=True).stdout
    out, pos = [], 0
    for p in pkts:
        size = int(p["size"])
        out.append((float(p["pts_time"]), float(p["duration_time"]),
                    raw[pos:pos + size]))
        pos += size
    return out


def _walk(buf: bytes):
    """Yield (key, type, size, repeat, payload) KLV entries at one level."""
    pos = 0
    while pos + 8 <= len(buf):
        key = buf[pos:pos + 4].decode("latin-1")
        typ = chr(buf[pos + 4])
        size = buf[pos + 5]
        repeat = struct.unpack(">H", buf[pos + 6:pos + 8])[0]
        length = size * repeat
        payload = buf[pos + 8:pos + 8 + length]
        yield key, typ, size, repeat, payload
        pos += 8 + ((length + 3) & ~3)


def _decode(typ: str, size: int, repeat: int, payload: bytes) -> np.ndarray | None:
    if typ not in _TYPES:
        return None
    fmt, width = _TYPES[typ]
    count = size // width
    vals = struct.unpack(">" + fmt * (count * repeat), payload[:width * count * repeat])
    return np.array(vals, dtype=float).reshape(repeat, count)


def read_sensors(path: str, keys=("ACCL", "GYRO", "GPS5")) -> dict[str, SensorStream]:
    """Decode the named sensor streams from a GoPro MP4.

    Samples inside each ~1s payload are spread evenly across that packet's
    duration. Returns {} if the file has no gpmd track.
    """
    index = _gpmd_stream_index(path)
    if index is None:
        return {}
    return _decode_packets(_packets(path, index), keys)


def read_sensors_dump(path: str, keys=("ACCL", "GYRO", "GPS5"),
                      packet_s: float = 1.001) -> dict[str, SensorStream]:
    """Like read_sensors(), for a bare gpmd track saved without its MP4
    (`ffmpeg -i GH010009.MP4 -map 0:<gpmd> -c copy -f rawvideo out.gpmd`).

    The dump has no packet timestamps, so each top-level DEVC is taken as
    one packet of `packet_s` seconds (GoPro writes one per 1001 ms).
    """
    with open(path, "rb") as fh:
        buf = fh.read()
    pkts = []
    for k, typ, size, rep, body in _walk(buf):
        if k != "DEVC":
            continue
        head = struct.pack(">4scBH", b"DEVC", typ.encode("latin-1"), size, rep)
        pkts.append((len(pkts) * packet_s, packet_s, head + body))
    return _decode_packets(pkts, keys)


def _decode_packets(packets, keys) -> dict[str, SensorStream]:
    chunks: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
    orin: dict[str, str] = {}
    for start, dur, payload in packets:
        for k, _, _, _, devc in _walk(payload):
            if k != "DEVC":
                continue
            for k2, _, _, _, strm in _walk(devc):
                if k2 != "STRM":
                    continue
                scal, data_key, data, o = None, None, None, ""
                for k3, typ, size, rep, body in _walk(strm):
                    if k3 == "SCAL":
                        scal = _decode(typ, size, rep, body)
                    elif k3 == "ORIN":
                        o = body[:size * rep].decode("latin-1")
                    elif k3 in keys:
                        data_key, data = k3, _decode(typ, size, rep, body)
                if data_key is None or data is None or len(data) == 0:
                    continue
                if scal is not None:
                    data = data / scal.reshape(-1)[:data.shape[1]] \
                        if scal.size > 1 else data / scal.item()
                t = start + dur * np.arange(len(data)) / len(data)
                chunks.setdefault(data_key, []).append((t, data))
                if o:
                    orin[data_key] = o
    return {k: SensorStream(k, np.concatenate([c[0] for c in v]),
                            np.vstack([c[1] for c in v]), orin.get(k, ""))
            for k, v in chunks.items()}


def _gps_time_in(packets):
    """(video seconds, UTC time) of the first GPS time stamp written with a
    2D/3D fix. GoPro writes GPSU ("yymmddhhmmss.sss", UTC) once a second
    in the GPS stream; without a fix it's the camera's guess, so skipped."""
    import datetime as dt
    for start, _dur, payload in packets:
        for k, _, _, _, devc in _walk(payload):
            if k != "DEVC":
                continue
            for k2, _, _, _, strm in _walk(devc):
                if k2 != "STRM":
                    continue
                gpsu, fix = None, None
                for k3, typ, size, rep, body in _walk(strm):
                    if k3 == "GPSU":
                        gpsu = body[:size * rep].decode("latin-1", "replace").strip("\x00 ")
                    elif k3 == "GPSF":
                        v = _decode(typ, size, rep, body)
                        fix = int(v.reshape(-1)[0]) if v is not None and v.size else None
                if not gpsu or (fix is not None and fix < 2):
                    continue
                try:
                    t = dt.datetime.strptime(gpsu[:16], "%y%m%d%H%M%S.%f")
                except ValueError:
                    continue
                return start, t.replace(tzinfo=dt.timezone.utc)
    return None


def gps_time(path: str):
    """(video seconds, UTC datetime) from a GoPro MP4's GPS clock, or None
    (GPS off, no fix, or no gpmd track)."""
    index = _gpmd_stream_index(path)
    if index is None:
        return None
    return _gps_time_in(_packets(path, index))


def gps_time_dump(path: str, packet_s: float = 1.001):
    """gps_time() for a bare gpmd dump (see read_sensors_dump)."""
    with open(path, "rb") as fh:
        buf = fh.read()
    pkts = []
    for k, typ, size, rep, body in _walk(buf):
        if k == "DEVC":
            head = struct.pack(">4scBH", b"DEVC", typ.encode("latin-1"), size, rep)
            pkts.append((len(pkts) * packet_s, packet_s, head + body))
    return _gps_time_in(pkts)
