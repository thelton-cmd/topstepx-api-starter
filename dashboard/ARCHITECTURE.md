# Dashboard architecture and build contract

This file is the contract every module in `dashboard/` is built against. If you change an
interface here, change it here first. Humans: it also explains how the pieces fit.

## Goal

`PY -m dashboard` (or `PY tools/dashboard.py`) starts a local web app at `http://127.0.0.1:8765`
and opens the browser. One page shows, for the selected account:

- account dropdown (every active account on the login), balance, hub connection badges
- a candlestick + volume chart per watched contract with EMA, VWAP and Supertrend overlays the
  user can toggle and configure
- a live tick stream ("tape"): trades and quote changes for the focused contract
- a working DOM (depth-of-market ladder) for the focused contract
- open positions with unrealized P&L, open orders, today's fills, P&L tiles
- an order ticket that can place orders, cancel open orders and close positions, **only on
  accounts the API marks `simulated: true` and `canTrade: true`**, and only after a preflight
  summary and a typed `yes` in the browser

Constraints that never change:

- Python 3.10+ and the standard library only, plus the packages already in `pyproject.toml`.
  No node, no build step, no PowerShell. A trader who ran the README setup prompt has nothing
  else installed.
- The server binds `127.0.0.1` only. Every state-changing request needs the per-process token
  (see Security). Funded/live accounts are read-only no matter what the browser sends.
- `print()` text is ASCII only (Windows cp1252 consoles).
- Nothing in code, comments or UI copy is trading advice. Indicators are drawn with the
  parameters the user chooses; they are never described as signals, entries or exits. Never
  claim anything is profitable.
- All P&L figures are an approximation built from API data (say so on the page).

## Package layout

```
dashboard/
  __init__.py          exports VERSION only
  __main__.py          python -m dashboard  -> cli.main()
  cli.py               argparse + asyncio.run(app.run(args)); the error safety net
  app.py               Session: client, hubs, state, account/focus switching, refresh loops
  state.py             DashboardState (+ DepthBook, Tape): pure bookkeeping, thread-safe
  indicators.py        ema / vwap / supertrend / atr, pure functions on candle dicts
  orders.py            OrderTicket validation, preflight summary, practice-only guard, send
  server.py            ThreadingHTTPServer, routes, token check, static files
  static/index.html    page shell (no inline JS besides reading the token meta tag)
  static/style.css
  static/app.js        polling loop, header, account dropdown, tiles, tables, module wiring
  static/chart.js      Lightweight Charts + indicator overlays + settings UI
  static/dom.js        DOM ladder + tape
  static/ticket.js     order ticket, cancel/close buttons, preflight modal, typed confirmation
tools/dashboard.py     thin launcher: `from dashboard.cli import main; main()`
tests/test_dashboard_indicators.py
tests/test_dashboard_state.py
tests/test_dashboard_orders.py
tests/test_dashboard_server.py
```

Owners during the build: indicators agent owns `indicators.py` + its test; state agent owns
`state.py`, `orders.py` + their tests; server agent owns `app.py`, `server.py`, `cli.py`,
`__main__.py`, `__init__.py`, `tools/dashboard.py` + server test; chart agent owns
`index.html`, `style.css`, `app.js`, `chart.js`; dom agent owns `dom.js`, `ticket.js`.
Do not edit files you do not own; if you need something from another module, code against the
interface below and leave a `TODO(integration)` comment.

## Threading model

- The asyncio event loop (main thread) runs both hubs and the refresh loops and is the only
  thing that calls `await hub.*`.
- `ThreadingHTTPServer` handles requests on worker threads. Handlers read `state` (it locks
  internally), call REST on `client` directly (httpx + TokenManager are thread-safe), and
  reach the event loop only via `session.call_async(coro)` which wraps
  `asyncio.run_coroutine_threadsafe(coro, session.loop).result(timeout=15)`.

## state.py

