"""Offline tests for the Pine Script converter (tools/pine2py): lexer, parser, runtime, generated
plugins checked numerically against the dashboard's own indicators and small reference
implementations, and the command line. Nothing here touches the network or an account."""

from __future__ import annotations

import importlib.util
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

from dashboard import indicator_plugins, indicators
from tools.pine2py import GENERATED_MARKER, convert
from tools.pine2py import nodes as N
from tools.pine2py import runtime as rt
from tools.pine2py.errors import PineSyntaxError, format_error
from tools.pine2py.lexer import tokenize
from tools.pine2py.parser import parse

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "tools" / "pine2py" / "examples"


# -- helpers --------------------------------------------------------------------------------------


def walk_candles(count=400, seed=11, step=60, start=1_700_000_000):
    rng = random.Random(seed)
    price = 4000.0
    out = []
    for i in range(count):
        o = price
        c = o + rng.gauss(0, 3)
        h = max(o, c) + abs(rng.gauss(0, 1.5))
        l = min(o, c) - abs(rng.gauss(0, 1.5))
        out.append({"time": start + step * i, "open": o, "high": h, "low": l, "close": c, "volume": 100 + rng.randint(0, 80)})
        price = c
    return out


CANDLES = walk_candles()
CLOSES = [c["close"] for c in CANDLES]


def load_module(code: str, tmp_path: Path, name="zz_pine_case"):
    path = tmp_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(source: str, tmp_path: Path, params=None, candles=None):
    result = convert(source)
    module = load_module(result.code, tmp_path)
    out = indicator_plugins.run(module, candles if candles is not None else CANDLES, params or {})
    return result, {s["id"]: s for s in out["series"]}


def example(name: str) -> str:
    return (EXAMPLES / f"{name}.pine").read_text(encoding="utf-8")


def by_time(series) -> dict:
    return {p["time"]: p for p in series["points"]}


def values(series) -> dict:
    return {p["time"]: p["value"] for p in series["points"]}


def close_to(a, b, rel=1e-6):
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-9)


def assert_same_line(got: dict, expected: list, candles=CANDLES):
    """``expected`` holds one value (or None) per candle."""
    want = {c["time"]: v for c, v in zip(candles, expected) if v is not None}
    assert set(got) == set(want), (sorted(set(got) ^ set(want))[:5], len(got), len(want))
    for t, v in want.items():
        assert close_to(got[t], v), (t, got[t], v)


def sma_ref(xs, n):
    return [sum(xs[i - n + 1:i + 1]) / n if i >= n - 1 else None for i in range(len(xs))]


def stdev_ref(xs, n):
    out = []
    for i in range(len(xs)):
        if i < n - 1:
            out.append(None)
            continue
        w = xs[i - n + 1:i + 1]
        m = sum(w) / n
        out.append(math.sqrt(sum((x - m) ** 2 for x in w) / n))
    return out


