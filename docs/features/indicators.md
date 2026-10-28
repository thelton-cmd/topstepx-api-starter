# Indicator plugins and volume profiles

The chart's **Indicators** menu lists the built-in EMA, VWAP and Supertrend, then every plugin it
finds in `dashboard/indicators_ext/`, then two volume profiles. Everything is drawn with the
parameters you pick. They are lines on a chart, not signals.

## Plugins

A plugin is one Python file. The kit ships `sma`, `bollinger`, `rsi`, `atr` and `donchian`; `rsi`
and `atr` open in their own pane under the price chart, the others draw on the price pane. Each
plugin's parameters appear in the menu when you turn it on, and the settings are remembered in the
browser.

To add your own, copy `dashboard/indicators_ext/sma.py`, change `NAME`, `TITLE`, `PARAMS` and
`compute()`, and save. The folder is scanned on every request, so the new file shows up the next
time you open the menu, no restart needed. A plugin that fails to import is listed with its error
instead of taking the chart down. The exact contract (series types `line`, `histogram`,
`markers`; parameter types `int`, `float`, `bool`, `str`) is in `dashboard/indicator_plugins.py`.

If you have a Pine Script indicator, the **Pine Script** panel converts it into a plugin file for
you (see `pine2py.md`).

## Volume profiles

- **Session volume profile** uses the depth feed's volume traded at each price for the current
  session (the same numbers as the DOM's `vol` column).
- **Visible range volume profile (from bars)** spreads each visible candle's volume evenly across
  its high-low range in tick-sized rows.

Both draw as a horizontal histogram against the right edge of the price pane; the row with the
most volume is brighter and labelled.

Routes: `GET /api/indicators`, `GET /api/indicators/series?contract=&name=&params=&after=`,
`GET /api/indicators/profile?contract=[&source=bars&from=&to=]`.