```python
class Tape:            # ring buffer of tick events for one contract, with a monotonic seq
    def __init__(self, capacity: int = 500)
    def add_trades(self, contract_id: str, trades: list[MarketTrade]) -> None
    def add_quote(self, contract_id: str, quote: Quote) -> None   # only when last/bid/ask changed
    def since(self, seq: int) -> tuple[list[dict], int]           # events with seq > given, and last_seq
# event dict: {"seq": int, "time": iso str, "kind": "trade"|"quote", "price": float|None,
#              "size": float|None, "side": "BUY"|"SELL"|None, "bid": float|None, "ask": float|None}

class DepthBook:       # price ladder for one contract
    def apply(self, entries: list[DepthEntry]) -> None
    def snapshot(self, levels: int = 20) -> dict
# DomType: 0 Unknown, 1 Ask, 2 Bid, 3 BestAsk, 4 BestBid, 5 Trade, 6 Reset, 7 Low, 8 High,
#          9 NewBestBid, 10 NewBestAsk, 11 Fill.
# Ask/BestAsk/NewBestAsk set asks[price] = volume (volume 0 or None removes the level);
# Bid/BestBid/NewBestBid set bids[price] likewise; Reset clears both sides; Low/High update
# session low/high. The live feed sends the resting size in volume and currentVolume as 0 on
# every level update (observed 2026-10-26), so currentVolume counts only when it is non-zero.
# The feed sends no removals when the touch moves (observed 2026-10-28; for simulated data it is
# one BestBid + one BestAsk per batch), so a level with size > 0 drops every level it crosses on
# the other side, and a Best* level also drops the levels beyond it on its own side.
# Trade/Fill: volume is the session total traded at that price and currentVolume this trade's
# size; the per-price replay sent right after subscribing has currentVolume 0 and does not touch
# last_price/last_size (when currentVolume is absent, volume is used).
# snapshot: {"bids": [{"price", "size"}...] best first, "asks": [...] best first,
#            "traded": [{"price", "volume"}...] highest price first (session volume at price from
#            Trade events, the prices nearest the last trade), "best_bid", "best_ask",
#            "last_price", "last_size", "low", "high", "version": int}

class DashboardState:
    def __init__(self, since: datetime)
    # accounts
    def set_accounts(self, accounts: list[Account]) -> None
    def select_account(self, account_id: int) -> None     # clears positions/orders/trades
    def selected_account(self) -> dict | None             # {"id","name","balance","simulated","can_trade"}
    def order_entry_allowed(self) -> bool                 # simulated is True and can_trade is True
    def apply_account_event(self, payload) -> None        # balance updates for any listed account
    # contracts / quotes (as the v1 tool, plus)
    def add_contract(self, contract: Contract) -> None
    def has_contract(self, contract_id) -> bool
    def contract(self, contract_id) -> dict | None
    def contract_ids(self) -> list[str]
    def position_contract_ids(self) -> list[str]
    def apply_quote(self, contract_id, quote: Quote) -> None     # merge + tape
    def apply_trades(self, contract_id, trades: list[MarketTrade]) -> None  # tape + depth last
    def apply_depth(self, contract_id, entries: list[DepthEntry]) -> None
    def depth(self, contract_id, levels=20) -> dict | None
    def tape_since(self, contract_id, seq: int) -> tuple[list[dict], int]
    def set_focus(self, contract_id) -> None ; def focus(self) -> str | None
    # positions / orders / fills (as v1)
    def apply_position(self, payload) -> str | None
    def apply_order(self, payload) -> None
    def apply_trade(self, payload) -> None
    # bars
    def set_bars(self, contract_id, bars: list[Bar]) -> int       # MERGES a window into the stored candles; returns new times added
    def add_history(self, contract_id, bars: list[Bar]) -> int    # same merge, for older pages loaded on demand
    def clear_bars(self) -> None                                  # timeframe changed: drop every candle, versions keep rising
    def candles(self, contract_id, after: int | None = None) -> list[dict] | None  # oldest first; after = UTC seconds, inclusive
    def candle_count(self, contract_id) -> int
    def oldest_time(self, contract_id) -> int | None
    def bars_version(self, contract_id) -> int
# MAX_STORED_CANDLES = 20_000 per contract: the newest are kept. An empty refresh keeps what is stored.
    # misc
    def set_connection(self, hub: str, ok: bool) ; def set_error(self, message: str | None)
    def snapshot(self) -> dict
```

`snapshot()` returns JSON-safe data:

```
{
  "generated_at", "since", "last_error",
  "connection": {"market": bool, "user": bool},
  "accounts": [{"id","name","balance","simulated","can_trade"}...],
  "account": {...selected or null},
  "order_entry_allowed": bool,
  "focus": contract_id | null,
  "contracts": [{"id","name","description","tick_size","tick_value"}...],
  "quotes": {contract_id: {"bid","ask","last","change","change_percent","open","high","low","volume","updated"}},
  "positions": [{"contract_id","name","side":"LONG"|"SHORT","contracts","size","average_price","last","unrealized","opened"}],
  "orders": [{"id","contract_id","status","type","side","size","filled","limit_price","stop_price","created"}],
  "trades": [{"id","time","contract_id","side","size","price","pnl","fees"}]  newest first, max 100,
  "totals": {"realized","costs","unrealized","total","open_contracts","fills","pending_prices":[...]},
  "bars_versions": {contract_id: int}
}
```

## indicators.py (pure; candles are oldest-first dicts with time/open/high/low/close/volume)