def rsi_ref(xs, n):
    out = [None] * len(xs)
    gains = [max(xs[i] - xs[i - 1], 0.0) for i in range(1, len(xs))]
    losses = [max(xs[i - 1] - xs[i], 0.0) for i in range(1, len(xs))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(xs)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


# -- lexer and parser ----------------------------------------------------------------------------


def kinds(source):
    return [t.type for t in tokenize(source).tokens]


def test_lexer_indentation_blocks_and_version():
    lexed = tokenize("//@version=5\nif a\n    b = 1\n    if c\n        d = 2\ne = 3\n")
    assert lexed.version == 5
    types = [t.type for t in lexed.tokens]
    assert types.count("INDENT") == 2 and types.count("DEDENT") == 2 and types[-1] == "EOF"


def test_lexer_line_continuation_forms():
    # an open bracket, a trailing operator and an odd indent all continue the line
    src = "x = f(1,\n  2,\n3)\ny = 1 +\n    2\nz = a\n  + b\n"
    assert kinds(src).count("NEWLINE") == 3
    script, _ = parse(src)
    assert len(script.body) == 3
    assert isinstance(script.body[2].value, N.Binary)


def test_lexer_keeps_dotted_names_and_colors():
    toks = tokenize("plot(ta.sma(close, 5), color=#FF000080)\n").tokens
    assert any(t.type == "NAME" and t.value == "ta.sma" for t in toks)
    assert any(t.type == "COLOR" and t.value == "#ff000080" for t in toks)


@pytest.mark.parametrize("source, line, col, text", [
    ("x = (1 + 2\ny = 3\n", 1, 5, "never closed"),
    ("x = 1\n    y = 2\n", 2, 5, "unexpected indentation"),
    ("if a\n        b = 1\n", 2, 9, "more than one level"),
    ("x = 1 $ 2\n", 1, 7, "unexpected character"),
    ("plot(close,\n", 1, 5, "never closed"),
    ("x = \"abc\n", 1, 5, "not closed"),
    ("if a\nb = 1\n", 2, 1, "indented"),
    ("x = * 2\n", 1, 5, "expected a value"),
])
def test_syntax_errors_carry_line_and_column(source, line, col, text):
    with pytest.raises(PineSyntaxError) as info:
        parse(source)
    err = info.value
    assert (err.line, err.col) == (line, col), str(err)
    assert text in err.message
    rendered = format_error(source, err, "x.pine")
    assert f"x.pine:{line}:{col}: error" in rendered
    assert rendered.splitlines()[-1].strip() == "^" and rendered.isascii()


def test_parser_builds_structures():
    script, _ = parse(
        "//@version=5\nindicator(\"t\")\nvar x = 0\nx := x + 1\n[a, b] = f(1)\n"
        "f(p) =>\n    q = p * 2\n    [q, p]\n"
        "r = if x > 1\n    1\nelse\n    2\nfor i = 0 to 3 by 1\n    x += i\nplot(x, color=color.red)\n")
    kinds_ = [type(s).__name__ for s in script.body]
    assert kinds_ == ["ExprStmt", "VarDecl", "Assign", "TupleDecl", "FuncDef", "VarDecl", "For", "ExprStmt"]
    assert script.body[1].mode == "var"
    assert isinstance(script.body[5].value, N.If)


# -- runtime ---------------------------------------------------------------------------------------


def test_na_semantics():
    na = rt.na
    assert (na + 1) is na and (1 + na) is na and (na * 2) is na and (-na) is na
    assert not (na < 1) and not (1 < na) and not (na == na) and not (na >= 0)
    assert not na and rt.nz(na) == 0 and rt.nz(na, 7) == 7 and rt.nz(3) == 3
    assert rt.isna(na) and rt.isna(None) and rt.isna(float("nan")) and not rt.isna(0)
    assert rt.div(1.0, 0.0) is na and rt.div(na, 2) is na and rt.mod(5, 0) is na and rt.mod(-7, 3) == -1


def test_series_history_is_na_out_of_range():
    s = rt.Series()
    assert s[0] is rt.na and s[1] is rt.na
    s.push(1.0)
    s.push(2.0)
    assert (s[0], s[1]) == (2.0, 1.0) and s[2] is rt.na and s[-1] is rt.na
    s.set(5.0)
    assert s[0] == 5.0


def test_call_sites_have_independent_state():
    ctx = rt.Context(CANDLES[:30], {})
    root = ctx.state()
    a, b = root.sub("a"), root.sub("b")
    fast, slow = [], []
    for bar in ctx.bars():
        fast.append(rt.ta_sma(a, bar.close, 3))
        slow.append(rt.ta_sma(b, bar.close, 10))
    assert fast[1] is rt.na and close_to(fast[2], sum(CLOSES[:3]) / 3)
    assert slow[8] is rt.na and close_to(slow[9], sum(CLOSES[:10]) / 10)


# -- generated plugins: numbers --------------------------------------------------------------------


def test_every_example_converts_and_runs(tmp_path):
    names = sorted(p.stem for p in EXAMPLES.glob("*.pine"))
    assert {"sma_cross", "bollinger", "rsi", "supertrend", "custom_function", "macd"} <= set(names)
    for name in names:
        result = convert(example(name))
        assert result.code.startswith(GENERATED_MARKER) and result.code.isascii()
        module = load_module(result.code, tmp_path, "zz_" + name)
        out = indicator_plugins.run(module, CANDLES, {})
        assert out["series"] and all(s["type"] in indicator_plugins.SERIES_TYPES for s in out["series"])
        assert result.warnings == [], (name, result.warnings)


def test_sma_cross_matches_reference(tmp_path):
    result, s = build(example("sma_cross"), tmp_path)
    assert result.overlay is True and result.title == "SMA cross"
    fast, slow = sma_ref(CLOSES, 9), sma_ref(CLOSES, 21)
    assert_same_line(values(s["fast_sma"]), fast)
    assert_same_line(values(s["slow_sma"]), slow)
    over, under = [], []
    for i in range(1, len(CLOSES)):
        if None in (fast[i], slow[i], fast[i - 1], slow[i - 1]):
            continue
        if fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]:
            over.append(CANDLES[i]["time"])
        if fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]:
            under.append(CANDLES[i]["time"])
    assert over and under
    assert [p["time"] for p in s["fast_over_slow"]["points"]] == over
    assert [p["time"] for p in s["fast_under_slow"]["points"]] == under
    assert {p["text"] for p in s["fast_over_slow"]["points"]} == {"fast over slow"}
    assert {p["text"] for p in s["fast_under_slow"]["points"]} == {"fast under slow"}
    assert {p["position"] for p in s["fast_over_slow"]["points"]} == {"belowBar"}


