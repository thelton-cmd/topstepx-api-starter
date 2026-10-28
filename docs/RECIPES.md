<!--
Recipe file format (version 1). dashboard/features/recipes.py parses this file for the dashboard's
Recipes panel (GET /api/recipes) and tests/test_feature_recipes.py checks it, so keep it strict:

  * Everything above the first "## " heading is the intro. The parser skips it.
  * "## <Group title>" starts a group. Each group holds one or more recipes and nothing else.
  * "### <Recipe title>" starts a recipe. Its id is the title in lowercase with apostrophes
    dropped and every other run of characters outside a-z and 0-9 turned into one dash.
    Ids must be unique.
  * Then, in this order, separated by blank lines:
      1. the summary: exactly one line saying what you get;
      2. the prompt: one fenced code block that opens with three backticks followed by "text"
         and closes with three backticks on their own line. Write it to the agent, in the
         second person, plain ASCII, at most 120 words;
      3. one line that starts with "**What the agent will touch:**" naming the extension point.
  * No other text inside a recipe. Inline `code` is allowed in the summary and the touch line.

Content rules (tested): recipes build tools. They never suggest a trading rule, entry or exit,
price level, size or timing, and never imply that anything makes money. When a recipe automates
something, the numbers and rules are the user's and anything that can send orders defaults to
the Practice Account. Check the file with: python -m dashboard.features.recipes
-->

# Prompt recipes

You set up the kit with one prompt. These are the next ones. Each recipe is a prompt you copy and
paste into Claude Code (or Codex) running **on your own computer, in this repo folder**. It names
real files, flags and routes in this kit, so it should work on the first try.

**How to use one**

1. Open Claude Code in the `topstepx-api-starter` folder. The dashboard can keep running in its
   own terminal.
2. Copy a prompt from this page, or click **Recipes** in the dashboard header and then
   **Copy prompt**.
3. Paste it, read the agent's plan, and answer its questions. Change any detail to fit what you
   want: the contract, the folder, the numbers.

The agent reads `AGENTS.md` before it starts and follows its ground rules: your API key stays in
`.env` and never enters the chat, nothing is sent to your account unless you ask for that specific
action, anything that can send orders starts on your Practice Account, and the numbers and rules
always come from you. Each recipe ends with what the agent will touch. Most add a file at an
extension point (a feature module in `dashboard/features/`, an indicator plugin in
`dashboard/indicators_ext/`, or a script in `tools/` or `examples/`) instead of editing the core
dashboard.

