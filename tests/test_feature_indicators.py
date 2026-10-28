"""Offline tests for the indicator plugins feature: the bundled plugins' math (worked by hand in
the comments), hot reloading of dashboard/indicators_ext, the /api/indicators routes and the
volume profile."""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from dashboard import features as feature_registry
from dashboard import indicator_plugins, indicators
from dashboard.features import indicators as feature
from dashboard.state import DashboardState, DepthBook
from tests.test_dashboard_server import ES, as_json, bar, dash, static_dir, utc  # noqa: F401 - fixtures
from topstepx import DepthEntry

PLUGIN_DIR = indicator_plugins.PLUGIN_DIR


def candle(t, h, l, c, v=1.0, o=None):
    return {"time": t, "open": c if o is None else o, "high": h, "low": l, "close": c, "volume": v}


def closes(values, step=60):
    """Candles whose close is each value (high/low one point either side)."""
    return [candle(step * (i + 1), v + 1.0, v - 1.0, float(v)) for i, v in enumerate(values)]


def plugin(name):
    plugins = indicator_plugins.discover()
    assert name in plugins, f"{name} plugin missing: {indicator_plugins.errors()}"
    return plugins[name]


def series_by_id(out):
    return {s["id"]: s for s in out["series"]}


def values(series):
    return [p["value"] for p in series["points"]]


def times(series):
    return [p["time"] for p in series["points"]]


# -- the bundled plugins ------------------------------------------------------------------------


def test_bundled_plugins_follow_the_contract():
    for name in ("sma", "bollinger", "rsi", "atr", "donchian"):
        module = plugin(name)
        info = indicator_plugins.describe(module)
        assert info["name"] == name and info["title"] and info["description"]
        assert info["file"] == f"{name}.py" and info["version"]
        defaults = indicator_plugins.parse_params(module, {})
        assert set(defaults) == {p["name"] for p in info["params"]}
        out = indicator_plugins.run(module, closes([1, 2, 3, 4, 5, 6]), {})
        assert json.dumps(out) and out["series"]
    assert indicator_plugins.describe(plugin("rsi"))["overlay"] is False
    assert indicator_plugins.describe(plugin("atr"))["overlay"] is False
    assert indicator_plugins.describe(plugin("bollinger"))["overlay"] is True
    assert indicator_plugins.describe(plugin("donchian"))["overlay"] is True


def test_bundled_plugins_never_use_signal_language():
    banned = ("signal", "entry", "exit", "buy", "sell", "overbought", "oversold", "bullish", "bearish")
    for name in ("bollinger", "rsi", "atr", "donchian"):
        module = plugin(name)
        info = indicator_plugins.describe(module)
        text = " ".join([info["title"], info["description"], *[p["label"] for p in info["params"]]]).lower()
        out = indicator_plugins.run(module, closes([1, 2, 3, 4, 5]), {"length": 2})
        text += " " + " ".join(s["label"] for s in out["series"]).lower()
        assert not [word for word in banned if word in text], name


def test_bollinger_bands_by_hand():
    # closes 1..5, length 3, mult 2. First window [1, 2, 3]: mean 2, population variance
    # ((1-2)^2 + 0 + (3-2)^2) / 3 = 2/3, sd = 0.8165; bands 2 +/- 1.63299.
    out = indicator_plugins.run(plugin("bollinger"), closes([1, 2, 3, 4, 5]), {"length": 3, "mult": 2})
    s = series_by_id(out)
    assert values(s["basis"]) == pytest.approx([2.0, 3.0, 4.0])
    spread = 2 * math.sqrt(2 / 3)
    assert values(s["upper"]) == pytest.approx([2 + spread, 3 + spread, 4 + spread])
    assert values(s["lower"]) == pytest.approx([2 - spread, 3 - spread, 4 - spread])
    assert times(s["basis"]) == [180, 240, 300]
    assert s["upper"]["label"] == "BB upper (3, 2)"
    # A flat window has zero width (never a negative variance from rounding).
    flat = series_by_id(indicator_plugins.run(plugin("bollinger"), closes([5000.25] * 4), {"length": 3}))
    assert values(flat["upper"]) == pytest.approx([5000.25, 5000.25]) == values(flat["lower"])
    # Source hl2 of these candles equals the close (high and low are close +/- 1).
    assert values(series_by_id(indicator_plugins.run(plugin("bollinger"), closes([1, 2, 3]), {"length": 3, "source": "hl2"}))["basis"]) == [2.0]


