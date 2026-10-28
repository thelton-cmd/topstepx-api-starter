"""Pure indicator math for the dashboard chart overlays.

Every function here takes plain Python data and returns plain Python data: no
network, no numpy, no state. Candles are oldest-first dicts with the keys
``time`` (unix seconds, int), ``open``, ``high``, ``low``, ``close`` and
``volume``, exactly as ``DashboardState.candles()`` stores them.

The overlays are drawn with whatever parameters the user picks in the chart
settings. Nothing in this module interprets them; it only computes the lines.
"""

from __future__ import annotations

import math
from typing import Sequence

MAX_EMA_PERIODS = 5
SECONDS_PER_DAY = 86_400


# -- validation helpers ----------------------------------------------------------


def _is_int(value) -> bool:
    # bool is a subclass of int; True is not a usable period.
    return isinstance(value, int) and not isinstance(value, bool)


def _check_period(value, label: str) -> int:
    if not _is_int(value) or value < 1:
        raise ValueError(f"{label} must be a whole number of 1 or more (got {value!r})")
    return value


def _check_session_start(session_start_utc) -> int:
    """Return the session start as seconds after midnight UTC."""
    try:
        hour, minute = session_start_utc
    except (TypeError, ValueError):
        raise ValueError("session_start_utc must be an (hour, minute) pair in UTC") from None
    if not _is_int(hour) or not _is_int(minute) or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(
            f"session_start_utc must be (hour 0-23, minute 0-59) in UTC (got {session_start_utc!r})"
        )
    return hour * 3600 + minute * 60