**Groups:** [Charts and indicators](#charts-and-indicators) |
[Data and exports](#data-and-exports) |
[Alerts and risk guardrails](#alerts-and-risk-guardrails) |
[Journal and review](#journal-and-review) |
[Dashboard layout and workflow](#dashboard-layout-and-workflow) |
[API learning](#api-learning) |
[Building your own tools](#building-your-own-tools)

**Compliance note.** These recipes build tools: charts, alerts at numbers you choose, exports,
journals, layout and data plumbing. None of them is trading advice or a trading rule, and nothing
here suggests what, when or how much to trade. Indicators draw what you configure. P&L figures
are approximations built from API data, and Topstep's own account rules are enforced by Topstep.

Want to add your own recipe? Follow the format in the comment at the top of this file, run
`python -m dashboard.features.recipes` to check it, and reopen the Recipes panel: the dashboard
re-reads this file when it changes.

## Charts and indicators

### A second chart for another contract

A second chart under the main one that follows a contract you pick, while the main chart stays on your focus contract.

```text
Add a second chart pane to my dashboard as a feature module: dashboard/features/second_chart.py plus dashboard/static/features/second_chart.js and second_chart.css. It registers a panel with api.registerPanel that has a dropdown of my watched contracts (api.snapshot().contracts) and draws candles and volume for the one I pick with the Lightweight Charts library the page already loads (api.chart.LC), polling GET /api/bars?contract=<id> every 15 seconds. Contracts that are not in focus refresh their bars every --refresh seconds (60 by default). Remember my pick in localStorage and leave the main chart and the focus alone. Add an offline test that the feature is discovered and its files exist, then run pytest.
```

**What the agent will touch:** a feature module `dashboard/features/second_chart.py` plus `dashboard/static/features/second_chart.js` and `.css`; it reads `GET /api/bars`.

### Start on 2-hour bars

The dashboard opens on 2-hour bars every time, without typing flags.

```text
I want my dashboard to start on 2-hour bars. First show me the one-off command: python -m dashboard --unit hour --unit-number 2 --bars 1000. Then make it my default without editing dashboard/cli.py: create tools/my_dashboard.py that calls dashboard.cli.main() with --unit hour --unit-number 2 --bars 1000 followed by any arguments I pass, so my own --port, --watch or --unit still win. Tell me the exact command to run it with the virtual environment's Python on this computer. The bar-size control on the page must still let me switch to other sizes.
```

**What the agent will touch:** a launcher `tools/my_dashboard.py` that sets the `--unit`, `--unit-number` and `--bars` chart flags.

### Volume profile beside the chart

Horizontal bars showing how much volume traded at each price across the candles the dashboard has loaded.

```text
Add a volume profile to my dashboard as a feature module. Keep the math in a pure Python function with offline tests: given candles and a bucket size in ticks, spread each bar's volume evenly across its high-low range and return the volume per price bucket. Serve it at GET /api/volprofile/levels?contract=<id>&ticks=<n>, reading session.state.candles(contract_id) and the tick size from session.state.contract(contract_id). On the page, draw it as horizontal bars on the right side of the main chart (api.chart, with a canvas overlay or a series primitive), with an on/off button and the bucket size as a setting I choose (default 4 ticks). Label it as traded volume only.
```

**What the agent will touch:** a feature module `dashboard/features/volprofile.py` (route `GET /api/volprofile/levels`) plus `dashboard/static/features/volprofile.js`; it draws on `api.chart`.

### Convert my Pine Script into an indicator

Paste a TradingView Pine Script and get the same lines in the dashboard's Indicators menu.

```text
I will paste a TradingView Pine Script in my next message. Turn it into an indicator plugin for my dashboard: run python -m tools.pine2py --help first and use it if it handles my script, otherwise translate the script by hand. Put the result in dashboard/indicators_ext/<name>.py following the contract in dashboard/indicator_plugins.py and the dashboard/indicators_ext/sma.py example, keep my script's inputs as PARAMS with the same defaults, and add an offline test that checks a few values against a hand-computed example. Draw lines, histograms or markers only. If the script has strategy or alert calls, leave them out and tell me what you skipped.
```

**What the agent will touch:** an indicator plugin in `dashboard/indicators_ext/` (made with `python -m tools.pine2py` when it fits) plus a test in `tests/`.

### An RSI pane under the chart

An RSI line in its own pane under the price chart, drawn with the length you choose.

```text
Add an RSI indicator plugin to my dashboard: dashboard/indicators_ext/rsi.py, following dashboard/indicators_ext/sma.py and the contract in dashboard/indicator_plugins.py. Set OVERLAY = False so it draws in its own pane under the price chart. Parameters: length (default 14, Wilder smoothing) and source (close by default). Add optional horizontal guide lines at values I type, with none drawn by default. Add an offline test that compares the output with a small hand-computed example, then run pytest.
```

**What the agent will touch:** an indicator plugin `dashboard/indicators_ext/rsi.py` plus a test in `tests/`.

## Data and exports

### Export today's fills to CSV every evening

A CSV of the day's fills lands in `journal/` each evening without you running anything.

```text
Write tools/export_fills.py: it saves one UTC day of fills for an account to journal/fills_<account>_<date>.csv, reusing journal_rows() from examples/09_trade_journal.py (load it by path the way tests/test_recipes.py does). Flags: --account-id (needed when it runs unattended), --date YYYY-MM-DD (default today), --out-dir (default journal). It is read-only and prints ASCII only. Add an offline test with a fake client and run it once. Then show me how to schedule it for an evening time I choose on this computer (Windows Task Scheduler with schtasks, or cron on macOS and Linux), and let me run the scheduling command myself.
```

**What the agent will touch:** a new script `tools/export_fills.py` built on example 09, plus a test in `tests/`; the CSVs stay in the git-ignored `journal/` folder.

### Download bars to CSV

Weeks or months of bars for any contract in a CSV you can open in Excel or load in Python.

```text
Write me tools/download_bars.py, a script that saves historical bars to CSV. Arguments: CONTRACT (a symbol or an exact ID, resolved with choose_active_contract from examples/_safety.py), --unit, --unit-number, --days and --out. Page backwards with TopstepXClient.get_bars, at most 20,000 bars per request and under 50 History/retrieveBars requests per 30 seconds, drop duplicates, and write the rows oldest first with UTC times. Save into a data/ folder and add data/ to .gitignore. Add offline tests for the paging and de-duplication with a fake client, then run it once for ES 1-minute bars over the last 5 days.
```

**What the agent will touch:** a new script `tools/download_bars.py` plus tests in `tests/`; `.gitignore` gains `data/`.

### Record today's stream and replay it tonight

Capture the live market messages during the session and play them back through the dashboard later.

```text
Check python -m dashboard --help for a record or replay option. If it has one, show me how to record today's session to a file and replay it tonight. If not, add one: --record FILE saves every incoming hub message (hub.add_raw_observer) as JSON lines in a git-ignored recordings/ folder, and --replay FILE feeds them back into the dashboard state at a speed I choose (--speed, default 1) without connecting to the hubs. Order entry stays off during a replay. Add offline tests for the file format and the replay timing, then show me both commands.
```

**What the agent will touch:** the dashboard's `--record` / `--replay` options (`dashboard/cli.py` and `dashboard/app.py`, or a feature module); recordings go in a git-ignored folder.

## Alerts and risk guardrails

### Desktop alert when the price crosses my number

A desktop notification and a toast the moment the last price crosses a price you typed.

```text
Add price alerts to my dashboard. If dashboard/features/ already has an alerts module, extend it; otherwise create dashboard/features/price_alerts.py plus dashboard/static/features/price_alerts.js and price_alerts.css. In a small panel I pick a watched contract and type a price. The page watches api.onState snapshots (quotes[contract].last) and fires once when the last price crosses my number in either direction, using the browser Notification API (ask for permission when I click Enable) plus api.notify. Keep my alerts in localStorage, list armed and fired ones, and let me delete them. Alerts only notify: they never place, change or cancel orders.
```

**What the agent will touch:** a feature module `dashboard/features/price_alerts.py` plus `dashboard/static/features/price_alerts.js` and `.css` (or the existing alerts module); it reads `GET /api/state`.

### Lock the order ticket at my daily loss number

The ticket and the DOM order clicks grey out once the day's P&L reaches a loss number you set.

```text
Add a daily loss lock to my dashboard as a feature module. In a Settings section (api.registerSettingsSection, saved under settings.losslock) I type my own daily loss number; empty means off, and there is no default. While api.snapshot().totals.total is at or below minus that number, grey out #ticket-root with an overlay that says why, and wrap api.orders.place so DOM clicks are refused with the same message. Cancels and closes stay available, and the module never sends anything itself. Present it as my own page-level guardrail: Topstep's rules are enforced by Topstep, and the total is an approximation built from API data.
```

**What the agent will touch:** a feature module `dashboard/features/losslock.py` plus `dashboard/static/features/losslock.js` and `.css` (a Settings section that wraps `api.orders.place`).

### Risk meter with my own limits

Two meters on the dashboard: today's P&L against your loss number and open contracts against your contract number.

```text
Bring example 10's numbers into my dashboard as a read-only feature module. In a Settings section I type my own max loss and max contracts, with the same meaning as --max-loss and --max-contracts in examples/10_risk_monitor.py; both are empty by default. A panel shows two meters from api.snapshot().totals: total P&L against my max loss, and open_contracts against my max contracts. They turn amber at a percentage I choose and red at the limit, with an api.notify toast the first time each limit is crossed. Label the numbers as approximations from API data. The module never sends or cancels anything.
```

**What the agent will touch:** a feature module `dashboard/features/riskmeter.py` plus `dashboard/static/features/riskmeter.js` and `.css`; the limits mirror `examples/10_risk_monitor.py`.

## Journal and review

### Session statistics page from example 09

A panel with per-day fills, round turns, P&L, fees and commissions for the selected account.

```text
Add a session statistics panel to my dashboard built on example 09. Create a feature module with GET /api/sessionstats/days?days=N that calls session.client.search_trades for the selected account (session.state.selected_account()) and reuses journal_rows() and daily_summary() from examples/09_trade_journal.py, loaded by path the way tests/test_recipes.py does. The panel shows one row per UTC day (fills, round turns, P&L, fees, commissions) plus totals by contract, with a days selector (default 7). It is read-only and labelled as an approximation from API data. Add route tests with the dash fixture from tests/test_dashboard_server.py.
```

**What the agent will touch:** a feature module `dashboard/features/sessionstats.py` (route `GET /api/sessionstats/days`) plus `dashboard/static/features/sessionstats.js`; the math comes from `examples/09_trade_journal.py`.

### Notes and tags in my trade journal

Your own notes and tags columns in the journal CSV, kept when you rerun the export.

```text
Add notes and tags columns to the CSV that examples/09_trade_journal.py writes. When I rerun it with the same --out file, keep whatever I already typed in those columns, matched by trade id, and add new fills with empty notes. Also print the per-day summary grouped by contract. Keep the merge logic in a pure function with an offline test in tests/test_recipes.py, keep the output in the git-ignored journal/ folder, and do not change how P&L, fees or commissions are calculated.
```

**What the agent will touch:** `examples/09_trade_journal.py` (next to `journal_rows` and `daily_summary`) plus `tests/test_recipes.py`.

### Review chart for one day

An HTML chart of one day's bars with your fills marked on it, for looking back after the session.

```text
Write tools/review_day.py based on examples/08_chart_bars.py. Arguments: CONTRACT, --date YYYY-MM-DD (UTC, default today), --account-id, --unit and --unit-number (default 1 minute). It fetches that day's bars and my fills on that contract (TopstepXClient.search_trades) and writes charts/review_<contract>_<date>.html with candles, volume, and my fills as markers showing side, size and price. It is read-only and prints ASCII only. Reuse the chart code from example 08 where you can instead of copying it. Add an offline test for building the markers, then run it once for today.
```

**What the agent will touch:** a new script `tools/review_day.py` built on example 08, plus a test in `tests/`; the HTML lands in the git-ignored `charts/` folder.

## Dashboard layout and workflow

### Watch list presets

One click adds a named group of contracts, such as index futures or metals, to the dashboard.

```text
Add watch list presets to my dashboard as a feature module. A header dropdown (api.addHeaderTool) lists named presets that I define in a Settings section (api.registerSettingsSection, saved under settings.presets), for example Index = ES NQ MES MNQ and Metals = GC MGC. Picking one calls POST /api/watch through api.post for each symbol that is not watched yet, then focuses the first one with api.setFocus. There is no unwatch route, so also show me the matching startup command for a clean list, like python -m dashboard --watch ES NQ MES MNQ.
```

**What the agent will touch:** a feature module `dashboard/features/presets.py` plus `dashboard/static/features/presets.js` (a header tool and a Settings section that call `POST /api/watch`).

### Dark and light theme toggle

A header button that switches the whole dashboard, chart included, between dark and light.

```text
Add a dark and light theme toggle to my dashboard as a feature module with its own CSS file. A header button (api.addHeaderTool) switches a data-theme attribute on the html element. The light palette overrides the CSS variables that dashboard/static/style.css defines in :root (--bg, --panel, --panel-2, --line, --text, --muted and the rest), so style.css itself does not change. Remember my choice in localStorage, and recolor the chart with api.chart.chart.applyOptions (background, text and grid colors) so it follows the theme. Check that tables, the DOM and the ticket stay readable in both themes.
```

**What the agent will touch:** a feature module `dashboard/features/theme.py` plus `dashboard/static/features/theme.js` and `theme.css`.

### A hotkey map

Keyboard shortcuts for bar sizes, switching contracts and opening menus, with a key list on screen.

```text
Add keyboard shortcuts for chart and layout actions to my dashboard. If a feature already handles shortcuts (check dashboard/features/ and docs/features/), extend it without clashing with its keys; otherwise add a feature module. Defaults: 1 to 5 pick 1m, 5m, 15m, 1h and 1D bars (POST /api/timeframe through api.post, keeping the bar count from api.chart.timeframe()), [ and ] move the focus to the previous or next watched contract (api.setFocus), r opens the Recipes panel, and ? lists every key. Let me remap them in a Settings section, and ignore keys while I type in a field. These shortcuts never place, cancel or close orders.
```

**What the agent will touch:** the existing shortcut feature, or a new feature module `dashboard/features/view_keys.py` plus `dashboard/static/features/view_keys.js` (with a Settings section).

### Bracket default in ticks

Orders you send from the page can carry an attached stop and limit at tick distances you type.

```text
Add a bracket option for orders I send from my dashboard on my Practice Account. In a Settings section I type a stop distance and a limit distance in ticks. Both start empty, so no bracket is sent until I fill them in. When they are set, orders from the ticket and the DOM carry the bracket parameters that TopstepXClient.place_order already accepts. Send them through a feature POST route listed in ORDER_ROUTES so the server applies the Practice-only and "confirm": "yes" guards, and show the tick distances and resulting prices in the review summary. Test only with a mocked client; I will try it myself in the browser.
```

**What the agent will touch:** a feature module `dashboard/features/bracket.py` (a POST route listed in `ORDER_ROUTES`) plus `dashboard/static/features/bracket.js`; it passes brackets to `TopstepXClient.place_order`.

### A flatten button

One button that cancels your working orders and closes your positions on the Practice Account, after a review.

```text
If my dashboard already has a Flatten button, show me where it is, which route it calls and how it confirms, and stop there. Otherwise add one for my Practice Account: it cancels my working orders and closes my open positions on the selected account through a feature POST route (for example /api/flatten/all) listed in ORDER_ROUTES, so the server applies the Practice-only and "confirm": "yes" guards. Before anything is sent, show exactly which orders and positions it will touch and use my confirmation mode from Settings (typed yes by default). Report what worked and what failed. Test it offline with a mocked client only; I will try it myself in the browser.
```

**What the agent will touch:** nothing if `dashboard/features/trading_tools.py` already has Flatten; otherwise a feature module `dashboard/features/flatten.py` (a POST route listed in `ORDER_ROUTES`) plus `dashboard/static/features/flatten.js`.

## API learning

### Ask: the quote stream vs the trade stream

A plain-English explanation of what each market stream carries, with real output from your own terminal.

```text
Explain the difference between the quote stream and the trade stream, using examples/04_stream_quotes.py, examples/05_stream_depth.py and topstepx/realtime/market.py. What does each message carry, why do quotes arrive with only the changed fields filled in, and how should a tool keep the last value of each field? Then run example 04 and example 05 for ES for about 20 seconds each and point at real lines in the output. Outside market hours they may print nothing; tell me if that happens. Do not change any files.
```

**What the agent will touch:** nothing; it reads examples 04 and 05 and `topstepx/realtime/market.py`, then runs both examples.

### Ask: the exact API call behind the order ticket

A walk-through from the ticket button to the REST request body, with every field explained and nothing sent.

```text
Show me the exact API call behind my dashboard's order ticket, without sending anything. Trace it from dashboard/static/ticket.js through POST /api/orders/preflight and POST /api/orders in dashboard/server.py and the checks in dashboard/orders.py, to TopstepXClient.place_order in topstepx/client.py and its /Order/place request body. Write out an example body with placeholder values and explain each field, including the integer codes from topstepx/enums.py and where the Practice Account check happens. If my dashboard has the API calls panel (dashboard/features/apilog.py), tell me how to watch that call there when I place a Practice order myself. Do not place, cancel or close any order, and do not POST to my running dashboard.
```

**What the agent will touch:** nothing; it reads `dashboard/static/ticket.js`, `dashboard/server.py`, `dashboard/orders.py` and `topstepx/client.py`.

### Ask: how login and the session token work

How the kit logs in with your API key, when the token refreshes, and how REST and both hubs share it.

```text
Explain how this kit logs in and keeps my session alive, using topstepx/auth.py (TokenManager), topstepx/http.py and examples/01_authenticate.py: what is sent at login, how long the token lasts, when it refreshes, what happens on a 401 or a 429, and how the REST client and both hubs share one token. Run examples/01_authenticate.py once to show the masked token. Never print, cat or copy my .env file or my API key, and do not change any files.
```

**What the agent will touch:** nothing; it reads `topstepx/auth.py`, `topstepx/http.py` and `examples/01_authenticate.py`, then runs example 01.

## Building your own tools

### My own rule as a dry-run script

Your own rule in a small script of yours, checked against live market data in dry-run, sending nothing.

```text
Create tools/my_rule.py for a rule I will describe in my next message. Start from examples/10_risk_monitor.py for the streaming pattern and examples/04_stream_quotes.py for quotes. Implement the rule exactly as I state it inside one function, my_rule(bars, quote), with every number as a command-line flag I control and no defaults I did not give you. The script only prints what it would do; it has no order code at all. Add offline tests for my_rule() in tests/, loading the script by path the way tests/test_recipes.py does. Run it once for 30 seconds on my Practice Account's data and show me the output.
```

**What the agent will touch:** a new script `tools/my_rule.py` built on the `topstepx` package, plus tests in `tests/`.

### Add an API endpoint to the client

A new `TopstepXClient` method for an endpoint the kit does not wrap yet, with a test.

```text
I want to use an API endpoint the kit does not wrap yet; I will name it from the Swagger spec (https://api.topstepx.com/swagger/v1/swagger.json) in my next message. Add it to TopstepXClient in topstepx/client.py following the existing pattern (build the body, call self._http.post(), parse the result into a dataclass in topstepx/models.py with from_api() and raw), add a test with httpx.MockTransport like tests/test_client.py, and show me a three-line usage example. If the endpoint sends, changes or cancels orders, stop and ask me first.
```

**What the agent will touch:** `topstepx/client.py` and `topstepx/models.py`, plus `tests/test_client.py`.

### Start a new dashboard feature from scratch

Your own panel, wired into the dashboard the same way the built-in features are.

```text
Scaffold a new dashboard feature called notes: a panel where I keep plain-text notes for the day. Follow the contract in dashboard/features/__init__.py and the "Features (extension points)" section of dashboard/ARCHITECTURE.md, using dashboard/features/recipes.py and dashboard/static/features/recipes.js as the reference: a Python module with NAME, TITLE, JS and CSS, a GET and a POST route under /api/notes/, notes saved in a git-ignored notes/ folder, a page module that registers a panel, its own CSS file, route tests with the dash fixture, and docs/features/notes.md. Then start it with --port 8766 --no-open --seconds 30 and check the GET route.
```

**What the agent will touch:** a feature module `dashboard/features/notes.py` plus `dashboard/static/features/notes.js` and `.css`, tests in `tests/`, and `docs/features/notes.md`.