def test_bollinger_running_sums_match_a_direct_calculation_at_futures_prices():
    rng = random.Random(7)
    price = 6000.0
    data = []
    for i in range(400):
        price += rng.choice([-0.25, 0.0, 0.25, 0.5, -0.5])
        data.append(price)
    out = series_by_id(indicator_plugins.run(plugin("bollinger"), closes(data), {"length": 20, "mult": 2.5}))
    expected_upper = []
    for i in range(19, len(data)):
        window = data[i - 19:i + 1]
        mean = sum(window) / 20
        sd = math.sqrt(sum((x - mean) ** 2 for x in window) / 20)
        expected_upper.append(mean + 2.5 * sd)
    assert values(out["upper"]) == pytest.approx(expected_upper, abs=1e-9)


def test_rsi_by_hand():
    # closes 1, 2, 3, 2, 3 with length 2: changes +1, +1, -1, +1.
    # Seed after two changes: avg gain (1+1)/2 = 1, avg loss 0 -> RSI 100 (no losses).
    # Next change -1: gain (1*1 + 0)/2 = 0.5, loss (0*1 + 1)/2 = 0.5 -> RS 1 -> RSI 50.
    # Next change +1: gain (0.5 + 1)/2 = 0.75, loss (0.5 + 0)/2 = 0.25 -> RS 3 -> RSI 75.
    out = series_by_id(indicator_plugins.run(plugin("rsi"), closes([1, 2, 3, 2, 3]), {"length": 2}))
    assert values(out["rsi"]) == pytest.approx([100.0, 50.0, 75.0])
    assert times(out["rsi"]) == [180, 240, 300]
    # The two reference lines sit at the user's levels over the same bars, dotted.
    assert values(out["upper"]) == [70.0] * 3 and values(out["lower"]) == [30.0] * 3
    assert out["upper"]["line_style"] == "dotted" and times(out["lower"]) == times(out["rsi"])
    custom = series_by_id(indicator_plugins.run(plugin("rsi"), closes([1, 2, 3, 2, 3]), {"length": 2, "upper": 80, "lower": 120}))
    assert values(custom["upper"]) == [80.0] * 3 and values(custom["lower"]) == [100.0] * 3   # clamped to 0-100
    assert custom["upper"]["label"] == "Line 80"
    # Only losses -> 0.
    assert values(series_by_id(indicator_plugins.run(plugin("rsi"), closes([3, 2, 1]), {"length": 2}))["rsi"]) == [0.0]


def test_rsi_restarts_after_a_gap_in_the_data():
    from dashboard.indicators_ext.rsi import rsi_values
    assert rsi_values([1, 2, 3, None, 5, 6, 7], 2) == [None, None, 100.0, None, None, None, 100.0]


def test_atr_by_hand_and_in_step_with_the_core_helper():
    # (high, low, close): (2, 1, 1.5), (3, 2, 2.5), (4, 2, 3); length 2.
    # TR: 1 (first bar), max(1, |3-1.5|, |2-1.5|) = 1.5, max(2, |4-2.5|, |2-2.5|) = 2.
    # ATR: (1 + 1.5)/2 = 1.25, then (1.25*1 + 2)/2 = 1.625.
    data = [candle(60, 2, 1, 1.5), candle(120, 3, 2, 2.5), candle(180, 4, 2, 3)]
    out = series_by_id(indicator_plugins.run(plugin("atr"), data, {"length": 2}))
    assert values(out["atr"]) == pytest.approx([1.25, 1.625]) and times(out["atr"]) == [120, 180]
    assert values(out["atr"]) == [v for v in indicators.atr(data, 2) if v is not None]


def test_donchian_by_hand():
    # highs 3, 5, 4, 6 and lows 1, 2, 1, 3 with length 2:
    # bar 2: max(3,5)=5, min(1,2)=1, mid 3; bar 3: 5, 1, 3; bar 4: max(4,6)=6, min(1,3)=1, mid 3.5
    data = [candle(60, 3, 1, 2), candle(120, 5, 2, 3), candle(180, 4, 1, 2), candle(240, 6, 3, 5)]
    out = series_by_id(indicator_plugins.run(plugin("donchian"), data, {"length": 2}))
    assert values(out["upper"]) == [5.0, 5.0, 6.0]
    assert values(out["lower"]) == [1.0, 1.0, 1.0]
    assert values(out["mid"]) == [3.0, 3.0, 3.5] and times(out["mid"]) == [120, 180, 240]
    assert set(series_by_id(indicator_plugins.run(plugin("donchian"), data, {"length": 2, "mid": False}))) == {"upper", "lower"}


