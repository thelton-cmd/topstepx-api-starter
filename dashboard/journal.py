"""Journal maths: fills -> round trips -> daily totals, stats, a weekday x hour grid, MFE/MAE.

Pure functions, no network. Every number is an approximation built from the fills and bars the API
returned; none of it is a signal, a recommendation or a promise about anything.

Pairing rule (documented once, used everywhere)
-----------------------------------------------
* Fills are normalised (``Trade`` dataclasses and the dicts in ``state.snapshot()["trades"]`` both
  work), voided fills are dropped, and the rest are ordered by (time, id).
* Each contract is paired on its own, FIFO. A fill on the same side as the open lots (or with no
  open lots) opens a lot. A fill on the opposite side closes the oldest lots first.
* One round trip is produced for every (opening fill, closing fill) pair that overlaps. A scale-in
  followed by one exit gives one round trip per entry fill; one entry followed by a scale-out gives
  one round trip per exit fill. ``size`` is the contracts matched by that pair.
* If a closing fill is bigger than the open lots, the rest opens a lot on the other side (a flip).
* ``pnl`` is the gross P&L the API reported on the closing fill, split across the pairs that fill
  closed in proportion to size (the API reports nothing on opening fills). ``fees`` is the
  closing fill's fees plus the opening fill's fees, each split in proportion to the contracts
  used. ``net = pnl - fees``. If the API reported no P&L on a closing fill, ``pnl`` is 0.0 and
  ``pnl_known`` is False.
* A closing fill with nothing to close (the window starts mid-position) is counted in
  ``unmatched`` and produces no round trip. Lots still open at the end are reported as ``open_lots``.
* A round trip belongs to the UTC day it closed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CSV_COLUMNS = [
    "entry_time_utc", "exit_time_utc", "contract_id", "side", "size", "entry_price", "exit_price",
    "pnl", "fees", "net", "hold_seconds", "entry_trade_id", "exit_trade_id",
]
_EPS = 1e-9


# -- normalising -----------------------------------------------------------------------------------


def _utc(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        when = value
    else:
        try:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _side(value) -> str | None:
    if isinstance(value, str):
        text = value.strip().upper()
        if text in ("BUY", "BID", "0"):
            return "BUY"
        if text in ("SELL", "ASK", "1"):
            return "SELL"
        return None
    if value is None or isinstance(value, bool):
        return None
    return {0: "BUY", 1: "SELL"}.get(value)


def _num(value, default=0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def normalise_fill(trade) -> dict | None:
    """One fill as {id, time, contract_id, side, size, price, pnl, fees}; None if unusable or voided."""
    if isinstance(trade, dict):
        get = trade.get
        if get("voided"):
            return None
        raw = {"id": get("id"), "time": get("time"), "contract_id": get("contract_id"),
               "side": get("side"), "size": get("size"), "price": get("price"),
               "pnl": get("pnl"), "fees": _num(get("fees"))}
    else:
        if getattr(trade, "voided", None):
            return None
        raw = {"id": getattr(trade, "id", None), "time": getattr(trade, "creation_timestamp", None),
               "contract_id": getattr(trade, "contract_id", None), "side": getattr(trade, "side", None),
               "size": getattr(trade, "size", None), "price": getattr(trade, "price", None),
               "pnl": getattr(trade, "profit_and_loss", None),
               "fees": _num(getattr(trade, "fees", None)) + _num(getattr(trade, "commissions", None))}
    side = _side(raw["side"])
    when = _utc(raw["time"])
    size = _num(raw["size"], 0.0)
    if side is None or when is None or size <= 0 or raw["price"] is None or not raw["contract_id"]:
        return None
    return {"id": raw["id"], "time": when, "contract_id": str(raw["contract_id"]), "side": side,
            "size": size, "price": float(raw["price"]),
            "pnl": None if raw["pnl"] is None else float(raw["pnl"]), "fees": raw["fees"]}


def _clean(value: float):
    return int(round(value)) if abs(value - round(value)) < _EPS else value


def _id_key(value) -> tuple:
    return (0, value, "") if isinstance(value, int) else (1, 0, str(value))


# -- pairing ---------------------------------------------------------------------------------------


def pair_fills(trades) -> dict:
    """Pair fills into round trips. Returns {round_trips, unmatched, open_lots} (see module docstring)."""
    fills = [f for f in (normalise_fill(t) for t in trades or []) if f]
    fills.sort(key=lambda f: (f["time"], _id_key(f["id"])))
    lots: dict[str, list[dict]] = defaultdict(list)    # contract -> open lots, oldest first
    trips: list[dict] = []
    unmatched = 0
    for fill in fills:
        book = lots[fill["contract_id"]]
        remaining = fill["size"]
        if book and book[0]["side"] != fill["side"]:
            while remaining > _EPS and book:
                lot = book[0]
                take = min(remaining, lot["left"])
                share_exit = take / fill["size"]
                share_entry = take / lot["size"]
                pnl = (fill["pnl"] or 0.0) * share_exit
                fees = fill["fees"] * share_exit + lot["fees"] * share_entry
                trips.append({
                    "id": f"{lot['id']}-{fill['id']}",
                    "contract_id": fill["contract_id"],
                    "side": "long" if lot["side"] == "BUY" else "short",
                    "size": _clean(take),
                    "entry_time": lot["time"].isoformat(),
                    "exit_time": fill["time"].isoformat(),
                    "entry_price": lot["price"],
                    "exit_price": fill["price"],
                    "pnl": round(pnl, 6),
                    "fees": round(fees, 6),
                    "net": round(pnl - fees, 6),
                    "hold_seconds": max(0.0, (fill["time"] - lot["time"]).total_seconds()),
                    "fill_ids": [lot["id"], fill["id"]],
                    "pnl_known": fill["pnl"] is not None,
                })
                lot["left"] -= take
                remaining -= take
                if lot["left"] <= _EPS:
                    book.pop(0)
        elif not book and fill["pnl"] is not None:
            unmatched += 1       # it closed something that began before the window
            continue
        if remaining > _EPS:
            book.append({"id": fill["id"], "side": fill["side"], "time": fill["time"], "price": fill["price"],
                         "size": remaining, "left": remaining, "fees": fill["fees"] * remaining / fill["size"]})
    open_lots = [{"contract_id": cid, "side": "long" if lot["side"] == "BUY" else "short",
                  "size": _clean(lot["left"]), "entry_time": lot["time"].isoformat(), "entry_price": lot["price"]}
                 for cid, book in lots.items() for lot in book]
    trips.sort(key=lambda t: (t["exit_time"], t["entry_time"], str(t["id"])))
    return {"round_trips": trips, "unmatched": unmatched, "open_lots": open_lots}


def round_trips(trades) -> list[dict]:
    """Round trips only (see ``pair_fills`` for the unmatched and still-open counts)."""
    return pair_fills(trades)["round_trips"]


# -- summaries -------------------------------------------------------------------------------------


def _kind(net: float) -> str:
    return "gain" if net > _EPS else "loss" if net < -_EPS else "flat"


_KEY = {"gain": "gains", "loss": "losses", "flat": "flat"}


def daily_summary(trips: list[dict]) -> list[dict]:
    """Per UTC day the round trips closed: counts, gross, fees, net. Oldest day first."""
    days: dict[str, dict] = {}
    for t in trips:
        date = _utc(t["exit_time"]).date().isoformat()
        d = days.setdefault(date, {"date": date, "round_trips": 0, "gains": 0, "losses": 0, "flat": 0,
                                   "gross": 0.0, "fees": 0.0, "net": 0.0})
        d["round_trips"] += 1
        d[_KEY[_kind(t["net"])]] += 1
        d["gross"] += t["pnl"]
        d["fees"] += t["fees"]
        d["net"] += t["net"]
    return [{**days[k], "gross": round(days[k]["gross"], 2), "fees": round(days[k]["fees"], 2),
             "net": round(days[k]["net"], 2)} for k in sorted(days)]


def _longest_run(kinds: list[str], wanted: str) -> int:
    best = run = 0
    for k in kinds:
        run = run + 1 if k == wanted else 0
        best = max(best, run)
    return best


def stats(trips: list[dict]) -> dict:
    """Counts, sums, averages, extremes and a per-contract breakdown. Approximations from API data."""
    ordered = sorted(trips, key=lambda t: (t["exit_time"], str(t["id"])))
    kinds = [_kind(t["net"]) for t in ordered]
    count = len(ordered)
    nets = [t["net"] for t in ordered]
    per: dict[str, dict] = {}
    for t in ordered:
        c = per.setdefault(t["contract_id"], {"contract_id": t["contract_id"], "round_trips": 0, "gains": 0,
                                              "losses": 0, "flat": 0, "gross": 0.0, "fees": 0.0, "net": 0.0})
        c["round_trips"] += 1
        c[_KEY[_kind(t["net"])]] += 1
        c["gross"] += t["pnl"]
        c["fees"] += t["fees"]
        c["net"] += t["net"]
    contracts = [{**c, "gross": round(c["gross"], 2), "fees": round(c["fees"], 2), "net": round(c["net"], 2)}
                 for c in sorted(per.values(), key=lambda c: c["contract_id"])]
    net = sum(nets)
    return {
        "round_trips": count,
        "closed_with_gain": kinds.count("gain"),
        "closed_with_loss": kinds.count("loss"),
        "flat": kinds.count("flat"),
        "gross": round(sum(t["pnl"] for t in ordered), 2),
        "fees": round(sum(t["fees"] for t in ordered), 2),
        "net": round(net, 2),
        "avg_net": round(net / count, 2) if count else None,
        "avg_hold_seconds": round(sum(t["hold_seconds"] for t in ordered) / count, 1) if count else None,
        "largest_gain": round(max(nets), 2) if nets and max(nets) > _EPS else None,
        "largest_loss": round(min(nets), 2) if nets and min(nets) < -_EPS else None,
        "longest_gain_run": _longest_run(kinds, "gain"),
        "longest_loss_run": _longest_run(kinds, "loss"),
        "contracts": contracts,
    }


def heatmap(trips: list[dict], tz_offset_minutes: int = 0) -> dict:
    """Net and count by weekday (0 = Monday) x hour of the day the round trip closed.

    ``tz_offset_minutes`` shifts UTC to a local clock (for example -300 for UTC-5); the default is UTC.
    """
    offset = timedelta(minutes=int(tz_offset_minutes))
    net = [[0.0] * 24 for _ in range(7)]
    count = [[0] * 24 for _ in range(7)]
    for t in trips:
        when = _utc(t["exit_time"]) + offset
        net[when.weekday()][when.hour] += t["net"]
        count[when.weekday()][when.hour] += 1
    return {"offset_minutes": int(tz_offset_minutes), "weekdays": list(WEEKDAYS),
            "net": [[round(v, 2) for v in row] for row in net], "count": count}


# -- excursion -------------------------------------------------------------------------------------


def _bar_fields(bar):
    if isinstance(bar, dict):
        return _utc(bar.get("time") or bar.get("t")), bar.get("high", bar.get("h")), bar.get("low", bar.get("l"))
    return _utc(getattr(bar, "time", None)), getattr(bar, "high", None), getattr(bar, "low", None)


def excursion(round_trip: dict, bars, tick_size: float | None = None, tick_value: float | None = None,
              bar_seconds: int = 60) -> dict:
    """Best and worst price the position saw while it was open, from the bars that overlap it.

    MFE (max favourable excursion) and MAE (max adverse excursion) are in price points, never
    negative. Money is points / tick_size * tick_value * size and only when both tick fields are
    known; otherwise ``money_available`` is False and only points are reported. A bar covers the
    round trip when it overlaps [entry, exit]; with 1-minute bars the extremes can include moves a
    few seconds before the entry or after the exit, so treat these as approximations.
    """
    entry, exit_ = _utc(round_trip["entry_time"]), _utc(round_trip["exit_time"])
    highs, lows = [], []
    for bar in bars or []:
        when, high, low = _bar_fields(bar)
        if when is None or high is None or low is None:
            continue
        if when + timedelta(seconds=bar_seconds) > entry and when <= exit_:
            highs.append(float(high))
            lows.append(float(low))
    out = {"id": round_trip.get("id"), "bars_used": len(highs), "bar_seconds": bar_seconds,
           "mfe_points": None, "mae_points": None, "mfe_money": None, "mae_money": None,
           "money_available": False, "note": ""}
    if not highs:
        out["note"] = "No bars overlap this round trip, so no excursion could be calculated."
        return out
    price = float(round_trip["entry_price"])
    if round_trip["side"] == "long":
        mfe, mae = max(highs) - price, price - min(lows)
    else:
        mfe, mae = price - min(lows), max(highs) - price
    mfe, mae = max(0.0, mfe), max(0.0, mae)
    out["mfe_points"], out["mae_points"] = round(mfe, 6), round(mae, 6)
    if tick_size and tick_value and tick_size > 0:
        size = float(round_trip["size"])
        out["mfe_money"] = round(mfe / tick_size * tick_value * size, 2)
        out["mae_money"] = round(mae / tick_size * tick_value * size, 2)
        out["money_available"] = True
        out["note"] = "Approximation from 1-minute bars; money uses the contract's tick size and tick value."
    else:
        out["note"] = ("Approximation from 1-minute bars. The contract's tick value is not known here, "
                       "so only price points are shown.")
    return out


# -- CSV -------------------------------------------------------------------------------------------


def to_csv_rows(trips: list[dict]) -> list[dict]:
    """One CSV row per round trip, oldest exit first, in example 09's column style (UTC, ISO times)."""
    rows = []
    for t in sorted(trips, key=lambda t: (t["exit_time"], str(t["id"]))):
        entry_id, exit_id = (list(t.get("fill_ids") or []) + ["", ""])[:2]
        rows.append({
            "entry_time_utc": t["entry_time"], "exit_time_utc": t["exit_time"],
            "contract_id": t["contract_id"], "side": t["side"].upper(), "size": t["size"],
            "entry_price": t["entry_price"], "exit_price": t["exit_price"],
            "pnl": round(t["pnl"], 2), "fees": round(t["fees"], 2), "net": round(t["net"], 2),
            "hold_seconds": round(t["hold_seconds"], 1), "entry_trade_id": entry_id, "exit_trade_id": exit_id,
        })
    return rows