```python
def ema(values: list[float], period: int) -> list[float | None]        # None until `period` values; SMA seed
def atr(candles: list[dict], period: int) -> list[float | None]        # Wilder smoothing
def vwap(candles: list[dict], session_start_utc: tuple[int, int] = (22, 0)) -> list[float | None]
    # typical price (h+l+c)/3 weighted by volume, cumulative, reset at the first bar at/after
    # session_start_utc each day (22:00 UTC = CME Globex 6pm ET in daylight time). Bars with zero
    # volume carry the previous value forward.
def supertrend(candles: list[dict], period: int = 10, multiplier: float = 3.0) -> list[dict]
    # standard: hl2 +/- multiplier*ATR, band ratchet, flip rule. Returns one dict per candle:
    # {"time", "value": float|None, "direction": 1|-1|None, "upper": float|None, "lower": float|None}
def indicator_series(candles, *, ema_periods: list[int], vwap_on: bool,
                     supertrend: tuple[int, float] | None, session_start_utc=(22, 0)) -> dict
    # -> {"ema": {"9": [{"time","value"}...], ...}, "vwap": [{"time","value"}...],
    #     "supertrend": [{"time","value","direction"}...]}   (points with value None are omitted)
```

## orders.py

```python
@dataclass
class OrderTicket:          # exactly what the browser sent, already type-checked
    account_id: int; contract_id: str; side: str  # "buy"|"sell"
    order_type: str         # "market"|"limit"|"stop"|"stop_limit"
    size: int; limit_price: float | None = None; stop_price: float | None = None
    stop_ticks: int | None = None; target_ticks: int | None = None   # bracket distances, 1..1000, None = no leg

def parse_ticket(body: dict) -> OrderTicket                 # ValueError with a plain-English message
                                                            # (stop_ticks/target_ticks: blank/null = none)
def preflight(ticket, account: dict, contract: dict, custom_tag=None, last_price=None) -> dict
    # raises ValueError if: account missing/not simulated/not can_trade, contract missing/inactive,
    # size <= 0 or > 50, price missing/forbidden for the type, price not tick aligned, bracket
    # ticks outside 1..1000, brackets without a usable tick size, a leg at or below zero.
    # returns {"account": "...", "simulated": True, "contract": "...", "side": "BUY", "type": "LIMIT",
    #          "size": n, "limit_price", "stop_price", "tick_size", "stop_ticks", "target_ticks",
    #          "brackets": None | {"reference_price", "basis": "limit price"|"stop price"|"last price",
    #              "approx": bool, "stop_loss": leg|None, "take_profit": leg|None},
    #          "bracket_note" (only with brackets), "custom_tag": "dash-<hex>",
    #          "warning": "Topstep states that API orders are final and cannot be reviewed or reversed."}
    # leg = {"ticks", "gateway_ticks" (signed), "type": "STOP"|"LIMIT", "type_code": 4|1,
    #        "direction": "above"|"below", "price": float|None}. Leg prices are Decimal tick-grid
    # arithmetic from the limit price (limit, stop_limit), the stop price (stop) or, for market,
    # the last quote (snapped to the grid, approx; None when there is no last price yet).
def bracket_requests(ticket) -> (stopLossBracket | None, takeProfitBracket | None)
    # the gateway objects {"ticks", "type"} (Swagger PlaceOrderBracket): buy -> stop -n type 4,
    # target +n type 1; sell -> stop +n, target -n. The spec does not document the sign; this is
    # the convention other API users report. Needs the account in Auto OCO Brackets mode; in
    # Position Brackets mode the gateway rejects the order (errorCode 2).
def send_order(client, ticket, summary) -> int                # client.place_order(..., stop_loss_bracket,
                                                              # take_profit_bracket); PermissionError if
                                                              # the summary's legs differ from the ticket's
def cancel_order(client, account: dict, order_id: int) -> None
def close_position(client, account: dict, contract_id: str) -> None
def flatten_account(client, account, contract_ids: list[str], orders: list[dict]) -> report
    # close each listed position, then cancel each listed order ({"id", "contract_id"}); a close
    # that fails leaves that contract's orders working ("kept"); failures never stop the batch.
def cancel_orders(client, account, orders: list[dict]) -> report
    # report = {"ok", "action", "account", "account_id", "positions": [{"contract_id", "status",
    #           "error"?}], "orders": [{"order_id", "contract_id", "status", "error"?}],
    #           "sent", "failed", "not_found", "kept"}; status "sent"|"failed"|"not_found"
    #           (gateway errorCode 2: already closed/cancelled)|"kept"
def parse_id_list(body, name, kind) -> list                  # reviewed ids: list, <= 200, deduped
# every send_* re-checks account["simulated"] is True and account["can_trade"] is True and raises
# PermissionError otherwise. They never run without confirm == "yes" (checked in server.py too).
```

## app.py

