"""Bollinger Bands: a moving average with bands a chosen number of standard deviations away.

Three lines, drawn with the user's length, multiplier and price source:

- basis = simple moving average of the source over the last ``length`` bars
- upper = basis + mult * standard deviation of those same ``length`` values
- lower = basis - mult * standard deviation

The standard deviation is the population form (divide by ``length``), the usual charting
convention. These are lines on a chart; nothing here is a signal or a recommendation.
"""

import math

from dashboard.indicator_plugins import SOURCES, source_values

NAME = "bollinger"
TITLE = "Bollinger Bands"
DESCRIPTION = "Simple moving average with bands a chosen number of standard deviations above and below it."
OVERLAY = True
PARAMS = [
    {"name": "length", "label": "Length", "type": "int", "default": 20, "min": 1, "max": 500},
    {"name": "mult", "label": "StdDev", "type": "float", "default": 2.0, "min": 0.1, "max": 10, "step": 0.1,
     "help": "How many standard deviations the bands sit from the basis"},
    {"name": "source", "label": "Source", "type": "str", "default": "close", "choices": list(SOURCES)},
    {"name": "basis_color", "label": "Basis", "type": "str", "default": "#f5b041"},
    {"name": "band_color", "label": "Bands", "type": "str", "default": "#5dade2"},
]


def compute(candles, params):
    length, mult = params["length"], params["mult"]
    values = source_values(candles, params["source"])

    basis, upper, lower = [], [], []
    window = []          # the last `length` source values
    total = 0.0          # running sum of (value - shift) over the window
    total_sq = 0.0       # running sum of (value - shift) ** 2
    shift = None         # values are measured from the first price seen, which keeps the running
                         # sums small and the variance accurate even for large prices
    for candle, value in zip(candles, values):
        if value is None:            # a gap in the data restarts the window
            window.clear()
            total = total_sq = 0.0
            continue
        if shift is None:
            shift = value
        x = value - shift
        window.append(x)
        total += x
        total_sq += x * x
        if len(window) > length:
            old = window.pop(0)
            total -= old
            total_sq -= old * old
        if len(window) < length:
            continue
        mean = total / length
        variance = max(total_sq / length - mean * mean, 0.0)   # never below 0 from rounding
        spread = mult * math.sqrt(variance)
        t = candle["time"]
        middle = mean + shift
        basis.append({"time": t, "value": middle})
        upper.append({"time": t, "value": middle + spread})
        lower.append({"time": t, "value": middle - spread})

    tag = f"({length}, {mult:g})"
    return {"series": [
        {"id": "basis", "label": f"BB basis {tag}", "type": "line", "color": params["basis_color"], "points": basis},
        {"id": "upper", "label": f"BB upper {tag}", "type": "line", "color": params["band_color"], "points": upper},
        {"id": "lower", "label": f"BB lower {tag}", "type": "line", "color": params["band_color"], "points": lower},
    ]}
