"""Feature: "Journal and session stats". Your fills paired into round trips, with daily totals,
stats, a weekday x hour grid, MFE/MAE per round trip and a CSV export.

Read-only: it only calls ``search_trades`` and ``get_bars`` (the latter one round trip at a time,
only when you press a row's MFE/MAE button, and cached) plus the fills the dashboard already holds.
It writes one file, the CSV you ask for, into the git-ignored ``journal/`` folder. Every number is an
approximation built from API data; none of it is advice or a signal. The maths is in
``dashboard/journal.py`` (the pairing rule is documented there).

Routes:
- ``GET /api/journal?days=N[&tz=minutes]``  N in 1..90; ``tz`` shifts the heatmap clock (default 0 = UTC)
- ``GET /api/journal/excursion?id=ID&days=N``  MFE/MAE for one round trip
- ``POST /api/journal/export`` ``{days}``  writes ``journal/dashboard_<account>_<since>_<today>.csv``
"""

from __future__ import annotations

import csv
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from topstepx import BarUnit, RateLimitError, TopstepXError

from .. import journal as jr
from ..server import RouteError

NAME = "journal"
TITLE = "Journal and session stats"
DESCRIPTION = "Round trips, daily totals, stats, a weekday x hour grid, MFE/MAE and a CSV export (approximations from API data)."
ORDER = 60
JS = "journal.js"
CSS = "journal.css"

MAX_DAYS = 90
CACHE_SECONDS = 30.0
EXCURSION_PAD = timedelta(minutes=2)
MAX_BARS = 20_000
JOURNAL_DIR = Path(__file__).resolve().parents[2] / "journal"   # tests monkeypatch this
NOTE = ("Approximations from the fills the API reports (FIFO pairing, UTC days). Not advice, not a signal, "
        "and not an official statement; check Topstep's own records.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    return time.monotonic()


def parse_days(value, default: int = 5) -> int:
    text = str(value).strip() if value is not None else ""
    if not text:
        return default
    if not text.isdigit() or not 1 <= int(text) <= MAX_DAYS:
        raise ValueError(f"days must be a whole number from 1 through {MAX_DAYS}")
    return int(text)


def parse_tz(value) -> int:
    text = str(value).strip() if value is not None else ""
    if not text:
        return 0
    try:
        minutes = int(text)
    except ValueError:
        raise ValueError("tz must be a whole number of minutes from UTC, for example -300") from None
    if not -840 <= minutes <= 840:
        raise ValueError("tz must be between -840 and 840 minutes")
    return minutes


class _Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fills: dict[tuple, tuple[float, list]] = {}      # (account, days) -> (stamp, raw fills)
        self.excursions: dict[tuple, dict] = {}                # (account, id) -> result


def _store(session) -> _Store:
    store = getattr(session, "_journal_store", None)
    if store is None:
        store = _Store()
        setattr(session, "_journal_store", store)
    return store


def _account_id(session) -> int:
    account = session.state.selected_account()
    if not account:
        raise RouteError(409, "No account is selected yet.")
    return account["id"]


def _api_call(fn, what: str):
    try:
        return fn()
    except RateLimitError:
        raise RouteError(503, f"The API is rate limiting {what} right now. Wait a few seconds and try again.") from None
    except TopstepXError as exc:
        raise RouteError(502, f"Could not load {what}: {type(exc).__name__}") from None


def load_fills(session, days: int) -> tuple[list, datetime]:
    """REST fills for the selected account for N days, cached 30 s, merged with today's fills in the state."""
    store = _store(session)
    account_id = _account_id(session)
    since = _now() - timedelta(days=days)
    key = (account_id, days)
    with store.lock:
        cached = store.fills.get(key)
    if cached and _monotonic() - cached[0] < CACHE_SECONDS:
        rest = cached[1]
    else:
        rest = _api_call(lambda: session.client.search_trades(account_id, since, _now()), "your fills")
        with store.lock:
            store.fills[key] = (_monotonic(), rest)
    seen = {getattr(t, "id", None) for t in rest}
    extra = [t for t in (session.state.snapshot().get("trades") or []) if t.get("id") not in seen]
    merged = list(rest) + [t for t in extra if (jr._utc(t.get("time")) or since) >= since]
    return merged, since


def build(session, days: int, tz: int = 0) -> dict:
    fills, since = load_fills(session, days)
    paired = jr.pair_fills(fills)
    trips = paired["round_trips"]
    return {
        "since": since.isoformat(),
        "account": session.state.selected_account(),
        "trades_count": len(fills),
        "round_trips": trips,
        "unmatched_fills": paired["unmatched"],
        "open_lots": paired["open_lots"],
        "daily": jr.daily_summary(trips),
        "stats": jr.stats(trips),
        "heatmap": jr.heatmap(trips, tz),
        "note": NOTE,
    }


def _account_out(account: dict | None) -> dict | None:
    return None if not account else {k: account.get(k) for k in ("id", "name", "simulated")}


def routes(session) -> dict:
    store = _store(session)

    def journal(query: dict) -> tuple[int, dict]:
        days = parse_days(query.get("days"))
        data = build(session, days, parse_tz(query.get("tz")))
        data["account"] = _account_out(data["account"])
        return 200, data

    def excursion(query: dict) -> tuple[int, dict]:
        trip_id = (query.get("id") or "").strip()
        if not trip_id:
            raise ValueError("id is required")
        days = parse_days(query.get("days"))
        account_id = _account_id(session)
        with store.lock:
            cached = store.excursions.get((account_id, trip_id))
        if cached:
            return 200, {**cached, "cached": True}
        fills, _since = load_fills(session, days)
        trip = next((t for t in jr.round_trips(fills) if t["id"] == trip_id), None)
        if trip is None:
            raise RouteError(404, "That round trip is not in the current window.")
        entry, exit_ = jr._utc(trip["entry_time"]), jr._utc(trip["exit_time"])
        start, end = entry - EXCURSION_PAD, exit_ + EXCURSION_PAD
        limit = int(min(MAX_BARS, (end - start).total_seconds() // 60 + 5))
        bars = _api_call(lambda: session.client.get_bars(trip["contract_id"], start, end, BarUnit.MINUTE, 1, limit),
                         "price bars")
        contract = session.state.contract(trip["contract_id"]) or {}
        result = jr.excursion(trip, bars, contract.get("tick_size"), contract.get("tick_value"))
        result["side"], result["entry_price"], result["size"] = trip["side"], trip["entry_price"], trip["size"]
        with store.lock:
            store.excursions[(account_id, trip_id)] = result
        return 200, {**result, "cached": False}

    def export(body: dict) -> tuple[int, dict]:
        days = parse_days((body or {}).get("days") if isinstance(body, dict) else None)
        fills, since = load_fills(session, days)
        rows = jr.to_csv_rows(jr.round_trips(fills))
        account_id = _account_id(session)
        today = _now()
        folder = Path(JOURNAL_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"dashboard_{account_id}_{since:%Y%m%d}_{today:%Y%m%d}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=jr.CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return 200, {"path": str(path.resolve()), "rows": len(rows)}

    return {"GET": {"/api/journal": journal, "/api/journal/excursion": excursion},
            "POST": {"/api/journal/export": export}}


def snapshot(session) -> dict:
    """Merged into /api/state: today's round trips from the fills the dashboard already holds (no REST call)."""
    trips = jr.round_trips(session.state.snapshot().get("trades") or [])
    today = _now().date().isoformat()
    todays = [t for t in trips if t["exit_time"][:10] == today]
    return {"today_round_trips": len(todays), "today_net": round(sum(t["net"] for t in todays), 2)}