```python
class Session:
    loop: asyncio.AbstractEventLoop; client: TopstepXClient; state: DashboardState
    market: MarketHub; user: UserHub; args
    replay: ReplayDriver | None            # --replay: replay client + hubs patched by dashboard/replay.py
    recorder: Recorder | None              # --record: raw observers on both hubs and the REST client
    async def start(self) -> None          # login, accounts, initial account + watch list, hubs, subscriptions
    async def stop(self) -> None
    async def select_account(self, account_id: int) -> None   # unsubscribe old user-hub subs, reseed, subscribe
    async def set_focus(self, contract_id: str) -> None       # subscribe depth+trades for focus, unsubscribe previous
    async def add_watch(self, text: str) -> str               # resolve symbol/ID via choose_active_contract, subscribe quotes, return id
    def call_async(self, coro, timeout=15.0)                  # from server threads
    async def refresh_bars_forever(self) -> None              # focus contract every args.refresh_focus (5s),
                                                              # others every args.refresh (60s); include_partial_bar=True
async def run(args) -> None                                   # build Session, start, serve, wait, stop
```

Initial account: `--account-id`, else the first simulated+can_trade account, else the first
active account. Initial focus: the first `--watch` contract.

`--replay PATH`: `Session.__init__` loads the recording, uses its `ReplayClient` and builds the real
`MarketHub`/`UserHub`; `ReplayDriver.attach()` replaces their `start`/`stop`/subscribe calls with
no-ops (hubs report connected) and `Session.start()` ends with `replay.begin()`, which feeds the
recorded messages to the hubs' event handlers. `--watch` becomes the recording's own list; the
`replay` feature is always loaded (its `order_entry_blocked` keeps order entry off even with
`--features none`). `--record PATH`: `replay.attach_recorder()` hooks `add_raw_observer` on both hubs
and `client._http.add_observer`; `Session.stop()` flushes it.

## server.py

```python
def make_server(session, host="127.0.0.1", port=8765) -> ThreadingHTTPServer
TOKEN = secrets.token_urlsafe(32)   # generated once per process in server.py
```

Routes (all JSON unless noted; errors are `{"error": "message"}` with 400/403/404/500):

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/` | | index.html with `__TOKEN__` replaced by TOKEN (text/html) |
| GET | `/static/<name>` | | file from `dashboard/static` (no `..`), correct content type, `Cache-Control: no-store` |
| GET | `/api/state` | | `state.snapshot()` |
| GET | `/api/bars` | `contract`, `ema=9,21`, `vwap=1`, `st=10,3`, optional `after` (UTC seconds) | `{"contract","version","bar_seconds","timeframe":{"unit","unit_number","bars"},"count","oldest","after","candles":[...],"volume":[{"time","value","color"}],"indicators":{...}}` 404 if no bars yet. Indicators are computed over every stored candle; with `after`, only candles/points at or after it are returned (the page polls incrementally) |
| GET | `/api/depth` | `contract`, `levels` | DepthBook snapshot or 404 |
| GET | `/api/tape` | `contract`, `after` (seq) | `{"events": [...], "last_seq": int}` |
| POST | `/api/account` | `{"account_id"}` | snapshot after switching |
| POST | `/api/focus` | `{"contract_id"}` | `{"focus": id}` |
| POST | `/api/watch` | `{"symbol"}` | `{"contract_id": id}` |
| POST | `/api/history` | `{"contract_id", "before"}` (UTC seconds) | `{"added", "oldest", "count", "complete"}`: fetches the page of bars ending at `before` in the current bar size and merges it (`Session.load_history`); `complete` = nothing older came back |
| POST | `/api/timeframe` | `{"unit", "unit_number", "bars"}` | `{"timeframe": {...}, "bar_seconds": n}`; 400 on bad values. Changes the bars fetched for every watched contract; the focus contract reloads before the reply |
| POST | `/api/orders/preflight` | ticket fields | `{"ok": true, "summary": {...}}` or 400 `{"ok": false, "error"}` |
| POST | `/api/orders` | ticket fields + `"confirm": "yes"` | `{"order_id": n}` |
| POST | `/api/orders/cancel` | `{"order_id", "confirm": "yes"}` | `{"ok": true}` |
| POST | `/api/positions/close` | `{"contract_id", "confirm": "yes"}` | `{"ok": true}` |
| POST | `/api/positions/flatten` | `{"account_id", "contract_ids": [...], "order_ids": [...], "confirm": "yes"}` | feature `trading_tools` (an order route): closes each listed position, then cancels each listed order; 200 with the `orders.flatten_account` report plus `"not_included": {"positions", "orders"}` (open now but not reviewed; nothing sent for them) |
| POST | `/api/orders/cancel_all` | `{"account_id", "order_ids": [...], "confirm": "yes"}` | feature `trading_tools` (an order route): cancels each listed order; same report shape |

Security (every POST): require header `X-Dashboard-Token` equal to TOKEN, else 403; require
`Host` to be `127.0.0.1[:port]` or `localhost[:port]`, else 403; if an `Origin` header is
present it must be `http://127.0.0.1[:port]` or `http://localhost[:port]`, else 403; reject
bodies over 64 KB; `/api/orders*` and `/api/positions/close` also return 403 unless
`state.order_entry_allowed()` and the body has `"confirm": "yes"` exactly. Never log request
bodies. `log_message` stays silent except for 5xx.

