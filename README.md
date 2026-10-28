# TopstepX API Starter Kit (Python)

Get live futures market data streaming from the TopstepX API in **under 5 minutes**, then build
your own charting, journaling, and risk tools on top. Bring your API key. Auth, REST, and
real-time SignalR are already wired up, modular, and ready to build on.

```text
$ python examples/04_stream_quotes.py
Streaming quotes for CON.F.US.EP.Z26 — Ctrl+C to stop

[connected]
CON.F.US.EP.Z26  bid    5301.00   ask    5301.25   last    5301.25
CON.F.US.EP.Z26  bid    5301.00   ask    5301.25   last    5301.00
CON.F.US.EP.Z26  bid    5300.75   ask    5301.00   last    5301.00
...
```

## Prerequisites

- **A TopstepX account linked to ProjectX with the separately billed API Access subscription**. Follow the [official linking and API-key steps](https://help.topstep.com/en/articles/11187768-topstepx-api-access).
- **Python 3.10+** ([download](https://www.python.org/downloads/)). If you use the one-prompt setup below, your AI agent can check this for you.

A free **Practice Account** is available with an active Trading Combine, but you must [activate it under Dashboard → Accounts → Add-ons](https://help.topstep.com/en/articles/8284134-practice-account). It is not available with only an Express Funded Account. There is no API sandbox, so examples 06 and 07 refuse any account the API does not mark as simulated and tradable.

## Set it up with one prompt (Claude Code or Codex)

Open [Claude Code](https://code.claude.com/docs/en/overview) or [Codex](https://developers.openai.com/codex)
**on your own computer** in the folder where you keep projects, and paste the prompt below. Use the
desktop app or terminal version, not a cloud sandbox: Topstep's Terms of Use require API activity to
come from your personal device, and VPNs, VPSs, and remote servers are prohibited.

```text
Set up the TopstepX API starter kit for me. Clone https://github.com/thelton-cmd/topstepx-api-starter.git, open the folder, read AGENTS.md, and follow its "First-time setup" steps. Tell me when I need to do something myself, and never ask me to paste my API key into this chat.
```

The agent installs everything, runs the offline tests, and creates your `.env` file. It then
asks you to type your username and API key into that file yourself, so the key never passes
through the chat. Finally it runs the setup checker, your first live data request, and starts the local dashboard in your browser.
After that, describe what you want to build in plain English, for example:

- *"Chart the last week of NQ 15-minute bars."* (starts from example 08)
- *"Add a notes column to my trade journal and group it by contract."* (example 09)
- *"Send me a Discord message when my practice account is down $300 today."* (example 10)

[`AGENTS.md`](AGENTS.md) holds the ground rules the agent follows: credentials stay in `.env`,
no order is sent unless you ask for that specific action, and trading logic always comes from you.

## Quickstart by hand (3 steps)

**Windows PowerShell**

```powershell
git clone https://github.com/thelton-cmd/topstepx-api-starter.git
cd topstepx-api-starter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
# Edit .env, then:
python examples/04_stream_quotes.py
```

**macOS / Linux**

```bash
git clone https://github.com/thelton-cmd/topstepx-api-starter.git
cd topstepx-api-starter
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# Edit .env, then:
python examples/04_stream_quotes.py
```

Something not working? `python examples/00_check_setup.py` diagnoses the usual suspects step by step.

## Examples tour

### Learn the API (00–07)

Work through them in order. Each one is short, heavily commented, and runnable on its own.

| Example | What it shows |
|---|---|
| `00_check_setup.py` | Diagnoses your setup: .env, credentials, network, login, and which accounts the API can see |
| `01_authenticate.py` | How API-key login and the 24h session token work |
| `02_search_contracts.py` | Find contract IDs (ES, NQ, CL, ...), or list everything with `--all` |
| `03_historical_bars.py` | Fetch historical OHLCV bars |
| `04_stream_quotes.py` | ★ Live quotes over SignalR, the showcase |
| `05_stream_depth.py` | Live market depth (DOM) + trade ticks |
| `06_user_events.py` | Your account/order/position/trade events in real time |
| `07_place_order_demo.py` | Order API mechanics: place → list → cancel, with explicit parameters and a confirm prompt |

### Build a tool (08–10)

Starting points for your own tools. All three are **read-only**: they never send orders.

| Example | What it builds |
|---|---|
| `08_chart_bars.py` | **Charting:** historical bars → an interactive candlestick + volume chart in your browser ([TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)) |
| `09_trade_journal.py` | **Journaling:** your fills → a CSV journal plus a per-day summary of fills, P&L, fees, and commissions |
| `10_risk_monitor.py` | **Risk:** live open contracts and P&L, with alerts at limits *you* set (`--max-loss`, `--max-contracts`) |

```bash
python examples/08_chart_bars.py NQ --unit minute --unit-number 15 --bars 500
python examples/09_trade_journal.py --days 30
python examples/10_risk_monitor.py --max-loss 500 --max-contracts 3
```

Charts land in `charts/` and journals in `journal/`. Both folders are git-ignored because they
contain your data.

### Local dashboard (`python -m dashboard`)

A local web app on your own computer at `http://127.0.0.1:8765` (the setup prompt starts it for
you). It shows, for the account you pick from a dropdown:

- a candlestick + volume chart per watched contract (1500 five-minute bars by default) with a
  bar-size control (any number of seconds, minutes, hours or days, plus 1m/5m/15m/1h/1D buttons),
  a history count up to 5000 bars per load (scroll left and older bars load on demand, up to
  20,000), and an Indicators menu with EMA, VWAP and Supertrend overlays you can toggle and configure
- a live tick stream (tape) and a DOM you can trade from: click a bid size to buy or an ask size
  to sell at that price, see your working orders and position on the ladder (click an order to
  cancel), plus session volume at each price
- open positions with unrealized P&L, open orders, today's fills, and P&L tiles
- your fills plotted on the chart, with working orders and your position drawn as price lines
- an order ticket that places orders, cancels open orders and closes positions **only on a Practice
  Account** (the API marks it `simulated`). You choose how to confirm in Settings: review and
  type `yes` (default), review and click Send, or one-click

```bash
python -m dashboard                                   # watch ES NQ GC CL + MES MNQ MGC MCL, pick an account
python -m dashboard --account-id 123 --watch ES NQ CL
python -m dashboard --unit minute --unit-number 15 --bars 2000 --port 8080   # starting chart timeframe
```

Press Ctrl+C in the terminal to stop it. Funded/live accounts are read-only in the dashboard. The
page is only reachable from this computer, and every action needs a token the page carries. The
indicators draw with the parameters you choose; nothing here is a signal or a recommendation. The
module contract lives in `dashboard/ARCHITECTURE.md`; offline tests in `tests/test_dashboard_*.py`.

#### Features

Everything below is a plug-in module (`dashboard/features/`), loaded automatically; start with
`--features none` to turn them all off or `--features apilog,alerts` to pick. Each has a short guide
in [`docs/features/`](docs/features/README.md).

| Feature | What you get |
|---|---|
| **API calls** (`apilog`) | The exact REST request and response, or hub message, behind what the dashboard just did, with secrets redacted and copy-as-curl / copy-as-Python snippets. Every click is a lesson in the API. |
| **Brackets, Flatten, Cancel all, hotkeys** (`trading_tools`) | Attach a stop and target in ticks you set; Flatten all and Cancel all buttons with an itemised review; optional keyboard shortcuts, off by default. Practice Account only, like everything that sends. |
| **Indicator plugins + volume profile** (`indicators`) | Drop a Python file in `dashboard/indicators_ext/` and it appears in the Indicators menu with its parameters; ships SMA, Bollinger, RSI, ATR, Donchian; session and visible-range volume profiles on the chart. |
| **Pine Script to Python** (`pine`) | Paste a Pine Script v5 indicator, get a Python plugin for the chart. Also `python -m tools.pine2py script.pine -o dashboard/indicators_ext/name.py`. Indicators only; order logic is not converted. |
| **Record and replay** (`replay`) | `--record file.ndjson.gz` saves the live stream; `--replay file --speed 10` plays it back into the whole dashboard with transport controls. Order entry is disabled while replaying. |
| **Alerts and guardrails** (`alerts`) | Price alerts you set (Alt+click the chart), fill notifications, sound and desktop notifications, and your own daily loss and contract limits that pause order entry on this dashboard. |
| **Journal and session stats** (`journal`) | Fills paired into round trips, per-day and per-contract statistics, a weekday x hour heatmap, MFE/MAE from 1-minute bars, CSV export. Approximations from API data. |
| **Prompt recipes** (`recipes`) | 25 copy-paste prompts for Claude Code that extend this dashboard and kit: charts, exports, alerts with your numbers, layout, API learning. |

## Module map: swap or extend any piece

| Module | Responsibility |
|---|---|
| `topstepx/config.py` | Loads credentials from `.env` / environment |
| `topstepx/auth.py` | `TokenManager`: login, auto-refresh ~30 min before expiry |
| `topstepx/http.py` | REST transport: Bearer auth, 401 re-login, 429 backoff |
| `topstepx/client.py` | `TopstepXClient`: every REST endpoint (contracts, bars, accounts, orders, positions, trades) |
| `topstepx/models.py` | Dataclasses for all payloads (`raw` dict always preserved) |
| `topstepx/enums.py` | `OrderType`, `OrderSide`, `OrderStatus`, `PositionType`, `BarUnit` integer codes |
| `topstepx/realtime/base.py` | SignalR plumbing: auto-reconnect **and auto-resubscribe** |
| `topstepx/realtime/market.py` | `MarketHub`: quotes, trades, depth |
| `topstepx/realtime/user.py` | `UserHub`: account, order, position, trade events |
| `dashboard/` | The local web dashboard: `state.py`, `indicators.py`, `orders.py`, `server.py`, `app.py`, `static/` |

Adding a REST endpoint is ~5 lines in `client.py`. Adding a hub event is a parser + callback list in `market.py`/`user.py`. Want Pydantic validation? Swap the dataclasses in `models.py`. Nothing else changes.

## Building on top

```python
import asyncio
from topstepx import TopstepXClient, MarketHub

async def main():
    client = TopstepXClient()                 # reads .env, handles tokens
    contract = client.get_contract("CON.F.US.ENQ.Z26")
    if contract.active_contract is not True:
        raise RuntimeError("Choose an active contract with example 02")

    hub = MarketHub(client.tokens)
    hub.on_quote(lambda cid, q: my_handler(q))   # sync or async callbacks both work
    await hub.start()
    await hub.subscribe_quotes(contract.id)
    await asyncio.Event().wait()              # stream forever

asyncio.run(main())
```

Reconnects, token refresh, and resubscription all happen automatically, so your callback just keeps receiving data.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `AuthenticationError` / 401 | Wrong or revoked API key, or `.env` not found. Run `examples/00_check_setup.py`. Regenerate the key in TopstepX → Settings → API Access. |
| `RateLimitError` / 429 | History/retrieveBars allows 50 requests per 30s (all other endpoints: 200 per 60s). The kit retries with backoff automatically. If you still hit this, slow your polling. |
| Stream connects but prints nothing | Market may be closed for that contract, or the contract ID is wrong (verify with `examples/02_search_contracts.py`). |
| "Contract ... is not active" | Futures expire. Run `examples/02_search_contracts.py ES` and use the current contract ID. |
| "matched several active contracts" | Pass an exact contract ID (e.g. `CON.F.US.EP.Z26`) so the script doesn't have to ask. |
| WebSocket keeps disconnecting | Auto-reconnect + resubscribe is built in, so brief drops self-heal. Persistent failures: check firewall rules for `rtc.topstepx.com`. |
| Rare asyncio errors on Windows | Try adding `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` before `asyncio.run(...)`. |

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs entirely offline (mocked transports), so no credentials are needed.

## Compliance note

This kit and its examples demonstrate **API mechanics only**: how to authenticate, fetch data, stream events, and submit requests with parameters you supply. Nothing in this repository is trading advice, a trading signal, or a recommendation to enter or exit any position. The risk monitor's numbers are an approximation built from API data. Topstep's account rules are enforced by Topstep.

## License

MIT. See [LICENSE](LICENSE).

The dashboard bundles [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)
5.2.1 (Apache License 2.0; see `dashboard/static/vendor/LICENSE-lightweight-charts.txt`). The
TradingView logo shown on the chart is part of that library's attribution requirement.