def test_sma_cross_parameters_change_the_result(tmp_path):
    result, s = build(example("sma_cross"), tmp_path, {"fastLen": 5, "slowLen": 13, "src": "hl2"})
    hl2 = [(c["high"] + c["low"]) / 2 for c in CANDLES]
    assert_same_line(values(s["fast_sma"]), sma_ref(hl2, 5))
    assert_same_line(values(s["slow_sma"]), sma_ref(hl2, 13))


def test_bollinger_matches_reference(tmp_path):
    result, s = build(example("bollinger"), tmp_path)
    basis, dev = sma_ref(CLOSES, 20), stdev_ref(CLOSES, 20)
    assert_same_line(values(s["basis"]), basis)
    assert_same_line(values(s["upper"]), [None if b is None else b + 2.0 * d for b, d in zip(basis, dev)])
    assert_same_line(values(s["lower"]), [None if b is None else b - 2.0 * d for b, d in zip(basis, dev)])


def test_rsi_matches_reference_and_hlines(tmp_path):
    result, s = build(example("rsi"), tmp_path)
    assert result.overlay is False
    assert_same_line(values(s["rsi"]), rsi_ref(CLOSES, 14))
    for sid, level in (("hline_upper_level", 70.0), ("hline_middle_level", 50.0), ("hline_lower_level", 30.0)):
        assert {p["value"] for p in s[sid]["points"]} == {level}
        assert s[sid]["line_style"] == "dotted"


def test_macd_matches_reference(tmp_path):
    result, s = build(example("macd"), tmp_path)
    fast, slow = indicators.ema(CLOSES, 12), indicators.ema(CLOSES, 26)
    line = [None if f is None or sl is None else f - sl for f, sl in zip(fast, slow)]
    first = next(i for i, v in enumerate(line) if v is not None)
    sig_tail = indicators.ema([v for v in line if v is not None], 9)
    signal = [None] * first + sig_tail
    hist = [None if sg is None else l - sg for l, sg in zip(line, signal)]
    assert_same_line(values(s["macd"]), line)
    assert_same_line(values(s["average_line"]), signal)
    assert_same_line(values(s["histogram"]), hist)
    assert s["histogram"]["type"] == "histogram"
    for p in s["histogram"]["points"]:
        assert p["color"] == ("#00897b" if p["value"] >= 0 else "#ff5252")


def test_supertrend_builtin_and_by_hand_match_dashboard(tmp_path):
    result, s = build(example("supertrend"), tmp_path)
    ref = indicators.supertrend(CANDLES, 10, 3.0)
    want = [r["value"] for r in ref]
    assert_same_line(values(s["built_in"]), want)
    assert_same_line(values(s["by_hand"]), want)
    green = "#4caf50"
    for series in (s["built_in"], s["by_hand"]):
        got = by_time(series)
        for c, r in zip(CANDLES, ref):
            if r["value"] is None:
                continue
            # the dashboard's direction 1 is the lower band; Pine's -1 (green in the example)
            assert (got[c["time"]]["color"] == green) == (r["direction"] == 1)


def test_ema_and_atr_match_dashboard(tmp_path):
    src = '''//@version=5
indicator("ea", overlay=true)
len = input.int(14, "Length", minval=1)
plot(ta.ema(close, len), "ema")
plot(ta.atr(len), "atr")
'''
    result, s = build(src, tmp_path)
    assert_same_line(values(s["ema"]), indicators.ema(CLOSES, 14))
    assert_same_line(values(s["atr"]), indicators.atr(CANDLES, 14))


