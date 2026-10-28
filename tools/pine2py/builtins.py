"""Tables of the Pine Script built-ins the converter knows: constants, variables, functions.

``FUNCS`` maps a Pine function to the runtime function that implements it (``rt.<py>``), its
parameter names in Pine's positional order (so named arguments bind correctly), the Python code
used for an omitted optional argument, and what the runtime function takes first: a per
call-site ``State`` ("state"), the bar ``Context`` ("ctx"), or nothing. Functions that need
special handling (inputs, plots, ``nz``, ``color.new``...) are handled in codegen.py instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OMIT = object()   # optional argument the runtime defaults itself (only passed when given)

# Pine v5 named colors.
COLORS = {
    "aqua": "#00bcd4", "black": "#363a45", "blue": "#2196f3", "fuchsia": "#e040fb", "gray": "#787b86",
    "green": "#4caf50", "lime": "#00e676", "maroon": "#880e4f", "navy": "#311b92", "olive": "#808000",
    "orange": "#ff9800", "purple": "#9c27b0", "red": "#ff5252", "silver": "#b2b5be", "teal": "#00897b",
    "white": "#ffffff", "yellow": "#ffeb3b",
}
DEFAULT_PLOT_COLOR = "#2962ff"
DEFAULT_HLINE_COLOR = "#787b86"
ALERT_COLOR = "#f5b041"
STRATEGY_COLOR = "#787b86"

# Named constants used as plot/shape arguments. Values are what const evaluation returns.
CONSTANTS: dict[str, object] = {f"color.{k}": v for k, v in COLORS.items()}
for _ns, _names in {
    "shape": ["xcross", "cross", "circle", "triangleup", "triangledown", "flag", "arrowup", "arrowdown",
              "labelup", "labeldown", "square", "diamond"],
    "location": ["abovebar", "belowbar", "top", "bottom", "absolute"],
    "size": ["auto", "tiny", "small", "normal", "large", "huge"],
    "plot": ["style_line", "style_linebr", "style_stepline", "style_steplinebr", "style_stepline_diamond",
             "style_histogram", "style_columns", "style_circles", "style_cross", "style_area", "style_areabr"],
    "hline": ["style_solid", "style_dotted", "style_dashed"],
    "display": ["none", "all", "pane", "data_window", "price_scale", "status_line"],
    "format": ["inherit", "price", "volume", "percent", "mintick"],
    "scale": ["right", "left", "none"],
    "position": ["top_left", "top_center", "top_right", "middle_left", "middle_center", "middle_right",
                 "bottom_left", "bottom_center", "bottom_right"],
    "extend": ["none", "left", "right", "both"],
    "xloc": ["bar_index", "bar_time"],
    "yloc": ["price", "abovebar", "belowbar"],
    "text": ["align_left", "align_center", "align_right", "align_top", "align_bottom", "wrap_auto", "wrap_none"],
    "font": ["family_default", "family_monospace"],
    "barmerge": ["gaps_on", "gaps_off", "lookahead_on", "lookahead_off"],
    "alert": ["freq_all", "freq_once_per_bar", "freq_once_per_bar_close"],
    "order": ["ascending", "descending"],
    "strategy": ["long", "short", "fixed", "cash", "percent_of_equity"],
    "adjustment": ["none", "splits", "dividends"],
    "session": ["regular", "extended"],
    "currency": ["NONE", "USD", "EUR"],
}.items():
    for _n in _names:
        CONSTANTS[f"{_ns}.{_n}"] = f"{_ns}.{_n}"
for _i, _day in enumerate(["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"], start=1):
    CONSTANTS[f"dayofweek.{_day}"] = _i
CONSTANTS.update({"math.pi": 3.141592653589793, "math.e": 2.718281828459045,
                  "math.phi": 1.618033988749895, "math.rphi": 0.6180339887498949})

# v4 names for the same constants.
V4_ALIASES = {
    "integer": "input.integer", "float": "input.float", "bool": "input.bool", "string": "input.string",
    "source": "input.source", "resolution": "input.resolution", "color": "input.color",
}

SHAPES = {
    "shape.triangleup": "arrowUp", "shape.arrowup": "arrowUp", "shape.labelup": "arrowUp",
    "shape.triangledown": "arrowDown", "shape.arrowdown": "arrowDown", "shape.labeldown": "arrowDown",
    "shape.circle": "circle", "shape.xcross": "circle", "shape.cross": "circle", "shape.diamond": "circle",
    "shape.flag": "circle", "shape.square": "square",
}
LOCATIONS = {"location.abovebar": "aboveBar", "location.top": "aboveBar", "location.absolute": "aboveBar",
             "location.belowbar": "belowBar", "location.bottom": "belowBar"}
SIZES = {"size.auto": None, "size.tiny": 0.5, "size.small": 0.75, "size.normal": 1, "size.large": 1.5, "size.huge": 2}
HLINE_STYLES = {"hline.style_solid": "solid", "hline.style_dotted": "dotted", "hline.style_dashed": "dashed"}
PLOT_STYLES = {
    "plot.style_line": "line", "plot.style_linebr": "line", "plot.style_stepline": "line",
    "plot.style_steplinebr": "line", "plot.style_stepline_diamond": "line", "plot.style_area": "line",
    "plot.style_areabr": "line", "plot.style_histogram": "histogram", "plot.style_columns": "histogram",
    "plot.style_circles": "markers", "plot.style_cross": "markers",
}
PLOT_STYLE_NOTES = {
    "plot.style_stepline": "step lines are drawn as plain lines", "plot.style_steplinebr": "step lines are drawn as plain lines",
    "plot.style_stepline_diamond": "step lines are drawn as plain lines",
    "plot.style_area": "area plots are drawn as lines", "plot.style_areabr": "area plots are drawn as lines",
    "plot.style_circles": "circle plots are drawn as markers above the bars",
    "plot.style_cross": "cross plots are drawn as markers above the bars",
}

# Price series the bar provides. ``ctx.<name>[n]`` is the history; the bar attribute is the current value.
COLUMNS = {
    "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume",
    "hl2": "hl2", "hlc3": "hlc3", "ohlc4": "ohlc4", "hlcc4": "hlcc4", "bar_index": "index", "time": "time",
}
COLUMN_ORDER = ["bar_index", "time", "open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4", "hlcc4"]
SOURCE_CHOICES = ["close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "hlcc4"]

# Other built-in variables: name -> (python code, kind, warning or None)
_UTC = "uses the bar time in UTC (Pine uses the exchange time zone)"
_TF = "is derived from the candle spacing (the dashboard has no symbol settings)"
_SYM = "is not available to dashboard indicators"
VARIABLES: dict[str, tuple] = {
    "last_bar_index": ("ctx.last_bar_index", "num", None),
    "last_bar_time": ("(ctx.times[-1] * 1000)", "num", None),
    "time_close": ("(time + ctx.bar_seconds * 1000)", "num", "time_close is the bar time plus one bar"),
    "barstate.isfirst": ("bar.isfirst", "bool", None),
    "barstate.islast": ("bar.islast", "bool", None),
    "barstate.ishistory": ("(not bar.islast)", "bool", None),
    "barstate.isrealtime": ("False", "bool", "barstate.isrealtime is always false (bars are computed after they arrive)"),
    "barstate.isnew": ("True", "bool", None),
    "barstate.isconfirmed": ("True", "bool", "barstate.isconfirmed is always true here"),
    "barstate.islastconfirmedhistory": ("bar.islast", "bool", None),
    "syminfo.mintick": ("ctx.mintick", "num", None),      # handled specially (adds the tick_size parameter)
    "syminfo.ticker": ("''", "str", f"syminfo.ticker {_SYM}; it is an empty string"),
    "syminfo.tickerid": ("''", "str", f"syminfo.tickerid {_SYM}; it is an empty string"),
    "syminfo.prefix": ("''", "str", f"syminfo.prefix {_SYM}; it is an empty string"),
    "syminfo.root": ("''", "str", f"syminfo.root {_SYM}; it is an empty string"),
    "syminfo.description": ("''", "str", f"syminfo.description {_SYM}; it is an empty string"),
    "syminfo.currency": ("'USD'", "str", f"syminfo.currency {_SYM}; it is 'USD'"),
    "syminfo.timezone": ("'UTC'", "str", "syminfo.timezone is UTC here"),
    "syminfo.pointvalue": ("1.0", "num", f"syminfo.pointvalue {_SYM}; it is 1"),
    "syminfo.type": ("'futures'", "str", f"syminfo.type {_SYM}; it is 'futures'"),
    "timeframe.period": ("ctx.timeframe_period()", "str", f"timeframe.period {_TF}"),
    "timeframe.multiplier": ("ctx.timeframe_multiplier()", "num", f"timeframe.multiplier {_TF}"),
    "timeframe.isintraday": ("(ctx.bar_seconds < 86400)", "bool", f"timeframe.isintraday {_TF}"),
    "timeframe.isdaily": ("(ctx.bar_seconds == 86400)", "bool", f"timeframe.isdaily {_TF}"),
    "timeframe.isweekly": ("(ctx.bar_seconds == 604800)", "bool", f"timeframe.isweekly {_TF}"),
    "timeframe.ismonthly": ("False", "bool", f"timeframe.ismonthly {_TF}"),
    "timeframe.isdwm": ("(ctx.bar_seconds >= 86400)", "bool", f"timeframe.isdwm {_TF}"),
    "timeframe.isminutes": ("(60 <= ctx.bar_seconds < 86400)", "bool", f"timeframe.isminutes {_TF}"),
    "timeframe.isseconds": ("(ctx.bar_seconds < 60)", "bool", f"timeframe.isseconds {_TF}"),
    "timeframe.isticks": ("False", "bool", f"timeframe.isticks {_TF}"),
    "year": ("rt.time_part(time, 'year')", "num", f"year {_UTC}"),
    "month": ("rt.time_part(time, 'month')", "num", f"month {_UTC}"),
    "weekofyear": ("rt.time_part(time, 'weekofyear')", "num", f"weekofyear {_UTC}"),
    "dayofmonth": ("rt.time_part(time, 'dayofmonth')", "num", f"dayofmonth {_UTC}"),
    "dayofweek": ("rt.time_part(time, 'dayofweek')", "num", f"dayofweek {_UTC}"),
    "hour": ("rt.time_part(time, 'hour')", "num", f"hour {_UTC}"),
    "minute": ("rt.time_part(time, 'minute')", "num", f"minute {_UTC}"),
    "second": ("rt.time_part(time, 'second')", "num", f"second {_UTC}"),
    "strategy.position_size": ("0", "num", "strategy.position_size is 0: strategy order logic is not converted"),
    "strategy.equity": ("na", "num", "strategy.equity is na: strategy order logic is not converted"),
    "strategy.netprofit": ("na", "num", "strategy.netprofit is na: strategy order logic is not converted"),
    "strategy.opentrades": ("0", "num", "strategy.opentrades is 0: strategy order logic is not converted"),
    "strategy.closedtrades": ("0", "num", "strategy.closedtrades is 0: strategy order logic is not converted"),
    "strategy.position_avg_price": ("na", "num", "strategy.position_avg_price is na: strategy order logic is not converted"),
}
# Variables that read the uses of these built-ins (time parts need the bar time).
VARIABLE_NEEDS = {name: ("time",) for name in ("year", "month", "weekofyear", "dayofmonth", "dayofweek",
                                                 "hour", "minute", "second", "time_close")}
# Stateful built-in variables: computed by a call-site state like a function call.
STATE_VARIABLES = {
    "ta.vwap": ("ta_vwap", ["hlc3"]),
    "ta.obv": ("ta_obv", []),
}
CTX_VARIABLES = {
    "ta.tr": "rt.ta_tr(ctx, False)",
}


@dataclass
class Fn:
    py: str                                    # attribute of the runtime module
    params: list                               # Pine parameter names, positional order
    defaults: dict = field(default_factory=dict)   # param -> python code (or OMIT)
    first: str | None = None                   # "state", "ctx" or None
    kind: str = "num"                          # num | bool | str | color | tuple
    variadic: bool = False
    note: str | None = None                    # warning text when used


def _ta(py, params, defaults=None, kind="num", note=None):
    return Fn(py, params, defaults or {}, "state", kind, False, note)


def _pure(py, params, defaults=None, kind="num", variadic=False):
    return Fn(py, params, defaults or {}, None, kind, variadic)


FUNCS: dict[str, Fn] = {
    # moving averages and smoothing
    "ta.sma": _ta("ta_sma", ["source", "length"]),
    "ta.ema": _ta("ta_ema", ["source", "length"]),
    "ta.rma": _ta("ta_rma", ["source", "length"]),
    "ta.wma": _ta("ta_wma", ["source", "length"]),
    "ta.vwma": _ta("ta_vwma", ["source", "length"]),
    "ta.hma": _ta("ta_hma", ["source", "length"]),
    "ta.swma": _ta("ta_swma", ["source"]),
    # volatility and bands
    "ta.atr": _ta("ta_atr", ["length"]),
    "ta.stdev": _ta("ta_stdev", ["source", "length", "biased"], {"biased": "True"}),
    "ta.variance": _ta("ta_variance", ["source", "length", "biased"], {"biased": "True"}),
    "ta.dev": _ta("ta_dev", ["source", "length"]),
    "ta.bb": _ta("ta_bb", ["series", "length", "mult"], kind="tuple"),
    "ta.bbw": _ta("ta_bbw", ["series", "length", "mult"]),
    "ta.kc": _ta("ta_kc", ["series", "length", "mult", "useTrueRange"], {"useTrueRange": "True"}, kind="tuple"),
    "ta.supertrend": _ta("ta_supertrend", ["factor", "atrPeriod"], kind="tuple"),
    "ta.sar": _ta("ta_sar", ["start", "inc", "max"]),
    # oscillators
    "ta.rsi": _ta("ta_rsi", ["source", "length"]),
    "ta.macd": _ta("ta_macd", ["source", "fastlen", "slowlen", "siglen"], kind="tuple"),
    "ta.stoch": _ta("ta_stoch", ["source", "high", "low", "length"]),
    "ta.cci": _ta("ta_cci", ["source", "length"]),
    "ta.mfi": _ta("ta_mfi", ["series", "length"]),
    "ta.wpr": _ta("ta_wpr", ["length"]),
    "ta.change": _ta("ta_change", ["source", "length"], {"length": "1"}),
    "ta.mom": _ta("ta_mom", ["source", "length"]),
    "ta.roc": _ta("ta_roc", ["source", "length"]),
    "ta.cum": _ta("ta_cum", ["source"]),
    "math.sum": _ta("math_sum", ["source", "length"]),
    "ta.sum": _ta("ta_sum", ["source", "length"]),
    "ta.median": _ta("ta_median", ["source", "length"]),
    "ta.linreg": _ta("ta_linreg", ["source", "length", "offset"], {"offset": "0"}),
    "ta.rising": _ta("ta_rising", ["source", "length"], kind="bool"),
    "ta.falling": _ta("ta_falling", ["source", "length"], kind="bool"),
    # windows (one-argument overloads are resolved in codegen)
    "ta.highest": _ta("ta_highest", ["source", "length"]),
    "ta.lowest": _ta("ta_lowest", ["source", "length"]),
    "ta.highestbars": _ta("ta_highestbars", ["source", "length"]),
    "ta.lowestbars": _ta("ta_lowestbars", ["source", "length"]),
    "ta.pivothigh": _ta("ta_pivothigh", ["source", "leftbars", "rightbars"]),
    "ta.pivotlow": _ta("ta_pivotlow", ["source", "leftbars", "rightbars"]),
    # events
    "ta.crossover": _ta("ta_crossover", ["source1", "source2"], kind="bool"),
    "ta.crossunder": _ta("ta_crossunder", ["source1", "source2"], kind="bool"),
    "ta.cross": _ta("ta_cross", ["source1", "source2"], kind="bool"),
    "ta.barssince": _ta("ta_barssince", ["condition"]),
    "ta.valuewhen": _ta("ta_valuewhen", ["condition", "source", "occurrence"], {"occurrence": "0"}),
    "ta.vwap": _ta("ta_vwap", ["source", "anchor", "stdev_mult"], {"anchor": OMIT, "stdev_mult": OMIT}),
    "ta.obv": _ta("ta_obv", []),
    "fixnan": _ta("fixnan", ["source"]),
    # math
    "math.abs": _pure("math_abs", ["number"]),
    "math.sqrt": _pure("math_sqrt", ["number"]),
    "math.exp": _pure("math_exp", ["number"]),
    "math.log": _pure("math_log", ["number"]),
    "math.log10": _pure("math_log10", ["number"]),
    "math.sin": _pure("math_sin", ["angle"]),
    "math.cos": _pure("math_cos", ["angle"]),
    "math.tan": _pure("math_tan", ["angle"]),
    "math.asin": _pure("math_asin", ["angle"]),
    "math.acos": _pure("math_acos", ["angle"]),
    "math.atan": _pure("math_atan", ["angle"]),
    "math.todegrees": _pure("math_todegrees", ["radians"]),
    "math.toradians": _pure("math_toradians", ["degrees"]),
    "math.floor": _pure("math_floor", ["number"]),
    "math.ceil": _pure("math_ceil", ["number"]),
    "math.sign": _pure("math_sign", ["number"]),
    "math.pow": _pure("math_pow", ["base", "exponent"]),
    "math.round": _pure("math_round", ["number", "precision"], {"precision": OMIT}),
    "math.max": _pure("math_max", [], variadic=True),
    "math.min": _pure("math_min", [], variadic=True),
    "math.avg": _pure("math_avg", [], variadic=True),
    "math.round_to_mintick": Fn("math_round_to_mintick", ["number"], {}, "ctx"),
    # conversions and strings
    "int": _pure("to_int", ["x"]),
    "float": _pure("to_float", ["x"]),
    "bool": _pure("to_bool", ["x"], kind="bool"),
    "str.tostring": _pure("str_tostring", ["value", "format"], {"format": OMIT}, kind="str"),
    "str.length": _pure("str_length", ["string"]),
    "str.upper": _pure("str_upper", ["source"], kind="str"),
    "str.lower": _pure("str_lower", ["source"], kind="str"),
    "str.contains": _pure("str_contains", ["source", "str"], kind="bool"),
    "str.startswith": _pure("str_startswith", ["source", "str"], kind="bool"),
    "str.endswith": _pure("str_endswith", ["source", "str"], kind="bool"),
    "str.replace_all": _pure("str_replace_all", ["source", "target", "replacement"], kind="str"),
    "str.format": _pure("str_format", [], kind="str", variadic=True),
    # colors (color.new / color.rgb fold to literals when their arguments are constant)
    "color.new": _pure("color_new", ["color", "transp"], {"transp": "0"}, kind="color"),
    "color.rgb": _pure("color_rgb", ["red", "green", "blue", "transp"], {"transp": "0"}, kind="color"),
    "color.from_gradient": _pure("color_from_gradient", ["value", "bottom_value", "top_value", "bottom_color", "top_color"], kind="color"),
}

# Window functions whose one-argument form reads high/low: name -> default source column.
OVERLOAD_SOURCE = {"ta.highest": "high", "ta.lowest": "low", "ta.highestbars": "high", "ta.lowestbars": "low"}
PIVOT_SOURCE = {"ta.pivothigh": "high", "ta.pivotlow": "low"}

# Pine v4 names (no namespace) -> v5 names.
V4_FUNCS = {name.split(".", 1)[1]: name for name in FUNCS if name.startswith(("ta.", "math."))}
V4_FUNCS.update({"tr": "ta.tr", "atr": "ta.atr", "vwap": "ta.vwap", "tostring": "str.tostring",
                 "highest": "ta.highest", "lowest": "ta.lowest", "pivothigh": "ta.pivothigh", "pivotlow": "ta.pivotlow"})
V4_FUNCS.pop("sum", None)
V4_FUNCS["sum"] = "math.sum"
V4_VARIABLES = {"tr": "ta.tr", "vwap": "ta.vwap", "obv": "ta.obv"}
for _k, _v in COLORS.items():   # v3/v4 bare color names
    V4_VARIABLES.setdefault(_k, f"color.{_k}")

PLOT_FUNCS = frozenset({"plot", "plotshape", "plotchar", "plotarrow", "hline", "plotcandle", "plotbar"})
DECLARATIONS = frozenset({"indicator", "study", "strategy", "library"})
SKIPPED_DRAWING = frozenset({"fill", "bgcolor", "barcolor"})
STRATEGY_ORDER_CALLS = {
    "strategy.entry": ["id", "direction", "qty", "limit", "stop", "oca_name", "oca_type", "comment",
                       "alert_message", "disable_alert", "when"],
    "strategy.order": ["id", "direction", "qty", "limit", "stop", "oca_name", "oca_type", "comment",
                       "alert_message", "disable_alert", "when"],
    "strategy.exit": ["id", "from_entry", "qty", "qty_percent", "profit", "limit", "loss", "stop", "trail_price",
                      "trail_points", "trail_offset", "oca_name", "comment", "comment_profit", "comment_loss",
                      "comment_trailing", "alert_message", "alert_profit", "alert_loss", "alert_trailing",
                      "disable_alert", "when"],
    "strategy.close": ["id", "comment", "qty", "qty_percent", "alert_message", "immediately", "disable_alert", "when"],
    "strategy.close_all": ["comment", "alert_message", "immediately", "disable_alert", "when"],
}
UNSUPPORTED_NAMESPACES = {
    "array": "arrays", "matrix": "matrices", "map": "maps", "line": "drawing objects (lines)",
    "label": "drawing objects (labels)", "box": "drawing objects (boxes)", "table": "tables",
    "linefill": "drawing objects (linefills)", "polyline": "drawing objects (polylines)",
    "chart": "chart.* information", "request": "other symbols or timeframes (request.*)",
    "ticker": "other symbols (ticker.*)", "log": "log.* messages", "runtime": "runtime.* calls",
    "strategy": "strategy.* functions", "str": "this str.* function", "timeframe": "this timeframe.* function",
    "ta": "this ta.* function", "math": "this math.* function", "color": "this color.* function",
    "input": "this input.* function", "session": "session.* information", "dividends": "dividends.*",
    "earnings": "earnings.*", "splits": "splits.*", "syminfo": "this syminfo.* value",
    "barstate": "this barstate.* value", "time": "time.*",
}
UNSUPPORTED_FUNCS = {
    "alert": "alert() has no alerts in the dashboard",
    "timestamp": "timestamp() is not supported",
    "max_bars_back": "max_bars_back() is not needed here",
    "time": "time(timeframe, session) is not supported; it gives the bar time",
    "time_close": "time_close() is not supported",
    "security": "security() (other symbols or timeframes) is not supported",
    "plotcandle": "plotcandle() is not supported by the chart contract",
    "plotbar": "plotbar() is not supported by the chart contract",
    "na": None,
}
TIME_FUNCS = {"year", "month", "weekofyear", "dayofmonth", "dayofweek", "hour", "minute", "second"}
INPUT_FUNCS = frozenset({"input", "input.int", "input.float", "input.bool", "input.string", "input.source",
                         "input.color", "input.timeframe", "input.session", "input.symbol", "input.price",
                         "input.time", "input.text_area", "input.enum"})
BOOL_FUNCS = frozenset({"na", "ta.crossover", "ta.crossunder", "ta.cross", "ta.rising", "ta.falling",
                        "str.contains", "str.startswith", "str.endswith", "bool"})