def test_donchian_matches_a_brute_force_window():
    from dashboard.indicators_ext.donchian import rolling_extremes
    rng = random.Random(3)
    highs = [rng.randint(10, 60) for _ in range(300)]
    lows = [h - rng.randint(0, 9) for h in highs]
    for length in (1, 5, 37):
        got = rolling_extremes(highs, lows, length)
        for i, (top, bottom) in enumerate(got):
            if i < length - 1:
                assert (top, bottom) == (None, None)
            else:
                assert (top, bottom) == (max(highs[i - length + 1:i + 1]), min(lows[i - length + 1:i + 1]))


def test_run_drops_non_finite_values_and_clamps_line_width():
    import types
    module = types.ModuleType("dashboard.indicators_ext.fake_nan")
    module.compute = lambda candles, params: {"series": [{"id": "x", "line_width": 9, "points": [
        {"time": 1, "value": float("nan")}, {"time": 2, "value": float("inf")}, {"time": 3, "value": 2}]}]}
    out = indicator_plugins.run(module, [], {})
    assert out["series"][0]["points"] == [{"time": 3, "value": 2.0}] and out["series"][0]["line_width"] == 4
    assert json.dumps(out, allow_nan=False)


# -- hot reload -------------------------------------------------------------------------------------


@pytest.fixture
def temp_plugins():
    """Write plugin files into dashboard/indicators_ext; every one is deleted afterwards, even on failure."""
    written: list[Path] = []
    tag = f"{os.getpid()}_{int(time.time() * 1000) % 100000}"

    def write(stem: str, source: str, bump: int = 0) -> Path:
        path = PLUGIN_DIR / f"zz_{stem}_{tag}.py"
        path.write_text(source, encoding="utf-8")
        if bump:   # make sure the stamp moves even on file systems with coarse timestamps
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + bump * 1_000_000_000))
        if path not in written:
            written.append(path)
        return path

    yield write
    for path in written:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    indicator_plugins.scan()   # forget them


def simple_plugin(title: str, value: float, name: str | None = None) -> str:
    name_line = f"NAME = {name!r}\n" if name else ""
    return (
        f"{name_line}TITLE = {title!r}\n"
        "PARAMS = [{'name': 'n', 'type': 'int', 'default': 2, 'min': 1, 'max': 9}]\n"
        "def compute(candles, params):\n"
        f"    return {{'series': [{{'id': 'v', 'points': [{{'time': c['time'], 'value': {value!r}}} for c in candles]}}]}}\n"
    )


def test_discover_picks_up_new_changed_and_deleted_files_without_a_restart(temp_plugins):
    path = temp_plugins("hot", simple_plugin("Hot v1", 1.0))
    stem = path.stem
    plugins, problems = indicator_plugins.scan()
    assert stem in plugins and not [p for p in problems if p["file"] == path.name]
    first = plugins[stem]
    info = indicator_plugins.describe(first)
    assert info["title"] == "Hot v1" and info["file"] == path.name and info["version"]
    assert indicator_plugins.discover()[stem] is first   # unchanged file: the same module object

    temp_plugins("hot", simple_plugin("Hot v2 with a longer title", 2.0), bump=2)
    second = indicator_plugins.discover()[stem]
    assert second is not first
    assert indicator_plugins.describe(second)["title"] == "Hot v2 with a longer title"
    assert indicator_plugins.describe(second)["version"] != info["version"]
    assert values(indicator_plugins.run(second, closes([1, 2]), {})["series"][0]) == [2.0, 2.0]

    # Same size as v2: only the modification time tells the two versions apart.
    temp_plugins("hot", simple_plugin("Hot v3 with a longer title", 3.0), bump=4)
    assert values(indicator_plugins.run(indicator_plugins.discover()[stem], closes([1]), {})["series"][0]) == [3.0]

    path.unlink()
    plugins, problems = indicator_plugins.scan()
    assert stem not in plugins and not [p for p in problems if p["file"] == path.name]
    assert f"dashboard.indicators_ext.{stem}" not in sys.modules
    # Plugins are compiled straight from source: no bytecode files are left behind for them.
    cache = PLUGIN_DIR / "__pycache__"
    assert not [p for p in (cache.iterdir() if cache.is_dir() else []) if p.name.startswith(stem)]


