"""Example 08 — charting starter: historical bars → an interactive chart.

Fetches bars for a contract and writes a self-contained HTML page that draws
them as candles + volume with TradingView Lightweight Charts. Your browser
opens it automatically; pan, zoom, and the crosshair all work out of the box.
Chart times are shown in UTC.

    python examples/08_chart_bars.py                          # ES, 5-minute bars
    python examples/08_chart_bars.py NQ --unit hour --unit-number 1 --bars 500
    python examples/08_chart_bars.py CON.F.US.EP.Z26 --no-open

A starting point for your own charting tool: change the bar request, add
series of your own calculations, or stream live MarketHub quotes into the page.
The chart library loads from cdn.jsdelivr.net, so viewing the page needs an
internet connection. Lightweight Charts is © TradingView, Inc. (Apache-2.0).
"""

import argparse
import html
import json
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from topstepx import Bar, BarUnit, TopstepXClient, TopstepXError
from _safety import choose_active_contract

# Pinned version + Subresource Integrity hash: the browser refuses the file if it changes.
LIGHTWEIGHT_CHARTS_URL = (
    "https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.1/"
    "dist/lightweight-charts.standalone.production.js"
)
LIGHTWEIGHT_CHARTS_SRI = "sha384-KkYqTZlM13Zya6fVUF3IGCBOQ1ehFeJVVgxaVvD/0MebineF9EI8Jkd3NnWNChqN"

UNITS = {
    "second": (BarUnit.SECOND, 1),
    "minute": (BarUnit.MINUTE, 60),
    "hour": (BarUnit.HOUR, 3600),
    "day": (BarUnit.DAY, 86_400),
    "week": (BarUnit.WEEK, 604_800),
    "month": (BarUnit.MONTH, 2_629_800),
}


# ── [arguments] What to chart ───────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chart historical bars with Lightweight Charts")
    parser.add_argument("contract", nargs="?", default="ES", help="symbol (ES, NQ...) or exact contract ID")
    parser.add_argument("--unit", choices=UNITS, default="minute", help="bar unit (default: minute)")
    parser.add_argument("--unit-number", type=int, default=5, help="units per bar (default: 5)")
    parser.add_argument("--bars", type=int, default=300, help="how many bars, 1-20000 (default: 300)")
    parser.add_argument("--out", type=Path, help="output file (default: charts/<contract>_<bar>.html)")
    parser.add_argument("--no-open", action="store_true", help="write the file without opening a browser")
    args = parser.parse_args()
    if args.unit_number <= 0:
        parser.error("--unit-number must be greater than zero")
    if not 1 <= args.bars <= 20_000:
        parser.error("--bars must be between 1 and 20000")
    return args


# ── [request-window] How far back to ask ────────────────────────────
def lookback_start(end: datetime, unit_seconds: int, unit_number: int, bars: int) -> datetime:
    """Ask for 3x the bars' span (markets close nights and weekends), at least 4 days, at most 10 years."""
    span = timedelta(seconds=unit_seconds * unit_number * bars * 3)
    return end - min(max(span, timedelta(days=4)), timedelta(days=3650))


# ── [chart-data] Bars → the shape the chart library wants ───────────
def chart_series(bars: list[Bar]) -> tuple[list[dict], list[dict]]:
    """Return (candles, volume) sorted oldest-first with unique UTC-second timestamps.

    The gateway returns bars newest-first; Lightweight Charts requires
    strictly ascending times, so sort and drop duplicates here.
    """
    by_time: dict[int, Bar] = {}
    for bar in bars:
        if bar.time is not None:
            by_time[int(bar.time.timestamp())] = bar
    candles, volume = [], []
    for t in sorted(by_time):
        bar = by_time[t]
        candles.append({"time": t, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close})
        up = bar.close >= bar.open
        volume.append({"time": t, "value": bar.volume, "color": "#26a69a66" if up else "#ef535066"})
    return candles, volume


def render_chart_html(title: str, subtitle: str, bars: list[Bar]) -> str:
    """Build a standalone HTML page. Data is embedded as JSON, so the file works anywhere."""
    candles, volume = chart_series(bars)
    # "</" inside a <script> block would end it early — escape it.
    data = json.dumps({"candles": candles, "volume": volume}).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  html, body {{ height: 100%; margin: 0; background: #0f1412; color: #d7e0dc;
               font: 14px/1.4 system-ui, -apple-system, Segoe UI, sans-serif; }}
  body {{ display: flex; flex-direction: column; }}
  header {{ padding: 12px 16px; border-bottom: 1px solid #22302a; }}
  h1 {{ margin: 0; font-size: 18px; font-weight: 600; }}
  p {{ margin: 2px 0 0; color: #8aa096; }}
  #chart {{ flex: 1; min-height: 320px; }}
  #error {{ display: none; padding: 16px; color: #ef5350; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <p>{html.escape(subtitle)}</p>
</header>
<div id="error">Could not load the chart library from cdn.jsdelivr.net. Check your internet connection and reload.</div>
<div id="chart"></div>
<script src="{LIGHTWEIGHT_CHARTS_URL}" integrity="{LIGHTWEIGHT_CHARTS_SRI}" crossorigin="anonymous"></script>
<script>
  const data = {data};
  if (!window.LightweightCharts) {{
    document.getElementById("error").style.display = "block";
  }} else {{
    const LC = window.LightweightCharts;
    const chart = LC.createChart(document.getElementById("chart"), {{
      autoSize: true,
      layout: {{ background: {{ color: "#0f1412" }}, textColor: "#d7e0dc" }},
      grid: {{ vertLines: {{ color: "#1b2420" }}, horzLines: {{ color: "#1b2420" }} }},
      timeScale: {{ timeVisible: true, secondsVisible: false }},
    }});
    const candles = chart.addSeries(LC.CandlestickSeries, {{
      upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
      wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    }});
    candles.setData(data.candles);
    const volume = chart.addSeries(LC.HistogramSeries, {{ priceFormat: {{ type: "volume" }}, priceScaleId: "" }});
    volume.priceScale().applyOptions({{ scaleMargins: {{ top: 0.8, bottom: 0 }} }});
    volume.setData(data.volume);
    chart.timeScale().fitContent();
  }}
</script>
</body>
</html>
"""


# ── [fetch] Log in, pick the contract, request bars ─────────────────
def main() -> None:
    args = parse_args()
    unit, unit_seconds = UNITS[args.unit]

    with TopstepXClient() as client:
        contract = choose_active_contract(client, args.contract)
        end = datetime.now(timezone.utc)
        bars = client.get_bars(
            contract.id,
            start=lookback_start(end, unit_seconds, args.unit_number, args.bars),
            end=end,
            unit=unit,
            unit_number=args.unit_number,
            limit=args.bars,
        )

    if not bars:
        print(f"No bars returned for {contract.id} — the market may be closed for this window.")
        return

    # ── [write-open] Write the page and open it ─────────────────────
    bar_label = f"{args.unit_number} {args.unit}{'s' if args.unit_number != 1 else ''}"
    title = f"{contract.name} — {bar_label}"
    subtitle = f"{contract.description} · {contract.id} · {len(bars)} bars · times in UTC · data: TopstepX API"
    out = args.out or Path("charts") / f"{contract.name}_{args.unit_number}{args.unit}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_chart_html(title, subtitle, bars), encoding="utf-8")

    print(f"Charted {len(bars)} bars of {contract.name} ({bar_label}) -> {out.resolve()}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


# ── [errors] The safety net ─────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        print(f"Cannot continue: {exc}")
        sys.exit(1)
