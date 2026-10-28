"""Donchian channel: the highest high and lowest low of the last N bars, plus their midpoint.

For every bar once ``length`` bars exist (the current bar included):

- upper = the highest high of the last ``length`` bars
- lower = the lowest low of the last ``length`` bars
- mid   = (upper + lower) / 2, drawn only when "Middle" is on

These are lines on a chart; nothing here is a signal or a recommendation.
"""

from collections import deque

NAME = "donchian"
TITLE = "Donchian channel"
DESCRIPTION = "Highest high and lowest low of the last N bars, with an optional middle line."
OVERLAY = True
PARAMS = [
    {"name": "length", "label": "Length", "type": "int", "default": 20, "min": 1, "max": 1000},
    {"name": "mid", "label": "Middle", "type": "bool", "default": True},
    {"name": "color", "label": "Channel", "type": "str", "default": "#5dade2"},
    {"name": "mid_color", "label": "Middle color", "type": "str", "default": "#f5b041"},
]


def rolling_extremes(highs, lows, length):
    """(highest high, lowest low) over the last ``length`` values at each index; None until full.

    Two monotonic queues of indexes keep this O(n): the high queue holds indexes whose highs only
    decrease from front to back, so its front is the window maximum (and the low queue mirrors it).
    """
    out = []
    hi_q, lo_q = deque(), deque()
    for i, (high, low) in enumerate(zip(highs, lows)):
        while hi_q and highs[hi_q[-1]] <= high:
            hi_q.pop()
        hi_q.append(i)
        while lo_q and lows[lo_q[-1]] >= low:
            lo_q.pop()
        lo_q.append(i)
        start = i - length + 1          # first index still inside the window
        if hi_q[0] < start:
            hi_q.popleft()
        if lo_q[0] < start:
            lo_q.popleft()
        out.append((highs[hi_q[0]], lows[lo_q[0]]) if start >= 0 else (None, None))
    return out


def compute(candles, params):
    length = params["length"]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    upper, lower, middle = [], [], []
    for candle, (top, bottom) in zip(candles, rolling_extremes(highs, lows, length)):
        if top is None:
            continue
        t = candle["time"]
        upper.append({"time": t, "value": top})
        lower.append({"time": t, "value": bottom})
        middle.append({"time": t, "value": (top + bottom) / 2})
    series = [
        {"id": "upper", "label": f"Donchian upper {length}", "type": "line", "color": params["color"], "points": upper},
        {"id": "lower", "label": f"Donchian lower {length}", "type": "line", "color": params["color"], "points": lower},
    ]
    if params["mid"]:
        series.append({"id": "mid", "label": f"Donchian middle {length}", "type": "line", "color": params["mid_color"],
                       "line_style": "dashed", "points": middle})
    return {"series": series}