`cli.py` arguments: `--account-id`, `--watch` (nargs +, default `cli.DEFAULT_WATCH`: ES NQ GC CL MES MNQ MGC MCL), `--unit`, `--unit-number`,
`--bars` (default 1500), `--refresh` (60), `--refresh-focus` (5), `--since`, `--port` (8765),
`--no-open`, `--seconds`, `--record PATH`, `--replay PATH`, `--speed N` (above 0, default 1; needs `--replay`),
`--replay-loop` (needs `--replay`); `--record` with `--replay` is an error, and `--record` will not overwrite
a file. Same validation as the v1 tool. `--unit/--unit-number/--bars` only set
the starting timeframe: `Session.timeframe` is what `refresh_bars` uses, and `POST /api/timeframe`
(`Session.set_timeframe`, validated by `app.parse_timeframe`) changes it while running. On start print the URL, watched contracts,
and "Order entry: enabled for Practice Accounts only" / "disabled (no Practice Account)".

## Frontend contract

`index.html` contains `<meta name="dashboard-token" content="__TOKEN__">`, loads
`/static/style.css`, Lightweight Charts 5.2.1 from `/static/vendor/` (vendored, byte-identical to
the pinned CDN build; the `onerror` fallback loads the `cdn.jsdelivr.net` copy with its SRI hash),
then `/static/app.js` as an ES module. `app.js` imports `{ mountChart } from "./chart.js"`, `{ mountDom } from "./dom.js"`,
`{ mountTicket } from "./ticket.js"`.

Element ids `app.js` must provide (the shell): `account-select`, `badge-market`, `badge-user`,
`updated`, `error`, `tiles`, `chart-root`, `dom-root`, `tape-root`, `ticket-root`, `quotes`,
`positions`, `orders`, `trades`, `watch-form` (input `watch-input`), `since`.

Shared `api` object passed to every mount function:

```js
api.get(path)                      // fetch GET -> json, throws Error(message) on !ok
api.post(path, body)               // adds X-Dashboard-Token, JSON body, throws Error(message) on !ok
api.onState(fn)                    // fn(snapshot) after every successful /api/state poll (1 s)
api.focus()                        // current focus contract id (from the latest snapshot)
api.setFocus(contractId)           // POST /api/focus then refresh
api.contractName(id)               // display name lookup from the latest snapshot
api.fmt.price(v), api.fmt.money(v), api.fmt.clock(iso)
```

Module responsibilities:

- `chart.js` `mountChart(rootEl, api)`: contract select (watched contracts; changing it calls
  `api.setFocus`), indicator controls (EMA periods text like `9,21`, VWAP checkbox, Supertrend
  period + multiplier, saved in localStorage), polls `/api/bars` for the focus contract every
  5 s and whenever the bars version changes, draws candles + volume + overlays (EMA lines,
  VWAP line, Supertrend as a line colored by direction via per-point `color`), nudges the last
  candle with each quote between refreshes. Exposes nothing else.
- `chart.js` keeps the indicator settings behind an "Indicators" button (a panel under the
  toolbar, like TradingView's): EMA on/off + periods, VWAP on/off, Supertrend on/off + period +
  multiplier, and "Reset to defaults". Settings live in `localStorage` (`dashboard.chart`, with
  `emaOn`); the button shows how many indicator groups are on and the legend lists the lines.
- `chart.js` also has a bar-size control (a number plus a sec/min/hour/day unit, so 7-minute or
  2-hour bars are one edit), quick buttons (1m 5m 15m 1h 1D) and a history count (1-5000 bars).
  Changing any of them POSTs `/api/timeframe` and reloads; the server's current values come back in
  every `/api/bars` response (`timeframe`), so the fields follow the CLI flags and other tabs. A
  new `bar_seconds` shows the newest 200 bars. Out-of-range input is clamped on the page and
  rejected (400) by the server.
- History: the page keeps every candle it has seen for the contract and bar size. Polls send
  `after=<newest time>` and merge the tail (candles, volume and indicator points at or after it);
  a full payload is fetched when the contract, bar size or indicator parameters change, or after
  older bars arrive. Scrolling within 40 bars of the left edge POSTs `/api/history` with the
  oldest time on screen and reloads; `added: 0` marks that contract/bar size as complete. The
  server stores at most 20,000 candles per contract.
- `settings.js` `loadSettings()` / `mountSettings(rootEl, settings, onChange)`: the gear menu in
  the header. `app.js` exposes the live object as `api.settings` plus `api.onSettings(fn)` and
  `api.saveSettings()`. Keys: `confirm` ("typed" | "click" | "oneclick"), `domSize`, `showFills`,
  `showOrders`; stored under `localStorage["dashboard.settings"]`. The confirmation mode decides
  how `ticket.js` asks before sending: review + type `yes` (default), review + click Send, or
  one-click (no summary). The server contract does not change: every order/cancel/close still
  carries the token and `"confirm": "yes"`, and the Practice-only guard still applies.
- `ticket.js` also publishes `api.orders = { place(body), cancel(orderId), close(contractId),
  allowed() }`. `place` takes `{side, order_type, size, limit_price?, stop_price?}`, fills in the
  selected account and focus contract, and runs the same preflight + confirmation + send flow as
  the ticket buttons; it resolves after the request or rejects with the message already shown.
- `dom.js` trades through `api.orders`: clicking a bid-size cell buys, an ask-size cell sells, at
  that price (limit when it rests, stop when it is past the market); the outer columns show the
  trader's working orders (click to cancel) and the position badge at its average price; a "vol"
  column shows session volume at price from the depth snapshot's `traded`. The DOM default size
  lives in `settings.domSize`. The module itself never POSTs.
- `chart.js` plots today's fills for the focus contract as markers (`createSeriesMarkers`, arrows
  with side/size/price) and draws working orders (dashed) and the position average (solid) as
  price lines, from `/api/state`; both can be turned off in Settings.
