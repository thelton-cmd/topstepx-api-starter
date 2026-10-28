"""Simple moving average: the smallest complete indicator plugin.

A line on the chart drawn with the user's length and price source. Nothing here is a signal.
"""

from dashboard.indicator_plugins import source_values

NAME = "sma"
TITLE = "SMA"
DESCRIPTION = "Simple moving average of the chosen price over N bars."
OVERLAY = True
PARAMS = [
    {"name": "length", "label": "Length", "type": "int", "default": 20, "min": 1, "max": 500},
    {"name": "source", "label": "Source", "type": "str", "default": "close",
     "choices": ["close", "open", "high", "low", "hl2", "hlc3", "ohlc4"]},
    {"name": "color", "label": "Color", "type": "str", "default": "#f7dc6f"},
]


def compute(candles, params):
    length = params["length"]
    values = source_values(candles, params["source"])
    points = []
    window_sum = 0.0
    window = []
    for candle, value in zip(candles, values):
        if value is None:
            window.clear()
            window_sum = 0.0
            continue
        window.append(value)
        window_sum += value
        if len(window) > length:
            window_sum -= window.pop(0)
        if len(window) == length:
            points.append({"time": candle["time"], "value": window_sum / length})
    return {"series": [{"id": "sma", "label": f"SMA {length}", "type": "line", "color": params["color"], "points": points}]}
