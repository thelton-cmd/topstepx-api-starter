"""RSI (relative strength index, Wilder's smoothing), drawn in its own pane under the chart.

For each bar after the first, the change from the previous value is split into a gain (the
change when it is positive, else 0) and a loss (minus the change when it is negative, else 0).

- The first average gain/loss is the simple mean of the first ``length`` gains/losses.
- After that: ``average = (previous average * (length - 1) + this bar's gain or loss) / length``.
- ``RSI = 100 - 100 / (1 + average gain / average loss)``; it is 100 when the average loss is 0
  and 0 when the average gain is 0 (the common charting convention).

Two dotted horizontal lines are drawn at the levels the user sets (70 and 30 unless changed).
They are plain reference lines on a 0-100 scale; nothing here is a signal or a recommendation.
"""

from dashboard.indicator_plugins import SOURCES, source_values

NAME = "rsi"
TITLE = "RSI"
DESCRIPTION = "Relative strength index with Wilder's smoothing, in its own pane, plus two reference lines at levels you choose."
OVERLAY = False
PARAMS = [
    {"name": "length", "label": "Length", "type": "int", "default": 14, "min": 1, "max": 500},
    {"name": "source", "label": "Source", "type": "str", "default": "close", "choices": list(SOURCES)},
    {"name": "upper", "label": "Upper line", "type": "float", "default": 70.0, "min": 0, "max": 100, "step": 1},
    {"name": "lower", "label": "Lower line", "type": "float", "default": 30.0, "min": 0, "max": 100, "step": 1},
    {"name": "color", "label": "Color", "type": "str", "default": "#af7ac5"},
    {"name": "level_color", "label": "Lines", "type": "str", "default": "#8aa096"},
]


def rsi_values(values, length):
    """RSI per value (None where it does not exist yet). A None value restarts the calculation."""
    out = []
    prev = None
    avg_gain = avg_loss = None
    seed_gain = seed_loss = 0.0
    seeded = 0
    for value in values:
        if value is None:
            prev, avg_gain, avg_loss, seed_gain, seed_loss, seeded = None, None, None, 0.0, 0.0, 0
            out.append(None)
            continue
        if prev is None:
            prev = value
            out.append(None)
            continue
        change = value - prev
        prev = value
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if avg_gain is None:
            seed_gain += gain
            seed_loss += loss
            seeded += 1
            if seeded < length:
                out.append(None)
                continue
            avg_gain, avg_loss = seed_gain / length, seed_loss / length
        else:
            avg_gain = (avg_gain * (length - 1) + gain) / length
            avg_loss = (avg_loss * (length - 1) + loss) / length
        if avg_loss == 0:
            out.append(100.0)
        elif avg_gain == 0:
            out.append(0.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return out


def compute(candles, params):
    length = params["length"]
    values = rsi_values(source_values(candles, params["source"]), length)
    points = [{"time": c["time"], "value": v} for c, v in zip(candles, values) if v is not None]
    upper, lower = params["upper"], params["lower"]
    # The reference lines span the same bars as the RSI line, so they never stretch the time scale.
    upper_points = [{"time": p["time"], "value": upper} for p in points]
    lower_points = [{"time": p["time"], "value": lower} for p in points]
    return {"series": [
        {"id": "rsi", "label": f"RSI {length}", "type": "line", "color": params["color"], "line_width": 2, "points": points},
        {"id": "upper", "label": f"Line {upper:g}", "type": "line", "color": params["level_color"],
         "line_style": "dotted", "points": upper_points},
        {"id": "lower", "label": f"Line {lower:g}", "type": "line", "color": params["level_color"],
         "line_style": "dotted", "points": lower_points},
    ]}