- `dom.js` `mountDom(domRootEl, tapeRootEl, api)`: polls `/api/depth` every 250 ms and
  `/api/tape?after=seq` every 500 ms for the focus contract. Ladder: one row per price from
  best ask + N levels down to best bid - N levels at tick size, bid size column left, price
  center, ask size column right, last trade row highlighted, size bars proportional to the
  largest size shown. Clicking a price calls `window.dispatchEvent(new CustomEvent("dom-price",
  {detail: {price}}))` so the ticket can fill its price field; it never sends anything. Tape:
  newest on top, max 200 rows, trades colored by side, quote rows muted.
- `ticket.js` `mountTicket(rootEl, api)`: shows the selected account and whether order entry is
  allowed (if not: a one-line explanation and disabled inputs). Fields: side (BUY/SELL
  buttons), type (market/limit/stop/stop_limit), size, limit price, stop price (shown only when
  the type uses them). "Review order" calls `/api/orders/preflight` and opens a modal with the
  summary and the warning text; the modal has a text input with placeholder `type yes` and a
  "Send order" button that stays disabled until the input equals `yes`; sending POSTs
  `/api/orders` with `confirm: "yes"`. Also renders Cancel buttons into the open-orders table
  and Close buttons into the positions table (via `api.onState`, by decorating rows that have a
  `data-order-id` / `data-contract-id` attribute that `app.js` adds), each opening the same
  typed-yes modal. Success and errors are shown in a status line inside the ticket, never as
  `alert()`.

Layout: CSS grid, min 320 px columns, every table inside an `overflow-x: auto` wrapper (the v1
page clipped its quotes table), dark theme matching v1 (`#0f1412` background, `#26a69a` up,
`#ef5350` down). Mobile-width friendly is a bonus, not a requirement.

## Verification required before "done"

- `PY -m pytest -q` passes offline (new tests included).
- `PY -m dashboard --no-open --seconds 25 --watch ES NQ` runs against the real API and, in
  that window, `curl` of `/api/state`, `/api/bars?contract=...&ema=9,21&vwap=1&st=10,3`,
  `/api/depth?contract=...` and `/api/tape?contract=...&after=0` all return 200 with data.
- A POST without the token returns 403; a POST to `/api/orders` without `confirm: "yes"` returns 403.
- **No agent places, cancels or modifies a real order, or closes a position, even on a Practice
  Account.** The order path is verified with `httpx.MockTransport` in tests only. Live order
  testing is the user's to do, in the browser, on a Practice Account.

## Implementation notes (where the code refines the contract above)

- `orders.preflight` also puts `account_id`, `can_trade` and `contract_id` in the summary;
  `send_order` re-checks those (ticket account must equal the summary account, which must be
  simulated and able to trade) and raises `PermissionError` otherwise. `cancel_order` and
  `close_position` re-check the account dict the same way.
- `/api/orders/preflight` needs the token and a Practice Account but not `confirm`, since the
  typed `yes` comes after the preflight modal. `/api/orders`, `/api/orders/cancel` and
  `/api/positions/close` need `confirm: "yes"` exactly. Order routes also require the ticket's
  `account_id` to equal the selected account.
- `/api/watch` resolves symbols with `app.resolve_contract` (same matching rules as
  `_safety.choose_active_contract`, but it raises a `ValueError` listing the candidates instead
  of prompting on the terminal). `--watch` at startup still uses `choose_active_contract`.
- `state.select_account` raises `ValueError` for an id that is not in the account list (400).
- `DepthBook` keeps the 1000 levels nearest the best price per side and `snapshot(levels)`
  caps `levels` at 100. `Reset` clears both sides but keeps last/low/high.
