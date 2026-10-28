"""Offline tests for dashboard/state.py: Tape, DepthBook and DashboardState bookkeeping."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from dashboard.state import DashboardState, DepthBook, Tape
from topstepx import Account, Bar, Contract, DepthEntry, MarketTrade, Quote

ES = Contract(id="CON.F.US.EP.Z26", name="ESZ26", description="E-mini S&P 500", tick_size=0.25, tick_value=12.5)
NQ = Contract(id="CON.F.US.ENQ.Z26", name="NQZ26", description="E-mini Nasdaq 100", tick_size=0.25, tick_value=5.0)
SINCE = datetime(2026, 10, 26, tzinfo=timezone.utc)

PRACTICE = Account(id=7, name="PRAC-1", balance=50_000.0, simulated=True, can_trade=True)
FUNDED = Account(id=8, name="FUNDED-1", balance=100_000.0, simulated=False, can_trade=True)
HALTED = Account(id=9, name="PRAC-2", balance=49_000.0, simulated=True, can_trade=False)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def make_state() -> DashboardState:
    state = DashboardState(SINCE)
    state.set_accounts([PRACTICE, FUNDED, HALTED])
    state.select_account(PRACTICE.id)
    state.add_contract(ES)
    return state


def depth(code: int, price: float, volume=None, current=None) -> DepthEntry:
    return DepthEntry(price=price, volume=volume, type=code, current_volume=current)


def trade(price: float, volume: float = 1, side: int = 0, when: datetime | None = None) -> MarketTrade:
    return MarketTrade(price=price, volume=volume, type=side, timestamp=when)


# -- v1 behaviour, ported -----------------------------------------------------------


def test_quotes_merge_partial_updates():
    state = make_state()
    state.apply_quote(ES.id, Quote(best_bid=5000.0, best_ask=5000.25, last_price=5000.25))
    state.apply_quote(ES.id, Quote(last_price=5000.5))  # bid/ask absent: keep the last seen
    quote = state.snapshot()["quotes"][ES.id]
    assert (quote["bid"], quote["ask"], quote["last"]) == (5000.0, 5000.25, 5000.5)


def test_positions_show_unrealized_pnl_and_pending_prices():
    state = make_state()
    state.apply_position({"contractId": ES.id, "type": 1, "size": 2, "averagePrice": 5000.0})
    snap = state.snapshot()
    assert snap["positions"][0]["unrealized"] is None
    assert snap["totals"]["pending_prices"] == [ES.id]

    state.apply_quote(ES.id, Quote(last_price=5001.0))  # +4 ticks x $12.50 x 2 contracts
    snap = state.snapshot()
    assert snap["positions"][0]["unrealized"] == 100.0
    assert snap["positions"][0]["side"] == "LONG" and snap["positions"][0]["contracts"] == 2
    assert snap["totals"]["unrealized"] == 100.0 and snap["totals"]["open_contracts"] == 2

    state.apply_position({"data": {"contractId": ES.id, "type": 2, "size": 1, "averagePrice": 5000.0}})
    assert state.snapshot()["positions"][0]["unrealized"] == -50.0  # short 1, price up 4 ticks
    state.apply_position({"contractId": ES.id, "type": 1, "size": 0, "averagePrice": 0})
    assert state.snapshot()["positions"] == []
    assert state.position_contract_ids() == []


def test_orders_keep_only_working_orders():
    state = make_state()
    state.apply_order({"id": 1, "contractId": ES.id, "status": 1, "type": 1, "side": 0, "size": 1, "limitPrice": 4990.0})
    state.apply_order({"data": {"id": 2, "contractId": ES.id, "status": 6, "type": 4, "side": 1, "size": 1, "stopPrice": 4980.0}})
    orders = state.snapshot()["orders"]
    assert [(o["id"], o["status"], o["type"], o["side"]) for o in orders] == [(1, "OPEN", "LIMIT", "BUY"), (2, "PENDING", "STOP", "SELL")]

    state.apply_order({"id": 1, "contractId": ES.id, "status": 2})  # filled
    state.apply_order({"id": 2, "contractId": ES.id, "status": 3})  # cancelled
    assert state.snapshot()["orders"] == []


def test_trades_count_once_respect_since_and_skip_voided():
    state = make_state()
    fill = {"id": 1, "contractId": ES.id, "side": 1, "size": 1, "price": 5001.0, "profitAndLoss": 50.0,
            "fees": 2.0, "commissions": 1.0, "creationTimestamp": "2026-10-26T14:00:00Z"}
    state.apply_trade(fill)
    state.apply_trade({"data": fill})  # same fill again via the stream
    state.apply_trade({"id": 2, "profitAndLoss": -500.0, "creationTimestamp": "2026-10-25T14:00:00Z"})  # before since
    state.apply_trade({"id": 3, "profitAndLoss": -500.0, "voided": True})
    state.apply_trade({"id": 4, "contractId": ES.id, "side": 0, "size": 1, "price": 5000.0, "fees": 1.0,
                       "creationTimestamp": "2026-10-26T13:00:00Z"})  # half-turn: no P&L
    snap = state.snapshot()
    assert [t["id"] for t in snap["trades"]] == [1, 4]  # newest first
    assert snap["trades"][1]["pnl"] is None
    assert snap["totals"] == {**snap["totals"], "realized": 50.0, "costs": 4.0, "total": 46.0, "fills": 2}


def test_trades_shown_are_capped_at_100_newest():
    state = make_state()
    for i in range(1, 121):
        when = (utc(2026, 10, 26, 14) + timedelta(seconds=i)).isoformat()
        state.apply_trade({"id": i, "contractId": ES.id, "creationTimestamp": when})
    snap = state.snapshot()
    assert len(snap["trades"]) == 100 and snap["totals"]["fills"] == 120
    assert snap["trades"][0]["id"] == 120


def bar(t: datetime, c=1.5) -> Bar:
    return Bar(time=t, open=1.0, high=2.0, low=0.5, close=c, volume=10)


def test_bars_are_sorted_deduplicated_and_versioned():
    state = make_state()
    assert state.candles(ES.id) is None and state.bars_version(ES.id) == 0
    state.set_bars(ES.id, [bar(utc(2026, 10, 26, 14, 10)), bar(utc(2026, 10, 26, 14, 5), c=0.9), bar(utc(2026, 10, 26, 14, 5))])
    candles = state.candles(ES.id)
    assert [c["time"] for c in candles] == sorted({c["time"] for c in candles}) and len(candles) == 2
    assert candles[0] == {"time": int(utc(2026, 10, 26, 14, 5).timestamp()), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}
    assert state.bars_version(ES.id) == 1 and state.snapshot()["bars_versions"] == {ES.id: 1}
    state.set_bars(ES.id, [])  # an empty refresh keeps what is stored (the chart never goes blank)
    assert state.bars_version(ES.id) == 2 and len(state.candles(ES.id)) == 2


def test_bars_merge_history_filter_and_clear():
    state = make_state()
    from datetime import timedelta

    m = lambda minute: utc(2026, 10, 26, 14, 0) + timedelta(minutes=minute)  # noqa: E731
    ts = lambda minute: int(m(minute).timestamp())  # noqa: E731
    assert state.set_bars(ES.id, [bar(m(0)), bar(m(5)), bar(m(10))]) == 3
    # The next window overlaps the last bar (replaced, e.g. the partial bar filled in) and adds one.
    assert state.set_bars(ES.id, [bar(m(10), c=9.0), bar(m(15))]) == 1
    candles = state.candles(ES.id)
    assert [c["time"] for c in candles] == [ts(0), ts(5), ts(10), ts(15)] and candles[2]["close"] == 9.0
    # Older history merges in front; only genuinely new times count.
    assert state.add_history(ES.id, [bar(m(-5)), bar(m(0))]) == 1
    assert state.oldest_time(ES.id) == ts(-5) and state.candle_count(ES.id) == 5
    assert [c["time"] for c in state.candles(ES.id, after=ts(10))] == [ts(10), ts(15)]
    assert state.candles(ES.id, after=ts(16)) == [] and state.candles("CON.X", after=1) is None
    assert state.candle_count("CON.X") == 0 and state.oldest_time("CON.X") is None
    version = state.bars_version(ES.id)
    state.clear_bars()
    assert state.candles(ES.id) == [] and state.oldest_time(ES.id) is None
    assert state.bars_version(ES.id) == version + 1 and state.snapshot()["bars_versions"] == {ES.id: version + 1}
    assert state.set_bars(ES.id, [bar(m(20))]) == 1 and state.candle_count(ES.id) == 1


def test_bars_storage_keeps_only_the_newest(monkeypatch):
    import dashboard.state as state_module

    monkeypatch.setattr(state_module, "MAX_STORED_CANDLES", 4)
    state = make_state()
    state.set_bars(ES.id, [bar(utc(2026, 10, 26, 14, minute)) for minute in (0, 5, 10, 15, 20)])
    assert [c["time"] for c in state.candles(ES.id)] == [int(utc(2026, 10, 26, 14, minute).timestamp()) for minute in (5, 10, 15, 20)]
    # Older history that would push the store over the cap is dropped again, newest kept.
    assert state.add_history(ES.id, [bar(utc(2026, 10, 26, 13, 55))]) == 1
    assert state.candle_count(ES.id) == 4 and state.oldest_time(ES.id) == int(utc(2026, 10, 26, 14, 5).timestamp())


def test_candles_returns_a_copy():
    state = make_state()
    state.set_bars(ES.id, [bar(utc(2026, 10, 26, 14, 5))])
    copy = state.candles(ES.id)
    copy[0]["close"] = 999.0
    copy.append({"time": 0})
    assert state.candles(ES.id) == [{"time": int(utc(2026, 10, 26, 14, 5).timestamp()), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}]


def test_snapshot_is_json_serializable_and_has_the_contract_shape():
    state = make_state()
    state.apply_position({"contractId": ES.id, "type": 1, "size": 1, "averagePrice": 5000.0})
    state.apply_quote(ES.id, Quote(last_price=5000.0, last_updated=utc(2026, 10, 26, 14)))
    state.apply_depth(ES.id, [depth(2, 4999.75, 3)])
    state.apply_trades(ES.id, [trade(5000.0, 2, when=utc(2026, 10, 26, 14))])
    state.set_focus(ES.id)
    state.set_connection("market", True)
    state.set_error("x")
    snap = state.snapshot()
    json.dumps(snap)
    assert set(snap) == {
        "generated_at", "since", "last_error", "connection", "accounts", "account", "order_entry_allowed",
        "focus", "contracts", "quotes", "positions", "orders", "trades", "totals", "bars_versions",
    }
    assert snap["connection"] == {"market": True, "user": False}
    assert snap["focus"] == ES.id and snap["last_error"] == "x"
    assert snap["contracts"] == [{"id": ES.id, "name": "ESZ26", "description": "E-mini S&P 500", "tick_size": 0.25, "tick_value": 12.5, "active_contract": ES.active_contract}]
    assert set(snap["totals"]) == {"realized", "costs", "unrealized", "total", "open_contracts", "fills", "pending_prices"}
    json.dumps(state.depth(ES.id))
    json.dumps(state.tape_since(ES.id, 0))


# -- accounts -----------------------------------------------------------------------


def test_set_accounts_keeps_the_list_and_selection_exposes_one():
    state = make_state()
    snap = state.snapshot()
    assert [a["id"] for a in snap["accounts"]] == [7, 8, 9]
    assert snap["account"] == {"id": 7, "name": "PRAC-1", "balance": 50_000.0, "simulated": True, "can_trade": True}
    assert state.selected_account() == snap["account"]


def test_no_account_selected_yet():
    state = DashboardState(SINCE)
    assert state.selected_account() is None
    assert state.order_entry_allowed() is False
    snap = state.snapshot()
    assert snap["account"] is None and snap["accounts"] == [] and snap["order_entry_allowed"] is False


def test_select_account_clears_positions_orders_and_trades():
    state = make_state()
    state.apply_position({"contractId": ES.id, "type": 1, "size": 1, "averagePrice": 5000.0})
    state.apply_order({"id": 1, "contractId": ES.id, "status": 1, "type": 1, "side": 0, "size": 1})
    state.apply_trade({"id": 1, "contractId": ES.id, "creationTimestamp": "2026-10-26T14:00:00Z"})
    state.apply_quote(ES.id, Quote(last_price=5000.0))
    state.select_account(FUNDED.id)
    snap = state.snapshot()
    assert snap["account"]["id"] == 8
    assert snap["positions"] == [] and snap["orders"] == [] and snap["trades"] == []
    assert snap["totals"]["fills"] == 0
    assert snap["quotes"][ES.id]["last"] == 5000.0  # market data is not per account


def test_select_unknown_account_is_rejected():
    state = make_state()
    with pytest.raises(ValueError):
        state.select_account(12345)
    assert state.selected_account()["id"] == 7


@pytest.mark.parametrize("account_id, allowed", [(7, True), (8, False), (9, False)])
def test_order_entry_allowed_only_for_simulated_and_can_trade(account_id, allowed):
    state = make_state()
    state.select_account(account_id)
    assert state.order_entry_allowed() is allowed
    assert state.snapshot()["order_entry_allowed"] is allowed


def test_order_entry_requires_literal_true_flags():
    state = DashboardState(SINCE)
    state.set_accounts([Account(id=1, name="odd", simulated=1, can_trade="yes")])
    state.select_account(1)
    assert state.order_entry_allowed() is False


def test_account_events_update_any_listed_account_by_id():
    state = make_state()
    state.apply_account_event({"data": {"id": 99, "balance": 1.0}})  # not on the login: ignored
    state.apply_account_event({"data": {"id": 7, "balance": 50_100.0}})
    state.apply_account_event({"id": 8, "balance": 99_000.0, "canTrade": False})
    snap = state.snapshot()
    assert snap["account"]["balance"] == 50_100.0
    by_id = {a["id"]: a for a in snap["accounts"]}
    assert by_id[8]["balance"] == 99_000.0 and by_id[8]["can_trade"] is False
    assert by_id[9]["balance"] == 49_000.0


def test_account_event_can_switch_order_entry_off():
    state = make_state()
    state.apply_account_event({"id": 7, "canTrade": False})
    assert state.order_entry_allowed() is False


# -- contracts and focus --------------------------------------------------------------


def test_contract_lookup_and_focus():
    state = make_state()
    assert state.has_contract(ES.id) and not state.has_contract(NQ.id)
    assert state.contract(ES.id)["tick_size"] == 0.25 and state.contract(NQ.id) is None
    state.contract(ES.id)["tick_size"] = 1.0  # a copy: the stored contract is untouched
    assert state.contract(ES.id)["tick_size"] == 0.25
    state.add_contract(NQ)
    state.add_contract(NQ)  # idempotent
    assert state.contract_ids() == [ES.id, NQ.id]
    assert state.focus() is None
    state.set_focus(NQ.id)
    assert state.focus() == NQ.id and state.snapshot()["focus"] == NQ.id


# -- Tape --------------------------------------------------------------------------------


def test_tape_seq_increments_and_since_filters():
    tape = Tape()
    assert tape.since(0) == ([], 0)
    tape.add_trades(ES.id, [trade(5000.0, 2, 0, utc(2026, 10, 26, 14)), trade(5000.25, 1, 1)])
    events, last = tape.since(0)
    assert last == 2 and [e["seq"] for e in events] == [1, 2]
    assert events[0] == {"seq": 1, "time": "2026-10-26T14:00:00+00:00", "kind": "trade", "price": 5000.0,
                         "size": 2.0, "side": "BUY", "bid": None, "ask": None}
    assert events[1]["side"] == "SELL" and events[1]["time"]
    events, last = tape.since(1)
    assert [e["seq"] for e in events] == [2] and last == 2
    assert tape.since(2) == ([], 2)
    assert tape.since(99) == ([], 2)


def test_tape_ring_capacity_keeps_newest_and_seq_keeps_counting():
    tape = Tape(capacity=3)
    tape.add_trades(ES.id, [trade(float(i)) for i in range(1, 6)])
    events, last = tape.since(0)
    assert last == 5
    assert [e["seq"] for e in events] == [3, 4, 5]
    assert [e["price"] for e in events] == [3.0, 4.0, 5.0]


def test_tape_quote_events_only_when_bid_ask_or_last_changes():
    tape = Tape()
    tape.add_quote(ES.id, Quote(best_bid=5000.0, best_ask=5000.25, last_price=5000.0, volume=10))
    tape.add_quote(ES.id, Quote(best_bid=5000.0, best_ask=5000.25, last_price=5000.0, volume=11))  # same prices
    tape.add_quote(ES.id, Quote(volume=12))  # partial update with no price fields
    tape.add_quote(ES.id, Quote(last_price=5000.25))  # last changed; bid/ask carried over
    events, last = tape.since(0)
    assert last == 2
    assert [e["kind"] for e in events] == ["quote", "quote"]
    assert events[0] == {**events[0], "price": 5000.0, "bid": 5000.0, "ask": 5000.25, "size": None, "side": None}
    assert events[1] == {**events[1], "price": 5000.25, "bid": 5000.0, "ask": 5000.25}


def test_tape_rejects_zero_capacity():
    with pytest.raises(ValueError):
        Tape(capacity=0)


# -- DepthBook -----------------------------------------------------------------------------


def test_depth_book_bids_asks_best_and_ordering():
    book = DepthBook()
    book.apply([depth(2, 4999.75, 5), depth(2, 4999.5, 7), depth(4, 5000.0, 3),
                depth(1, 5000.5, 4), depth(3, 5000.25, 2), depth(1, 5001.0, 9)])
    snap = book.snapshot()
    assert snap["bids"] == [{"price": 5000.0, "size": 3.0}, {"price": 4999.75, "size": 5.0}, {"price": 4999.5, "size": 7.0}]
    assert snap["asks"] == [{"price": 5000.25, "size": 2.0}, {"price": 5000.5, "size": 4.0}, {"price": 5001.0, "size": 9.0}]
    assert snap["best_bid"] == 5000.0 and snap["best_ask"] == 5000.25
    assert snap["last_price"] is None and snap["low"] is None and snap["high"] is None
    assert snap["version"] == 1
    assert set(snap) == {"bids", "asks", "traded", "best_bid", "best_ask", "last_price", "last_size", "low", "high", "version"}
    assert snap["traded"] == []


def test_depth_book_levels_limit_keeps_nearest_to_best():
    book = DepthBook()
    book.apply([depth(2, 5000.0 - i * 0.25, i + 1) for i in range(10)] + [depth(1, 5001.0 + i * 0.25, i + 1) for i in range(10)])
    snap = book.snapshot(levels=2)
    assert [b["price"] for b in snap["bids"]] == [5000.0, 4999.75]
    assert [a["price"] for a in snap["asks"]] == [5001.0, 5001.25]
    assert snap["best_bid"] == 5000.0 and snap["best_ask"] == 5001.0
    assert len(book.snapshot(levels=10_000)["bids"]) == 10  # capped, not an error


def test_depth_book_size_zero_or_none_removes_a_level():
    book = DepthBook()
    book.apply([depth(2, 5000.0, 3), depth(2, 4999.75, 2), depth(1, 5000.25, 4)])
    book.apply([depth(2, 5000.0, 0)])
    book.apply([depth(1, 5000.25, None)])
    snap = book.snapshot()
    assert snap["bids"] == [{"price": 4999.75, "size": 2.0}] and snap["asks"] == []
    assert snap["best_bid"] == 4999.75 and snap["best_ask"] is None
    book.apply([depth(2, 4998.0, 0)])  # removing a level that was never there is fine
    assert book.snapshot()["bids"] == [{"price": 4999.75, "size": 2.0}]


def test_depth_book_current_volume_wins_only_when_non_zero():
    book = DepthBook()
    book.apply([depth(2, 5000.0, volume=10, current=4)])
    assert book.snapshot()["bids"] == [{"price": 5000.0, "size": 4.0}]
    # The live gateway sends currentVolume=0 with the real size in volume (seen 2026-10-26).
    book.apply([depth(2, 5000.0, volume=10, current=0)])
    assert book.snapshot()["bids"] == [{"price": 5000.0, "size": 10.0}]
    book.apply([depth(2, 5000.0, volume=0, current=0)])  # both zero: the level is gone
    assert book.snapshot()["bids"] == []
    book.apply([depth(2, 5000.0, volume=6)])  # no currentVolume: volume is the size
    assert book.snapshot()["bids"] == [{"price": 5000.0, "size": 6.0}]


def test_depth_book_new_best_codes_update_the_ladder():
    book = DepthBook()
    book.apply([depth(9, 5000.0, 1), depth(10, 5000.25, 2)])
    snap = book.snapshot()
    assert snap["best_bid"] == 5000.0 and snap["best_ask"] == 5000.25


def test_depth_book_trade_fill_low_high_and_reset():
    book = DepthBook()
    book.apply([depth(2, 5000.0, 3), depth(1, 5000.25, 4), depth(5, 5000.25, 2), depth(7, 4990.0), depth(8, 5010.0)])
    snap = book.snapshot()
    assert (snap["last_price"], snap["last_size"], snap["low"], snap["high"]) == (5000.25, 2.0, 4990.0, 5010.0)
    book.apply([depth(11, 5000.0, 1)])  # fill
    assert book.snapshot()["last_price"] == 5000.0 and book.snapshot()["last_size"] == 1.0
    before = book.snapshot()["version"]
    book.apply([depth(6, None)])  # reset clears both sides but keeps last/low/high
    snap = book.snapshot()
    assert snap["bids"] == [] and snap["asks"] == [] and snap["best_bid"] is None and snap["best_ask"] is None
    assert snap["last_price"] == 5000.0 and snap["low"] == 4990.0 and snap["high"] == 5010.0
    assert snap["version"] == before + 1


def test_depth_book_best_level_clears_levels_beyond_it():
    book = DepthBook()
    book.apply([depth(2, 4999.75, 5), depth(4, 5000.0, 3), depth(1, 5000.5, 4), depth(3, 5000.25, 2)])
    # The market ticks down: the new best bid is 4999.75 and the new best ask is 5000.0.
    book.apply([depth(4, 4999.75, 6), depth(3, 5000.0, 1)])
    snap = book.snapshot()
    assert snap["bids"] == [{"price": 4999.75, "size": 6.0}]  # the stale 5000.0 bid is gone
    # Asks above the new best ask are still resting; nothing was crossed.
    assert snap["asks"] == [{"price": 5000.0, "size": 1.0}, {"price": 5000.25, "size": 2.0}, {"price": 5000.5, "size": 4.0}]
    assert snap["best_bid"] == 4999.75 and snap["best_ask"] == 5000.0
    # A plain (non-best) bid at 5000.25 crosses the asks at 5000.0 and 5000.25: both go.
    book.apply([depth(2, 5000.25, 9)])
    snap = book.snapshot()
    assert snap["asks"] == [{"price": 5000.5, "size": 4.0}] and snap["best_bid"] == 5000.25


def test_depth_book_never_crosses_on_live_shaped_top_of_book_stream():
    # The gateway's depth stream for simulated data sends one BestBid (4) and one BestAsk (3)
    # per batch and no removals when the touch moves (captured 2026-10-28).
    book = DepthBook()
    book.apply([depth(6, 0, 0, current=0)])
    for bid, ask in [(7776.25, 7776.5), (7776.5, 7776.75), (7776.25, 7776.5), (7776.75, 7777.0), (7777.5, 7777.75)]:
        book.apply([depth(4, bid, 20, current=0), depth(3, ask, 15, current=0)])
        snap = book.snapshot()
        assert snap["best_bid"] == bid and snap["best_ask"] == ask
        assert snap["best_bid"] < snap["best_ask"]
        assert all(b["price"] < ask for b in snap["bids"]) and all(a["price"] > bid for a in snap["asks"])
        assert snap["bids"][0] == {"price": bid, "size": 20.0} and snap["asks"][0] == {"price": ask, "size": 15.0}


def test_depth_book_keeps_session_volume_at_price_from_trade_events():
    book = DepthBook()
    # The replay after subscribing (currentVolume 0) and live trades (currentVolume = this trade's
    # size) both carry the running session volume at that price in ``volume``.
    book.apply([depth(5, 5000.0, volume=120, current=0), depth(5, 5000.25, volume=80, current=0)])
    book.apply([depth(5, 5000.25, volume=81, current=1), depth(2, 5000.0, 7), depth(1, 5000.25, 3)])
    snap = book.snapshot()
    assert snap["traded"] == [{"price": 5000.25, "volume": 81.0}, {"price": 5000.0, "volume": 120.0}]  # highest price first
    assert snap["last_price"] == 5000.25
    book.apply([depth(6, None)])  # reset clears the profile with the book
    assert book.snapshot()["traded"] == []
    # A trade entry without currentVolume (older payload shape) is a trade but not a profile point.
    book.apply([depth(5, 5001.0, volume=2)])
    assert book.snapshot()["traded"] == [] and book.snapshot()["last_size"] == 2.0


def test_depth_book_trade_snapshot_with_zero_current_volume_is_not_a_trade():
    book = DepthBook()
    # Right after subscribing, the feed replays session volume per price as Trade entries with
    # currentVolume 0. Those must not become the "last trade".
    book.apply([depth(5, 7761.75, volume=2798, current=0), depth(5, 7761.5, volume=2251, current=0)])
    snap = book.snapshot()
    assert snap["last_price"] is None and snap["last_size"] is None
    # A real trade carries its size in currentVolume (volume is the running total at that price).
    book.apply([depth(5, 7776.0, volume=1576, current=1)])
    snap = book.snapshot()
    assert (snap["last_price"], snap["last_size"]) == (7776.0, 1.0)


def test_depth_book_ignores_unknown_codes_and_empty_batches():
    book = DepthBook()
    book.apply([])
    book.apply([depth(0, 5000.0, 1), depth(42, 5000.0, 1), DepthEntry(price=None, volume=1, type=2)])
    snap = book.snapshot()
    assert snap["bids"] == [] and snap["asks"] == [] and snap["version"] == 1


def test_depth_book_storage_stays_bounded():
    book = DepthBook()
    book.apply([depth(2, 5000.0 - i * 0.25, 1) for i in range(1500)])
    assert len(book._bids) == 1000
    assert min(book._bids) == 5000.0 - 999 * 0.25  # the levels farthest from the best were dropped


# -- state wiring for depth, trades and tape --------------------------------------------


def test_state_depth_and_tape_are_per_contract():
    state = make_state()
    assert state.depth(ES.id) is None and state.tape_since(ES.id, 0) == ([], 0)
    state.apply_depth(ES.id, [depth(2, 5000.0, 1)])
    state.apply_depth(NQ.id, [depth(1, 20_000.0, 2)])
    assert state.depth(ES.id)["best_bid"] == 5000.0 and state.depth(ES.id)["asks"] == []
    assert state.depth(NQ.id)["best_ask"] == 20_000.0 and state.depth(NQ.id)["bids"] == []
    assert state.depth(ES.id, levels=1)["bids"] == [{"price": 5000.0, "size": 1.0}]


def test_state_apply_trades_feeds_tape_and_depth_last():
    state = make_state()
    state.apply_trades(ES.id, [])
    assert state.depth(ES.id) is None
    state.apply_trades(ES.id, [trade(5000.0, 2, 0), trade(5000.25, 3, 1)])
    events, last = state.tape_since(ES.id, 0)
    assert last == 2 and [(e["price"], e["side"]) for e in events] == [(5000.0, "BUY"), (5000.25, "SELL")]
    book = state.depth(ES.id)
    assert book["last_price"] == 5000.25 and book["last_size"] == 3.0 and book["version"] == 1
    events, last = state.tape_since(ES.id, 2)
    assert events == [] and last == 2


def test_state_apply_quote_merges_and_tapes_only_changes():
    state = make_state()
    state.apply_quote(ES.id, Quote(best_bid=5000.0, best_ask=5000.25, last_price=5000.0))
    state.apply_quote(ES.id, Quote(volume=5))
    state.apply_quote(ES.id, Quote(best_bid=5000.0, best_ask=5000.25, last_price=5000.0))
    state.apply_quote(ES.id, Quote(best_ask=5000.5))
    events, last = state.tape_since(ES.id, 0)
    assert last == 2 and [e["kind"] for e in events] == ["quote", "quote"]
    assert events[1]["ask"] == 5000.5 and events[1]["bid"] == 5000.0 and events[1]["price"] == 5000.0
    assert state.snapshot()["quotes"][ES.id] == {"bid": 5000.0, "ask": 5000.5, "last": 5000.0, "volume": 5}


def test_state_tape_interleaves_trades_and_quotes_in_arrival_order():
    state = make_state()
    state.apply_quote(ES.id, Quote(last_price=5000.0))
    state.apply_trades(ES.id, [trade(5000.25)])
    state.apply_quote(ES.id, Quote(last_price=5000.25))
    events, _ = state.tape_since(ES.id, 0)
    assert [e["kind"] for e in events] == ["quote", "trade", "quote"]
    assert [e["seq"] for e in events] == [1, 2, 3]
