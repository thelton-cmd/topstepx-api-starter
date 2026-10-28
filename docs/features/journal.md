# Journal and session stats

Click **Journal** in the dashboard header to see your fills turned into round trips (one entry
matched with one exit), with daily totals, simple counts and averages, a weekday-by-hour grid and
a CSV export. It only reads: it never sends, changes or cancels an order.

Everything here is an approximation built from the fills and bars the API returned. It is not
advice, not a signal, and not an official statement of your account. Topstep's own records decide.

## What you see

- **Days back** (1, 5, 20, 60) and a **heatmap time zone** (UTC, or your computer's clock).
- **Tiles:** round trips; how many closed with a gain, closed with a loss, or closed flat;
  gross, fees and net after fees; average net per round trip; average hold time; the largest gain
  and largest loss; the longest run of gains and of losses in a row.
- **Per day (UTC)** and **per contract** tables.
- **Heatmap:** net by weekday and hour of the day the round trip closed. Hover a cell for its count.
- **Round trips**, newest first. **MFE/MAE** on a row loads 1-minute bars for that window (one API
  call, then cached) and shows the best and worst price the position saw while open, in price
  points, and in money when the contract's tick size and tick value are known.
- **Export CSV** writes `journal/dashboard_<account>_<since>_<today>.csv` (the `journal/` folder is
  git-ignored) and shows the saved path.

## How fills are paired

Fills are ordered by time, voided fills are skipped, and each contract is paired on its own, first in,
first out. A fill on the opposite side of the open position closes the oldest open lot first.

- A scale-in followed by one exit gives one round trip per entry fill; one entry followed by a
  scale-out gives one round trip per exit fill. A flip closes the old side and opens the new one.
- Gross P&L is what the API reported on the closing fill, split by size. Fees are the closing
  fill's plus the opening fill's, split by size. Net is gross minus fees.
- A closing fill whose opening fill is before the chosen window cannot be paired; the page says how
  many. Positions still open are not counted until they close.
- A round trip belongs to the UTC day it closed.

## Limits and notes

- MFE/MAE use 1-minute bars, so the extremes can include a few seconds before the entry or after
  the exit. Without a tick value you get points only.
- Excursions are fetched only when you press the button, one at a time, because bars are limited
  to 50 requests per 30 seconds. If the API is rate limiting, wait and press again.
- Fills are cached for 30 seconds per account and window; **Refresh** reloads from that cache.

## For developers

`dashboard/journal.py` holds the pure maths (`round_trips`, `daily_summary`, `stats`, `heatmap`,
`excursion`, `to_csv_rows`); `dashboard/features/journal.py` the routes `GET /api/journal?days=N&tz=M`,
`GET /api/journal/excursion?id=&days=` and `POST /api/journal/export {days}`, and a
`features.journal` entry in `/api/state` (`today_round_trips`, `today_net`). Tests:
`tests/test_dashboard_journal.py`, `tests/test_feature_journal.py`.