def test_broken_plugins_are_reported_not_fatal(temp_plugins, capsys):
    broken = temp_plugins("broken", "def compute(candles, params)\n    return {}\n")      # SyntaxError
    nocompute = temp_plugins("nocompute", "TITLE = 'No compute here'\n")
    badparams = temp_plugins("badparams", "PARAMS = [{'name': 'x', 'type': 'decimal'}]\ndef compute(c, p):\n    return {'series': []}\n")
    crash = temp_plugins("crash", "raise RuntimeError('boom at import')\n")
    dup = temp_plugins("dup", simple_plugin("Another SMA", 1.0, name="sma"))
    badname = temp_plugins("badname", simple_plugin("Spaces", 1.0, name="has spaces"))

    plugins, problems = indicator_plugins.scan()
    by_file = {p["file"]: p for p in problems}
    assert "SyntaxError" in by_file[broken.name]["error"] and by_file[broken.name]["name"] == broken.stem
    assert "compute" in by_file[nocompute.name]["error"]
    assert "unknown type" in by_file[badparams.name]["error"]
    assert "RuntimeError: boom at import" in by_file[crash.name]["error"]
    assert "already used by sma.py" in by_file[dup.name]["error"]
    assert "NAME" in by_file[badname.name]["error"]
    assert indicator_plugins.describe(plugins["sma"])["file"] == "sma.py"   # the original keeps its name
    assert {"bollinger", "rsi", "atr", "donchian"} <= set(plugins)          # the rest still load
    assert indicator_plugins.errors() == problems
    assert "not loaded" in capsys.readouterr().err

    # Fixing a file brings it back on the next scan.
    temp_plugins("broken", simple_plugin("Fixed now", 5.0), bump=2)
    plugins, problems = indicator_plugins.scan()
    assert broken.stem in plugins and broken.name not in {p["file"] for p in problems}


# -- routes -----------------------------------------------------------------------------------------


def install(session):
    session.features = [feature]
    session.feature_routes = feature_registry.collect_routes([feature], session)


def seed_bars(state, count=6):
    bars = [bar(utc(2026, 10, 26, 14, 5 * i), o=100.0 + i, c=100.5 + i) for i in range(count)]
    state.set_bars(ES.id, bars)
    return [int(b.time.timestamp()) for b in bars]


def get_json(dash, path):
    status, body, _ = dash.get(path)
    return status, as_json(body)


def series_url(name, params=None, after=None, contract=ES.id):
    url = f"/api/indicators/series?contract={quote(contract)}&name={quote(name)}"
    if params is not None:
        url += "&params=" + quote(params if isinstance(params, str) else json.dumps(params))
    if after is not None:
        url += f"&after={after}"
    return url


def test_feature_module_metadata():
    info = feature_registry.describe(feature)
    assert info["name"] == "indicators" and info["order"] == 20 and info["js"] is None
    assert set(feature.routes(None)["GET"]) == {"/api/indicators", "/api/indicators/series", "/api/indicators/profile"}


def test_list_route(dash, temp_plugins):
    install(dash.session)
    broken = temp_plugins("listbroken", "def compute(candles, params:\n")
    status, data = get_json(dash, "/api/indicators")
    assert status == 200
    names = [p["name"] for p in data["plugins"]]
    assert {"sma", "bollinger", "rsi", "atr", "donchian"} <= set(names)
    rsi = next(p for p in data["plugins"] if p["name"] == "rsi")
    assert rsi["overlay"] is False and [p["name"] for p in rsi["params"]][:2] == ["length", "source"]
    assert any(e["file"] == broken.name and "SyntaxError" in e["error"] for e in data["errors"])


def test_series_route_full_and_after(dash):
    install(dash.session)
    times_ = seed_bars(dash.session.state)
    status, data = get_json(dash, series_url("sma", {"length": 2}))
    assert status == 200
    assert data["name"] == "sma" and data["title"] == "SMA" and data["overlay"] is True
    assert data["count"] == 6 and data["oldest"] == times_[0] and data["after"] is None
    assert data["params"] == {"length": 2, "source": "close", "color": "#f7dc6f"}
    assert data["plugin_version"] and data["version"] == dash.session.state.bars_version(ES.id)
    sma = data["series"][0]
    assert [p["time"] for p in sma["points"]] == times_[1:]
    assert sma["points"][0]["value"] == pytest.approx((100.5 + 101.5) / 2)

    # Incremental: values are computed over everything stored, only the tail comes back.
    status, tail = get_json(dash, series_url("sma", {"length": 2}, after=times_[4]))
    assert status == 200 and tail["after"] == times_[4] and tail["count"] == 6
    assert tail["series"][0]["points"] == sma["points"][-2:]

    # Pane plugins work the same way.
    status, rsi = get_json(dash, series_url("rsi", {"length": 3}))
    assert status == 200 and rsi["overlay"] is False and [s["id"] for s in rsi["series"]] == ["rsi", "upper", "lower"]


