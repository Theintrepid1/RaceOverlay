# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Theintrepid1 and Race Overlay contributors
"""Math channels: your own channels, worked out from the ones in the file.

    Brake/throttle overlap   if(brake > 5 and throttle > 10, 1, 0)
    Corner radius            if(abs(lat_g) > 0.3, speed_ms**2 / (abs(lat_g) * 9.81), nan)

One library for both apps (math_channels.json in the data folder). A math
channel shows up wherever a channel can be picked, as "math:<name>", for any
file that has what its formula uses; for one that doesn't it's simply not
there, like a gauge for a channel the logger didn't record.

In a formula:
  channels   speed (km/h), speed_mph, speed_ms (m/s), throttle and brake
             (0-100 %), rpm, lat_g, lon_g, lap, time (s), gps_lat, gps_lon;
             any column of the file by its name in lower case with spaces
             and symbols as _ ("Oil Temp (F)" -> oil_temp_f), or as
             col("Oil Temp (F)"); other math channels the same way, or
             math("Name")
  maths      + - * / ** %  ( )  < <= > >= == !=  and or not
             abs min max sqrt hypot clip(x, lo, hi) round floor ceil
             log exp sin cos  pi nan
             if(cond, a, b)            a where cond holds, else b
             smooth(x, seconds=0.2)    moving average over time
             rate(x)                   change per second
             integral(x)               running total over time (x * seconds)
             prev(x, seconds)          x that many seconds earlier
  comments   # to the end of the line

Formulas are parsed and checked node by node (never run as Python), so a
library shared by someone else can't do anything but arithmetic.
"""
from __future__ import annotations

import ast
import json
import math
import os
import re
from dataclasses import asdict, dataclass

import numpy as np

KEY = "math:"


class FormulaError(ValueError):
    """The formula is wrong (syntax, an unknown function...)."""


class MissingInput(FormulaError):
    """The formula is fine but this file lacks a channel it uses."""


@dataclass
class MathChannel:
    name: str
    formula: str
    unit: str = ""
    on: bool = True
    note: str = ""          # what it's for (shown in the editor)

    @property
    def key(self) -> str:
        return KEY + self.name


# Ready-made ones to start from (copied into the library, then editable).
PRESETS = [
    MathChannel("Brake/throttle overlap", "if(brake > 5 and throttle > 10, 1, 0)", "",
                note="1 while you're on both pedals at once."),
    MathChannel("Coasting", "if(brake < 2 and throttle < 5 and speed_mph > 20, 1, 0)", "",
                note="1 while you're on neither pedal at speed: time that could be "
                     "braking or accelerating."),
    MathChannel("Combined g", "hypot(lat_g, lon_g)", "g",
                note="Total g in any direction."),
    MathChannel("Power estimate", "max(lon_g, 0) * 9.81 * (2800 * 0.4536) * speed_ms / 745.7",
                "hp", note="Power going into accelerating the car. Change 2800 to your "
                           "car and driver's weight in lb. Reads low (no drag or "
                           "rolling resistance) and needs good g data."),
    MathChannel("Corner radius",
                "if(abs(lat_g) > 0.3, speed_ms ** 2 / (abs(lat_g) * 9.81), nan)", "m",
                note="How tight the car is turning, from speed and lateral g."),
    MathChannel("Throttle rate", "rate(smooth(throttle, 0.1))", "%/s",
                note="How fast you're opening (+) or closing (-) the throttle."),
    MathChannel("Brake release rate", "-rate(smooth(brake, 0.1))", "%/s",
                note="How fast you come off the brake (+): smooth trail braking "
                     "shows as a steady, moderate number."),
    MathChannel("Distance", "integral(speed_ms)", "m",
                note="Distance from the start of the file."),
]

FUNCTIONS = ["abs", "min", "max", "sqrt", "hypot", "clip", "round", "floor", "ceil", "log",
             "exp", "sin", "cos", "if", "smooth", "rate", "integral", "prev", "col", "math"]
BUILT_IN_NAMES = ["speed", "speed_mph", "speed_ms", "throttle", "brake", "rpm", "lat_g",
                  "lon_g", "lap", "time", "gps_lat", "gps_lon"]


