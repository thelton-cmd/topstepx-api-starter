"""Pure bookkeeping for the dashboard: accounts, contracts, quotes, depth, tape, positions,
orders, fills and chart bars.

Nothing in here talks to the network. The asyncio side writes (hub callbacks, REST seeds,
bar refreshes) and the HTTP server threads read, so every object carries one lock and every
public method takes it. No method calls out to another object while holding its own lock.

P&L figures are an approximation built from API data. Nothing here is trading advice.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone

from topstepx import Account, Bar, Contract, DepthEntry, MarketTrade, OrderSide, OrderStatus, OrderType, PositionType, Quote

# DOM type codes from the gateway's market depth stream.
DOM_UNKNOWN = 0
DOM_ASK = 1
DOM_BID = 2
DOM_BEST_ASK = 3
DOM_BEST_BID = 4
DOM_TRADE = 5
DOM_RESET = 6
DOM_LOW = 7
DOM_HIGH = 8
DOM_NEW_BEST_BID = 9
DOM_NEW_BEST_ASK = 10
DOM_FILL = 11

ASK_TYPES = {DOM_ASK, DOM_BEST_ASK, DOM_NEW_BEST_ASK}
BID_TYPES = {DOM_BID, DOM_BEST_BID, DOM_NEW_BEST_BID}
# "Best" codes name the top of the book: nothing on that side can rest beyond them.
BEST_ASK_TYPES = {DOM_BEST_ASK, DOM_NEW_BEST_ASK}
BEST_BID_TYPES = {DOM_BEST_BID, DOM_NEW_BEST_BID}

MAX_STORED_CANDLES = 20_000  # per contract; also the gateway's own per-request maximum
MAX_DEPTH_LEVELS = 100  # most levels per side a snapshot will ever return
MAX_STORED_LEVELS = 1000  # per side; beyond this the levels farthest from the best are dropped
MAX_TRADED_PRICES = 2000  # session volume-at-price entries kept (nearest the last trade)

# Orders still working at the exchange. Everything else (filled, cancelled, rejected...) is done.
OPEN_ORDER_STATUSES = {
    OrderStatus.OPEN,
    OrderStatus.PENDING,
    OrderStatus.PENDING_CANCELLATION,
    OrderStatus.SUSPENDED,
}
MAX_TRADES_SHOWN = 100


# -- small helpers -------------------------------------------------------------------


def unwrap(payload: object) -> dict:
    """User-hub events may arrive as {"action": ..., "data": {...}} or as the data itself."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def parse_time(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enum_name(enum_cls, value: object) -> str:
    try:
        return enum_cls(value).name
    except (TypeError, ValueError):
        return str(value)


def candle_series(bars: list[Bar]) -> list[dict]:
    """Oldest-first candle dicts with unique UTC-second timestamps (same rule as example 08)."""
    by_time: dict[int, Bar] = {}
    for bar in bars:
        if bar.time is not None:
            by_time[int(bar.time.timestamp())] = bar
    return [
        {
            "time": t,
            "open": by_time[t].open,
            "high": by_time[t].high,
            "low": by_time[t].low,
            "close": by_time[t].close,
            "volume": by_time[t].volume,
        }
        for t in sorted(by_time)
    ]


