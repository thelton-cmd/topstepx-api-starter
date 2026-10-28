"""Offline tests for the pure journal maths (dashboard/journal.py) with hand-built fills."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dashboard import journal as jr
from topstepx import Trade


def t(minute, side, size, price, pnl=None, fees=0.0, contract="ES", id=None, day=26, hour=14, second=0):
    when = datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc)
    return Trade(id=id if id is not None else minute * 100 + second, contract_id=contract, price=price, size=size,
                 side=0 if side == "B" else 1, profit_and_loss=pnl, fees=fees, commissions=0.0,
                 voided=False, creation_timestamp=when)


def test_long_round_trip_with_fees_and_hold_time():
    fills = [t(0, "B", 1, 100.0, fees=1.0, id=1), t(5, "S", 1, 101.0, pnl=50.0, fees=1.0, id=2)]
    (trip,) = jr.round_trips(fills)
    assert (trip["side"], trip["size"], trip["entry_price"], trip["exit_price"]) == ("long", 1, 100.0, 101.0)
    assert (trip["pnl"], trip["fees"], trip["net"], trip["hold_seconds"]) == (50.0, 2.0, 48.0, 300.0)
    assert trip["fill_ids"] == [1, 2] and trip["id"] == "1-2" and trip["pnl_known"] is True


def test_short_round_trip_loss():
    (trip,) = jr.round_trips([t(0, "S", 2, 100.0, fees=2.0, id=1), t(1, "B", 2, 101.0, pnl=-100.0, fees=2.0, id=2)])
    assert trip["side"] == "short" and trip["size"] == 2 and trip["net"] == -104.0


def test_scale_in_gives_one_trip_per_entry_fill_with_fee_split():
    fills = [t(0, "B", 1, 100.0, fees=1.0, id=1), t(1, "B", 1, 102.0, fees=1.0, id=2),
             t(5, "S", 2, 103.0, pnl=30.0, fees=2.0, id=3)]
    a, b = jr.round_trips(fills)
    assert (a["entry_price"], a["pnl"], a["fees"]) == (100.0, 15.0, 2.0)     # 1.0 entry + 1.0 of the exit's 2.0
    assert (b["entry_price"], b["pnl"], b["fees"]) == (102.0, 15.0, 2.0)
    assert a["net"] + b["net"] == 26.0


def test_scale_out_and_remaining_lot():
    fills = [t(0, "B", 3, 100.0, fees=3.0, id=1), t(2, "S", 1, 101.0, pnl=10.0, fees=1.0, id=2),
             t(4, "S", 1, 102.0, pnl=20.0, fees=1.0, id=3)]
    paired = jr.pair_fills(fills)
    first, second = paired["round_trips"]
    assert first["size"] == 1 and first["fees"] == 2.0 and second["fees"] == 2.0   # 1.0 entry share + 1.0 exit
    assert paired["open_lots"] == [{"contract_id": "ES", "side": "long", "size": 1,
                                    "entry_time": "2026-10-26T14:00:00+00:00", "entry_price": 100.0}]


def test_fifo_uses_oldest_lot_first():
    fills = [t(0, "B", 1, 100.0, id=1), t(1, "B", 1, 110.0, id=2), t(2, "S", 1, 105.0, pnl=5.0, id=3)]
    (trip,) = jr.round_trips(fills)
    assert trip["entry_price"] == 100.0 and trip["fill_ids"] == [1, 3]


def test_flip_closes_then_opens_other_side():
    fills = [t(0, "B", 1, 100.0, fees=1.0, id=1), t(1, "S", 3, 101.0, pnl=9.0, fees=3.0, id=2),
             t(2, "B", 2, 100.0, pnl=20.0, fees=2.0, id=3)]
    first, second = jr.round_trips(fills)
    assert first["side"] == "long" and first["size"] == 1 and first["pnl"] == pytest.approx(3.0)   # 1/3 of 9.0
    assert first["fees"] == pytest.approx(2.0)                                                       # 1.0 + 1/3 of 3.0
    assert second["side"] == "short" and second["size"] == 2 and second["entry_price"] == 101.0
    assert second["fees"] == pytest.approx(2.0 + 2.0)      # 2/3 of the flip fill's 3.0, plus the closing fill's 2.0


def test_interleaved_contracts_pair_separately():
    fills = [t(0, "B", 1, 100.0, contract="ES", id=1), t(1, "S", 1, 20.0, contract="NQ", id=2),
             t(2, "S", 1, 101.0, pnl=50.0, contract="ES", id=3), t(3, "B", 1, 19.0, pnl=20.0, contract="NQ", id=4)]
    es, nq = jr.round_trips(fills)
    assert (es["contract_id"], es["side"]) == ("ES", "long") and (nq["contract_id"], nq["side"]) == ("NQ", "short")


def test_unmatched_closing_fill_voided_and_unusable_fills():
    voided = t(0, "B", 1, 100.0, id=9)
    voided.voided = True
    fills = [t(1, "S", 1, 101.0, pnl=5.0, id=1), voided, t(2, "B", 0, 100.0, id=3),
             Trade(id=4, contract_id="ES", price=None, size=1, side=0,
                   creation_timestamp=datetime(2026, 10, 26, tzinfo=timezone.utc))]
    paired = jr.pair_fills(fills)
    assert paired["round_trips"] == [] and paired["unmatched"] == 1 and paired["open_lots"] == []


def test_missing_pnl_on_closing_fill_is_flagged():
    (trip,) = jr.round_trips([t(0, "B", 1, 100.0, id=1), t(1, "S", 1, 101.0, id=2)])
    assert trip["pnl"] == 0.0 and trip["pnl_known"] is False


def test_accepts_state_dicts_with_string_sides():
    fills = [{"id": 1, "time": "2026-10-26T14:00:00+00:00", "contract_id": "ES", "side": "BUY", "size": 1,
              "price": 100.0, "pnl": None, "fees": 1.0},
             {"id": 2, "time": "2026-10-26T14:01:00Z", "contract_id": "ES", "side": "SELL", "size": 1,
              "price": 101.0, "pnl": 50.0, "fees": 1.0}]
    (trip,) = jr.round_trips(fills)
    assert trip["net"] == 48.0 and trip["hold_seconds"] == 60.0


def _trips():
    fills = [
        t(0, "B", 1, 100.0, fees=1.0, id=1), t(5, "S", 1, 101.0, pnl=50.0, fees=1.0, id=2),          # gain 48
        t(10, "B", 1, 100.0, id=3), t(15, "S", 1, 99.0, pnl=-50.0, id=4),                             # loss -50
        t(20, "S", 1, 100.0, id=5), t(25, "B", 1, 100.0, pnl=0.0, id=6),                              # flat
        t(0, "B", 1, 100.0, id=7, day=27), t(1, "S", 1, 103.0, pnl=150.0, fees=2.0, id=8, day=27),    # gain 148
    ]
    return jr.round_trips(fills)


def test_daily_summary_and_stats():
    trips = _trips()
    daily = jr.daily_summary(trips)
    assert [d["date"] for d in daily] == ["2026-10-26", "2026-10-27"]
    assert daily[0] == {"date": "2026-10-26", "round_trips": 3, "gains": 1, "losses": 1, "flat": 1,
                        "gross": 0.0, "fees": 2.0, "net": -2.0}
    s = jr.stats(trips)
    assert (s["round_trips"], s["closed_with_gain"], s["closed_with_loss"], s["flat"]) == (4, 2, 1, 1)
    assert (s["gross"], s["fees"], s["net"], s["avg_net"]) == (150.0, 4.0, 146.0, 36.5)
    assert (s["largest_gain"], s["largest_loss"]) == (148.0, -50.0)
    assert (s["longest_gain_run"], s["longest_loss_run"]) == (1, 1)
    assert s["avg_hold_seconds"] == (300 + 300 + 300 + 60) / 4
    assert s["contracts"][0]["contract_id"] == "ES" and s["contracts"][0]["round_trips"] == 4


def test_stats_empty_and_runs():
    s = jr.stats([])
    assert s["round_trips"] == 0 and s["avg_net"] is None and s["largest_gain"] is None
    fills = []
    for i, pnl in enumerate([10, 20, 30, -5, -5, 7]):
        fills += [t(i * 2, "B", 1, 100.0, id=i * 2 + 1), t(i * 2 + 1, "S", 1, 100.0, pnl=pnl, id=i * 2 + 2)]
    s = jr.stats(jr.round_trips(fills))
    assert (s["longest_gain_run"], s["longest_loss_run"]) == (3, 2)


def test_heatmap_utc_boundaries_and_offset():
    # 2026-10-26 is a Monday: a trip closes at 23:59:59 UTC Monday, another at 00:00:01 UTC Tuesday.
    fills = [t(0, "B", 1, 100.0, hour=23, id=1), t(59, "S", 1, 100.0, pnl=10.0, hour=23, second=59, id=2),
             t(0, "B", 1, 100.0, hour=0, day=27, id=3), t(0, "S", 1, 100.0, pnl=-4.0, hour=0, day=27, second=1, id=4)]
    trips = jr.round_trips(fills)
    h = jr.heatmap(trips)
    assert h["net"][0][23] == 10.0 and h["count"][0][23] == 1
    assert h["net"][1][0] == -4.0 and h["count"][1][0] == 1
    shifted = jr.heatmap(trips, tz_offset_minutes=-300)   # UTC-5: Mon 18:59 and Mon 19:00
    assert shifted["count"][0][18] == 1 and shifted["count"][0][19] == 1
    assert sum(map(sum, shifted["count"])) == 2 and shifted["offset_minutes"] == -300
    assert len(h["net"]) == 7 and len(h["net"][0]) == 24 and h["weekdays"][0] == "Mon"


def _bar(minute, high, low, hour=14):
    return {"time": datetime(2026, 10, 26, hour, minute, tzinfo=timezone.utc), "high": high, "low": low}


def test_excursion_long_with_and_without_tick_value():
    (trip,) = jr.round_trips([t(1, "B", 2, 100.0, id=1, second=30), t(4, "S", 2, 101.0, pnl=0.0, id=2)])
    bars = [_bar(0, 150.0, 90.0),                       # ends 14:01:00, before the 14:01:30 entry: ignored
            _bar(1, 102.0, 99.0), _bar(2, 103.0, 98.5), _bar(4, 101.5, 100.5),
            _bar(5, 200.0, 50.0)]                       # starts after the exit: ignored
    r = jr.excursion(trip, bars, 0.25, 12.5)
    assert (r["mfe_points"], r["mae_points"], r["bars_used"]) == (3.0, 1.5, 3)
    assert r["money_available"] is True
    assert r["mfe_money"] == 3.0 / 0.25 * 12.5 * 2 and r["mae_money"] == 1.5 / 0.25 * 12.5 * 2
    r = jr.excursion(trip, bars)
    assert r["money_available"] is False and r["mfe_money"] is None and "points" in r["note"]


def test_excursion_short_clamps_and_empty():
    (trip,) = jr.round_trips([t(0, "S", 1, 100.0, id=1), t(3, "B", 1, 99.0, pnl=0.0, id=2)])
    r = jr.excursion(trip, [_bar(0, 100.0, 98.0), _bar(1, 100.0, 99.0)], 0.25, 12.5)
    assert (r["mfe_points"], r["mae_points"]) == (2.0, 0.0)     # price never went above the entry
    empty = jr.excursion(trip, [])
    assert empty["mfe_points"] is None and "No bars" in empty["note"]


def test_to_csv_rows_columns_and_order():
    rows = jr.to_csv_rows(_trips())
    assert list(rows[0]) == jr.CSV_COLUMNS and len(rows) == 4
    assert rows[0]["side"] == "LONG" and rows[0]["net"] == 48.0
    assert rows[0]["entry_trade_id"] == 1 and rows[0]["exit_trade_id"] == 2
    assert [r["exit_time_utc"] for r in rows] == sorted(r["exit_time_utc"] for r in rows)