def test_custom_function_keeps_state_per_call_site(tmp_path):
    result, s = build(example("custom_function"), tmp_path)
    n = 10
    highs = [c["high"] for c in CANDLES]

    def half_range(xs, n):
        out = []
        for i in range(len(xs)):
            w = xs[i - n + 1:i + 1] if i >= n - 1 else None
            out.append(None if w is None else (max(w) - min(w)) / 2)
        return out

    mid_c, mid_h = sma_ref(CLOSES, n), sma_ref(highs, n)
    half_c = half_range(CLOSES, n)
    assert_same_line(values(s["mid_close"]), mid_c)
    assert_same_line(values(s["upper_close"]), [None if m is None else m + h for m, h in zip(mid_c, half_c)])
    assert_same_line(values(s["lower_close"]), [None if m is None else m - h for m, h in zip(mid_c, half_c)])
    assert_same_line(values(s["mid_high"]), mid_h)
    assert_same_line(values(s["mid_close_2x"]), sma_ref(CLOSES, 2 * n))


def test_same_function_from_two_sites_inside_another_function(tmp_path):
    src = '''//@version=5
indicator("sites")
inner(x, n) => ta.sma(x, n)
outer(x) => inner(x, 3) - inner(x, 5)
plot(outer(close), "a")
plot(outer(open), "b")
plot(ta.sma(close, 3) - ta.sma(close, 5), "ref")
'''
    result, s = build(src, tmp_path)
    ref = values(s["ref"])
    assert values(s["a"]) == ref
    opens = [c["open"] for c in CANDLES]
    assert_same_line(values(s["b"]), [None if a is None or b is None else a - b
                                      for a, b in zip(sma_ref(opens, 3), sma_ref(opens, 5))])


# -- generated plugins: language semantics ----------------------------------------------------------------


def test_history_na_var_assignment_and_if_expression(tmp_path):
    src = '''//@version=5
indicator("sem")
d = close - close[1]
prev = d[1]
var count = 0
count := count + 1
tone = if close > open
    1
else if close < open
    -1
else
    0
acc = 0.0
for i = 0 to 2
    acc += close[i]
k = 0
while k < 4
    k += 1
plot(d, "d")
plot(prev, "prev")
plot(count, "count")
plot(tone, "tone")
plot(acc / 3, "acc")
plot(k, "k")
plot(na + 1, "never")
plot(close[1] > na ? 5 : 6, "cmp")
plot(nz(close[400]) + 1, "nz")
'''
    result, s = build(src, tmp_path)
    assert len(s["d"]["points"]) == len(CANDLES) - 1
    got = values(s["d"])
    for i in range(1, len(CANDLES)):
        assert close_to(got[CANDLES[i]["time"]], CLOSES[i] - CLOSES[i - 1])
    prev = values(s["prev"])
    assert len(prev) == len(CANDLES) - 2 and close_to(prev[CANDLES[2]["time"]], CLOSES[1] - CLOSES[0])
    assert [p["value"] for p in s["count"]["points"]] == [float(i + 1) for i in range(len(CANDLES))]
    for c in CANDLES:
        expected = 1.0 if c["close"] > c["open"] else -1.0 if c["close"] < c["open"] else 0.0
        assert values(s["tone"])[c["time"]] == expected
    assert close_to(values(s["acc"])[CANDLES[2]["time"]], sum(CLOSES[:3]) / 3)
    assert {p["value"] for p in s["k"]["points"]} == {4.0}
    assert s["never"]["points"] == []
    assert {p["value"] for p in s["cmp"]["points"]} == {6.0}       # a comparison with na is false
    assert {p["value"] for p in s["nz"]["points"]} == {1.0}


def test_runaway_while_loop_is_stopped(tmp_path, monkeypatch):
    src = '//@version=5\nindicator("loop")\nk = 0\nwhile true\n    k += 1\nplot(k)\n'
    result = convert(src)
    module = load_module(result.code, tmp_path)
    monkeypatch.setattr(rt, "MAX_LOOP", 50)
    with pytest.raises(RuntimeError, match="while loop"):
        indicator_plugins.run(module, CANDLES[:5], {})


