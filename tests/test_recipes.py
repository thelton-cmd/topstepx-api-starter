"""Offline tests for the example helpers and the 08-10 recipes' pure logic.

Example files start with digits, so they are loaded by path. The examples
directory goes on sys.path because they import the shared ``_safety`` helper.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from topstepx import Bar, Contract, Trade

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES))

import _safety  # noqa: E402


def load_example(filename: str):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), EXAMPLES / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chart = load_example("08_chart_bars.py")
journal = load_example("09_trade_journal.py")
risk = load_example("10_risk_monitor.py")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# -- contract selection -------------------------------------------------------


class FakeClient:
    def __init__(self, contracts):
        self._contracts = contracts

    def search_contracts(self, text, live=False):
        return self._contracts


ES = Contract(id="CON.F.US.EP.Z26", name="ESZ26", description="E-mini S&P 500", active_contract=True)
MES = Contract(id="CON.F.US.MES.Z26", name="MESZ26", description="Micro E-mini S&P 500", active_contract=True)
ES_NEXT = Contract(id="CON.F.US.EP.H27", name="ESH27", description="E-mini S&P 500", active_contract=True)


@pytest.mark.parametrize(
    "name, value, expected",
    [("ESZ26", "ES", True), ("esz26", "ES", True), ("MESZ26", "ES", False), ("ES", "ES", False), ("ESZ2026", "ES", False)],
)
def test_root_symbol_match(name, value, expected):
    assert _safety.is_root_symbol_match(name, value) is expected


def test_root_symbol_beats_other_text_matches():
    assert _safety.choose_active_contract(FakeClient([MES, ES]), "ES") is ES


def test_ambiguous_choice_raises_instead_of_prompting_without_a_terminal(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(ValueError, match="CON.F.US.EP.Z26"):
        _safety.choose_active_contract(FakeClient([ES, ES_NEXT, MES]), "ES")


def test_closed_stdin_that_claims_to_be_a_terminal_still_raises(monkeypatch):
    # Windows reports the null device (an agent shell's stdin) as a TTY.
    def closed_input(prompt=""):
        raise EOFError

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", closed_input)
    with pytest.raises(ValueError, match="CON.F.US.EP.Z26"):
        _safety.choose_active_contract(FakeClient([ES, ES_NEXT, MES]), "ES")


# -- 08 chart -------------------------------------------------------------------


def bar(t: datetime, o=1.0, h=2.0, l=0.5, c=1.5, v=10) -> Bar:
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def test_chart_series_sorts_oldest_first_and_drops_duplicate_times():
    newest_first = [bar(utc(2026, 10, 26, 14, 10)), bar(utc(2026, 10, 26, 14, 5), c=0.9), bar(utc(2026, 10, 26, 14, 5))]
    candles, volume = chart.chart_series(newest_first)
    times = [c["time"] for c in candles]
    assert times == sorted(set(times)) and len(times) == 2
    assert [v["time"] for v in volume] == times


def test_chart_html_embeds_data_safely_and_pins_the_library():
    page = chart.render_chart_html("ES </script><b>", "sub", [bar(utc(2026, 10, 26, 14, 5))])
    assert "</script><b>" not in page
    assert chart.LIGHTWEIGHT_CHARTS_SRI in page and "lightweight-charts@5.2.1" in page
    assert '"open": 1.0' in page


def test_lookback_window_is_bounded():
    end = utc(2026, 10, 26)
    assert end - chart.lookback_start(end, 60, 5, 10) >= timedelta(days=4)
    assert end - chart.lookback_start(end, 2_629_800, 1, 20_000) <= timedelta(days=3650)


# -- 09 journal -----------------------------------------------------------------


def trade(id, t, pnl=None, fees=1.0, commissions=0.0, voided=False, side=0) -> Trade:
    return Trade(id=id, contract_id="CON.F.US.EP.Z26", price=5000.0, size=1, side=side,
                 profit_and_loss=pnl, fees=fees, commissions=commissions, voided=voided, creation_timestamp=t)


def test_journal_rows_are_oldest_first_with_net_only_on_closing_fills():
    rows = journal.journal_rows([trade(2, utc(2026, 10, 26, 15), pnl=50.0, side=1), trade(1, utc(2026, 10, 26, 14))])
    assert [r["trade_id"] for r in rows] == [1, 2]
    assert rows[0]["pnl"] == "" and rows[0]["net"] == "" and rows[0]["side"] == "BUY"
    assert rows[1]["net"] == 49.0 and rows[1]["side"] == "SELL"
    assert set(rows[0]) == set(journal.COLUMNS)


def test_daily_summary_groups_by_utc_day_and_skips_voided():
    trades = [
        trade(1, utc(2026, 10, 26, 14)),
        trade(2, utc(2026, 10, 26, 15), pnl=50.0, commissions=0.5),
        trade(3, utc(2026, 10, 27, 1), pnl=-20.0),
        trade(4, utc(2026, 10, 27, 2), pnl=999.0, voided=True),
    ]
    day1, day2 = journal.daily_summary(trades)
    assert (day1["date"], day1["fills"], day1["round_turns"], day1["pnl"]) == ("2026-10-26", 2, 1, 50.0)
    assert day1["net"] == pytest.approx(50.0 - 2.0 - 0.5)
    assert (day2["date"], day2["fills"], day2["pnl"]) == ("2026-10-27", 1, -20.0)


# -- 10 risk monitor --------------------------------------------------------------


ES_SPEC = Contract(id="CON.F.US.EP.Z26", name="ESZ26", tick_size=0.25, tick_value=12.5)


def make_book(since=utc(2026, 10, 26)):
    return risk.RiskBook(lambda contract_id: ES_SPEC, since)


def test_unrealized_pnl_long_and_short():
    book = make_book()
    book.apply_position({"contractId": ES_SPEC.id, "type": 1, "size": 2, "averagePrice": 5000.0})
    book.apply_quote(ES_SPEC.id, 5001.0)  # +4 ticks x $12.50 x 2 contracts
    assert book.unrealized() == (100.0, [])

    book.apply_position({"data": {"contractId": ES_SPEC.id, "type": 2, "size": 1, "averagePrice": 5000.0}})
    assert book.unrealized()[0] == -50.0  # short 1, price up 4 ticks
    assert book.open_contracts == 1


def test_flat_position_is_removed_and_missing_price_is_reported():
    book = make_book()
    book.apply_position({"contractId": ES_SPEC.id, "type": 1, "size": 1, "averagePrice": 5000.0})
    assert book.unrealized() == (0.0, [ES_SPEC.id])
    book.apply_position({"contractId": ES_SPEC.id, "type": 1, "size": 0, "averagePrice": 0})
    assert book.positions == {} and book.open_contracts == 0


def test_trades_count_once_and_respect_since_and_voided():
    book = make_book()
    fill = {"id": 1, "profitAndLoss": -100.0, "fees": 2.0, "commissions": 1.0, "creationTimestamp": "2026-10-26T14:00:00Z"}
    book.apply_trade(fill)
    book.apply_trade({"data": fill})  # same fill again via the stream
    book.apply_trade({"id": 2, "profitAndLoss": -500.0, "creationTimestamp": "2026-10-25T14:00:00Z"})  # before since
    book.apply_trade({"id": 3, "profitAndLoss": -500.0, "voided": True})
    assert (book.realized, book.costs) == (-100.0, 3.0)


def test_alerts_fire_once_and_clear():
    book = make_book()
    book.apply_trade({"id": 1, "profitAndLoss": -480.0, "fees": 25.0})
    assert book.check(max_loss=500, max_contracts=None) == ["ALERT  P&L -505.00 is at or below -500.00"]
    assert book.check(max_loss=500, max_contracts=None) == []  # still breached: no repeat
    book.apply_trade({"id": 2, "profitAndLoss": 100.0})
    assert book.check(max_loss=500, max_contracts=None) == ["CLEAR  back within your loss limit"]

    book.apply_position({"contractId": ES_SPEC.id, "type": 1, "size": 4, "averagePrice": 5000.0})
    assert book.check(max_loss=None, max_contracts=3) == ["ALERT  4 open contracts exceeds 3"]
