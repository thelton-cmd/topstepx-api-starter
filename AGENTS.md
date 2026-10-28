# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, and others) working in this repository. Humans
are welcome to read it too. `README.md` is the human-facing tour.

## What this repo is

A Python starter kit for the TopstepX API (the ProjectX Gateway): a small package (`topstepx/`)
that handles auth, REST, and real-time SignalR streams, plus numbered, runnable examples
(`examples/00`–`10`). The person you are helping is often new to APIs, Python, or the terminal,
so explain each step in plain language and do the typing for them wherever the rules below allow.

## Ground rules (follow these on every task)

1. **Run on the user's own computer.** Topstep's Terms of Use require all trading activity to
   originate from the trader's personal device; VPNs, VPSs, and remote servers are prohibited
   ([source](https://help.topstep.com/en/articles/11187768-topstepx-api-access)). If you are
   running in a cloud or remote sandbox rather than on the user's machine, stop before any step
   that uses their credentials and tell them to run you locally (Claude Code or Codex CLI/desktop).
2. **The API key never enters the chat.** Credentials live only in `.env` (git-ignored). Never ask
   the user to paste their key into the conversation, and never print, `cat`, echo, log, copy, or
   commit `.env` or the key. To find out whether `.env` is filled in correctly, run
   `examples/00_check_setup.py`, which reports status without revealing the key. If the user
   pastes a key into the chat anyway, tell them to regenerate it in TopstepX (Settings → API Access).
3. **No orders unless the user explicitly asks for that specific action.** Do not place, modify,
   or cancel orders, or close positions, on your own initiative, including "just to test." When
   the user does ask, use a **Practice Account** (the API marks it `simulated: true`) and mirror
   example 07's guards: explicit parameters, a preflight summary, and a typed confirmation. Let
   the user type the confirmation themselves.
4. **Trading decisions belong to the user.** Build what they ask for: data, charts, journals,
   alerts, and order plumbing. Do not invent entry or exit rules, signals, price targets, stop
   levels, position sizes, or timing. If they want to automate rules, implement exactly the rules
   they specify, as parameters they control, defaulting to the Practice Account. Don't describe any
   code as profitable or likely to make money.
5. **Keep user data out of git.** `.env`, `charts/`, and `journal/` are git-ignored; keep it that way.

## First-time setup

Do these steps in order from the repository root, explaining each one briefly. Call the virtual
environment's Python directly (as below) instead of "activating" it. That avoids PowerShell
execution-policy errors.

| | Windows (PowerShell) | macOS / Linux |
|---|---|---|
| venv Python (`PY` below) | `.venv\Scripts\python.exe` | `.venv/bin/python` |

1. **Python 3.10+.** Check with `python --version` (Windows: if that opens the Microsoft Store or
   fails, try `py -3 --version`; macOS/Linux: `python3 --version`). If Python is missing or older,
   ask the user before installing anything: Windows `winget install -e --id Python.Python.3.12`;
   macOS `brew install python@3.12` or the installer from python.org.
2. **Virtual environment and install.** Windows: `python -m venv .venv`; macOS/Linux:
   `python3 -m venv .venv`. Then `PY -m pip install -e ".[dev]"`.
3. **Offline sanity check.** `PY -m pytest -q` must pass. It needs no credentials or network.
4. **Create `.env`.** If `.env` does not exist, copy `.env.example` to `.env`. Never overwrite an
   existing `.env`. Then ask the user to open `.env` in a text editor, fill in
   `TOPSTEPX_USERNAME` and `TOPSTEPX_API_KEY`, save, and tell you when done. Offer to open the
   file for them: Windows `notepad .env`, macOS `open -e .env`, Linux `xdg-open .env`. If they
   don't have a key yet, point them to the
   [official API-access steps](https://help.topstep.com/en/articles/11187768-topstepx-api-access):
   link ProjectX, activate the separately billed API Access subscription, then TopstepX →
   Settings → API Access.
5. **Verify.** `PY examples/00_check_setup.py`. For each `[FAIL]`, follow its fix hint and rerun.
   It also lists the user's accounts and whether a Practice Account exists.
6. **First live data.** `PY examples/03_historical_bars.py ES` finishes on its own. Then stream
   with `PY examples/04_stream_quotes.py ES` for about 20 seconds (see "Long-running examples").
   Outside market hours a stream may connect and print nothing. That is normal.
7. **Start the dashboard.** Run `PY -m dashboard` **in the background** (it runs until Ctrl+C)
   and tell the user it is open at `http://127.0.0.1:8765` (the browser opens by itself; if not,
   give them the link). If the port is taken, rerun with `--port 8766`. Confirm it is serving with
   a GET of `http://127.0.0.1:8765/api/state` (never POST to it: that is the user's order ticket,
   see "Local dashboard"). Leave it running when you hand off.
8. **Hand off.** Summarize what works: the dashboard (chart with EMA/VWAP/Supertrend, DOM, tape,
   account dropdown, Practice-only order ticket) and examples 08 (charting), 09 (journaling),
   10 (risk alerts). Then ask what they would like to build.

## Local dashboard

`PY -m dashboard` (or `PY tools/dashboard.py`) serves a local web app on 127.0.0.1 only. The
package lives in `dashboard/`; `dashboard/ARCHITECTURE.md` is the module contract and route table.

- Flags: `--account-id ID`, `--watch ES NQ CL` (symbols or exact IDs; default ES NQ GC CL and the
  micros MES MNQ MGC MCL), `--unit/--unit-number/--bars`
  for the chart's starting timeframe (the page has a bar-size control for any number of
  seconds/minutes/hours/days and a history count; default 5-minute bars, 1500 of them), `--port`, `--no-open`, `--seconds N` (stop by itself; use this for unattended checks).
- The order ticket (and the click-to-trade DOM) sends orders **only** on accounts the API marks
  `simulated: true` and `canTrade: true`, and only after the trader confirms in the browser. By
  default they review a preflight summary and type `yes`; the page's Settings menu also offers
  "review and click Send" and "one-click" modes, which are the trader's choice alone. The server
  requires the page token and `"confirm": "yes"` in every mode. Funded/live accounts are read-only
  in the dashboard no matter what is sent.
- Agents never place, cancel or close anything through the dashboard, not even "to test". The
  order path is covered offline by `tests/test_dashboard_orders.py` and `tests/test_dashboard_server.py`
  with a mocked API; live order testing is the user's, in the browser, on a Practice Account.
- Every POST needs the per-process token the page carries; GETs of `/api/state`, `/api/bars`,
  `/api/depth`, `/api/tape` and every feature's GET route are safe to use for checks. The order
  routes are `/api/orders*`, `/api/positions/close`, `/api/positions/flatten` and
  `/api/orders/cancel_all`; never POST to them.
- Features (`dashboard/features/*.py`, auto-discovered; `--features none|a,b` selects): `apilog`
  (shows the API call behind each action), `trading_tools` (brackets in ticks, Flatten all,
  Cancel all, hotkeys off by default), `indicators` (plugins in `dashboard/indicators_ext/` plus
  volume profiles), `pine` (Pine Script to plugin converter, also `python -m tools.pine2py`),
  `replay` (`--record` / `--replay` of the hub stream; order entry disabled in replay), `alerts`
  (the user's price alerts and loss/contract guard), `journal` (round trips and statistics),
  `recipes` (`docs/RECIPES.md`). User guides live in `docs/features/`.
- To extend it: add a feature module (contract in `dashboard/features/__init__.py` and
  `dashboard/ARCHITECTURE.md`, "Features (extension points)") or an indicator plugin
  (`dashboard/indicator_plugins.py`); core logic goes in `dashboard/state.py`,
  `dashboard/indicators.py` or `dashboard/orders.py` with offline tests; routes in
  `dashboard/server.py`; page modules in `dashboard/static/`. Indicators are drawn with the
  user's parameters and are never described as signals, entries or exits.

## Running the examples

| Example | What it does | Finishes on its own? |
|---|---|---|
| `00_check_setup.py` | Diagnoses .env, credentials, network, login, accounts | yes |
| `01_authenticate.py` | Logs in and shows the (masked) session token | yes |
| `02_search_contracts.py [TEXT\|--all]` | Finds contract IDs | yes |
| `03_historical_bars.py [CONTRACT]` | Prints recent 5-minute bars | yes |
| `04_stream_quotes.py [CONTRACT]` | Streams live quotes | no, runs until Ctrl+C |
| `05_stream_depth.py [CONTRACT]` | Streams market depth + trade ticks | no, runs until Ctrl+C |
| `06_user_events.py [--account-id ID]` | Streams the user's own account/order/position/trade events (Practice Account) | no, runs until Ctrl+C |
| `07_place_order_demo.py --account-id … --contract-id … --side … --type … --size …` | **Sends a real order** to a Practice Account after a typed "yes". Only when the user asks. | waits for input |
| `08_chart_bars.py [CONTRACT] [--unit U --unit-number N --bars B --no-open]` | Writes `charts/*.html` (TradingView Lightweight Charts) and opens it | yes |
| `09_trade_journal.py [--account-id ID --days N]` | Writes `journal/*.csv` + prints a per-day summary | yes |
| `10_risk_monitor.py [--account-id ID --max-loss X --max-contracts N --seconds S]` | Read-only live P&L/position monitor with the user's alert limits | no, unless `--seconds` |
| `PY -m dashboard [--account-id ID --watch ES NQ --port P --seconds S]` | Local web dashboard: chart + indicators, DOM, tape, positions/orders/fills, Practice-only order ticket (see "Local dashboard") | no, unless `--seconds` |

- **Contracts:** pass a root symbol (`ES`, `NQ`, `CL`) or an exact ID (`CON.F.US.EP.Z26`).
  Futures expire, so get current IDs from example 02.
- **Non-interactive runs:** if a symbol or account choice is ambiguous and stdin is not a
  terminal, the examples exit with a numbered list instead of waiting. Rerun with an exact
  contract ID or `--account-id`.
- **Long-running examples (04, 05, 06, 10):** run them in the background or with your tool's
  timeout (about 20 seconds), then stop them. Example 10 accepts `--seconds 20`.
- Examples 06 and 07 only accept accounts the API marks simulated and tradable. Examples 08–10
  are read-only and work with any active account.

## How the package fits together

```
config.py → auth.py (TokenManager) → http.py (ApiHttp) → client.py (TopstepXClient)
                  ↘ realtime/base.py (BaseHub) → realtime/market.py (MarketHub), realtime/user.py (UserHub)
models.py, enums.py, exceptions.py are shared leaves
```

- **REST:** every gateway endpoint is a `POST` that returns a `{success, errorCode, errorMessage, ...}`
  envelope. `ApiHttp.post()` attaches the token, re-logs-in once on 401, backs off on 429, and
  raises `ApiError` on `success: false`. Each `TopstepXClient` method is a ~5-line wrapper:
  build the body → `post()` → parse into a dataclass. Add new endpoints the same way.
- **Auth:** `TokenManager.get_token()` logs in lazily with the API key and refreshes ~30 minutes
  before the ~24h token expires. REST and both hubs share it.
- **Real-time:** `MarketHub` (`on_quote`, `on_trade`, `on_depth`; `subscribe_*(contract_id)`)
  and `UserHub` (`on_account`, `on_order`, `on_position`, `on_user_trade`;
  `subscribe_*(account_id)`). Callbacks may be sync or async. Quotes are partial updates: each
  carries only the fields that changed (the rest are `None`), so keep the last value seen per
  field (examples 04 and 10). Reconnects re-subscribe automatically, and a callback exception is
  logged without killing the stream. `UserHub` callbacks receive the raw payload dict, which may
  be wrapped as `{"action": ..., "data": {...}}` (see `unwrap()` in example 10).
- **Models** are plain dataclasses with `from_api()`. Unknown fields are ignored, and the original
  payload is always kept in `.raw`.
- **Shared example helpers** live in `examples/_safety.py`: `choose_active_contract`,
  `choose_account` (read-only tools), `choose_practice_account` (anything that can send orders),
  and `require_tick_aligned`.

## API facts worth knowing

- REST base `https://api.topstepx.com/api`; hubs `wss://rtc.topstepx.com/hubs/market` and `/hubs/user`.
- Rate limits: `History/retrieveBars` 50 requests / 30 s; everything else 200 / 60 s. Max 20,000 bars per request.
- `live=False` (the default everywhere) selects simulated data, the universe a Practice Account trades in.
- Datetimes sent to the API must be timezone-aware (use UTC). Bars come back newest-first.
- Integer codes (see `topstepx/enums.py`): `OrderSide` 0 = buy (bid), 1 = sell (ask);
  `OrderType` 1 limit, 2 market, 3 stop-limit, 4 stop, 5 trailing stop, 6 join bid, 7 join ask;
  `PositionType` 1 long, 2 short; `BarUnit` 1 second … 6 month, 7 tick.
- A trade with `profit_and_loss is None` is a half-turn (it opened or added to a position).
- Source of truth: the [Swagger spec](https://api.topstepx.com/swagger/v1/swagger.json) and the
  [ProjectX Gateway docs](https://gateway.docs.projectx.com/).

## Building something new

- Put a new tool in its own file (for example `tools/my_dashboard.py` or a new numbered example)
  and import from `topstepx`. Start from the closest example: 08 charting, 09 journaling,
  10 risk alerts, 04/05 streaming.
- Keep pure logic (calculations, formatting, rules) in plain functions or classes that need no
  network, and add offline tests for them in `tests/`. `tests/test_recipes.py` shows how to load
  an example file in a test, and `tests/test_client.py` shows how to fake the API with
  `httpx.MockTransport`.
- Keep `print()` text ASCII. On Windows, piped output (including your own shell) is encoded as
  cp1252, and characters such as → or ✓ crash the script mid-run.
- Before you finish: run `PY -m pytest -q`, run the tool once against the API if `.env` is set
  up, and tell the user exactly how to run it.

## Compliance

This kit demonstrates API mechanics only. Keep code, comments, and docs free of trading advice,
signals, or entry/exit recommendations, and never claim that anything is profitable. Topstep's own
account rules are enforced by Topstep. Anything this kit calculates (for example the risk
monitor's P&L) is an approximation from API data.
