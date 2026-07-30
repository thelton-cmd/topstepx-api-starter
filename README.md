# TopstepX API Starter Kit (Python)

Get live futures market data streaming from the TopstepX API in **under 5 minutes**. Bring your API key — auth, REST, and real-time SignalR are already wired up, modular, and ready to build on.

```text
$ python examples/04_stream_quotes.py
Streaming quotes for CON.F.US.EP.U26 — Ctrl+C to stop

[connected]
CON.F.US.EP.U26  bid    5301.00   ask    5301.25   last    5301.25
CON.F.US.EP.U26  bid    5301.00   ask    5301.25   last    5301.00
CON.F.US.EP.U26  bid    5300.75   ask    5301.00   last    5301.00
...
```

## Prerequisites

- **Python 3.10+** ([download](https://www.python.org/downloads/))
- **A TopstepX account with API access** — generate your API key in TopstepX under **Settings → API Access**. Details: [TopstepX API Access help article](https://help.topstep.com/en/articles/11187768-topstepx-api-access)

Every TopstepX Combine subscription includes a **free practice account** — the safe place to exercise the API while you learn. There is no separate sandbox environment; the `live: false` flag on data endpoints selects sim data, and orders go to whichever account ID you pass.

## Quickstart (3 steps)

```bash
# 1. Clone and install
git clone https://github.com/thelton-cmd/topstepx-api-starter.git
cd topstepx-api-starter
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -e .

# 2. Add your credentials
copy .env.example .env          # Windows   (macOS/Linux: cp .env.example .env)
#    ...then edit .env and paste in your username + API key

# 3. See live data
python examples/04_stream_quotes.py
```

Something not working? `python examples/00_check_setup.py` diagnoses the usual suspects step by step.

## Examples tour

Work through them in order — each one is short, heavily commented, and runnable on its own.

| Example | What it shows |
|---|---|
| `00_check_setup.py` | Diagnoses your setup: .env, credentials, network, login |
| `01_authenticate.py` | How API-key login and the 24h session token work |
| `02_search_contracts.py` | Find contract IDs (ES, NQ, CL, ...) — or list everything with `--all` |
| `03_historical_bars.py` | Fetch historical OHLCV bars |
| `04_stream_quotes.py` | ★ Live quotes over SignalR — the showcase |
| `05_stream_depth.py` | Live market depth (DOM) + trade ticks |
| `06_user_events.py` | Your account/order/position/trade events in real time |
| `07_place_order_demo.py` | Order API mechanics: place → list → cancel, with explicit parameters and a confirm prompt |

## Docs site — guided onboarding + live playground

Prefer a visual walkthrough? The kit has a companion docs site at
**<https://topstepx-api-docs.fly.dev>** — it walks through the quickstart, tours every example
with the real source code, documents each module, and includes a **live playground**: paste
your API key and stream real quotes over SignalR right in the browser. Market data only — no
order endpoints are reachable from the site, and credentials are kept in memory (never stored).

## Module map — swap or extend any piece

| Module | Responsibility |
|---|---|
| `topstepx/config.py` | Loads credentials from `.env` / environment |
| `topstepx/auth.py` | `TokenManager` — login, auto-refresh ~30 min before expiry |
| `topstepx/http.py` | REST transport — Bearer auth, 401 re-login, 429 backoff |
| `topstepx/client.py` | `TopstepXClient` — every REST endpoint (contracts, bars, accounts, orders, positions, trades) |
| `topstepx/models.py` | Dataclasses for all payloads (`raw` dict always preserved) |
| `topstepx/enums.py` | `OrderType`, `OrderSide`, `OrderStatus`, `PositionType`, `BarUnit` integer codes |
| `topstepx/realtime/base.py` | SignalR plumbing — auto-reconnect **and auto-resubscribe** |
| `topstepx/realtime/market.py` | `MarketHub` — quotes, trades, depth |
| `topstepx/realtime/user.py` | `UserHub` — account, order, position, trade events |

Adding a REST endpoint is ~5 lines in `client.py`. Adding a hub event is a parser + callback list in `market.py`/`user.py`. Want Pydantic validation? Swap the dataclasses in `models.py` — nothing else changes.

## Building on top

```python
import asyncio
from topstepx import TopstepXClient, MarketHub

async def main():
    client = TopstepXClient()                 # reads .env, handles tokens
    contract = client.search_contracts("NQ")[0]

    hub = MarketHub(client.tokens)
    hub.on_quote(lambda cid, q: my_handler(q))   # sync or async callbacks both work
    await hub.start()
    await hub.subscribe_quotes(contract.id)
    await asyncio.Event().wait()              # stream forever

asyncio.run(main())
```

Reconnects, token refresh, and resubscription all happen automatically — your callback just keeps receiving data.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `AuthenticationError` / 401 | Wrong or revoked API key, or `.env` not found. Run `examples/00_check_setup.py`. Regenerate the key in TopstepX → Settings → API Access. |
| `RateLimitError` / 429 | History/retrieveBars allows 50 requests per 30s (all other endpoints: 200 per 60s). The kit retries with backoff automatically — if you still hit this, slow your polling. |
| Stream connects but prints nothing | Market may be closed for that contract, or the contract ID is wrong (verify with `examples/02_search_contracts.py`). |
| WebSocket keeps disconnecting | Auto-reconnect + resubscribe is built in, so brief drops self-heal. Persistent failures: check firewall/VPN rules for `rtc.topstepx.com`. |
| Rare asyncio errors on Windows | Try adding `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` before `asyncio.run(...)`. |

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs entirely offline (mocked transports) — no credentials needed.

## Compliance note

This kit and its examples demonstrate **API mechanics only** — how to authenticate, fetch data, stream events, and submit requests with parameters you supply. Nothing in this repository is trading advice, a trading signal, or a recommendation to enter or exit any position.

## License

MIT — see [LICENSE](LICENSE).
