"""Offline tests for dashboard/indicators.py: pure math on candle dicts.

Every expected number below is worked out by hand in the comments so a reader
can check the arithmetic without trusting another library.
"""

import json
from datetime import datetime, timezone

import pytest

from dashboard import indicators
from dashboard.indicators import atr, ema, indicator_series, session_key, supertrend, true_ranges, vwap


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def candle(t: int, h: float, l: float, c: float, v: float = 1.0, o: float | None = None) -> dict:
    return {"time": t, "open": c if o is None else o, "high": h, "low": l, "close": c, "volume": v}


def series(rows, start=utc(2026, 10, 26, 14, 0), step=300) -> list[dict]:
    """Build 5-minute candles from (high, low, close[, volume]) tuples."""
    return [candle(start + i * step, *row) for i, row in enumerate(rows)]


# -- ema --------------------------------------------------------------------------------


def test_ema_seeds_with_a_simple_average_then_smooths():
    # seed = (1+2+3)/3 = 2; alpha = 2/(3+1) = 0.5; 2 + 0.5*(4-2) = 3; 3 + 0.5*(5-3) = 4
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_period_one_returns_the_values_themselves():
    assert ema([3.0, 1.0, 2.0], 1) == [3.0, 1.0, 2.0]


def test_ema_alpha_matches_the_textbook_formula():
    values = [10.0, 11.0, 12.0, 11.5, 12.5, 13.0]
    out = ema(values, 4)
    alpha = 2 / 5
    seed = sum(values[:4]) / 4
    assert out[:3] == [None, None, None]
    assert out[3] == pytest.approx(seed)
    assert out[4] == pytest.approx(seed + alpha * (values[4] - seed))
    assert out[5] == pytest.approx(out[4] + alpha * (values[5] - out[4]))


def test_ema_shorter_than_period_is_all_none_and_empty_is_empty():
    assert ema([1.0, 2.0], 3) == [None, None]
    assert ema([], 3) == []


@pytest.mark.parametrize("period", [0, -1, 2.5, "3", True, None])
def test_ema_rejects_unusable_periods(period):
    with pytest.raises(ValueError, match="EMA period"):
        ema([1.0, 2.0, 3.0], period)


# -- atr --------------------------------------------------------------------------------


ATR_ROWS = [(10, 8, 9), (12, 9, 11), (11, 7, 8), (13, 12, 12.5)]
# true ranges: 2 (first bar is high-low);
#   max(3, |12-9|, |9-9|)  = 3
#   max(4, |11-11|, |7-11|) = 4
#   max(1, |13-8|, |12-8|)  = 5


def test_true_ranges_use_high_low_on_the_first_bar_and_gaps_afterwards():
    assert true_ranges(series(ATR_ROWS)) == [2, 3, 4, 5]


def test_atr_uses_wilder_smoothing():
    # period 2: seed (2+3)/2 = 2.5; (2.5*1 + 4)/2 = 3.25; (3.25*1 + 5)/2 = 4.125
    assert atr(series(ATR_ROWS), 2) == [None, 2.5, 3.25, 4.125]


def test_atr_period_one_equals_true_range():
    assert atr(series(ATR_ROWS), 1) == [2, 3, 4, 5]


def test_atr_without_enough_bars_is_all_none():
    assert atr(series(ATR_ROWS), 5) == [None] * 4
    assert atr([], 3) == []


@pytest.mark.parametrize("period", [0, -3, 1.5, "2", False])
def test_atr_rejects_unusable_periods(period):
    with pytest.raises(ValueError, match="ATR period"):
        atr(series(ATR_ROWS), period)


# -- vwap -------------------------------------------------------------------------------


def test_vwap_accumulates_typical_price_by_volume_within_a_session():
    # typical prices 10, 20, 30 with volumes 1, 2, 2 -> 10; (10+40)/3; (10+40+60)/5
    candles = series([(11, 9, 10, 1), (21, 19, 20, 2), (31, 29, 30, 2)])
    assert vwap(candles) == pytest.approx([10.0, 50 / 3, 22.0])


