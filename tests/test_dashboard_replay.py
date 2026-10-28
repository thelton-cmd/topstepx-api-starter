"""Offline tests for dashboard/replay.py: the recorder, the loaded recording, the replay driver
(driving the real MarketHub / UserHub objects, no network) and the replay client."""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dashboard import replay
from dashboard.state import DashboardState
from topstepx import BarUnit, TopstepXError
from topstepx.models import Account, DepthEntry, Quote
from topstepx.realtime.market import MarketHub
from topstepx.realtime.user import UserHub

ES = "CON.F.US.EP.Z26"
NQ = "CON.F.US.ENQ.Z26"
START = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
ACCOUNT = {"id": 7, "name": "PRACTICE7", "balance": 50000.0, "canTrade": True, "isVisible": True, "simulated": True}
ES_CONTRACT = {"id": ES, "name": "ESZ6", "description": "E-mini S&P", "tickSize": 0.25, "tickValue": 12.5,
               "activeContract": True, "symbolId": "F.US.EP"}
NQ_CONTRACT = {"id": NQ, "name": "NQZ6", "description": "E-mini Nasdaq", "tickSize": 0.25, "tickValue": 5.0,
               "activeContract": True, "symbolId": "F.US.ENQ"}


class FakeTokens:
    def get_token(self):
        return "fake-token"


def ok(**body):
    return {"success": True, "errorCode": 0, "errorMessage": None, **body}


def hub_line(t, hub, name, args, direction="in"):
    return {"t": t, "wall": (START + timedelta(seconds=t)).isoformat(), "hub": hub, "dir": direction,
            "name": name, "args": args}


def rest_line(t, path, request, response):
    return {"t": t, "wall": (START + timedelta(seconds=t)).isoformat(), "kind": "rest", "path": path,
            "request": request, "response": response}


def bar_raw(minute, close=100.0):
    return {"t": (START + timedelta(minutes=minute)).isoformat(), "o": 99.0, "h": 101.0, "l": 98.0, "c": close, "v": 10}


def sample_lines():
    """A small recording: setup REST answers, subscriptions, then market and user events interleaved."""
    return [
        {"kind": "meta", "version": 1, "started": START.isoformat()},
        rest_line(0.1, "/Account/search", {"onlyActiveAccounts": True}, ok(accounts=[ACCOUNT])),
        rest_line(0.2, "/Contract/search", {"searchText": "ES", "live": False}, ok(contracts=[ES_CONTRACT])),
        rest_line(0.3, "/Contract/search", {"searchText": "NQ", "live": False}, ok(contracts=[NQ_CONTRACT])),
        rest_line(0.4, "/History/retrieveBars", {"contractId": ES, "unit": 2, "unitNumber": 5},
                  ok(bars=[bar_raw(10), bar_raw(5), bar_raw(0)])),   # newest first, like the gateway
        rest_line(30.0, "/History/retrieveBars", {"contractId": ES, "unit": 2, "unitNumber": 5},
                  ok(bars=[bar_raw(20), bar_raw(15), bar_raw(10), bar_raw(5), bar_raw(0)])),
        rest_line(0.5, "/Position/searchOpen", {"accountId": 7},
                  ok(positions=[{"id": 1, "accountId": 7, "contractId": ES, "type": 1, "size": 1, "averagePrice": 100.0}])),
        hub_line(0.6, "market", "SubscribeContractQuotes", [ES], "out"),
        hub_line(0.7, "market", "SubscribeContractMarketDepth", [ES], "out"),
        hub_line(0.8, "user", "SubscribeOrders", [7], "out"),
        hub_line(1.0, "market", "GatewayQuote", [ES, {"lastPrice": 100.0, "bestBid": 99.75, "bestAsk": 100.0}]),
        hub_line(2.0, "user", "GatewayUserOrder", [{"action": 1, "data": {"id": 11, "accountId": 7, "contractId": ES,
                                                                          "status": 1, "type": 1, "side": 0, "size": 1}}]),
        hub_line(3.0, "market", "GatewayDepth", [ES, [{"price": 99.75, "volume": 12, "type": 2, "currentVolume": 12}]]),
        hub_line(4.0, "market", "GatewayQuote", [ES, {"lastPrice": 100.25}]),
        hub_line(10.0, "market", "GatewayQuote", [ES, {"lastPrice": 101.0}]),
    ]