def test_inputs_become_params(tmp_path):
    src = '''//@version=5
indicator("opts", overlay=true)
a = input.int(5, "A", minval=2, maxval=20, step=1, tooltip="how many")
f = input.float(1.5, "F")
b = input.string("x", "B", options=["x", "y"])
c = input.color(color.red, "C")
d = input.bool(true, "D")
n = input.int(5, "N", options=[3, 5, 8])
s = input.source(hl2, "S")
plot(ta.sma(s, n), "m", color=c)
plot(d ? a * f : na, "d")
'''
    result = convert(src)
    module = load_module(result.code, tmp_path)
    specs = {p["name"]: p for p in module.PARAMS}
    assert specs["a"] == {"name": "a", "label": "A", "type": "int", "default": 5, "min": 2, "max": 20, "step": 1,
                          "help": "how many"}
    assert specs["f"]["type"] == "float" and specs["f"]["default"] == 1.5
    assert specs["b"]["choices"] == ["x", "y"]
    assert specs["c"]["format"] == "color" and specs["c"]["default"] == "#ff5252"
    assert specs["d"] == {"name": "d", "label": "D", "type": "bool", "default": True}
    assert specs["n"]["type"] == "str" and specs["n"]["choices"] == ["3", "5", "8"]
    assert specs["s"]["default"] == "hl2" and "close" in specs["s"]["choices"]
    out = indicator_plugins.run(module, CANDLES, {"a": 4, "n": "8", "d": False, "c": "#112233", "s": "close"})
    series = {x["id"]: x for x in out["series"]}
    assert series["m"]["color"] == "#112233" and series["d"]["points"] == []
    assert_same_line(values(series["m"]), sma_ref(CLOSES, 8))
    described = indicator_plugins.describe(module)
    assert described["overlay"] is True and described["title"] == "opts"


def test_plot_kinds_and_per_point_colors(tmp_path):
    src = '''//@version=5
indicator("kinds")
plot(close, "line", color=close > open ? color.green : color.red, linewidth=3)
plot(close - open, "hist", style=plot.style_columns)
plot(close, "dots", style=plot.style_circles)
plotshape(close > open, "up", style=shape.triangleup, location=location.belowbar, text="up bar")
plotarrow(close - open, "arrows")
hline(100, "level", linestyle=hline.style_dashed)
alertcondition(close > open, "Up bar", "message")
'''
    result, s = build(src, tmp_path)
    assert s["line"]["type"] == "line" and s["line"]["line_width"] == 3
    assert {p["color"] for p in s["line"]["points"]} == {"#4caf50", "#ff5252"}
    assert s["hist"]["type"] == "histogram" and s["dots"]["type"] == "markers"
    assert all(p["shape"] == "arrowUp" and p["position"] == "belowBar" for p in s["up"]["points"])
    assert {p["shape"] for p in s["arrows"]["points"]} == {"arrowUp", "arrowDown"}
    assert s["hline_level"]["line_style"] == "dashed"
    assert s["alert_up_bar"]["type"] == "markers"
    assert any("alertcondition" in w for w in result.warnings)
    assert any("circle plots" in w for w in result.warnings)


def test_unsupported_drawing_is_reported_not_silent(tmp_path):
    src = '''//@version=5
indicator("warn")
bgcolor(close > open ? color.new(color.green, 80) : na)
p1 = plot(close, "a")
p2 = plot(open, "b")
fill(p1, p2)
label.new(bar_index, high, "text")
plot(close, "c")
'''
    result, s = build(src, tmp_path)
    text = "\n".join(result.warnings)
    assert "bgcolor" in text and "fill" in text and "label.new" in text
    assert set(s) == {"a", "b", "c"}


def test_strategy_script_converts_conditions_only(tmp_path):
    src = '''//@version=5
strategy("My strategy", overlay=true)
fast = ta.sma(close, 5)
slow = ta.sma(close, 10)
if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("Long")
plot(fast, "fast")
'''
    result, s = build(src, tmp_path)
    assert "NOT" in result.code.splitlines()[1] and "order logic" in result.code.lower()
    assert any("order logic" in w and "not converted" in w for w in result.warnings)
    assert s["long"]["type"] == "markers" and s["long_2"]["label"] == "Long (close)"
    fast, slow = sma_ref(CLOSES, 5), sma_ref(CLOSES, 10)
    want = [CANDLES[i]["time"] for i in range(1, len(CLOSES))
            if None not in (fast[i - 1], slow[i - 1]) and fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]]
    assert [p["time"] for p in s["long"]["points"]] == want