def ident(name: str) -> str:
    """'Oil Temp (F)' -> 'oil_temp_f': how a column or math channel is
    written in a formula."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip()).strip("_").lower()
    return ("_" + s) if s[:1].isdigit() else s


# ------------------------------------------------------------------ library

def library_path():
    from .osutil import data_dir
    return data_dir() / "math_channels.json"


_lib: dict = {"path": None, "mtime": None, "channels": []}
_generation = [0]         # bumped whenever the library changes: cached results go stale


def library(path=None) -> list[MathChannel]:
    """The saved math channels (re-read when the file changes)."""
    p = path or library_path()
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = None
    if _lib["path"] != str(p) or _lib["mtime"] != mt:
        chans = []
        if mt is not None:
            try:
                d = json.loads(open(p, encoding="utf-8").read())
                for c in d.get("channels", []):
                    if c.get("name") and c.get("formula"):
                        chans.append(MathChannel(c["name"], c["formula"], c.get("unit", ""),
                                                 bool(c.get("on", True)), c.get("note", "")))
            except (OSError, ValueError, AttributeError):
                chans = []
        _lib.update(path=str(p), mtime=mt, channels=chans)
        _generation[0] += 1
    return list(_lib["channels"])


def save_library(chans: list[MathChannel], path=None):
    p = path or library_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "channels": [asdict(c) for c in chans]},
                              indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    _lib["mtime"] = None             # re-read next time
    _generation[0] += 1


def find(name: str, chans: list[MathChannel] | None = None) -> MathChannel | None:
    chans = library() if chans is None else chans
    want = ident(name)
    return next((c for c in chans if c.name == name), None) or \
        next((c for c in chans if ident(c.name) == want), None)


# --------------------------------------------------------------- evaluating

_BIN = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply,
        ast.Div: np.divide, ast.Pow: np.power, ast.Mod: np.mod}
_CMP = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater,
        ast.GtE: np.greater_equal, ast.Eq: np.equal, ast.NotEq: np.not_equal}
_CONST = {"pi": math.pi, "nan": float("nan"), "true": 1.0, "false": 0.0}


def _prepare(formula: str) -> str:
    lines = [re.sub(r"#.*$", "", ln) for ln in formula.splitlines()]
    text = " ".join(ln.strip() for ln in lines).strip()
    if not text:
        raise FormulaError("The formula is empty.")
    # if is a Python keyword: if(c, a, b) -> _if(c, a, b)
    return re.sub(r"\bif\s*\(", "_if(", text)


def parse(formula: str) -> ast.AST:
    """Parse and check a formula (no channels needed). FormulaError if it's
    not one."""
    try:
        tree = ast.parse(_prepare(formula), mode="eval")
    except SyntaxError as e:
        where = f" near character {e.offset}" if e.offset else ""
        raise FormulaError(f"Can't read the formula{where}: {e.msg} (something missing "
                           "after an operator, or a bracket not closed?).") from None
    for node in ast.walk(tree):
        ok = isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare,
                               ast.BoolOp, ast.Call, ast.Name, ast.Constant, ast.IfExp,
                               ast.Load, ast.operator, ast.unaryop, ast.cmpop, ast.boolop,
                               ast.keyword))
        if not ok or isinstance(node, (ast.MatMult, ast.FloorDiv, ast.LShift, ast.RShift,
                                       ast.BitOr, ast.BitXor, ast.BitAnd, ast.Invert,
                                       ast.Is, ast.IsNot, ast.In, ast.NotIn)):
            raise FormulaError(f"'{ast.unparse(node) if hasattr(node, 'lineno') else type(node).__name__}'"
                               " isn't allowed in a formula.")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str)):
            raise FormulaError(f"{node.value!r} isn't allowed in a formula.")
        if isinstance(node, ast.Call):
            f = node.func.id if isinstance(node.func, ast.Name) else None
            fname = "if" if f == "_if" else f
            if fname not in FUNCTIONS:
                raise FormulaError(f"There's no function called '{ast.unparse(node.func)}'. "
                                   f"Functions: {', '.join(FUNCTIONS)}.")
            if node.keywords and fname != "smooth":
                raise FormulaError(f"{fname}() doesn't take named settings.")
            for kw in node.keywords:
                if kw.arg != "seconds":
                    raise FormulaError(f"smooth() has no setting called '{kw.arg}'.")
    return tree


def inputs(formula: str) -> set[str]:
    """Names a formula reads (channels, columns, math channels)."""
    tree = parse(formula)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id in ("col", "math") and node.args and \
                isinstance(node.args[0], ast.Constant):
            out.add(f"{node.func.id}:{node.args[0].value}")
        elif isinstance(node, ast.Name) and node.id not in FUNCTIONS + ["_if"] and \
                node.id.lower() not in _CONST:
            out.add(node.id)
    return out


class _Eval:
    def __init__(self, tel, chans, stack):
        self.tel, self.chans, self.stack = tel, chans, stack
        self.t = np.asarray(tel.time, dtype=float)
        self.n = len(self.t)

    # -- names
    def _live(self, key: str, name: str) -> np.ndarray:
        """A channel with data: one that's all blank or never changes (rpm
        with no OBD link) counts as missing, as it does in the pickers."""
        if not self.tel.has(key):
            raise MissingInput(f"'{name}' has no data in this file (blank or constant).")
        return self.tel.series(key)

    def channel(self, name: str) -> np.ndarray:
        tel, ch = self.tel, self.tel.channels
        n = name.lower()
        if n == "time":
            return self.t
        if n in ("speed", "speed_mph", "speed_ms") and ch.get("speed") is not None:
            v = self._live("speed", name)
            return v if n == "speed" else v / 1.60934 if n == "speed_mph" else v / 3.6
        if n in ("throttle", "brake") and ch.get(n) is not None:
            return self._live(n, name) * 100.0
        if n in ch and n not in ("ax", "ay", "az"):
            return self._live(n, name)
        m = find(name, self.chans)
        if m is not None:
            return self.math(m.name)
        for c in tel.source_columns:
            if ident(c) == n:
                return self._live("col:" + c, name)
        raise MissingInput(f"No channel '{name}' in this file.")

    def column(self, name: str) -> np.ndarray:
        if self.tel.raw.get(name) is None:
            raise MissingInput(f"No column '{name}' in this file.")
        return self._live("col:" + name, name)

    def math(self, name: str) -> np.ndarray:
        m = find(name, self.chans)
        if m is None:
            raise MissingInput(f"No math channel called '{name}'.")
        return evaluate(self.tel, m, self.chans, self.stack)

    # -- time functions (NaN-tolerant)
    def _fill(self, x):
        ok = np.isfinite(x)
        if ok.all() or not ok.any():
            return x
        return np.interp(self.t, self.t[ok], x[ok])

    def smooth(self, x, seconds=0.2):
        x = self._full(x)
        if self.n < 3:
            return x
        dt = float(np.median(np.diff(self.t))) or 0.01
        k = max(1, int(round(float(seconds) / dt)))
        if k <= 1:
            return x
        bad = ~np.isfinite(x)
        y = self._fill(x)
        c = np.cumsum(np.insert(y, 0, 0.0))
        h = k // 2
        i0 = np.clip(np.arange(self.n) - h, 0, self.n)
        i1 = np.clip(np.arange(self.n) + (k - h), 0, self.n)
        out = (c[i1] - c[i0]) / np.maximum(i1 - i0, 1)
        out[bad] = np.nan
        return out

    def rate(self, x):
        x = self._full(x)
        if self.n < 2:
            return np.zeros(self.n)
        t = self.t + np.arange(self.n) * 1e-9          # repeated time stamps
        return np.gradient(self._fill(x), t)

    def integral(self, x):
        x = np.nan_to_num(self._full(x))
        if self.n < 2:
            return np.zeros(self.n)
        steps = np.diff(self.t) * (x[1:] + x[:-1]) / 2.0
        return np.concatenate([[0.0], np.cumsum(steps)])

    def prev(self, x, seconds):
        x = self._full(x)
        return np.interp(self.t - float(seconds), self.t, self._fill(x))

    def _full(self, x):
        x = np.asarray(x, dtype=float)
        return np.full(self.n, float(x)) if x.ndim == 0 else x

    # -- tree
    def run(self, node):
        if isinstance(node, ast.Expression):
            return self.run(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                raise FormulaError(f"Text ('{node.value}') only goes in col(\"…\") or "
                                   "math(\"…\").")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id.lower() in _CONST:
                return _CONST[node.id.lower()]
            return self.channel(node.id)
        if isinstance(node, ast.BinOp):
            return _BIN[type(node.op)](self.run(node.left), self.run(node.right))
        if isinstance(node, ast.UnaryOp):
            v = self.run(node.operand)
            if isinstance(node.op, ast.USub):
                return np.negative(v)
            if isinstance(node.op, ast.UAdd):
                return v
            return np.logical_not(v).astype(float)
        if isinstance(node, ast.Compare):
            left, out = self.run(node.left), None
            for op, comp in zip(node.ops, node.comparators):
                right = self.run(comp)
                r = _CMP[type(op)](left, right)
                out = r if out is None else np.logical_and(out, r)
                left = right
            return np.asarray(out, dtype=float)
        if isinstance(node, ast.BoolOp):
            vals = [np.asarray(self.run(v), dtype=float) != 0 for v in node.values]
            f = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
            out = vals[0]
            for v in vals[1:]:
                out = f(out, v)
            return np.asarray(out, dtype=float)
        if isinstance(node, ast.IfExp):
            return np.where(np.asarray(self.run(node.test)) != 0, self.run(node.body),
                            self.run(node.orelse))
        if isinstance(node, ast.Call):
            return self.call(node)
        raise FormulaError(f"'{ast.unparse(node)}' isn't allowed in a formula.")

    def call(self, node):
        f = node.func.id
        a = node.args

        def need(lo, hi=None):
            if len(a) < lo or (hi is not None and len(a) > hi):
                want = f"{lo}" if hi == lo else f"{lo} to {hi}" if hi else f"at least {lo}"
                raise FormulaError(f"{'if' if f == '_if' else f}() takes {want} value(s).")
        if f in ("col", "math"):
            need(1, 1)
            if not isinstance(a[0], ast.Constant) or not isinstance(a[0].value, str):
                raise FormulaError(f'{f}() takes a name in quotes: {f}("…").')
            return self.column(a[0].value) if f == "col" else self.math(a[0].value)
        if f == "smooth":
            need(1, 2)
            secs = self.run(a[1]) if len(a) > 1 else next(
                (self.run(k.value) for k in node.keywords), 0.2)
            return self.smooth(self.run(a[0]), float(np.nanmean(secs)))
        v = [self.run(x) for x in a]
        if f == "_if":
            need(3, 3)
            return np.where(np.asarray(v[0]) != 0, v[1], v[2])
        if f in ("min", "max"):
            need(1)
            if len(v) == 1:                 # over the whole file
                return float(np.nanmin(v[0]) if f == "min" else np.nanmax(v[0]))
            out = v[0]
            for x in v[1:]:
                out = (np.fmin if f == "min" else np.fmax)(out, x)
            return out
        if f == "clip":
            need(3, 3)
            return np.clip(v[0], v[1], v[2])
        if f == "hypot":
            need(2, 2)
            return np.hypot(v[0], v[1])
        if f == "rate":
            need(1, 1)
            return self.rate(v[0])
        if f == "integral":
            need(1, 1)
            return self.integral(v[0])
        if f == "prev":
            need(2, 2)
            return self.prev(v[0], float(np.nanmean(v[1])))
        need(1, 1)
        return {"abs": np.abs, "sqrt": np.sqrt, "round": np.round, "floor": np.floor,
                "ceil": np.ceil, "log": np.log, "exp": np.exp, "sin": np.sin,
                "cos": np.cos}[f](v[0])


def evaluate(tel, m: MathChannel, chans: list[MathChannel] | None = None,
             stack: tuple = ()) -> np.ndarray:
    """m's values for every sample of tel. MissingInput if the file lacks
    something it uses, FormulaError if the formula is wrong."""
    chans = library() if chans is None else chans
    if m.name in stack:
        raise FormulaError("Math channels use each other in a loop: "
                           + " → ".join(stack + (m.name,)) + ".")
    tree = parse(m.formula)
    with np.errstate(all="ignore"):
        v = _Eval(tel, chans, stack + (m.name,)).run(tree)
        v = np.asarray(v, dtype=float)
        if v.ndim == 0:
            v = np.full(len(tel.time), float(v))
    v[~np.isfinite(v)] = np.nan
    return v


def series(tel, key: str) -> np.ndarray | None:
    """Values of 'math:<name>' for tel, or None when it can't be worked out
    for this file (cached on the Telemetry until it or the library changes)."""
    name = key[len(KEY):]
    lib = library()
    cache = tel.__dict__.setdefault("_math_cache", {})
    if cache.get("_gen") != _generation[0]:
        cache.clear()
        cache["_gen"] = _generation[0]
    if name in cache:
        return cache[name]
    m = find(name, lib)
    v = None
    if m is not None:
        try:
            v = evaluate(tel, m, lib)
        except FormulaError:
            v = None
    cache[name] = v
    return v


def available(tel) -> list[MathChannel]:
    """The library's switched-on math channels that work for this file."""
    return [m for m in library() if m.on and tel.has(m.key)]


def unit(key: str) -> str:
    m = find(key[len(KEY):]) if key.startswith(KEY) else None
    return m.unit if m else ""


def label(key: str) -> str:
    return key[len(KEY):] if key.startswith(KEY) else key
