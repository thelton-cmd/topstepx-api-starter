"""Indicator plugins and volume profiles for the chart (server side; the page side is chart.js).

Routes (all GET, all read-only):

- ``/api/indicators`` -> ``{"plugins": [describe(module)...], "errors": [{"name", "file", "error"}...]}``.
  The plugin folder (``dashboard/indicators_ext``) is scanned on every call, so a file saved a
  moment ago is listed without restarting the dashboard.
- ``/api/indicators/series?contract=ID&name=NAME&params=<JSON object>&after=<UTC seconds>`` ->
  the plugin's ``run()`` output over every stored candle of the contract, with only the points at
  or after ``after`` when it is given (the chart polls incrementally, like ``/api/bars``), plus
  ``count`` / ``oldest`` / ``after`` / ``version`` like ``/api/bars`` and ``plugin_version``
  (changes when the plugin file is saved). Bad parameters -> 400 with the plugin's message.
- ``/api/indicators/profile?contract=ID`` -> session volume at price from the depth feed (every
  stored price). With ``source=bars&from=<UTC s>&to=<UTC s>`` it is built from the stored candles
  instead: each candle's volume is spread evenly over the tick-sized price levels from its low to
  its high. Rows are ``{"price", "volume"}``, highest price first; ``poc`` is the row with the most
  volume.

Indicators are lines drawn with the user's parameters and the profile is a count of volume at
each price; nothing here is a signal or a recommendation, and every figure is an approximation
built from API data.
"""

from __future__ import annotations

import json
import math
import sys

from .. import indicator_plugins
from ..server import RouteError, parse_after

NAME = "indicators"
TITLE = "Indicator plugins"
DESCRIPTION = ("Lists the indicator plugins in dashboard/indicators_ext, computes their lines over the stored "
               "candles, and builds volume-at-price profiles for the chart.")
ORDER = 20

MAX_PARAMS_TEXT = 4096      # characters of JSON in the params query value
MAX_PROFILE_BINS = 5000     # price rows in a bars profile; wider ranges use bins of several ticks
_reported: set[tuple] = set()   # plugin failures already printed to the terminal (once each)


def routes(session) -> dict:
    return {"GET": {
        "/api/indicators": lambda query: list_plugins(),
        "/api/indicators/series": lambda query: plugin_series(session, query),
        "/api/indicators/profile": lambda query: volume_profile(session, query),
    }}


# -- helpers ---------------------------------------------------------------------------------


def _required(query: dict, key: str) -> str:
    value = (query.get(key) or "").strip()
    if not value:
        raise RouteError(400, f"{key} is required")
    return value


def _parse_params(text: str | None) -> dict:
    """The ``params`` query value: a JSON object (URL-encoded by the page), or nothing for defaults."""
    text = (text or "").strip()
    if not text:
        return {}
    if len(text) > MAX_PARAMS_TEXT:
        raise RouteError(400, "params is too long")
    try:
        value = json.loads(text)
    except ValueError:
        raise RouteError(400, "params must be a JSON object, for example {\"length\": 20}") from None
    if not isinstance(value, dict):
        raise RouteError(400, "params must be a JSON object, for example {\"length\": 20}")
    return value


def _report_once(key: tuple, message: str) -> None:
    if key in _reported:
        return
    _reported.add(key)
    print("[dashboard] " + message.encode("ascii", "replace").decode("ascii"), file=sys.stderr)


def _tail(points: list[dict], after: int | None) -> list[dict]:
    return points if after is None else [p for p in points if p["time"] >= after]


# -- /api/indicators -------------------------------------------------------------------------


def list_plugins() -> tuple[int, dict]:
    plugins, problems = indicator_plugins.scan()
    listed = [indicator_plugins.describe(module) for module in plugins.values()]
    listed.sort(key=lambda p: (p["title"].lower(), p["name"]))
    return 200, {"plugins": listed, "errors": problems}


# -- /api/indicators/series ------------------------------------------------------------------


def plugin_series(session, query: dict) -> tuple[int, dict]:
    contract_id = _required(query, "contract")
    name = _required(query, "name")
    params = _parse_params(query.get("params"))
    after = parse_after(query.get("after"))

    plugins, problems = indicator_plugins.scan()
    module = plugins.get(name)
    if module is None:
        broken = next((p for p in problems if p["name"] == name), None)
        if broken is not None:
            raise RouteError(422, f"{broken['file']} could not be loaded: {broken['error']}")
        raise RouteError(404, f"no indicator plugin named {name!r} in dashboard/indicators_ext")

    candles = session.state.candles(contract_id)
    if candles is None:
        raise RouteError(404, "no bars for that contract yet")
    try:
        result = indicator_plugins.run(module, candles, params)
    except ValueError:
        raise   # bad parameters or malformed output: 400 with the plugin's own message
    except Exception as exc:  # noqa: BLE001 - a plugin bug is reported to the page, never a crash
        message = f"{type(exc).__name__}: {exc}"
        _report_once((name, getattr(module, "__plugin_version__", None), message),
                     f"indicator plugin {name} failed: {message}")
        raise RouteError(422, f"the plugin failed: {message}") from None

    series = [{**s, "points": _tail(s["points"], after)} for s in result["series"]]
    info = indicator_plugins.describe(module)
    return 200, {
        "contract": contract_id,
        "name": result["name"],
        "title": info["title"],
        "plugin_version": info["version"],
        "version": session.state.bars_version(contract_id),
        "params": result["params"],
        "overlay": result["overlay"],
        "count": len(candles),
        "oldest": candles[0]["time"] if candles else None,
        "after": after,
        "series": series,
    }