def test_version_4_names_work(tmp_path):
    src = '''//@version=4
study("v4", overlay=true)
len = input(14, minval=1, title="Len")
plot(sma(close, len), color=red)
plot(highest(10), "hh")
'''
    result, s = build(src, tmp_path)
    assert_same_line(values(s["plot_0"]), sma_ref(CLOSES, 14))
    highs = [c["high"] for c in CANDLES]
    assert_same_line(values(s["hh"]), [max(highs[i - 9:i + 1]) if i >= 9 else None for i in range(len(highs))])


def test_mintick_is_a_setting_with_a_warning(tmp_path):
    src = '//@version=5\nindicator("mt")\nplot(syminfo.mintick, "mt")\n'
    result, s = build(src, tmp_path, {"tick_size": 0.5})
    assert {p["value"] for p in s["mt"]["points"]} == {0.5}
    assert any("mintick" in w for w in result.warnings)


@pytest.mark.parametrize("source, line, col, text", [
    ('//@version=5\nindicator("x")\nplot(foo(close))\n', 3, 6, "unknown function"),
    ('//@version=5\nindicator("x")\nplot(zzz)\n', 3, 6, "not defined"),
    ('//@version=5\nindicator("x")\narr = array.new_float(3)\n', 3, 7, "arrays"),
    ('//@version=5\nindicator("x")\nplot(request.security("X", "D", close))\n', 3, 6, "not supported"),
    ('//@version=5\nindicator("x")\ny := 1\n', 3, 1, "not declared"),
    ('//@version=5\nindicator("x")\nf(a) =>\n    plot(a)\n    a\nplot(f(close))\n', 4, 5, "inside a function"),
    ('//@version=5\nindicator("x")\nx = ta.sma(close)\n', 3, 5, "missing"),
])
def test_conversion_errors_point_at_the_problem(source, line, col, text):
    with pytest.raises(PineSyntaxError) as info:
        convert(source)
    assert (info.value.line, info.value.col) == (line, col), str(info.value)
    assert text in info.value.message


def test_names_that_clash_with_python_are_renamed(tmp_path):
    src = '//@version=5\nindicator("clash")\nclass = close\ndef = class + 1\nplot(def, "d")\n'
    # `class` and `def` are not Pine keywords; they must not break the generated Python
    result, s = build(src, tmp_path)
    assert len(s["d"]["points"]) == len(CANDLES)


def test_generated_code_is_ascii_and_has_the_marker():
    src = '//@version=5\nindicator("Café → test")\nplot(close, "prís")\n'
    result = convert(src)
    assert result.code.isascii() and result.code.splitlines()[0].startswith(GENERATED_MARKER)
    assert result.code.splitlines()[0].endswith("do not edit by hand")
    assert result.name.isidentifier()


# -- command line -------------------------------------------------------------------------------------


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "tools.pine2py", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=60)


def test_cli_writes_a_loadable_plugin(tmp_path):
    out = tmp_path / "from_cli.py"
    done = run_cli(str(EXAMPLES / "rsi.pine"), "-o", str(out), "--name", "rsi_cli")
    assert done.returncode == 0, done.stderr
    code = out.read_text(encoding="utf-8")
    assert code.startswith(GENERATED_MARKER) and "NAME = 'rsi_cli'" in code
    module = load_module(code, tmp_path, "zz_cli_plugin")
    result = indicator_plugins.run(module, CANDLES, {})
    assert {s["id"] for s in result["series"]} >= {"rsi"}
    # a second run may overwrite its own output, but never a file it did not write
    assert run_cli(str(EXAMPLES / "rsi.pine"), "-o", str(out)).returncode == 0
    other = tmp_path / "mine.py"
    other.write_text("# my own file\n", encoding="utf-8")
    refused = run_cli(str(EXAMPLES / "rsi.pine"), "-o", str(other))
    assert refused.returncode == 1 and other.read_text(encoding="utf-8") == "# my own file\n"


def test_cli_prints_and_reports_errors(tmp_path):
    shown = run_cli(str(EXAMPLES / "macd.pine"), "--print")
    assert shown.returncode == 0 and "def compute(candles, params):" in shown.stdout
    bad = tmp_path / "bad.pine"
    bad.write_text('//@version=5\nindicator("x")\nplot(close,\n', encoding="utf-8")
    failed = run_cli(str(bad))
    assert failed.returncode == 1 and "bad.pine:3:5: error" in failed.stderr and "^" in failed.stderr
