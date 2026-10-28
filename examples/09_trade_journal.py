"""Example 09 — journaling starter: your fills → a CSV trade journal.

Pulls the executed trades (fills) on one of your accounts for a date range,
writes them to a CSV you can open in Excel or Google Sheets, and prints a
per-day summary of what the API reports: fills, closed round turns, P&L,
fees, and commissions. Read-only — it never sends orders.

    python examples/09_trade_journal.py                        # last 7 days, choose an account
    python examples/09_trade_journal.py --days 30 --account-id 123
    python examples/09_trade_journal.py --out my_journal.csv

Days are grouped by UTC date. Extend it: add your own columns (setup tags,
notes, screenshot links), join it with example 08's bars, or append each day
to a spreadsheet or database. The journal/ folder is git-ignored because it
contains your account data.
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from topstepx import OrderSide, Trade, TopstepXClient, TopstepXError
from _safety import choose_account

COLUMNS = [
    "time_utc", "trade_id", "order_id", "contract_id", "side", "size", "price",
    "pnl", "fees", "commissions", "net", "voided",
]


# ── [arguments] Which account and dates ─────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export your fills to a CSV trade journal")
    parser.add_argument("--account-id", type=int, help="account to journal (default: choose)")
    parser.add_argument("--days", type=int, default=7, help="how many days back (default: 7)")
    parser.add_argument("--out", type=Path, help="output CSV (default: journal/trades_<account>_<dates>.csv)")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be greater than zero")
    return args


# ── [rows] One CSV row per fill ─────────────────────────────────────
def _money(value: float | None) -> float:
    return float(value) if value is not None else 0.0


def journal_rows(trades: list[Trade]) -> list[dict]:
    """Flatten fills into CSV rows, oldest first.

    ``pnl`` is blank for a half-turn (a fill that opened or added to a
    position); ``net`` is P&L minus fees and commissions, only on fills
    that closed something.
    """
    ordered = sorted(trades, key=lambda t: (t.creation_timestamp or datetime.min.replace(tzinfo=timezone.utc), t.id))
    rows = []
    for t in ordered:
        side = {OrderSide.BUY: "BUY", OrderSide.SELL: "SELL"}.get(t.side, str(t.side))
        closed = t.profit_and_loss is not None
        rows.append({
            "time_utc": t.creation_timestamp.isoformat() if t.creation_timestamp else "",
            "trade_id": t.id,
            "order_id": t.order_id if t.order_id is not None else "",
            "contract_id": t.contract_id or "",
            "side": side,
            "size": t.size if t.size is not None else "",
            "price": t.price if t.price is not None else "",
            "pnl": t.profit_and_loss if closed else "",
            "fees": _money(t.fees),
            "commissions": _money(t.commissions),
            "net": round(_money(t.profit_and_loss) - _money(t.fees) - _money(t.commissions), 2) if closed else "",
            "voided": bool(t.voided),
        })
    return rows


# ── [summary] Totals per UTC day ────────────────────────────────────
def daily_summary(trades: list[Trade]) -> list[dict]:
    """Per-UTC-day totals of what the API reports. Voided fills are skipped."""
    days: dict[str, dict] = defaultdict(lambda: {"fills": 0, "round_turns": 0, "pnl": 0.0, "fees": 0.0, "commissions": 0.0})
    for t in trades:
        if t.voided or t.creation_timestamp is None:
            continue
        day = days[t.creation_timestamp.astimezone(timezone.utc).date().isoformat()]
        day["fills"] += 1
        if t.profit_and_loss is not None:
            day["round_turns"] += 1
            day["pnl"] += t.profit_and_loss
        day["fees"] += _money(t.fees)
        day["commissions"] += _money(t.commissions)
    summary = []
    for date in sorted(days):
        d = days[date]
        net = d["pnl"] - d["fees"] - d["commissions"]
        summary.append({"date": date, **d, "net": net})
    return summary


# ── [fetch] Log in, pick the account, pull the fills ────────────────
def main() -> None:
    args = parse_args()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    with TopstepXClient() as client:
        account = choose_account(client, args.account_id)
        trades = client.search_trades(account.id, start=start, end=end)

    print(f"Account {account.id} ({account.name}) — {len(trades)} fill(s) from {start:%Y-%m-%d} to {end:%Y-%m-%d} UTC\n")
    if not trades:
        print("No fills in this window. Try a larger --days value.")
        return

    # ── [write-csv] Save the journal ────────────────────────────────
    out = args.out or Path("journal") / f"trades_{account.id}_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(journal_rows(trades))

    # ── [print-summary] Print the daily totals ──────────────────────
    summary = daily_summary(trades)
    print(f"{'DATE (UTC)':<12} {'FILLS':>6} {'CLOSED':>7} {'P&L':>11} {'FEES':>9} {'COMM':>9} {'NET':>11}")
    print("-" * 70)
    for d in summary:
        print(f"{d['date']:<12} {d['fills']:>6} {d['round_turns']:>7} {d['pnl']:>11.2f} "
              f"{d['fees']:>9.2f} {d['commissions']:>9.2f} {d['net']:>11.2f}")
    print("-" * 70)
    total = {k: sum(d[k] for d in summary) for k in ("fills", "round_turns", "pnl", "fees", "commissions", "net")}
    print(f"{'TOTAL':<12} {total['fills']:>6} {total['round_turns']:>7} {total['pnl']:>11.2f} "
          f"{total['fees']:>9.2f} {total['commissions']:>9.2f} {total['net']:>11.2f}")
    print(f"\nJournal written to {out.resolve()}")


# ── [errors] The safety net ─────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        print(f"Cannot continue: {exc}")
        sys.exit(1)
