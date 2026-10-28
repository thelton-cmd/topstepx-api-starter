# Dashboard features

Each optional dashboard feature has a short guide here, written for the trader using it. The
feature modules themselves live in `dashboard/features/` (server side) and
`dashboard/static/features/` (page side); `dashboard/ARCHITECTURE.md`, section "Features
(extension points)", explains how they plug in.

| Guide | Feature |
|---|---|
| [apilog.md](apilog.md) | The API call behind every click, with copy-as-curl / copy-as-Python |
| [trading.md](trading.md) | Brackets in ticks, Flatten all, Cancel all, hotkeys |
| [indicators.md](indicators.md) | Indicator plugins and volume profiles |
| [pine2py.md](pine2py.md) | Pine Script to Python indicator conversion |
| [replay.md](replay.md) | Record the live stream and replay it at speed |
| [alerts.md](alerts.md) | Price alerts, fill notifications, your own guardrails |
| [journal.md](journal.md) | Round trips, session statistics, MFE/MAE, CSV export |
| [recipes.md](recipes.md) | Prompt recipes for Claude Code (the list is `docs/RECIPES.md`) |

Start the dashboard without any of them with `python -m dashboard --features none`, or with a
chosen few: `--features apilog,alerts`.

Everything in these guides describes product mechanics. Nothing here is trading advice, and
nothing the dashboard calculates is more than an approximation built from API data.