def test_vwap_resets_on_the_first_bar_at_or_after_the_session_start():
    candles = [
        candle(utc(2026, 10, 26, 21, 55), 11, 9, 10, 1),   # tail of the previous session
        candle(utc(2026, 10, 26, 22, 0), 21, 19, 20, 2),   # first bar of the new session -> reset
        candle(utc(2026, 10, 26, 22, 5), 31, 29, 30, 2),   # (40 + 60) / 4
        candle(utc(2026, 10, 27, 21, 55), 41, 39, 40, 1),  # still the same session next evening: (100 + 40) / 5
        candle(utc(2026, 10, 27, 22, 0), 51, 49, 50, 1),   # next session -> reset
    ]
    assert vwap(candles, (22, 0)) == pytest.approx([10.0, 20.0, 25.0, 28.0, 50.0])


def test_vwap_session_boundary_follows_the_configured_start():
    late = candle(utc(2026, 10, 26, 23, 59), 11, 9, 10, 1)
    midnight = candle(utc(2026, 10, 27, 0, 0), 21, 19, 20, 1)
    # midnight UTC is a boundary only when the session starts at 00:00
    assert vwap([late, midnight], (0, 0)) == pytest.approx([10.0, 20.0])
    assert vwap([late, midnight], (22, 0)) == pytest.approx([10.0, 15.0])


def test_session_key_shifts_by_the_session_start():
    assert session_key(utc(2026, 10, 26, 21, 59), (22, 0)) == session_key(utc(2026, 10, 26, 0, 0), (22, 0))
    assert session_key(utc(2026, 10, 26, 22, 0), (22, 0)) == session_key(utc(2026, 10, 27, 21, 59), (22, 0))
    assert session_key(utc(2026, 10, 26, 22, 0), (22, 0)) != session_key(utc(2026, 10, 26, 21, 59), (22, 0))
    assert session_key(utc(2026, 10, 26, 22, 30), (22, 30)) != session_key(utc(2026, 10, 26, 22, 29), (22, 30))


def test_vwap_carries_the_previous_value_while_volume_is_zero():
    candles = series([(11, 9, 10, 0), (21, 19, 20, 4), (31, 29, 30, 0), (41, 39, 40, 4)])
    assert vwap(candles) == pytest.approx([None, 20.0, 20.0, 30.0])


