"""Indicator plugins: one Python file per indicator in ``dashboard/indicators_ext/``.

The chart's Indicators menu lists every plugin it finds; the user picks parameters; the server
computes the series over the stored candles and the page draws them. Plugins are lines on a chart
drawn with the user's parameters. They are never described as signals, entries or exits.

Module contract (everything but ``compute`` has a default):

    NAME = "bollinger"                 # id used in URLs (default: module name); letters, digits, _ . -
    TITLE = "Bollinger Bands"
    DESCRIPTION = "Moving average with bands N standard deviations away."
    OVERLAY = True                     # True: drawn on the price pane; False: its own pane under the chart
    PARAMS = [                         # shown as inputs, validated and clamped by parse_params()
        {"name": "length", "label": "Length", "type": "int", "default": 20, "min": 1, "max": 500},
        {"name": "mult", "label": "StdDev", "type": "float", "default": 2.0, "min": 0.1, "max": 10, "step": 0.1},
        {"name": "source", "label": "Source", "type": "str", "default": "close", "choices": ["close", "hl2", "hlc3", "ohlc4"]},
        {"name": "fill", "label": "Fill", "type": "bool", "default": True},
        {"name": "color", "label": "Color", "type": "str", "default": "#5dade2"},
    ]
    # Optional spec keys: "min", "max", "step" (numbers), "choices" (str), "help" (tooltip text) and
    # "format": "color". A str parameter whose default is "#rrggbb" (or with "format": "color")
    # gets a color picker on the page.

    def compute(candles: list[dict], params: dict) -> dict:
        # candles: oldest first, each {"time": int (UTC seconds), "open", "high", "low", "close", "volume"}
        # return {"series": [ {"id": "upper", "label": "Upper", "type": "line", "color": "#5dade2",
        #                       "line_style": "solid" | "dashed" | "dotted", "line_width": 1,
        #                       "points": [{"time": int, "value": float | None, "color"?: "#hex"}, ...]},
        #                     {"id": "hist", "type": "histogram", ...},
        #                     {"id": "flags", "type": "markers", "points": [{"time", "position": "aboveBar"|"belowBar",
        #                       "shape": "circle"|"arrowUp"|"arrowDown"|"square", "color", "text"?}]} ]}
        # Points whose value is None (or NaN/infinite) are dropped by run(); times must be candle times.

Helpers here: ``discover()``, ``scan()``, ``errors()``, ``describe()``, ``parse_params()``, ``run()``,
``source_values()``. ``tests/test_dashboard_features.py`` and ``tests/test_feature_indicators.py``
show the expected shapes; ``indicators_ext/sma.py`` is the smallest complete example.

Hot reload: ``discover()`` and ``scan()`` look at the folder on every call. A file is (re)loaded
when its modification time or size changes, so a plugin saved while the dashboard runs shows up
on the next call without a restart, and a deleted file disappears. The source is compiled
straight from the file (no ``__pycache__``), so two quick saves never run stale bytecode. A file
that fails to import, has no ``compute`` or declares bad ``PARAMS`` is left out and reported by
``scan()`` / ``errors()`` as ``{"name", "file", "error"}``; it never stops the other plugins or
the dashboard.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import math
import re
import sys
import threading
from pathlib import Path
from types import ModuleType

from . import indicators_ext

PARAM_TYPES = {"int", "float", "bool", "str"}
SERIES_TYPES = {"line", "histogram", "markers"}
LINE_STYLES = {"solid", "dashed", "dotted"}
SOURCES = ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4")
PARAM_SPEC_KEYS = ("min", "max", "step", "choices", "help", "format")   # optional keys passed to the page
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
FILE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.py$")   # importable names; "_x.py" files are ignored
PLUGIN_DIR = Path(indicators_ext.__file__).resolve().parent


def name_of(module: ModuleType) -> str:
    return str(getattr(module, "NAME", None) or module.__name__.rsplit(".", 1)[-1])


# -- discovery with hot reload ---------------------------------------------------------------


class _FreshSourceLoader(importlib.machinery.SourceFileLoader):
    """Compile the .py file every time: no bytecode cache, so an edit is never shadowed by a stale .pyc."""

    def get_code(self, fullname):
        source = self.get_data(self.path)
        return self.source_to_code(source, self.path)


_lock = threading.RLock()
_cache: dict[str, dict] = {}     # file name -> {"stamp": (mtime_ns, size), "module": ModuleType | None, "error": str | None}
_last_errors: list[dict] = []


def _warn(message: str) -> None:
    print("[dashboard] " + message.encode("ascii", "replace").decode("ascii"), file=sys.stderr)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _load_file(path: Path, stamp: tuple[int, int]) -> ModuleType:
    """Import one plugin file as a fresh module object (replacing any older copy in sys.modules)."""
    qualname = f"{indicators_ext.__name__}.{path.stem}"
    loader = _FreshSourceLoader(qualname, str(path))
    spec = importlib.util.spec_from_file_location(qualname, str(path), loader=loader)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(qualname)
    sys.modules[qualname] = module
    try:
        loader.exec_module(module)
    except BaseException:
        if previous is not None:
            sys.modules[qualname] = previous
        else:
            sys.modules.pop(qualname, None)
        raise
    module.__plugin_version__ = f"{stamp[0]:x}-{stamp[1]:x}"
    return module


def _check_module(module: ModuleType) -> None:
    """Raise ValueError when a loaded module breaks the contract (so it is reported, not served)."""
    name = name_of(module)
    if not NAME_PATTERN.match(name):
        raise ValueError(f"NAME {name!r} must be 1-64 letters, digits, '_', '.' or '-'")
    if not callable(getattr(module, "compute", None)):
        raise ValueError("has no compute(candles, params) function")
    _param_specs(module)   # raises ValueError for malformed PARAMS


def _plugin_files() -> list[Path]:
    try:
        entries = sorted(PLUGIN_DIR.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [p for p in entries if FILE_PATTERN.match(p.name) and p.is_file()]


def scan() -> tuple[dict[str, ModuleType], list[dict]]:
    """Look at dashboard/indicators_ext now: ``({name: module}, [{"name", "file", "error"}, ...])``.

    Unchanged files reuse the module loaded earlier; new or changed files are imported again;
    deleted files are forgotten. Safe to call from several server threads at once.
    """
    global _last_errors
    with _lock:
        files = _plugin_files()
        present = {p.name for p in files}
        for gone in [name for name in _cache if name not in present]:
            del _cache[gone]
            sys.modules.pop(f"{indicators_ext.__name__}.{gone[:-3]}", None)

        found: dict[str, ModuleType] = {}
        problems: list[dict] = []
        for path in files:
            stamp = _stamp(path)
            if stamp is None:
                continue   # removed between listing and stat
            entry = _cache.get(path.name)
            if entry is None or entry["stamp"] != stamp:
                entry = {"stamp": stamp, "module": None, "error": None}
                try:
                    module = _load_file(path, stamp)
                    _check_module(module)
                    entry["module"] = module
                except Exception as exc:  # noqa: BLE001 - a broken plugin is reported, never fatal
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    _warn(f"indicator plugin {path.name} not loaded: {entry['error']}")
                _cache[path.name] = entry
            module = entry["module"]
            if module is None:
                problems.append({"name": path.stem, "file": path.name, "error": entry["error"]})
                continue
            name = name_of(module)
            if name in found:
                other = Path(getattr(found[name], "__file__", "") or "?").name
                problems.append({"name": name, "file": path.name, "error": f"NAME {name!r} is already used by {other}"})
                continue
            found[name] = module
        _last_errors = problems
        return found, [dict(p) for p in problems]


def discover() -> dict[str, ModuleType]:
    """Every working plugin in dashboard/indicators_ext, by name (see ``scan()``; broken ones are left out)."""
    return scan()[0]


def errors() -> list[dict]:
    """The files the most recent ``scan()``/``discover()`` could not use: ``[{"name", "file", "error"}]``."""
    with _lock:
        return [dict(p) for p in _last_errors]


# -- describing and running one plugin --------------------------------------------------------


def _param_specs(module: ModuleType) -> list[dict]:
    specs = []
    for raw in getattr(module, "PARAMS", None) or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError(f"plugin {name_of(module)}: every PARAMS entry needs a name")
        kind = str(raw.get("type", "float"))
        if kind not in PARAM_TYPES:
            raise ValueError(f"plugin {name_of(module)}: parameter {raw['name']} has unknown type {kind!r}")
        spec = {
            "name": str(raw["name"]),
            "label": str(raw.get("label") or raw["name"]),
            "type": kind,
            "default": raw.get("default"),
        }
        for key in PARAM_SPEC_KEYS:
            if key in raw:
                spec[key] = raw[key]
        specs.append(spec)
    return specs


def describe(module: ModuleType) -> dict:
    """What GET /api/indicators tells the page about one plugin.

    ``version`` changes whenever the file is saved again (None for a module that was not loaded
    from a file by ``scan()``); ``file`` is the plugin's file name (or None).
    """
    path = getattr(module, "__file__", None)
    return {
        "name": name_of(module),
        "title": str(getattr(module, "TITLE", None) or name_of(module)),
        "description": str(getattr(module, "DESCRIPTION", "") or ""),
        "overlay": bool(getattr(module, "OVERLAY", True)),
        "params": _param_specs(module),
        "version": getattr(module, "__plugin_version__", None),
        "file": Path(path).name if path else None,
    }


def parse_params(module: ModuleType, raw: dict | None) -> dict:
    """Validate/clamp user parameters against PARAMS; unknown keys are ignored, missing ones default.

    Raises ValueError with a message the page can show.
    """
    raw = raw or {}
    out: dict = {}
    for spec in _param_specs(module):
        name, kind = spec["name"], spec["type"]
        value = raw.get(name, spec.get("default"))
        if kind == "bool":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            out[name] = bool(value)
        elif kind in ("int", "float"):
            try:
                number = int(float(value)) if kind == "int" else float(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{spec['label']} must be a number") from None
            if isinstance(number, float) and not math.isfinite(number):
                raise ValueError(f"{spec['label']} must be a finite number")
            if "min" in spec and number < spec["min"]:
                number = spec["min"]
            if "max" in spec and number > spec["max"]:
                number = spec["max"]
            out[name] = number
        else:
            text = "" if value is None else str(value)
            choices = spec.get("choices")
            if choices and text not in choices:
                raise ValueError(f"{spec['label']} must be one of: {', '.join(map(str, choices))}")
            out[name] = text[:200]
    return out


def source_values(candles: list[dict], source: str = "close") -> list[float | None]:
    """A price series from candles: close/open/high/low or the usual averages (hl2, hlc3, ohlc4)."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SOURCES)}")
    out: list[float | None] = []
    for c in candles:
        o, h, l, cl = c.get("open"), c.get("high"), c.get("low"), c.get("close")
        try:
            if source == "close":
                v = cl
            elif source == "open":
                v = o
            elif source == "high":
                v = h
            elif source == "low":
                v = l
            elif source == "hl2":
                v = (h + l) / 2
            elif source == "hlc3":
                v = (h + l + cl) / 3
            else:
                v = (o + h + l + cl) / 4
            out.append(None if v is None else float(v))
        except TypeError:
            out.append(None)
    return out