def _number(value: object) -> float | None:
    """Floats for the page; None for anything that is not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# -- Tape: a ring buffer of tick events for one contract -------------------------------


class Tape:
    """Trades and quote changes for one contract, newest last, with a monotonic ``seq``.

    Event dict: {"seq", "time", "kind": "trade"|"quote", "price", "size", "side", "bid", "ask"}.
    """

    def __init__(self, capacity: int = 500):
        if capacity <= 0:
            raise ValueError("capacity must be greater than zero")
        self._lock = threading.Lock()
        self._events: deque[dict] = deque(maxlen=capacity)
        self._seq = 0
        self._quote = {"bid": None, "ask": None, "last": None}  # last values seen, merged

    def _append(self, event: dict) -> None:
        self._seq += 1
        self._events.append({"seq": self._seq, **event})

    def add_trades(self, contract_id: str, trades: list[MarketTrade]) -> None:
        with self._lock:
            for trade in trades:
                side = {0: "BUY", 1: "SELL"}.get(trade.type)
                self._append({
                    "time": iso(trade.timestamp) or now_iso(),
                    "kind": "trade",
                    "price": _number(trade.price),
                    "size": _number(trade.volume),
                    "side": side,
                    "bid": None,
                    "ask": None,
                })

    def add_quote(self, contract_id: str, quote: Quote) -> None:
        """Record a quote event only when the bid, ask or last price actually changed."""
        incoming = {"bid": quote.best_bid, "ask": quote.best_ask, "last": quote.last_price}
        with self._lock:
            changed = False
            for key, value in incoming.items():
                if value is not None and value != self._quote[key]:
                    self._quote[key] = value
                    changed = True
            if not changed:
                return
            self._append({
                "time": iso(quote.last_updated or quote.timestamp) or now_iso(),
                "kind": "quote",
                "price": _number(self._quote["last"]),
                "size": None,
                "side": None,
                "bid": _number(self._quote["bid"]),
                "ask": _number(self._quote["ask"]),
            })

    def since(self, seq: int) -> tuple[list[dict], int]:
        """Events with a seq greater than ``seq`` (oldest first) and the latest seq."""
        with self._lock:
            events = [dict(e) for e in self._events if e["seq"] > seq]
            return events, self._seq


# -- DepthBook: the price ladder for one contract --------------------------------------


class DepthBook:
    """Bid and ask sizes by price, plus last trade and session low/high, from depth events."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._last_price: float | None = None
        self._last_size: float | None = None
        self._low: float | None = None
        self._high: float | None = None
        self._traded: dict[float, float] = {}   # session volume traded at each price (Trade events)
        self._version = 0

    @staticmethod
    def _size(entry: DepthEntry) -> float | None:
        # The live feed sends ``volume`` as the resting size and ``currentVolume`` as 0 on
        # nearly every update, so currentVolume only wins when it carries a real (non-zero)
        # size. A level is removed when the size that applies is 0.
        current = _number(entry.current_volume)
        if current:
            return current
        return _number(entry.volume)

    @staticmethod
    def _set_level(side: dict[float, float], price: float | None, size: float | None) -> None:
        if price is None:
            return
        if not size:  # 0 or None removes the level
            side.pop(price, None)
        else:
            side[price] = size

    @staticmethod
    def _prune(side: dict[float, float], best_first: bool) -> None:
        if len(side) <= MAX_STORED_LEVELS:
            return
        keep = set(sorted(side, reverse=best_first)[:MAX_STORED_LEVELS])
        for price in list(side):
            if price not in keep:
                del side[price]

    @staticmethod
    def _drop_where(side: dict[float, float], predicate) -> None:
        for price in [p for p in side if predicate(p)]:
            del side[price]

    def apply(self, entries: list[DepthEntry]) -> None:
        if not entries:
            return
        with self._lock:
            for entry in entries:
                price = _number(entry.price)
                code = entry.type
                if code in ASK_TYPES:
                    size = self._size(entry)
                    self._set_level(self._asks, price, size)
                    if price is not None and size:
                        # The feed does not send removals for levels the market has moved past
                        # (observed 2026-10-28), so an ask resting here means no bid can rest at
                        # or above it, and a *best* ask means no ask can rest below it.
                        self._drop_where(self._bids, lambda p: p >= price)
                        if code in BEST_ASK_TYPES:
                            self._drop_where(self._asks, lambda p: p < price)
                elif code in BID_TYPES:
                    size = self._size(entry)
                    self._set_level(self._bids, price, size)
                    if price is not None and size:
                        self._drop_where(self._asks, lambda p: p <= price)
                        if code in BEST_BID_TYPES:
                            self._drop_where(self._bids, lambda p: p > price)
                elif code in (DOM_TRADE, DOM_FILL):
                    # Trade events carry the session volume traded at that price in ``volume``
                    # and this trade's size in ``currentVolume``. The snapshot sent right after
                    # subscribing has currentVolume 0 for every price and is not a trade.
                    size = _number(entry.current_volume)
                    if entry.current_volume is None:
                        size = _number(entry.volume)
                    if price is not None and size:
                        self._last_price = price
                        self._last_size = size
                    if code == DOM_TRADE and price is not None and entry.current_volume is not None:
                        traded = _number(entry.volume)
                        if traded is not None and traded >= 0:
                            self._traded[price] = traded
                elif code == DOM_RESET:
                    self._bids.clear()
                    self._asks.clear()
                    self._traded.clear()
                elif code == DOM_LOW:
                    if price is not None:
                        self._low = price
                elif code == DOM_HIGH:
                    if price is not None:
                        self._high = price
                # DOM_UNKNOWN and anything undocumented is ignored
            self._prune(self._bids, best_first=True)
            self._prune(self._asks, best_first=False)
            if len(self._traded) > MAX_TRADED_PRICES:
                anchor = self._last_price if self._last_price is not None else 0.0
                for p in sorted(self._traded, key=lambda x: abs(x - anchor))[MAX_TRADED_PRICES:]:
                    del self._traded[p]
            self._version += 1

    def set_last(self, price: float | None, size: float | None) -> None:
        """Record the last trade from the trade stream (used when depth has no Trade event)."""
        price = _number(price)
        if price is None:
            return
        with self._lock:
            self._last_price = price
            self._last_size = _number(size)
            self._version += 1

    def snapshot(self, levels: int = 20) -> dict:
        levels = max(1, min(int(levels), MAX_DEPTH_LEVELS))
        with self._lock:
            bid_prices = sorted(self._bids, reverse=True)[:levels]
            ask_prices = sorted(self._asks)[:levels]
            anchor = self._last_price if self._last_price is not None else (bid_prices[0] if bid_prices else ask_prices[0] if ask_prices else None)
            traded_prices = sorted(self._traded, key=lambda p: abs(p - anchor))[: 4 * levels + 20] if anchor is not None else []
            return {
                "bids": [{"price": p, "size": self._bids[p]} for p in bid_prices],
                "asks": [{"price": p, "size": self._asks[p]} for p in ask_prices],
                "traded": [{"price": p, "volume": self._traded[p]} for p in sorted(traded_prices, reverse=True)],
                "best_bid": max(self._bids) if self._bids else None,
                "best_ask": min(self._asks) if self._asks else None,
                "last_price": self._last_price,
                "last_size": self._last_size,
                "low": self._low,
                "high": self._high,
                "version": self._version,
            }

    def traded(self) -> list[dict]:
        """Every stored session volume-at-price entry (not only the ladder window), highest price first."""
        with self._lock:
            return [{"price": p, "volume": self._traded[p]} for p in sorted(self._traded, reverse=True)]


