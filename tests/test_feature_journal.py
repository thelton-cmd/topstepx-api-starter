"""Offline tests for the journal feature (dashboard/features/journal.py): routes on the test server
with a fake client returning synthetic Trade objects and bars. Nothing touches the network."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dashboard import features as feature_registry
from dashboard.features import journal
from tests.test_dashboard_features import install
from tests.test_dashboard_server import ES, as_json, bar, dash, server_mod, static_dir, utc  # noqa: F401 - fixtures
from topstepx import RateLimitError, Trade

NOW = utc(2026, 10, 28, 15, 0)


def fill(id, minute, side, size, price, pnl=None, fees=0.5, day=27, hour=14):
    return Trade(id=id, account_id=7, contract_id=ES.id, price=price, size=size, side=0 if side == "B" else 1,
                 profit_and_loss=pnl, fees=fees, commissions=0.0, voided=False,
                 creation_timestamp=utc(2026, 10, day, hour, minute))


FILLS = [fill(1, 0, "B", 1, 5000.0), fill(2, 10, "S", 1, 5001.0, pnl=50.0),
         fill(3, 20, "S", 2, 5002.0), fill(4, 30, "B", 2, 5003.0, pnl=-100.0, fees=1.0)]


class JournalClient:
    def __init__(self, fills=FILLS):
        self.fills = list(fills)
        self.trade_calls: list[tuple] = []
        self.bar_calls: list[tuple] = []
        self.fail_bars: Exception | None = None

    def search_trades(self, account_id, start, end=None):
        self.trade_calls.append((account_id, start, end))
        return list(self.fills)

    def get_bars(self, contract_id, start, end, unit=None, unit_number=5, limit=1000, **kw):
        self.bar_calls.append((contract_id, start, end, int(unit), unit_number, limit))
        if self.fail_bars:
            raise self.fail_bars
        return [bar(utc(2026, 10, 27, 14, m), o=5000.0, c=5001.0) for m in range(0, 12)]


@pytest.fixture
def jdash(dash, monkeypatch, tmp_path):
    monkeypatch.setattr(journal, "_now", lambda: NOW)
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    dash.session.client = JournalClient()
    install(dash.session, journal)
    return dash


def test_feature_is_well_formed():
    assert journal in feature_registry.discover("journal")
    info = feature_registry.describe(journal)
    assert (info["name"], info["js"], info["css"], info["order"]) == ("journal", "journal.js", "journal.css", 60)
    static = Path(journal.__file__).resolve().parents[1] / "static" / "features"
    assert (static / "journal.js").is_file() and (static / "journal.css").is_file()
    assert (Path(journal.__file__).resolve().parents[2] / "docs" / "features" / "journal.md").is_file()
    assert not feature_registry.order_routes([journal])


def test_journal_route_shape(jdash):
    status, body, _ = jdash.get("/api/journal?days=5")
    assert status == 200
    data = as_json(body)
    assert set(data) >= {"since", "account", "trades_count", "round_trips", "daily", "stats", "heatmap", "note"}
    assert data["account"] == {"id": 7, "name": "PRAC-1", "simulated": True}
    assert data["trades_count"] == 4 and len(data["round_trips"]) == 2
    assert data["stats"]["closed_with_gain"] == 1 and data["stats"]["closed_with_loss"] == 1
    assert data["daily"][0]["date"] == "2026-10-27" and data["daily"][0]["round_trips"] == 2
    assert data["since"].startswith("2026-10-23T15:00")
    assert len(data["heatmap"]["net"]) == 7 and data["heatmap"]["offset_minutes"] == 0
    assert "approximation" in data["note"].lower()
    account_id, start, end = jdash.session.client.trade_calls[0]
    assert account_id == 7 and start == NOW - timedelta(days=5)


def test_heatmap_timezone_and_validation(jdash):
    data = as_json(jdash.get("/api/journal?days=5&tz=-300")[1])
    assert data["heatmap"]["offset_minutes"] == -300
    for bad in ("days=0", "days=91", "days=abc", "days=-1", "days=5&tz=9999", "days=5&tz=x"):
        assert jdash.get("/api/journal?" + bad)[0] == 400, bad
    assert as_json(jdash.get("/api/journal")[1])["since"].startswith("2026-10-23")   # default 5 days


def test_fills_are_cached_per_account_and_days(jdash):
    client = jdash.session.client
    jdash.get("/api/journal?days=5")
    jdash.get("/api/journal?days=5&tz=60")
    assert len(client.trade_calls) == 1
    jdash.get("/api/journal?days=20")
    assert len(client.trade_calls) == 2


def test_cache_expires_after_thirty_seconds(jdash, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(journal, "_monotonic", lambda: clock["t"])
    jdash.get("/api/journal?days=5")
    clock["t"] += 29
    jdash.get("/api/journal?days=5")
    assert len(jdash.session.client.trade_calls) == 1
    clock["t"] += 2
    jdash.get("/api/journal?days=5")
    assert len(jdash.session.client.trade_calls) == 2


def test_todays_state_fills_are_merged_without_duplicates(jdash):
    state = jdash.session.state
    payload = {"id": 99, "accountId": 7, "contractId": ES.id, "price": 5010.0, "size": 1, "side": 0,
               "creationTimestamp": "2026-10-27T16:00:00+00:00", "fees": 0.5, "commissions": 0.0}
    state.apply_trade(payload)
    state.apply_trade({**payload, "id": 1})      # same id as a REST fill: must not double count
    data = as_json(jdash.get("/api/journal?days=5")[1])
    assert data["trades_count"] == 5 and len(data["open_lots"]) == 1 and len(data["round_trips"]) == 2


def test_rate_limit_on_fills_is_503(jdash):
    def boom(*a, **k):
        raise RateLimitError("/Trade/search", 3)
    jdash.session.client.search_trades = boom
    status, body, _ = jdash.get("/api/journal?days=5")
    assert status == 503 and "rate limiting" in as_json(body)["error"]


def test_excursion_route_and_cache(jdash):
    trip_id = as_json(jdash.get("/api/journal?days=5")[1])["round_trips"][0]["id"]
    assert trip_id == "1-2"
    status, body, _ = jdash.get(f"/api/journal/excursion?id={trip_id}&days=5")
    data = as_json(body)
    assert status == 200 and data["cached"] is False and data["bars_used"] > 0
    assert data["mfe_points"] == 1.5 and data["mae_points"] == 0.5     # bars: high 5001.5 / low 4999.5 vs entry 5000.0
    assert data["money_available"] is True and data["mfe_money"] == 1.5 / 0.25 * 12.5
    client = jdash.session.client
    assert len(client.bar_calls) == 1
    contract, start, end, unit, unit_number, limit = client.bar_calls[0]
    assert (contract, unit, unit_number) == (ES.id, 2, 1) and start < utc(2026, 10, 27, 14, 0) and end > utc(2026, 10, 27, 14, 10)
    again = as_json(jdash.get(f"/api/journal/excursion?id={trip_id}&days=5")[1])
    assert again["cached"] is True and len(client.bar_calls) == 1


def test_excursion_errors(jdash):
    assert jdash.get("/api/journal/excursion?days=5")[0] == 400
    assert jdash.get("/api/journal/excursion?id=1-2&days=0")[0] == 400
    assert jdash.get("/api/journal/excursion?id=nope&days=5")[0] == 404
    jdash.session.client.fail_bars = RateLimitError("/History/retrieveBars", 2)
    status, body, _ = jdash.get("/api/journal/excursion?id=1-2&days=5")
    assert status == 503 and "rate limiting" in as_json(body)["error"]
    jdash.session.client.fail_bars = None          # a failure is not cached: the next try works
    assert jdash.get("/api/journal/excursion?id=1-2&days=5")[0] == 200


def test_export_writes_csv_into_the_journal_folder(jdash, tmp_path):
    assert jdash.post("/api/journal/export", {"days": 5}, token=False)[0] == 403
    assert jdash.post("/api/journal/export", {"days": 0})[0] == 400
    status, body, _ = jdash.post("/api/journal/export", {"days": 5})
    data = as_json(body)
    assert status == 200 and data["rows"] == 2
    path = Path(data["path"])
    assert path.parent == (tmp_path / "journal").resolve() and path.name == "dashboard_7_20261023_20261028.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["side"] for r in rows] == ["LONG", "SHORT"]
    assert rows[0]["net"] == "49.0" and rows[1]["net"] == "-101.5"


def test_snapshot_counts_todays_closed_round_trips(jdash):
    state = jdash.session.state
    for payload in (
        {"id": 1, "accountId": 7, "contractId": ES.id, "price": 5000.0, "size": 1, "side": 0,
         "creationTimestamp": "2026-10-28T14:00:00+00:00", "fees": 1.0, "commissions": 0.0},
        {"id": 2, "accountId": 7, "contractId": ES.id, "price": 5001.0, "size": 1, "side": 1,
         "creationTimestamp": "2026-10-28T14:05:00+00:00", "fees": 1.0, "commissions": 0.0, "profitAndLoss": 50.0},
    ):
        state.apply_trade(payload)
    snap = journal.snapshot(jdash.session)
    assert snap == {"today_round_trips": 1, "today_net": 48.0}