def write_recording(path, lines=None):
    text = "".join(json.dumps(row) + "\n" for row in (lines if lines is not None else sample_lines()))
    if str(path).endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


class Rig:
    """Real hubs + a driver + callback logs, in the order the dashboard registers them."""

    def __init__(self, lines=None, **driver_kwargs):
        self.recording = replay.Recording.from_lines(lines if lines is not None else sample_lines())
        self.client = replay.ReplayClient(self.recording)
        self.driver = replay.ReplayDriver(self.recording, self.client, path="rec.ndjson", **driver_kwargs)
        self.market = MarketHub(FakeTokens())
        self.user = UserHub(FakeTokens())
        self.state = DashboardState(START - timedelta(days=1))
        self.state.set_accounts([Account.from_api(ACCOUNT)])
        self.state.select_account(7)
        self.log: list[tuple] = []
        self.market.on_quote(lambda cid, q: self.log.append(("quote", cid, q)))
        self.market.on_depth(lambda cid, entries: self.log.append(("depth", cid, entries)))
        self.user.on_order(lambda payload: self.log.append(("order", payload)))
        self.market.on_quote(self.state.apply_quote)
        self.market.on_depth(self.state.apply_depth)
        self.market.on_trade(self.state.apply_trades)
        self.user.on_order(self.state.apply_order)
        self.driver.attach(self.market, self.user, self.state)


def run(coro):
    return asyncio.run(coro)


# -- recorder -----------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def wall(self):
        return START + timedelta(seconds=self.now - 100.0)