def test_vwap_treats_missing_volume_as_zero_and_empty_input_as_empty():
    bar = {"time": utc(2026, 10, 26, 14), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": None}
    assert vwap([bar]) == [None]
    assert vwap([]) == []


@pytest.mark.parametrize("start", [(24, 0), (22, 60), (-1, 0), (22,), "22:00", (22.0, 0), None])
def test_vwap_rejects_unusable_session_starts(start):
    with pytest.raises(ValueError, match="session_start_utc"):
        vwap(series([(11, 9, 10)]), start)


# -- supertrend ------------------------------------------------------------------------


ST_ROWS = [(11, 9, 10), (12, 10, 11), (14, 12, 13.5), (15, 13, 14), (13, 9, 9.5)]
# period 1, multiplier 1 (ATR equals the true range of each bar):
# bar0: tr 2, hl2 10, bands 12 / 8; close 10 is not above 12 -> direction -1, value 12
# bar1: tr 2, hl2 11, basic 13 / 9; prev close 10 <= 12 -> upper min(13,12)=12;
#       prev close 10 >= 8 -> lower max(9,8)=9; close 11 not above 12 -> -1, value 12
# bar2: tr max(2,3,1)=3, hl2 13, basic 16 / 10; upper min(16,12)=12; lower max(10,9)=10;
#       close 13.5 > 12 -> direction 1, value 10
# bar3: tr max(2,1.5,0.5)=2, hl2 14, basic 16 / 12; prev close 13.5 > 12 -> upper 16 (no ratchet);
#       lower max(12,10)=12; close 14 not below 12 -> 1, value 12
# bar4: tr max(4,1,5)=5, hl2 11, basic 16 / 6; upper min(16,16)=16; lower max(6,12)=12;
#       close 9.5 < 12 -> direction -1, value 16


def test_supertrend_hand_computed_bands_directions_and_values():
    rows = supertrend(series(ST_ROWS), period=1, multiplier=1)
    assert [r["upper"] for r in rows] == [12, 12, 12, 16, 16]
    assert [r["lower"] for r in rows] == [8, 9, 10, 12, 12]
    assert [r["direction"] for r in rows] == [-1, -1, 1, 1, -1]
    assert [r["value"] for r in rows] == [12, 12, 10, 12, 16]
    assert [r["time"] for r in rows] == [c["time"] for c in series(ST_ROWS)]


def test_supertrend_is_none_until_atr_exists_and_keeps_one_row_per_candle():
    candles = series(ST_ROWS)
    rows = supertrend(candles, period=3, multiplier=2.0)
    assert len(rows) == len(candles)
    for row in rows[:2]:
        assert row == {"time": row["time"], "value": None, "direction": None, "upper": None, "lower": None}
    for row in rows[2:]:
        assert set(row) == {"time", "value", "direction", "upper", "lower"}
        assert row["direction"] in (1, -1)
        assert row["value"] == (row["lower"] if row["direction"] == 1 else row["upper"])


def test_supertrend_direction_flips_on_a_synthetic_series():
    down = [(100 - i, 98 - i, 99 - i) for i in range(20)]
    up = [(80 + 2 * i, 78 + 2 * i, 79.5 + 2 * i) for i in range(20)]
    rows = supertrend(series(down + up), period=5, multiplier=1.5)
    directions = [r["direction"] for r in rows if r["direction"] is not None]
    assert directions[0] == -1 and directions[-1] == 1
    assert directions.count(-1) >= 10 and directions.count(1) >= 10
    assert directions == sorted(directions)  # exactly one flip: all -1 then all 1

    # While direction is 1 the drawn lower band only ratchets upward and stays below the close.
    rising = [r for r in rows if r["direction"] == 1]
    lows = [r["value"] for r in rising]
    assert lows == sorted(lows)
    assert all(r["value"] < c["close"] for r, c in zip(rows, series(down + up)) if r["direction"] == 1)


def test_supertrend_empty_input_is_empty():
    assert supertrend([]) == []


@pytest.mark.parametrize("period", [0, -1, 2.0, "10", True])
def test_supertrend_rejects_unusable_periods(period):
    with pytest.raises(ValueError, match="Supertrend period"):
        supertrend(series(ST_ROWS), period=period)


@pytest.mark.parametrize("multiplier", [0, -1.0, "3", True, float("nan"), float("inf")])
def test_supertrend_rejects_unusable_multipliers(multiplier):
    with pytest.raises(ValueError, match="Supertrend multiplier"):
        supertrend(series(ST_ROWS), period=1, multiplier=multiplier)


# -- indicator_series --------------------------------------------------------------------


CANDLES = series([(11, 9, 10, 1), (12, 10, 11, 2), (13, 11, 12, 3), (14, 12, 13, 4), (15, 13, 14, 5), (16, 14, 15, 6)])


def test_indicator_series_shape_and_alignment():
    out = indicator_series(CANDLES, ema_periods=[3, 5], vwap_on=True, supertrend=(2, 1.0))
    assert set(out) == {"ema", "vwap", "supertrend"}
    assert list(out["ema"]) == ["3", "5"]

    times = [c["time"] for c in CANDLES]
    closes = [c["close"] for c in CANDLES]
    assert [p["time"] for p in out["ema"]["3"]] == times[2:]
    assert [p["value"] for p in out["ema"]["3"]] == [v for v in ema(closes, 3) if v is not None]
    assert [p["time"] for p in out["ema"]["5"]] == times[4:]
    assert all(set(p) == {"time", "value"} for p in out["ema"]["3"])

    assert [p["time"] for p in out["vwap"]] == times
    assert [p["value"] for p in out["vwap"]] == pytest.approx(vwap(CANDLES))
    assert all(set(p) == {"time", "value"} for p in out["vwap"])

    assert [p["time"] for p in out["supertrend"]] == times[1:]
    assert all(set(p) == {"time", "value", "direction"} for p in out["supertrend"])
    assert all(p["direction"] in (1, -1) for p in out["supertrend"])

    json.dumps(out)  # JSON-ready


def test_indicator_series_omits_disabled_overlays():
    out = indicator_series(CANDLES, ema_periods=[], vwap_on=False, supertrend=None)
    assert out == {"ema": {}, "vwap": [], "supertrend": []}


def test_indicator_series_de_duplicates_ema_periods_keeping_order():
    out = indicator_series(CANDLES, ema_periods=[21, 9, 21, 9], vwap_on=False, supertrend=None)
    assert list(out["ema"]) == ["21", "9"]
    assert out["ema"]["21"] == []  # not enough bars yet: no points rather than None values


def test_indicator_series_with_no_candles_is_empty_but_well_formed():
    out = indicator_series([], ema_periods=[9], vwap_on=True, supertrend=(10, 3.0))
    assert out == {"ema": {"9": []}, "vwap": [], "supertrend": []}


def test_indicator_series_honours_the_session_start_for_vwap():
    late = candle(utc(2026, 10, 26, 23, 59), 11, 9, 10, 1)
    midnight = candle(utc(2026, 10, 27, 0, 0), 21, 19, 20, 1)
    out = indicator_series([late, midnight], ema_periods=[], vwap_on=True, supertrend=None, session_start_utc=(0, 0))
    assert [p["value"] for p in out["vwap"]] == pytest.approx([10.0, 20.0])


def test_indicator_series_limits_the_number_of_ema_lines():
    assert indicators.MAX_EMA_PERIODS == 5
    indicator_series(CANDLES, ema_periods=[1, 2, 3, 4, 5], vwap_on=False, supertrend=None)
    indicator_series(CANDLES, ema_periods=[1, 2, 3, 4, 5, 5], vwap_on=False, supertrend=None)  # duplicates do not count
    with pytest.raises(ValueError, match="At most 5 EMA periods"):
        indicator_series(CANDLES, ema_periods=[1, 2, 3, 4, 5, 6], vwap_on=False, supertrend=None)


@pytest.mark.parametrize("periods", [[0], [9, -1], [9.5], ["9"], [True], "9,21", 9])
def test_indicator_series_rejects_unusable_ema_periods(periods):
    with pytest.raises(ValueError, match="EMA period"):
        indicator_series(CANDLES, ema_periods=periods, vwap_on=False, supertrend=None)


@pytest.mark.parametrize(
    "spec, message",
    [
        ((0, 3.0), "Supertrend period"),
        ((10.0, 3.0), "Supertrend period"),
        ((10, 0), "Supertrend multiplier"),
        ((10, -2.0), "Supertrend multiplier"),
        ((10, "3"), "Supertrend multiplier"),
        ((10,), "Supertrend settings"),
        ("10,3", "Supertrend settings"),
        (10, "Supertrend settings"),
    ],
)
def test_indicator_series_rejects_unusable_supertrend_settings(spec, message):
    with pytest.raises(ValueError, match=message):
        indicator_series(CANDLES, ema_periods=[], vwap_on=False, supertrend=spec)


def test_indicator_series_rejects_unusable_session_start():
    with pytest.raises(ValueError, match="session_start_utc"):
        indicator_series(CANDLES, ema_periods=[], vwap_on=False, supertrend=None, session_start_utc=(25, 0))


def test_indicator_series_coerces_an_integer_multiplier_to_float():
    out = indicator_series(CANDLES, ema_periods=[], vwap_on=False, supertrend=(2, 1))
    expected = indicator_series(CANDLES, ema_periods=[], vwap_on=False, supertrend=(2, 1.0))
    assert out == expected


# -- indicator plugins that reuse these helpers -------------------------------------------------------


def test_atr_plugin_draws_exactly_the_core_atr():
    from dashboard import indicator_plugins

    data = series([(2, 1, 1.5), (3, 2, 2.5), (4, 2, 3), (5, 3, 4.5), (4.5, 3.5, 4)])
    out = indicator_plugins.run(indicator_plugins.discover()["atr"], data, {"length": 3})
    expected = [{"time": c["time"], "value": v} for c, v in zip(data, atr(data, 3)) if v is not None]
    assert out["series"][0]["points"] == expected and len(expected) == 3