# -- /api/indicators/profile -----------------------------------------------------------------


def _tick_size(session, contract_id: str) -> float | None:
    info = session.state.contract(contract_id) or {}
    try:
        tick = float(info.get("tick_size") or 0)
    except (TypeError, ValueError):
        return None
    return tick if tick > 0 and math.isfinite(tick) else None


def _poc(rows: list[dict]) -> dict | None:
    """The row with the most volume (the highest price wins a tie, rows being highest first)."""
    best = None
    for row in rows:
        if best is None or row["volume"] > best["volume"]:
            best = row
    return dict(best) if best else None


def bars_profile(candles: list[dict], tick_size: float, max_bins: int = MAX_PROFILE_BINS) -> tuple[list[dict], float]:
    """Volume at price from candles: ``(rows highest price first, bin size)``.

    Each candle's volume is spread evenly over the price levels from its low to its high, one level
    per tick (a candle whose high equals its low puts all its volume on that one price). When the
    whole range would need more than ``max_bins`` levels, each row covers a whole number of ticks
    instead and its ``price`` is the lowest price in it. Candles without volume are skipped.
    """
    if not tick_size or tick_size <= 0:
        raise ValueError("tick size must be greater than 0")
    usable = []
    for c in candles:
        try:
            high, low, volume = float(c["high"]), float(c["low"]), float(c.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if volume > 0 and math.isfinite(high) and math.isfinite(low) and math.isfinite(volume) and high >= low:
            usable.append((round(low / tick_size), round(high / tick_size), volume))   # prices as whole ticks
    if not usable:
        return [], tick_size
    first = min(lo for lo, _, _ in usable)
    last = max(hi for _, hi, _ in usable)
    ticks_per_bin = max(1, math.ceil((last - first + 1) / max_bins))
    bin_size = ticks_per_bin * tick_size

    bins: dict[int, float] = {}
    for lo, hi, volume in usable:
        per_tick = volume / (hi - lo + 1)
        for b in range(lo // ticks_per_bin, hi // ticks_per_bin + 1):
            # how many of this candle's tick levels fall inside bin b
            overlap = min(hi, b * ticks_per_bin + ticks_per_bin - 1) - max(lo, b * ticks_per_bin) + 1
            bins[b] = bins.get(b, 0.0) + per_tick * overlap
    rows = [{"price": round(b * bin_size, 10), "volume": round(v, 6)} for b, v in sorted(bins.items(), reverse=True)]
    return rows, bin_size


def _depth_rows(session, contract_id: str) -> list[dict]:
    state = session.state
    if callable(getattr(state, "traded", None)):
        rows = state.traded(contract_id)
    else:   # older state objects: the snapshot only carries the prices near the last trade
        book = state.depth(contract_id, 100)
        rows = book.get("traded") if book else None
    out = []
    for row in rows or []:
        try:
            price, volume = float(row["price"]), float(row["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        if volume > 0 and math.isfinite(price) and math.isfinite(volume):
            out.append({"price": price, "volume": volume})
    out.sort(key=lambda r: r["price"], reverse=True)
    return out


def volume_profile(session, query: dict) -> tuple[int, dict]:
    contract_id = _required(query, "contract")
    source = (query.get("source") or "depth").strip().lower()
    if source not in {"depth", "session", "bars"}:
        raise RouteError(400, "source must be depth (the session's volume at price) or bars")
    tick = _tick_size(session, contract_id)
    payload = {"contract": contract_id, "tick_size": tick, "from": None, "to": None, "candles": None}

    if source in {"depth", "session"}:
        rows = _depth_rows(session, contract_id)
        payload.update({"source": "depth", "bin_size": tick})
    else:
        start = parse_after(query.get("from"), "from")
        end = parse_after(query.get("to"), "to")
        if start is not None and end is not None and start > end:
            raise RouteError(400, "from must not be after to")
        candles = session.state.candles(contract_id)
        if candles is None:
            raise RouteError(404, "no bars for that contract yet")
        if tick is None:
            raise RouteError(400, "the contract's tick size is not known yet")
        chosen = [c for c in candles if (start is None or c["time"] >= start) and (end is None or c["time"] <= end)]
        rows, bin_size = bars_profile(chosen, tick)
        payload.update({"source": "bars", "bin_size": bin_size, "from": start, "to": end, "candles": len(chosen)})

    payload["rows"] = rows
    payload["total"] = round(sum(r["volume"] for r in rows), 6)
    payload["poc"] = _poc(rows)
    return 200, payload
