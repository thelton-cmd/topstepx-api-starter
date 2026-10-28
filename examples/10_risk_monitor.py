"""Example 10 — risk-tool starter: watch positions and P&L against YOUR limits.

Streams your open positions, fills, and live prices; keeps a running count of
open contracts and P&L (realized since --since, plus unrealized at the last
price); and prints an ALERT when a limit you set is crossed.

==============================================================================
 READ-ONLY by design: this script never places, changes, or cancels orders.
 The limits are yours to choose — it only reports when they are crossed.
 Nothing here is trading advice, a trading signal, or a recommendation.
==============================================================================

    python examples/10_risk_monitor.py --max-loss 500 --max-contracts 3
    python examples/10_risk_monitor.py --account-id 123 --max-loss 250 --seconds 60

Topstep's own account rules are enforced by Topstep, not by this script — its
numbers are an approximation built from API data. Extend it: send alerts to
Discord, Slack, or email; log every update to a file; or add rules of your
own (per-contract size, time-of-day windows). Press Ctrl+C to stop.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from topstepx import (
    Contract,
    MarketHub,
    PositionType,
    Quote,
    TopstepXClient,
    TopstepXError,
    UserHub,
)
from _safety import choose_account


# ── [arguments] Your limits ─────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only position and P&L monitor with your own alert limits")
    parser.add_argument("--account-id", type=int, help="account to watch (default: choose)")
    parser.add_argument("--max-loss", type=float, help="alert when realized + unrealized P&L falls to -X or below")
    parser.add_argument("--max-contracts", type=int, help="alert when total open contracts exceed N")
    parser.add_argument("--since", help="count realized P&L from this UTC date, YYYY-MM-DD (default: today)")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between status lines (default: 5)")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds (default: run until Ctrl+C)")
    args = parser.parse_args()
    if args.max_loss is not None and args.max_loss <= 0:
        parser.error("--max-loss must be a positive amount, e.g. 500")
    if args.max_contracts is not None and args.max_contracts < 0:
        parser.error("--max-contracts cannot be negative")
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")
    return args


# ── [book] The running tally — plain Python, no network ─────────────
def unwrap(payload: object) -> dict:
    """User-hub events may arrive as {"action": ..., "data": {...}} or as the data itself."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


@dataclass
class OpenPosition:
    contract_id: str
    size: int  # positive = long, negative = short
    average_price: float


class RiskBook:
    """Open positions, realized P&L, and last prices → totals and limit checks."""

    def __init__(self, contract_lookup: Callable[[str], Contract], since: datetime):
        self._lookup = contract_lookup
        self._since = since
        self.positions: dict[str, OpenPosition] = {}
        self.last_price: dict[str, float] = {}
        self.realized = 0.0
        self.costs = 0.0  # fees + commissions
        self._seen_trades: set[int] = set()
        self._breached: set[str] = set()

    def apply_position(self, payload: object) -> str | None:
        """Update from a position snapshot/event. Returns its contract ID."""
        p = unwrap(payload)
        contract_id, size = p.get("contractId"), p.get("size") or 0
        if not contract_id:
            return None
        if size == 0:
            self.positions.pop(contract_id, None)
            return contract_id
        signed = -abs(size) if p.get("type") == PositionType.SHORT else abs(size)
        self.positions[contract_id] = OpenPosition(contract_id, signed, float(p.get("averagePrice") or 0.0))
        return contract_id

    def apply_trade(self, payload: object) -> None:
        """Add a fill's realized P&L and costs once (fills can arrive by REST and stream)."""
        t = unwrap(payload)
        trade_id = t.get("id")
        if trade_id is None or trade_id in self._seen_trades or t.get("voided"):
            return
        stamp = t.get("creationTimestamp")
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp else None
        except ValueError:
            when = None  # unreadable timestamp: count it rather than silently drop it
        if when is not None and when < self._since:
            return
        self._seen_trades.add(trade_id)
        self.realized += t.get("profitAndLoss") or 0.0
        self.costs += (t.get("fees") or 0.0) + (t.get("commissions") or 0.0)

    def apply_quote(self, contract_id: str, last_price: float | None) -> None:
        if last_price is not None:
            self.last_price[contract_id] = last_price

    @property
    def open_contracts(self) -> int:
        return sum(abs(p.size) for p in self.positions.values())

    def unrealized(self) -> tuple[float, list[str]]:
        """Return (unrealized P&L, contracts still waiting for a price)."""
        total, pending = 0.0, []
        for p in self.positions.values():
            price = self.last_price.get(p.contract_id)
            contract = self._lookup(p.contract_id)
            if price is None or not contract.tick_size or contract.tick_value is None:
                pending.append(p.contract_id)
                continue
            ticks = (price - p.average_price) / contract.tick_size
            total += ticks * contract.tick_value * p.size
        return total, pending

    def total_pnl(self) -> float:
        return self.realized - self.costs + self.unrealized()[0]

    def check(self, max_loss: float | None, max_contracts: int | None) -> list[str]:
        """Return messages for limits that were just crossed — or just cleared."""
        rules = {}
        if max_loss is not None:
            rules["loss"] = (self.total_pnl() <= -max_loss, f"P&L {self.total_pnl():,.2f} is at or below -{max_loss:,.2f}")
        if max_contracts is not None:
            rules["size"] = (self.open_contracts > max_contracts, f"{self.open_contracts} open contracts exceeds {max_contracts}")
        messages = []
        for name, (breached, text) in rules.items():
            if breached and name not in self._breached:
                self._breached.add(name)
                messages.append(f"ALERT  {text}")
            elif not breached and name in self._breached:
                self._breached.discard(name)
                messages.append(f"CLEAR  back within your {name} limit")
        return messages

    def status_line(self) -> str:
        unrealized, pending = self.unrealized()
        line = (f"open {self.open_contracts:>3} contracts | realized {self.realized - self.costs:>10,.2f} "
                f"| unrealized {unrealized:>10,.2f} | total {self.realized - self.costs + unrealized:>10,.2f}")
        return line + (f" | waiting for prices: {', '.join(pending)}" if pending else "")


