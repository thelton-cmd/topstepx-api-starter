"""ATR (average true range, Wilder's smoothing), drawn in its own pane under the chart.

The true range of a bar is the widest of: high - low, |high - previous close| and
|low - previous close| (the first bar has no previous close, so it uses high - low). The first
ATR value is the simple mean of the first ``length`` true ranges; after that
``ATR = (previous ATR * (length - 1) + true range) / length``. The math is
``dashboard.indicators.atr``, the same function the Supertrend overlay uses.

The line is in price units of the contract. Nothing here is a signal or a recommendation.
"""

from dashboard.indicators import atr

NAME = "atr"
TITLE = "ATR"
DESCRIPTION = "Average true range (Wilder's smoothing) in price units, in its own pane."
OVERLAY = False
PARAMS = [
    {"name": "length", "label": "Length", "type": "int", "default": 14, "min": 1, "max": 500},
    {"name": "color", "label": "Color", "type": "str", "default": "#f1948a"},
]


def compute(candles, params):
    length = params["length"]
    values = atr(candles, length)
    points = [{"time": c["time"], "value": v} for c, v in zip(candles, values) if v is not None]
    return {"series": [{"id": "atr", "label": f"ATR {length}", "type": "line", "color": params["color"], "points": points}]}