- The server drains an unread request body before replying to an early 403/413, so the browser
  sees the status code instead of a connection reset on Windows.
- Error payloads: order routes answer `{"ok": false, "error": ...}` for 4xx; other routes
  `{"error": ...}`; `TopstepXError` is 502; a `call_async` timeout is 504; anything else 500
  with a generic message and the traceback on stderr only.
- Hardening after review: every request (GETs too) needs a loopback `Host`, so a page on another
  hostname rebound to 127.0.0.1 cannot read the token or account data; `/` is sent with
  `X-Frame-Options: DENY`; the handler has a 30 s socket timeout; `state.apply_position/order/trade`
  ignore payloads whose `accountId` is not the selected account (stale events during a switch) and
  `Session.select_account` unsubscribes the old account before switching; contracts keep their
  `active_contract` flag so preflight can refuse an expired one; the ticket sends back the
  `custom_tag` it showed in the review modal and `/api/orders` reuses it when it is well formed.

## Features (extension points)

Everything beyond the core page is a *feature*: a Python module in `dashboard/features/` plus,
optionally, a page module in `dashboard/static/features/`. Features are discovered at startup
(`--features all|none|a,b`), so adding one never means editing `server.py`, `app.py` or `app.js`.

**Server side** (`dashboard/features/__init__.py` has the full contract in its docstring):

- `routes(session) -> {"GET": {path: fn(query)}, "POST": {path: fn(body)}}`; paths start with
  `/api/<feature>/...`, may not shadow built-ins, and handlers return `(status, payload)`. POST
  routes get the token/Host/Origin checks automatically. A feature that sends orders lists those
  paths in `ORDER_ROUTES` so the server applies the Practice-only + `"confirm": "yes"` guard.
- `async start(session)` runs after the hubs are connected (register hub callbacks there, e.g.
  `session.market.on_quote(...)`), `async stop(session)` on shutdown.
- `snapshot(session) -> dict` is merged into `GET /api/state` as `snapshot["features"][NAME]`.
- `BODY_LIMITS = {path: bytes}` raises the 64 KB POST body cap for a feature's own routes (the
  server caps it at 64 MB); used by the Pine converter for long scripts.
- `order_entry_blocked(session) -> str | None`: a reason string makes every order route answer
  403 with it and `/api/state` report `order_entry_allowed: false` plus `order_entry_block`
  (used by user-set guards and replay mode). A failing hook never blocks.
- `GET /api/features` lists `{name, title, description, js, css, order}` for the page.
- Kit hooks for observers: `client._http.add_observer(fn)` sees every REST call
  (`{time, path, request, status, response, error, duration_ms}`; never the token);
  `hub.add_raw_observer(fn)` sees every hub message (`direction "in"|"out", name, args`).
- `replay` (`features/replay.py`, `dashboard/replay.py`, `docs/features/replay.md`): `--record PATH` writes every hub
  message and the page's REST lookups to NDJSON (`.gz` = gzip); `--replay PATH [--speed N] [--replay-loop]` runs the dashboard
  offline on that file with order entry disabled. `GET /api/replay/status`, `POST /api/replay/control
  {action: play|pause|speed|seek|step, value?}`; snapshot `features.replay` = status plus `recording {active, path, lines}`.
- Rules: a feature never places, modifies or cancels orders on its own initiative; never logs
  bodies or tokens; shows approximations from API data, never advice; ASCII on the terminal.

**Page side**: `app.js` fetches `/api/features`, injects each `css`, dynamic-imports each `js`
from `/static/features/` and calls `mount(api, info)`. `api` offers, besides `get/post/onState/
focus/contractName/fmt/settings/onSettings/saveSettings/orders`:

- `api.registerPanel({id, title, span, before})` -> container `<div>` in a new `<section>`.
- `api.addHeaderTool(node)` for a header button; `api.notify(text, kind)` toast; `api.showError`.
- `api.registerSettingsSection({title, render(container, settings, update)})`: a group in the
  Settings menu; keep feature keys namespaced (`settings.alerts = {...}`), `update(partial)` saves.
- `api.chart = {chart, candleSeries, volumeSeries, LC, candles(), barSeconds(), contract(),
  timeframe(), onBars(fn), reload()}` (Lightweight Charts objects; draw with price lines,
  markers or primitives). `api.dom = {onSnapshot(fn), size(), focus(), last()}`.
- `api.snapshot()` is the latest `/api/state`.
- Each feature keeps its CSS in its own file; `style.css` stays core-only.
- Pine Script converter (`pine` feature, `static/features/pine.js`, engine in `tools/pine2py/`):
  `POST /api/pine/convert {source, name?}` -> `{ok, name, title, code, warnings}` or 400 `{error, line,
  col}`; `POST /api/pine/save {source, name}` validates the name, converts, test-runs the generated plugin
  in-process and writes `dashboard/indicators_ext/<name>.py` (never over a file without the
  `# generated by tools.pine2py` first line; `TARGET_DIR` in `features/pine.py` is the folder);
  `GET /api/pine/examples`. Guide: `docs/features/pine2py.md`.