def test_series_route_errors(dash, temp_plugins):
    install(dash.session)
    assert get_json(dash, "/api/indicators/series?name=sma")[0] == 400                     # no contract
    assert get_json(dash, f"/api/indicators/series?contract={ES.id}")[0] == 400            # no name
    status, data = get_json(dash, series_url("sma"))
    assert status == 404 and "no bars" in data["error"]
    seed_bars(dash.session.state)
    status, data = get_json(dash, series_url("sma", {"length": "abc"}))
    assert status == 400 and data["error"] == "Length must be a number"
    status, data = get_json(dash, series_url("sma", {"source": "typical"}))
    assert status == 400 and "Source must be one of" in data["error"]
    assert get_json(dash, series_url("sma", "{not json"))[0] == 400
    assert get_json(dash, series_url("sma", "[1, 2]"))[0] == 400
    assert get_json(dash, series_url("sma", after="soon"))[0] == 400
    status, data = get_json(dash, series_url("nope"))
    assert status == 404 and "nope" in data["error"]

    broken = temp_plugins("routebroken", "def compute(:\n")
    status, data = get_json(dash, series_url(broken.stem))
    assert status == 422 and "could not be loaded" in data["error"] and "SyntaxError" in data["error"]
    crash = temp_plugins("routecrash", "def compute(candles, params):\n    return 1 / 0\n")
    status, data = get_json(dash, series_url(crash.stem))
    assert status == 422 and "ZeroDivisionError" in data["error"]
    # The server stays up and keeps answering.
    assert get_json(dash, series_url("sma", {"length": 2}))[0] == 200


def trade(price, session_volume, size=1.0):
    return DepthEntry(price=price, volume=session_volume, type=5, current_volume=size)


def test_profile_route_session_volume_from_depth(dash):
    install(dash.session)
    state = dash.session.state
    status, data = get_json(dash, f"/api/indicators/profile?contract={ES.id}")
    assert status == 200 and data["rows"] == [] and data["poc"] is None and data["source"] == "depth"

    # 300 traded prices: the ladder snapshot only carries the ones near the last trade, the
    # profile carries every one.
    entries = [trade(5000.0 + 0.25 * i, 10.0 + (i % 7)) for i in range(300)]
    entries.append(trade(5010.0, 99.0, size=2))
    state.apply_depth(ES.id, entries)
    assert len(state.depth(ES.id, 20)["traded"]) < 300
    status, data = get_json(dash, f"/api/indicators/profile?contract={ES.id}")
    assert status == 200 and len(data["rows"]) == 300
    assert data["rows"][0]["price"] == 5000.0 + 0.25 * 299 and data["rows"][-1]["price"] == 5000.0   # highest first
    assert data["poc"] == {"price": 5010.0, "volume": 99.0}
    assert data["tick_size"] == 0.25 == data["bin_size"]
    assert data["total"] == pytest.approx(sum(r["volume"] for r in data["rows"]))


def test_profile_route_from_bars(dash):
    install(dash.session)
    times_ = seed_bars(dash.session.state, count=3)
    # bar(o, c): high = max(o, c) + 0.5, low = min(o, c) - 0.5, volume 10 -> bar 0 covers
    # 99.5-101.0 (7 ticks of 0.25), bar 1 100.5-102.0, bar 2 101.5-103.0.
    status, data = get_json(dash, f"/api/indicators/profile?contract={ES.id}&source=bars&from={times_[0]}&to={times_[0]}")
    assert status == 200 and data["source"] == "bars" and data["candles"] == 1 and data["bin_size"] == 0.25
    assert [r["price"] for r in data["rows"]] == [101.0, 100.75, 100.5, 100.25, 100.0, 99.75, 99.5]
    assert all(r["volume"] == pytest.approx(10 / 7) for r in data["rows"])
    status, data = get_json(dash, f"/api/indicators/profile?contract={ES.id}&source=bars")
    assert data["candles"] == 3 and data["total"] == pytest.approx(30.0)
    by_price = {r["price"]: r["volume"] for r in data["rows"]}
    assert by_price[100.5] == pytest.approx(20 / 7) and by_price[103.0] == pytest.approx(10 / 7)
    # 100.5-101.0 (bars 0 and 1) and 101.5-102.0 (bars 1 and 2) tie at 20/7; the highest price wins a tie.
    assert data["poc"] == {"price": 102.0, "volume": pytest.approx(20 / 7)}
    assert get_json(dash, f"/api/indicators/profile?contract={ES.id}&source=nope")[0] == 400
    assert get_json(dash, f"/api/indicators/profile?contract={ES.id}&source=bars&from=9&to=1")[0] == 400
    assert get_json(dash, f"/api/indicators/profile?contract={ES.id}&source=bars&from=x")[0] == 400
    assert get_json(dash, "/api/indicators/profile?contract=CON.F.US.NONE&source=bars")[0] == 404
    assert get_json(dash, "/api/indicators/profile")[0] == 400