def read_lines(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_recorder_writes_hub_and_rest_lines_and_flushes_on_stop(tmp_path):
    clock = FakeClock()
    said: list[str] = []
    rec = replay.Recorder(tmp_path / "out" / "rec.ndjson", clock=clock, wall=clock.wall, announce=said.append).start()
    assert rec.active and said == [f"Recording to {tmp_path / 'out' / 'rec.ndjson'}"]
    clock.now = 101.5
    rec.record_hub("market", "in", "GatewayQuote", [ES, {"lastPrice": 100.0}])
    clock.now = 102.0
    rec.record_hub("user", "out", "SubscribeOrders", [7])
    rec.record_rest({"path": "/Account/search", "request": {"onlyActiveAccounts": True}, "error": None, "status": 200,
                     "response": ok(accounts=[ACCOUNT])})
    summary = rec.stop()
    assert rec.stop() is summary                       # idempotent
    rows = read_lines(tmp_path / "out" / "rec.ndjson")
    assert rows[0]["kind"] == "meta" and rows[0]["version"] == 1
    quote, sub, rest = rows[1:]
    assert quote == {"t": 1.5, "wall": (START + timedelta(seconds=1.5)).isoformat(), "hub": "market", "dir": "in",
                     "name": "GatewayQuote", "args": [ES, {"lastPrice": 100.0}]}
    assert sub["hub"] == "user" and sub["dir"] == "out" and sub["name"] == "SubscribeOrders" and sub["args"] == [7]
    assert rest["kind"] == "rest" and rest["path"] == "/Account/search" and rest["response"]["accounts"][0]["id"] == 7
    assert summary["hub"] == 2 and summary["rest"] == 1 and summary["lines"] == 3 and summary["dropped"] == 0
    assert summary["bytes"] == (tmp_path / "out" / "rec.ndjson").stat().st_size
    assert not rec.active
    assert said[-1].startswith("Recording saved:") and "2 hub messages" in said[-1] and "1 REST answers" in said[-1]
    for line in said:
        line.encode("ascii")
    rec.record_hub("market", "in", "GatewayQuote", [ES, {}])   # after stop: ignored, no crash
    assert len(read_lines(tmp_path / "out" / "rec.ndjson")) == 4


def test_recorder_gzip_and_refuses_to_overwrite(tmp_path):
    path = tmp_path / "rec.ndjson.gz"
    rec = replay.Recorder(path, announce=None).start()
    for i in range(50):
        rec.record_hub("market", "in", "GatewayQuote", [ES, {"lastPrice": float(i)}])
    rec.stop()
    assert path.read_bytes()[:2] == b"\x1f\x8b"
    assert len(read_lines(path)) == 51
    with pytest.raises(ValueError, match="already exists"):
        replay.Recorder(path, announce=None).start()


def test_recorder_flushes_about_every_interval_without_stopping(tmp_path):
    import time

    path = tmp_path / "live.ndjson"
    rec = replay.Recorder(path, flush_interval=0.05, announce=None).start()
    try:
        rec.record_hub("market", "in", "GatewayQuote", [ES, {"lastPrice": 1.0}])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(read_lines(path)) < 2:
            time.sleep(0.05)
        assert len(read_lines(path)) == 2 and rec.lines == 1
    finally:
        rec.stop()


def test_recorder_redacts_secrets_and_keeps_only_the_needed_rest_paths(tmp_path):
    rec = replay.Recorder(tmp_path / "r.ndjson", announce=None).start()
    rec.record_rest({"path": "/Account/search", "error": None,
                     "request": {"userName": "me", "apiKey": "SECRET-KEY", "nested": [{"Password": "pw"}]},
                     "response": ok(token="SECRET-JWT", accounts=[ACCOUNT])})
    rec.record_rest({"path": "/Auth/loginKey", "error": None, "request": {"apiKey": "SECRET-KEY"},
                     "response": ok(token="SECRET-JWT")})                      # not a recorded path
    rec.record_rest({"path": "/Account/search", "error": "boom", "request": {}, "response": ok()})   # failed call
    rec.record_rest({"path": "/Account/search", "error": None, "request": {}, "response": "not json"})
    rec.record_hub("market", "in", "GatewayQuote", [ES, {"token": "SECRET-JWT"}])
    rec.stop()
    text = (tmp_path / "r.ndjson").read_text(encoding="utf-8")
    assert "SECRET" not in text and "pw" not in text.replace('"pw"', "")
    rows = read_lines(tmp_path / "r.ndjson")
    assert len(rows) == 3 and rows[1]["request"]["apiKey"] == "<redacted>" and rows[1]["response"]["token"] == "<redacted>"
    assert rows[1]["request"]["nested"][0]["Password"] == "<redacted>"
    assert rows[1]["request"]["userName"] == "me"


def test_recorder_never_blocks_and_counts_drops(tmp_path):
    rec = replay.Recorder(tmp_path / "r.ndjson", announce=None, max_queue=1)
    rec._thread = object()   # noqa: SLF001 - pretend started so the producer path runs with no writer draining
    rec.record_hub("market", "in", "A", [])
    rec.record_hub("market", "in", "B", [])
    assert rec._dropped == 1   # noqa: SLF001


def test_hub_and_http_observers_feed_the_recorder(tmp_path):
    market, user = MarketHub(FakeTokens()), UserHub(FakeTokens())
    http_observers = []
    client = SimpleNamespace(_http=SimpleNamespace(add_observer=http_observers.append))
    args = SimpleNamespace(record=str(tmp_path / "rec.ndjson"))
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        rec = replay.attach_recorder(args, client, market, user)
    run(market._event_handlers["GatewayQuote"]([ES, {"lastPrice": 5.0}]))
    run(user._event_handlers["GatewayUserOrder"]([{"id": 1}]))
    http_observers[0]({"path": "/Contract/search", "request": {"searchText": "ES"}, "error": None,
                       "response": ok(contracts=[ES_CONTRACT])})
    with contextlib.redirect_stdout(io.StringIO()):
        summary = rec.stop()
    assert summary["hub"] == 2 and summary["rest"] == 1
    rows = read_lines(tmp_path / "rec.ndjson")
    assert [r.get("hub") for r in rows[1:3]] == ["market", "user"]


# -- the loaded recording -------------------------------------------------------------------------------


def test_recording_load_sorts_skips_bad_lines_and_survives_truncation(tmp_path):
    lines = sample_lines()
    text = "".join(json.dumps(r) + "\n" for r in reversed(lines[1:])) + "not json\n[1, 2]\n" + json.dumps({"t": -1}) + "\n"
    path = tmp_path / "messy.ndjson"
    path.write_text(json.dumps(lines[0]) + "\n" + text, encoding="utf-8")
    rec = replay.Recording.load(path)
    assert [e.t for e in rec.events] == sorted(e.t for e in rec.events) and len(rec.events) == 5
    assert rec.skipped == 3 and rec.started == START and rec.duration == 30.0
    assert rec.clock_at(90) == START + timedelta(seconds=90)

    gz = write_recording(tmp_path / "cut.ndjson.gz")
    gz.write_bytes(gz.read_bytes()[:-12])          # a recording whose process was killed
    cut = replay.Recording.load(gz)
    assert cut.truncated is True

    with pytest.raises(ValueError, match="not found"):
        replay.Recording.load(tmp_path / "nope.ndjson")


def test_recording_without_a_meta_line_derives_the_start_from_the_first_wall_time():
    rec = replay.Recording.from_lines(sample_lines()[1:])
    assert rec.started == START
    assert rec.watch_ids() == [ES]   # depth focus first, then the other quoted contracts


# -- the replay driver with real hubs ---------------------------------------------------------------------


def test_hubs_become_connected_and_swallow_subscriptions():
    rig = Rig()
    changes = []
    rig.market.on_connection_change(changes.append)

    async def go():
        await rig.market.start()
        await rig.user.start()
        await rig.market.subscribe_quotes(ES)
        await rig.market.subscribe_depth(ES)
        await rig.user.subscribe_orders(7)
        await rig.market.unsubscribe_depth(ES)
        await rig.user.subscribe_accounts()

    run(go())
    assert rig.market.connected and rig.user.connected and changes == [True]
    assert rig.driver.status()["subscriptions"] == [
        f"market SubscribeContractQuotes {ES}", "user SubscribeOrders 7", "user SubscribeAccounts"]
    assert ("SubscribeContractQuotes", (ES,)) in rig.market._subscriptions
    assert ("SubscribeContractMarketDepth", (ES,)) not in rig.market._subscriptions


def test_events_reach_real_callbacks_parsed_and_interleaved_by_time():
    rig = Rig()

    async def go():
        await rig.driver.advance(3.5)

    run(go())
    kinds = [entry[0] for entry in rig.log]
    assert kinds == ["quote", "order", "depth"]            # t = 1.0, 2.0, 3.0: market and user interleaved
    quote, order, depth = rig.log
    assert quote[1] == ES and isinstance(quote[2], Quote) and quote[2].last_price == 100.0
    assert order[1]["data"]["id"] == 11
    assert isinstance(depth[2][0], DepthEntry) and depth[2][0].price == 99.75
    assert rig.driver.position == 3.5 and rig.driver.index == 3


def test_pause_play_speed_and_end():
    rig = Rig()

    async def go():
        rig.driver.pause()
        await rig.driver.advance(5.0)
        assert rig.log == [] and rig.driver.position == 0.0
        rig.driver.play()
        rig.driver.set_speed(10)
        await rig.driver.advance(0.45)                     # 4.5 s of recording
        assert [e[0] for e in rig.log] == ["quote", "order", "depth", "quote"] and rig.driver.speed == 10
        await rig.driver.advance(100)                      # to the end
        return rig.driver.status()

    status = run(go())
    assert rig.driver.index == 5 and status["playing"] is False and status["ended"] is True
    assert status["position_s"] == 30.0 and status["duration_s"] == 30.0
    rig.driver.play()
    assert rig.driver.playing is False                     # ended and not looping: play does nothing
    for bad in (0, -1, True, float("nan"), 5000, "5"):
        with pytest.raises(ValueError):
            rig.driver.set_speed(bad)


def test_step_dispatches_one_event_while_paused():
    rig = Rig(playing=False)

    async def go():
        assert await rig.driver.step() is True
        assert [e[0] for e in rig.log] == ["quote"] and rig.driver.position == 1.0
        assert await rig.driver.step() is True
        assert [e[0] for e in rig.log] == ["quote", "order"] and rig.driver.position == 2.0
        for _ in range(10):
            await rig.driver.step()
        return await rig.driver.step()

    assert run(go()) is False


def test_seek_forward_and_backward_keeps_state_consistent():
    rig = Rig()

    async def go():
        await rig.driver.seek(5.0)
        first = rig.state.snapshot()
        depth = rig.state.depth(ES)
        await rig.driver.seek(11.0)
        later = rig.state.snapshot()
        await rig.driver.seek(1.5)                          # backwards: reset, then replay up to 1.5 s
        back = rig.state.snapshot()
        back_depth = rig.state.depth(ES)
        return first, depth, later, back, back_depth

    first, depth, later, back, back_depth = run(go())
    assert first["quotes"][ES]["last"] == 100.25 and later["quotes"][ES]["last"] == 101.0
    assert [o["id"] for o in first["orders"]] == [11]
    assert depth is not None and any(level.get("price") == 99.75 for level in depth["bids"])
    assert back["quotes"][ES]["last"] == 100.0 and back["orders"] == []         # the t=2 order is not in yet
    assert back_depth is not None and not back_depth["bids"]   # an empty book, so /api/depth still answers 200
    assert [p["contract_id"] for p in back["positions"]] == [ES]                # re-seeded from the recording
    assert rig.driver.position == 1.5 and rig.driver.index == 1


def test_rewind_keeps_the_tape_sequence_rising():
    rig = Rig()

    async def go():
        await rig.driver.seek(5.0)
        seq_before = rig.state.tape_since(ES, 0)[1]
        await rig.driver.seek(0.0)
        await rig.driver.seek(5.0)
        return seq_before, rig.state.tape_since(ES, 0)[1]

    before, after = run(go())
    assert after >= before > 0
    with pytest.raises(ValueError):
        run(rig.driver.seek(float("nan")))


def test_seek_is_clamped_to_the_recording():
    rig = Rig()
    run(rig.driver.seek(1e9))
    assert rig.driver.position == 30.0
    run(rig.driver.seek(-5))
    assert rig.driver.position == 0.0 and rig.driver.index == 0


def test_loop_restarts_from_the_beginning():
    rig = Rig(loop=True)

    async def go():
        await rig.driver.advance(40)                        # past the end
        return rig.driver.status()

    status = run(go())
    assert status["loops"] == 1 and status["playing"] is True and status["ended"] is False
    assert status["position_s"] == 0.0 and rig.driver.index == 0
    assert [e[0] for e in rig.log].count("quote") == 3      # the first pass ran in full


def test_a_failing_callback_never_stops_the_replay():
    rig = Rig()
    rig.market.on_quote(lambda cid, q: 1 / 0)
    run(rig.driver.advance(5))
    assert rig.driver.index == 4 and [e[0] for e in rig.log].count("quote") == 2


def test_the_clock_task_runs_on_its_own():
    rig = Rig(speed=100.0)

    async def go():
        rig.driver.begin()
        await asyncio.sleep(0.4)
        await rig.driver.close()

    run(go())
    assert rig.driver.position > 0 and rig.log


# -- the replay client ----------------------------------------------------------------------------------------


def test_client_answers_accounts_contracts_and_seeds_from_the_recording():
    rig = Rig()
    client = rig.client
    assert [a.id for a in client.search_accounts(True)] == [7]
    assert [c.id for c in client.search_contracts("ES")] == [ES]
    assert [c.id for c in client.search_contracts("nq")] == [NQ]
    assert [c.id for c in client.search_contracts("Nasdaq")] == [NQ]      # matched against known contracts
    assert {c.id for c in client.list_available_contracts()} == {ES, NQ}
    assert client.get_contract(ES).tick_value == 12.5
    assert [p.contract_id for p in client.search_open_positions(7)] == [ES]
    assert client.search_open_positions(8) == [] and client.search_open_orders(7) == []
    assert client.search_trades(7, START) == [] and client.search_orders(7, START) == []
    for forbidden in ("place_order", "cancel_order", "modify_order", "close_position", "partial_close_position"):
        assert not hasattr(client, forbidden)


def test_client_bars_grow_with_the_replay_clock_and_come_back_newest_first():
    rig = Rig()
    start, end = START - timedelta(days=1), START + timedelta(days=1)
    kwargs = dict(unit=BarUnit.MINUTE, unit_number=5, limit=100)

    def times():
        return [b.time for b in rig.client.get_bars(ES, start, end, **kwargs)]

    # t = 0: only bars at or before the clock (14:00) from the first answer.
    assert times() == [START]
    rig.driver.position = 12 * 60                    # clock 14:12: bars 14:00, 14:05, 14:10 (all in the first answer)
    assert times() == [START + timedelta(minutes=m) for m in (10, 5, 0)]
    rig.driver.position = 31.0                       # the second recorded answer is now "known"...
    assert times() == [START]                        # ...but its later bars are still ahead of the clock
    rig.driver.position = 20 * 60 + 1                # clock 14:20:01 and past the second answer
    assert times()[0] == START + timedelta(minutes=20) and len(times()) == 5
    assert len(rig.client.get_bars(ES, start, end, unit=BarUnit.MINUTE, unit_number=5, limit=2)) == 2
    cutoff = START + timedelta(minutes=12)
    assert [b.time for b in rig.client.get_bars(ES, start, cutoff, **kwargs)][0] == START + timedelta(minutes=10)


def test_client_reports_missing_data_instead_of_inventing_it():
    notes = []
    rig = Rig()
    rig.client.on_note = notes.append
    with pytest.raises(TopstepXError, match="no recorded bars"):
        rig.client.get_bars(ES, START, START + timedelta(hours=1), unit=BarUnit.HOUR, unit_number=1)
    with pytest.raises(TopstepXError, match="no recorded bars"):
        rig.client.get_bars(NQ, START, START + timedelta(hours=1), unit=BarUnit.MINUTE, unit_number=5)
    assert rig.client.search_contracts("ZZZ") == [] and "no credentials" in notes[0]
    assert rig.client.get_contract("CON.UNKNOWN").id == "CON.UNKNOWN" and len(notes) == 2
    rig.client.search_contracts("ZZZ")
    assert len(notes) == 2                           # each note is shown once


def test_client_falls_back_to_a_live_client_only_for_lookups():
    class Live:
        def search_contracts(self, text, live=False):
            return [replay.Contract.from_api({"id": "CON.LIVE", "name": text, "activeContract": True})]

        def search_accounts(self, only_active=True):
            raise AssertionError("recorded accounts must win")

        def get_bars(self, *args, **kwargs):
            return [replay.Bar.from_api(bar_raw(0))]

        def search_open_positions(self, account_id):
            raise AssertionError("positions must never come from the live account in a replay")

        def close(self):
            self.closed = True

    rig = Rig()
    rig.client.fallback = live = Live()
    assert [c.id for c in rig.client.search_contracts("RTY")] == ["CON.LIVE"]
    assert [a.id for a in rig.client.search_accounts()] == [7]
    assert len(rig.client.get_bars(NQ, START, START + timedelta(hours=1), unit=BarUnit.MINUTE, unit_number=5)) == 1
    assert len(rig.client.search_open_positions(7)) == 1
    rig.client.close()
    assert live.closed is True


def test_replay_client_fits_the_dashboard_session_surface():
    client = Rig().client
    assert client.tokens is not None and callable(client._http.add_observer)   # noqa: SLF001
    client._http.add_observer(lambda record: None)   # noqa: SLF001 - features attach REST observers