# ── [connect] Seed from REST, then stream ───────────────────────────
async def main() -> None:
    args = parse_args()
    since = (datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.since
             else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))
    client = TopstepXClient()
    market, user = MarketHub(client.tokens), UserHub(client.tokens)
    try:
        account = choose_account(client, args.account_id)
        contracts: dict[str, Contract] = {}

        def lookup(contract_id: str) -> Contract:
            if contract_id not in contracts:
                contracts[contract_id] = client.get_contract(contract_id)
            return contracts[contract_id]

        book = RiskBook(lookup, since)
        for position in client.search_open_positions(account.id):
            book.apply_position(position.raw)
        for trade in client.search_trades(account.id, start=since):
            book.apply_trade(trade.raw)

        watching: set[str] = set()

        async def watch_prices(contract_id: str | None) -> None:
            if contract_id and contract_id not in watching:
                watching.add(contract_id)
                await asyncio.to_thread(lookup, contract_id)  # fetch tick size/value off the event loop
                await market.subscribe_quotes(contract_id)

        async def on_position(payload: object) -> None:
            await watch_prices(book.apply_position(payload))

        market.on_quote(lambda contract_id, quote: book.apply_quote(contract_id, quote.last_price))
        user.on_position(on_position)
        user.on_user_trade(book.apply_trade)

        await market.start()
        await user.start()
        for contract_id in list(book.positions):
            await watch_prices(contract_id)
        await user.subscribe_positions(account.id)
        await user.subscribe_trades(account.id)

        # ── [watch-loop] Print status, raise alerts ─────────────────
        limits = []
        if args.max_loss is not None:
            limits.append(f"max loss {args.max_loss:,.2f}")
        if args.max_contracts is not None:
            limits.append(f"max contracts {args.max_contracts}")
        print(f"Watching account {account.id} ({account.name}) — read-only, never sends orders.")
        print(f"Realized P&L counted from {since:%Y-%m-%d %H:%M} UTC. Limits: {', '.join(limits) or 'none set (status only)'}.")
        print("Ctrl+C to stop.\n")

        loop = asyncio.get_running_loop()
        stop_at = loop.time() + args.seconds if args.seconds else None
        while stop_at is None or loop.time() < stop_at:
            for message in book.check(args.max_loss, args.max_contracts):
                print(message)
            print(f"{datetime.now(timezone.utc):%H:%M:%S}  {book.status_line()}")
            await asyncio.sleep(args.interval if stop_at is None else max(0.0, min(args.interval, stop_at - loop.time())))
    finally:
        await user.stop()
        await market.stop()
        client.close()


# ── [errors] Exit cleanly ───────────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        print(f"Cannot continue: {exc}")
        sys.exit(1)