# -- the pure profile helper and the state addition ------------------------------------------------


def test_bars_profile_spreads_volume_evenly_by_tick():
    # One candle 1.00-2.00 with volume 5 on a 0.25 tick: 5 price levels of 1 each.
    rows, size = feature.bars_profile([candle(0, 2.0, 1.0, 1.5, v=5)], 0.25)
    assert size == 0.25 and rows == [{"price": p, "volume": 1.0} for p in (2.0, 1.75, 1.5, 1.25, 1.0)]
    # A second candle 1.50-1.75 with volume 4 adds 2 to each of its two levels.
    rows, _ = feature.bars_profile([candle(0, 2.0, 1.0, 1.5, v=5), candle(60, 1.75, 1.5, 1.5, v=4)], 0.25)
    assert {r["price"]: r["volume"] for r in rows} == {2.0: 1.0, 1.75: 3.0, 1.5: 3.0, 1.25: 1.0, 1.0: 1.0}
    # A bar with no range puts everything on one price; bars without volume are skipped.
    rows, _ = feature.bars_profile([candle(0, 3.0, 3.0, 3.0, v=7), candle(60, 9.0, 1.0, 5.0, v=0)], 0.25)
    assert rows == [{"price": 3.0, "volume": 7.0}]
    assert feature.bars_profile([], 0.25) == ([], 0.25)
    with pytest.raises(ValueError):
        feature.bars_profile([candle(0, 2.0, 1.0, 1.5)], 0)


def test_bars_profile_widens_bins_past_the_row_limit_and_keeps_the_total():
    # Ticks 4..8 (prices 1.00-2.00) with at most 2 bins: 3 ticks per bin, bins start at multiples
    # of 0.75. Bin 0.75 holds ticks 4-5 (2 of the 5 levels), bin 1.50 ticks 6-8 (3 levels).
    rows, size = feature.bars_profile([candle(0, 2.0, 1.0, 1.5, v=5)], 0.25, max_bins=2)
    assert size == 0.75 and rows == [{"price": 1.5, "volume": 3.0}, {"price": 0.75, "volume": 2.0}]
    rng = random.Random(11)
    data = []
    for i in range(200):
        low = 4000 + rng.randint(0, 400) * 0.25
        data.append(candle(60 * i, low + rng.randint(0, 40) * 0.25, low, low, v=rng.randint(1, 500)))
    rows, size = feature.bars_profile(data, 0.25, max_bins=50)
    assert len(rows) <= 50 and size > 0.25
    assert sum(r["volume"] for r in rows) == pytest.approx(sum(c["volume"] for c in data))


def test_depth_book_traded_returns_every_stored_price():
    book = DepthBook()
    book.apply([trade(10.0 + i, 1.0 + i) for i in range(50)])
    rows = book.traded()
    assert len(rows) == 50 and rows[0] == {"price": 59.0, "volume": 50.0} and rows[-1]["price"] == 10.0
    state = DashboardState(utc(2026, 10, 26))
    assert state.traded(ES.id) is None
    state.apply_depth(ES.id, [trade(5000.0, 3.0)])
    assert state.traded(ES.id) == [{"price": 5000.0, "volume": 3.0}]


def test_parse_after_helper_shared_with_the_bars_route():
    from dashboard.server import RouteError, parse_after
    assert parse_after(None) is None and parse_after("  ") is None and parse_after("1700000000") == 1700000000
    for bad in ("-5", "1.5", "soon"):
        with pytest.raises(RouteError) as info:
            parse_after(bad, "from")
        assert info.value.status == 400 and "from must be" in str(info.value)