- Hotkeys (`trading_tools` feature, `static/features/hotkeys.js`): off by default; Settings >
  Hotkeys sets `settings.hotkeys = {enabled}` and shows the map. When on, and focus is not in an
  input/select/textarea and no confirmation window or Settings menu is open: Shift+F
  `api.orders.flatten()`, Shift+C `api.orders.cancelAll()`, Shift+B / Shift+S
  `api.orders.place({side, order_type: "market", size: api.dom.size()})` (plus the bracket
  settings). Auto-repeat is ignored; Escape stays `ticket.js`'s. Every key goes through
  `api.orders`, so the confirmation mode applies; a toast confirms each key and the ticket shows a
  "Hotkeys on" badge (`api.hotkeys = {active, enabled(), map}`). `ticket.js` adds the bracket
  inputs (`settings.bracket = {stopTicks, targetTicks}`, sent as `stop_ticks`/`target_ticks` with
  every ticket, DOM and hotkey order), the Flatten all / Cancel all buttons (shown when
  `/api/features` lists `trading_tools`), and `api.orders.flatten()/cancelAll()/bracket()`; every
  `api.orders` call resolves with the server reply or rejects (`"Nothing was sent."` when the
  trader backs out of the review).

**Indicator plugins** (`dashboard/indicator_plugins.py`, files in `dashboard/indicators_ext/`):
`NAME, TITLE, DESCRIPTION, OVERLAY, PARAMS` + `compute(candles, params) -> {"series": [...]}`
with `line | histogram | markers` series; `discover()`, `describe()`, `parse_params()`, `run()`
normalise everything for the page. `indicators_ext/sma.py` is the reference example.

- `indicators` (`features/indicators.py`, ORDER 20, read-only; page side lives in `chart.js`):
  `GET /api/indicators` (plugins found in `indicators_ext/`, hot-reloaded by file mtime, plus
  import errors), `GET /api/indicators/series?contract&name&params&after` (a plugin's series over
  every stored candle, tail-filtered like `/api/bars`), `GET /api/indicators/profile?contract
  [&source=bars&from&to]` (session volume at price from the depth feed, or a bars-based profile).
  Non-overlay plugins draw in their own Lightweight Charts pane; the profile is a canvas overlay.
- `alerts` (`features/alerts.py`, `static/features/alerts.js` + `.css`, ORDER 50): price alerts
  checked on every quote (previous vs new price, so gaps fire; once vs repeating with re-arm),
  fill events from the user hub, and the trader's own guard (`max_daily_loss` against
  `totals.total`, `max_contracts` against `totals.open_contracts`) exposed through
  `order_entry_blocked()`; the pause stands until the trader clears it. Events ring (200) polled
  with `GET /api/alerts/events?after`. Settings persist in `%LOCALAPPDATA%	opstepxlerts.json`
  (`TOPSTEPX_ALERTS_FILE` overrides). Page: Alerts panel, dotted alert lines on the chart via
  `api.chart`, Alt+click to add, toasts/sound/desktop notifications, a banner while paused.
- `recipes` (`features/recipes.py`, `static/features/recipes.js` + `.css`, ORDER 90, read-only):
  `GET /api/recipes` -> `{"source", "count", "groups": [{"id", "title", "recipes": [{"id", "title",
  "summary", "prompt", "touches"}]}]}` parsed from `docs/RECIPES.md` by the pure
  `parse_recipes(text)` (strict format in that file's top comment; `RecipeFormatError` names the
  line -> 500; a missing file -> 404; re-read when the file's mtime/size changes). Page: a
  "Recipes" header tool toggling the "Prompt recipes" panel (search, collapsible groups, Copy
  prompt with a textarea fallback). `python -m dashboard.features.recipes` checks the file.
- `journal` (`features/journal.py` + pure `dashboard/journal.py`, `static/features/journal.js` + `.css`,
  ORDER 60, read-only): fills paired FIFO per contract into round trips (pairing rule in the module
  docstring). `GET /api/journal?days=1..90[&tz=minutes]` -> `{since, account, trades_count, round_trips,
  unmatched_fills, open_lots, daily, stats, heatmap, note}` (`search_trades` cached 30 s per
  (account, days), merged with the state's fills); `GET /api/journal/excursion?id&days` -> MFE/MAE
  from 1-minute `get_bars` (one call, cached by id; RateLimitError -> 503); `POST /api/journal/export
  {days}` -> `{path, rows}` in `journal/`; snapshot `{today_round_trips, today_net}`. Page: a "Journal"
  header tool toggling the "Journal and session stats" panel. Guide: `docs/features/journal.md`.

Feature documentation for users goes in `docs/features/<name>.md`; the README links the folder.