def _finite(value) -> float | None:
    """float(value), or None for NaN/infinity (JSON has no way to send those)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"point value {value!r} is not a number") from None
    return number if math.isfinite(number) else None


def run(module: ModuleType, candles: list[dict], params: dict | None = None) -> dict:
    """Compute a plugin and normalise its output for the page (drops None/NaN points, checks shapes)."""
    clean = parse_params(module, params)
    result = module.compute(candles, clean) or {}
    series_in = result.get("series") if isinstance(result, dict) else None
    if not isinstance(series_in, list):
        raise ValueError(f"plugin {name_of(module)}: compute() must return {{'series': [...]}}")
    series_out = []
    for i, s in enumerate(series_in):
        if not isinstance(s, dict):
            raise ValueError(f"plugin {name_of(module)}: series {i} is not a dict")
        kind = str(s.get("type", "line"))
        if kind not in SERIES_TYPES:
            raise ValueError(f"plugin {name_of(module)}: series {i} has unknown type {kind!r}")
        points = []
        for p in s.get("points") or []:
            if not isinstance(p, dict) or p.get("time") is None:
                continue
            if kind != "markers" and p.get("value") is None:
                continue
            point = {"time": int(p["time"])}
            if kind != "markers":
                value = _finite(p["value"])
                if value is None:
                    continue
                point["value"] = value
            for key in ("color", "position", "shape", "text", "size"):
                if key in p and p[key] is not None:
                    point[key] = p[key]
            points.append(point)
        style = str(s.get("line_style", "solid"))
        series_out.append({
            "id": str(s.get("id") or f"s{i}"),
            "label": str(s.get("label") or s.get("id") or f"series {i}"),
            "type": kind,
            "color": str(s.get("color") or "#c8d6e5"),
            "line_style": style if style in LINE_STYLES else "solid",
            "line_width": max(1, min(4, int(s.get("line_width", 1) or 1))),
            "points": points,
        })
    return {"name": name_of(module), "params": clean, "overlay": bool(getattr(module, "OVERLAY", True)), "series": series_out}