# -- ema ----------------------------------------------------------------------------


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average of ``values``.

    The first ``period`` values are averaged (simple mean) to seed the line;
    after that each point is ``prev + alpha * (value - prev)`` with
    ``alpha = 2 / (period + 1)``. Indexes before the seed completes are None.
    """
    _check_period(period, "EMA period")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = current + alpha * (values[i] - current)
        out[i] = current
    return out


# -- atr ----------------------------------------------------------------------------


def true_ranges(candles: Sequence[dict]) -> list[float]:
    """True range per candle: the widest of high-low, |high-prev close|, |low-prev close|.

    The first candle has no previous close, so its true range is high-low.
    """
    out: list[float] = []
    prev_close: float | None = None
    for candle in candles:
        high, low = candle["high"], candle["low"]
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        out.append(tr)
        prev_close = candle["close"]
    return out


def atr(candles: Sequence[dict], period: int) -> list[float | None]:
    """Average true range with Wilder smoothing.

    The first value is the simple mean of the first ``period`` true ranges;
    after that ``atr = (prev * (period - 1) + tr) / period``. None before the
    first value exists.
    """
    _check_period(period, "ATR period")
    ranges = true_ranges(candles)
    out: list[float | None] = [None] * len(ranges)
    if len(ranges) < period:
        return out
    current = sum(ranges[:period]) / period
    out[period - 1] = current
    for i in range(period, len(ranges)):
        current = (current * (period - 1) + ranges[i]) / period
        out[i] = current
    return out


# -- vwap ---------------------------------------------------------------------------


def session_key(time_seconds: int, session_start_utc: tuple[int, int] = (22, 0)) -> int:
    """Identify the trading session a bar belongs to.

    Shift the bar time back by the session start (minutes into the UTC day)
    and take the UTC date of the result. Every bar from one session start up to
    the next shares a key, even though the session crosses midnight UTC.
    """
    offset = _check_session_start(session_start_utc)
    return (int(time_seconds) - offset) // SECONDS_PER_DAY


def vwap(candles: Sequence[dict], session_start_utc: tuple[int, int] = (22, 0)) -> list[float | None]:
    """Volume-weighted average price, cumulative within each session.

    Typical price is ``(high + low + close) / 3``. The running sums restart on
    the first bar of a new session (the first bar at or after
    ``session_start_utc`` on a new day; 22:00 UTC matches the CME Globex open
    during US daylight time). While the cumulative volume of a session is still
    zero the previous value is carried forward (None if there is none yet).
    """
    offset = _check_session_start(session_start_utc)
    out: list[float | None] = []
    current_key: int | None = None
    price_volume = 0.0
    volume = 0.0
    last: float | None = None
    for candle in candles:
        key = (int(candle["time"]) - offset) // SECONDS_PER_DAY
        if key != current_key:
            current_key = key
            price_volume = 0.0
            volume = 0.0
        bar_volume = float(candle.get("volume") or 0.0)
        typical = (candle["high"] + candle["low"] + candle["close"]) / 3.0
        price_volume += typical * bar_volume
        volume += bar_volume
        if volume > 0:
            last = price_volume / volume
        out.append(last)
    return out


# -- supertrend ---------------------------------------------------------------------


def supertrend(candles: Sequence[dict], period: int = 10, multiplier: float = 3.0) -> list[dict]:
    """Supertrend bands: ``hl2 +/- multiplier * ATR`` with the usual ratchet.

    Per candle (once ATR exists):

    - basic upper/lower = ``(high + low) / 2`` plus/minus ``multiplier * ATR``
    - final upper = ``min(basic upper, previous final upper)`` while the previous
      close was at or below the previous final upper, else the basic upper
    - final lower = ``max(basic lower, previous final lower)`` while the previous
      close was at or above the previous final lower, else the basic lower
    - direction becomes 1 when the close moves above the final upper band and
      -1 when it moves below the final lower band; otherwise it is unchanged
    - the drawn value is the final lower band while direction is 1 and the
      final upper band while it is -1

    Returns one dict per candle: ``{"time", "value", "direction", "upper",
    "lower"}``; everything except ``time`` is None until ATR is available.
    """
    _check_period(period, "Supertrend period")
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) or not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError(f"Supertrend multiplier must be a number greater than 0 (got {multiplier!r})")

    averages = atr(candles, period)
    out: list[dict] = []
    prev_upper: float | None = None
    prev_lower: float | None = None
    prev_close: float | None = None
    direction: int | None = None

    for candle, average in zip(candles, averages):
        close = candle["close"]
        if average is None:
            out.append({"time": candle["time"], "value": None, "direction": None, "upper": None, "lower": None})
            prev_close = close
            continue

        hl2 = (candle["high"] + candle["low"]) / 2.0
        basic_upper = hl2 + multiplier * average
        basic_lower = hl2 - multiplier * average

        if prev_upper is not None and prev_close is not None and prev_close <= prev_upper:
            final_upper = min(basic_upper, prev_upper)
        else:
            final_upper = basic_upper
        if prev_lower is not None and prev_close is not None and prev_close >= prev_lower:
            final_lower = max(basic_lower, prev_lower)
        else:
            final_lower = basic_lower

        if direction is None:
            direction = 1 if close > final_upper else -1
        elif direction == -1 and close > final_upper:
            direction = 1
        elif direction == 1 and close < final_lower:
            direction = -1

        value = final_lower if direction == 1 else final_upper
        out.append({"time": candle["time"], "value": value, "direction": direction, "upper": final_upper, "lower": final_lower})
        prev_upper, prev_lower, prev_close = final_upper, final_lower, close

    return out


_supertrend = supertrend  # the keyword argument below shadows the function name


# -- assembly for /api/bars ------------------------------------------------------------


def _validate_ema_periods(ema_periods) -> list[int]:
    if ema_periods is None:
        return []
    if isinstance(ema_periods, (str, bytes)) or not isinstance(ema_periods, (list, tuple)):
        raise ValueError("EMA periods must be a list of whole numbers, for example [9, 21]")
    unique: list[int] = []
    for period in ema_periods:
        _check_period(period, "EMA period")
        if period not in unique:
            unique.append(period)
    if len(unique) > MAX_EMA_PERIODS:
        raise ValueError(f"At most {MAX_EMA_PERIODS} EMA periods can be drawn at once (got {len(unique)})")
    return unique


def _validate_supertrend(spec) -> tuple[int, float]:
    try:
        period, multiplier = spec
    except (TypeError, ValueError):
        raise ValueError("Supertrend settings must be a (period, multiplier) pair, for example (10, 3)") from None
    _check_period(period, "Supertrend period")
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) or not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError(f"Supertrend multiplier must be a number greater than 0 (got {multiplier!r})")
    return period, float(multiplier)


def _points(times: list, values: list[float | None]) -> list[dict]:
    return [{"time": t, "value": v} for t, v in zip(times, values) if v is not None]


def indicator_series(
    candles: Sequence[dict],
    *,
    ema_periods: list[int],
    vwap_on: bool,
    supertrend: tuple[int, float] | None,
    session_start_utc: tuple[int, int] = (22, 0),
) -> dict:
    """Build the JSON-ready ``indicators`` block served by ``/api/bars``.

    Returns ``{"ema": {"<period>": [{"time", "value"}...]}, "vwap": [...],
    "supertrend": [{"time", "value", "direction"}...]}``. Points whose value is
    None are left out so the chart can draw each series straight away.
    Raises ValueError with a plain-English message when the settings are not
    usable.
    """
    periods = _validate_ema_periods(ema_periods)
    st_settings = _validate_supertrend(supertrend) if supertrend is not None else None
    _check_session_start(session_start_utc)

    times = [candle["time"] for candle in candles]
    closes = [candle["close"] for candle in candles]
    result: dict = {"ema": {}, "vwap": [], "supertrend": []}

    for period in periods:
        result["ema"][str(period)] = _points(times, ema(closes, period))
    if vwap_on:
        result["vwap"] = _points(times, vwap(candles, session_start_utc))
    if st_settings is not None:
        result["supertrend"] = [
            {"time": row["time"], "value": row["value"], "direction": row["direction"]}
            for row in _supertrend(candles, *st_settings)
            if row["value"] is not None
        ]
    return result