# -- DashboardState: everything the page shows ------------------------------------------


class DashboardState:
    """Accounts, contracts, quotes, depth, tape, positions, orders, fills and chart bars.

    Tape and DepthBook objects have their own locks; this class fetches them under its lock
    and then calls them with the lock released.
    """

    def __init__(self, since: datetime):
        self._lock = threading.Lock()
        self.since = since
        self.accounts: dict[int, dict] = {}
        self.account_id: int | None = None
        self.contracts: dict[str, dict] = {}
        self.quotes: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.orders: dict[int, dict] = {}
        self.trades: dict[int, dict] = {}
        self.bars: dict[str, dict] = {}
        self.tapes: dict[str, Tape] = {}
        self.depths: dict[str, DepthBook] = {}
        self.focus_id: str | None = None
        self.connection = {"market": False, "user": False}
        self.last_error: str | None = None

    # -- accounts ------------------------------------------------------------------

    @staticmethod
    def _account_dict(account: Account) -> dict:
        return {
            "id": account.id,
            "name": account.name,
            "balance": account.balance,
            "simulated": account.simulated,
            "can_trade": account.can_trade,
        }

    def set_accounts(self, accounts: list[Account]) -> None:
        with self._lock:
            self.accounts = {a.id: self._account_dict(a) for a in accounts}

    def select_account(self, account_id: int) -> None:
        """Switch accounts: the positions, orders and fills shown belong to one account only."""
        with self._lock:
            if account_id not in self.accounts:
                raise ValueError(f"Account {account_id} is not on this login's active account list.")
            self.account_id = account_id
            self.positions.clear()
            self.orders.clear()
            self.trades.clear()

    def selected_account(self) -> dict | None:
        with self._lock:
            account = self.accounts.get(self.account_id) if self.account_id is not None else None
            return dict(account) if account else None

    def order_entry_allowed(self) -> bool:
        """True only for an account the API marks simulated (a Practice Account) that can trade."""
        account = self.selected_account()
        return bool(account) and account["simulated"] is True and account["can_trade"] is True

    def apply_account_event(self, payload: object) -> None:
        """Balance updates arrive for every account on the login; update whichever one it names."""
        a = unwrap(payload)
        with self._lock:
            account = self.accounts.get(a.get("id"))
            if account is None:
                return
            for key, api_key in (("balance", "balance"), ("can_trade", "canTrade"), ("name", "name")):
                if a.get(api_key) is not None:
                    account[key] = a[api_key]

    # -- contracts and quotes ------------------------------------------------------

    def add_contract(self, contract: Contract) -> None:
        with self._lock:
            self.contracts.setdefault(contract.id, {
                "id": contract.id,
                "name": contract.name,
                "description": contract.description,
                "tick_size": contract.tick_size,
                "tick_value": contract.tick_value,
                "active_contract": contract.active_contract,
            })

    def has_contract(self, contract_id: str) -> bool:
        with self._lock:
            return contract_id in self.contracts

    def contract(self, contract_id: str) -> dict | None:
        with self._lock:
            found = self.contracts.get(contract_id)
            return dict(found) if found else None

    def contract_ids(self) -> list[str]:
        with self._lock:
            return list(self.contracts)

    def position_contract_ids(self) -> list[str]:
        with self._lock:
            return list(self.positions)

    def _tape(self, contract_id: str) -> Tape:
        with self._lock:
            return self.tapes.setdefault(contract_id, Tape())

    def _depth(self, contract_id: str) -> DepthBook:
        with self._lock:
            return self.depths.setdefault(contract_id, DepthBook())

    def apply_quote(self, contract_id: str, quote: Quote) -> None:
        """Quotes are partial updates: keep the last value seen for each field, then tape it."""
        fields = {
            "bid": quote.best_bid, "ask": quote.best_ask, "last": quote.last_price,
            "change": quote.change, "change_percent": quote.change_percent,
            "open": quote.open, "high": quote.high, "low": quote.low, "volume": quote.volume,
            "updated": iso(quote.last_updated or quote.timestamp),
        }
        with self._lock:
            merged = self.quotes.setdefault(contract_id, {})
            for key, value in fields.items():
                if value is not None:
                    merged[key] = value
        self._tape(contract_id).add_quote(contract_id, quote)

    def apply_trades(self, contract_id: str, trades: list[MarketTrade]) -> None:
        """Trade ticks go on the tape and move the depth ladder's last trade marker."""
        if not trades:
            return
        self._tape(contract_id).add_trades(contract_id, trades)
        last = trades[-1]
        self._depth(contract_id).set_last(last.price, last.volume)

    def apply_depth(self, contract_id: str, entries: list[DepthEntry]) -> None:
        self._depth(contract_id).apply(entries)

    def depth(self, contract_id: str, levels: int = 20) -> dict | None:
        with self._lock:
            book = self.depths.get(contract_id)
        return book.snapshot(levels) if book else None

    def traded(self, contract_id: str) -> list[dict] | None:
        """Session volume at price from the depth feed, every stored price (``DepthBook.traded``), or None."""
        with self._lock:
            book = self.depths.get(contract_id)
        return book.traded() if book else None

    def tape_since(self, contract_id: str, seq: int) -> tuple[list[dict], int]:
        with self._lock:
            tape = self.tapes.get(contract_id)
        return tape.since(seq) if tape else ([], 0)

    def set_focus(self, contract_id: str | None) -> None:
        with self._lock:
            self.focus_id = contract_id

    def focus(self) -> str | None:
        with self._lock:
            return self.focus_id

    # -- positions, orders, fills ---------------------------------------------------

    def _is_selected_account(self, payload: dict) -> bool:
        """Events for another account (a stale subscription during a switch) are ignored."""
        account_id = payload.get("accountId")
        if account_id is None:
            return True  # some payloads carry no accountId; they come from our own REST seeds
        with self._lock:
            return self.account_id is None or account_id == self.account_id

    def apply_position(self, payload: object) -> str | None:
        """Update from a position snapshot/event. Returns its contract ID."""
        p = unwrap(payload)
        contract_id, size = p.get("contractId"), p.get("size") or 0
        if not contract_id or not self._is_selected_account(p):
            return None
        with self._lock:
            if size == 0:
                self.positions.pop(contract_id, None)
                return contract_id
            signed = -abs(size) if p.get("type") == PositionType.SHORT else abs(size)
            self.positions[contract_id] = {
                "contract_id": contract_id,
                "size": signed,
                "average_price": float(p.get("averagePrice") or 0.0),
                "opened": p.get("creationTimestamp"),
            }
        return contract_id

    def apply_order(self, payload: object) -> None:
        """Keep working orders; drop an order once the API reports it done."""
        o = unwrap(payload)
        order_id = o.get("id")
        if order_id is None or not self._is_selected_account(o):
            return
        with self._lock:
            if o.get("status") not in OPEN_ORDER_STATUSES:
                self.orders.pop(order_id, None)
                return
            self.orders[order_id] = {
                "id": order_id,
                "contract_id": o.get("contractId"),
                "status": enum_name(OrderStatus, o.get("status")),
                "type": enum_name(OrderType, o.get("type")),
                "side": enum_name(OrderSide, o.get("side")),
                "size": o.get("size"),
                "filled": o.get("fillVolume") or 0,
                "limit_price": o.get("limitPrice"),
                "stop_price": o.get("stopPrice"),
                "created": o.get("creationTimestamp"),
            }

    def apply_trade(self, payload: object) -> None:
        """Record a fill once (fills can arrive by REST and by stream)."""
        t = unwrap(payload)
        trade_id = t.get("id")
        if trade_id is None or t.get("voided") or not self._is_selected_account(t):
            return
        when = parse_time(t.get("creationTimestamp"))
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when is not None and when < self.since:
            return
        with self._lock:
            if trade_id in self.trades:
                return
            self.trades[trade_id] = {
                "id": trade_id,
                "time": iso(when) or t.get("creationTimestamp"),
                "contract_id": t.get("contractId"),
                "side": enum_name(OrderSide, t.get("side")),
                "size": t.get("size"),
                "price": t.get("price"),
                "pnl": t.get("profitAndLoss"),  # None = half-turn (opened or added to a position)
                "fees": (t.get("fees") or 0.0) + (t.get("commissions") or 0.0),
            }

    # -- chart bars and connections -------------------------------------------------

    def set_bars(self, contract_id: str, bars: list[Bar]) -> int:
        """Merge a fresh window of bars into the stored candles.

        A bar at a time already stored replaces it (the partial bar fills in), new times are
        added, older candles loaded earlier stay. Only the newest ``MAX_STORED_CANDLES`` are
        kept. Returns how many new candle times were added; the version always goes up.
        """
        return self._merge_bars(contract_id, bars)

    def add_history(self, contract_id: str, bars: list[Bar]) -> int:
        """Merge older bars loaded on demand (same rules as ``set_bars``)."""
        return self._merge_bars(contract_id, bars)

    def _merge_bars(self, contract_id: str, bars: list[Bar]) -> int:
        fresh = candle_series(bars)
        with self._lock:
            stored = self.bars.get(contract_id)
            by_time: dict[int, dict] = dict(stored["by_time"]) if stored else {}
            added = 0
            for candle in fresh:
                if candle["time"] not in by_time:
                    added += 1
                by_time[candle["time"]] = candle
            times = sorted(by_time)
            if len(times) > MAX_STORED_CANDLES:
                for t in times[:-MAX_STORED_CANDLES]:
                    del by_time[t]
                times = times[-MAX_STORED_CANDLES:]
            self.bars[contract_id] = {
                "version": (stored["version"] if stored else 0) + 1,
                "candles": [by_time[t] for t in times],
                "by_time": by_time,
            }
            return added

    def clear_bars(self) -> None:
        """Forget every stored candle (the timeframe changed). Versions keep counting up."""
        with self._lock:
            for contract_id, stored in list(self.bars.items()):
                self.bars[contract_id] = {"version": stored["version"] + 1, "candles": [], "by_time": {}}

    def candles(self, contract_id: str, after: int | None = None) -> list[dict] | None:
        """Stored candles oldest first, or only those at/after ``after`` (UTC seconds)."""
        with self._lock:
            stored = self.bars.get(contract_id)
            if stored is None:
                return None
            rows = stored["candles"]
            if after is not None:
                rows = [c for c in rows if c["time"] >= after]
            return [dict(c) for c in rows]

    def candle_count(self, contract_id: str) -> int:
        with self._lock:
            stored = self.bars.get(contract_id)
            return len(stored["candles"]) if stored else 0

    def oldest_time(self, contract_id: str) -> int | None:
        with self._lock:
            stored = self.bars.get(contract_id)
            return stored["candles"][0]["time"] if stored and stored["candles"] else None

    def bars_version(self, contract_id: str) -> int:
        with self._lock:
            return self.bars.get(contract_id, {}).get("version", 0)

    def set_connection(self, hub: str, ok: bool) -> None:
        with self._lock:
            self.connection[hub] = ok

    def set_error(self, message: str | None) -> None:
        with self._lock:
            self.last_error = message

    # -- the read side ----------------------------------------------------------------

    def _unrealized(self, position: dict) -> float | None:
        contract = self.contracts.get(position["contract_id"], {})
        last = self.quotes.get(position["contract_id"], {}).get("last")
        tick_size, tick_value = contract.get("tick_size"), contract.get("tick_value")
        if last is None or not tick_size or tick_value is None:
            return None
        ticks = (last - position["average_price"]) / tick_size
        return ticks * tick_value * position["size"]

    def snapshot(self) -> dict:
        """Everything the page needs for one refresh, as JSON-safe plain data (bars excluded)."""
        with self._lock:
            account = self.accounts.get(self.account_id) if self.account_id is not None else None
            positions, unrealized_total, pending = [], 0.0, []
            for p in self.positions.values():
                unrealized = self._unrealized(p)
                if unrealized is None:
                    pending.append(p["contract_id"])
                else:
                    unrealized_total += unrealized
                positions.append({
                    **p,
                    "name": self.contracts.get(p["contract_id"], {}).get("name") or p["contract_id"],
                    "side": "LONG" if p["size"] > 0 else "SHORT",
                    "contracts": abs(p["size"]),
                    "last": self.quotes.get(p["contract_id"], {}).get("last"),
                    "unrealized": unrealized,
                })
            realized = sum(t["pnl"] or 0.0 for t in self.trades.values())
            costs = sum(t["fees"] for t in self.trades.values())
            trades = sorted(self.trades.values(), key=lambda t: (t["time"] or "", t["id"]), reverse=True)
            return {
                "generated_at": now_iso(),
                "since": self.since.isoformat(),
                "last_error": self.last_error,
                "connection": dict(self.connection),
                "accounts": [dict(a) for a in self.accounts.values()],
                "account": dict(account) if account else None,
                "order_entry_allowed": bool(account) and account["simulated"] is True and account["can_trade"] is True,
                "focus": self.focus_id,
                "contracts": list(self.contracts.values()),
                "quotes": {cid: dict(q) for cid, q in self.quotes.items()},
                "positions": positions,
                "orders": sorted(self.orders.values(), key=lambda o: (o["created"] or "", o["id"])),
                "trades": trades[:MAX_TRADES_SHOWN],
                "totals": {
                    "realized": realized,
                    "costs": costs,
                    "unrealized": unrealized_total,
                    "total": realized - costs + unrealized_total,
                    "open_contracts": sum(abs(p["size"]) for p in self.positions.values()),
                    "fills": len(self.trades),
                    "pending_prices": pending,
                },
                "bars_versions": {cid: b["version"] for cid, b in self.bars.items()},
            }
