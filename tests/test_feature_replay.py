"""Offline tests for the replay feature (routes, order-entry block, snapshot), the --record/--replay
CLI flags and the Session wiring."""

from __future__ import annotations

import asyncio
import contextlib
import io
from argparse import Namespace
from pathlib import Path

import pytest

from dashboard import app, cli, replay
from dashboard import features as feature_registry
from dashboard.features import replay as feature
from tests.test_dashboard_replay import ES, Rig, START, sample_lines, write_recording
from tests.test_dashboard_server import as_json, dash, static_dir, ticket  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]


def install(session):
    session.features = [feature]
    session.feature_routes = feature_registry.collect_routes(session.features, session)


def install_replay(session, **driver_kwargs):
    rig = Rig(**driver_kwargs)
    install(session)
    session.replay = rig.driver
    return rig


# -- module metadata -----------------------------------------------------------------------------


def test_feature_metadata_and_assets_exist():
    assert feature.NAME == "replay" and feature.ORDER == 5
    for name in (feature.JS, feature.CSS):
        assert (ROOT / "dashboard" / "static" / "features" / name).is_file()
    assert (ROOT / "docs" / "features" / "replay.md").is_file()
    source = Path(feature.__file__).read_text(encoding="utf-8")
    for call in ("place_order", "cancel_order", "modify_order", "close_position"):
        assert call not in source


# -- routes ----------------------------------------------------------------------------------------


def test_status_is_inactive_when_not_replaying(dash):
    install(dash.session)
    status, body, _ = dash.get("/api/replay/status")
    data = as_json(body)
    assert status == 200 and data["active"] is False and data["file"] is None and data["subscriptions"] == []
    assert set(data) >= {"active", "file", "speed", "playing", "position_s", "duration_s", "clock", "loop", "subscriptions"}
    snap = as_json(dash.get("/api/state")[1])
    assert snap["features"]["replay"]["active"] is False and "recording" not in snap["features"]["replay"]
    assert snap["order_entry_allowed"] is True      # a live session is not blocked
    status, body, _ = dash.post("/api/replay/control", {"action": "pause"})
    assert status == 200 and as_json(body)["ok"] is False and as_json(body)["active"] is False


def test_control_requires_the_token_and_a_valid_action(dash):
    install(dash.session)
    assert dash.post("/api/replay/control", {"action": "play"}, token=False)[0] == 403
    for bad in ({}, {"action": "rewind"}, {"action": 5}, {"action": None}):
        status, body, _ = dash.post("/api/replay/control", bad)
        assert status == 400 and "action must be one of" in as_json(body)["error"], bad


def test_control_play_pause_speed_seek_and_step(dash):
    rig = install_replay(dash.session)
    status, body, _ = dash.get("/api/replay/status")
    data = as_json(body)
    assert data["active"] is True and data["file"] == "rec.ndjson" and data["playing"] is True
    assert data["duration_s"] == 30.0 and data["speed"] == 1.0 and data["clock"] == START.isoformat(timespec="seconds")

    data = as_json(dash.post("/api/replay/control", {"action": "pause"})[1])
    assert data["ok"] is True and data["playing"] is False and rig.driver.playing is False
    data = as_json(dash.post("/api/replay/control", {"action": "play"})[1])
    assert data["playing"] is True
    data = as_json(dash.post("/api/replay/control", {"action": "speed", "value": 10})[1])
    assert data["speed"] == 10.0

    dash.post("/api/replay/control", {"action": "pause"})
    data = as_json(dash.post("/api/replay/control", {"action": "seek", "value": 5})[1])
    assert data["position_s"] == 5.0 and data["clock"].endswith("14:00:05+00:00")
    data = as_json(dash.post("/api/replay/control", {"action": "seek", "fraction": 0.5})[1])
    assert data["position_s"] == 15.0
    data = as_json(dash.post("/api/replay/control", {"action": "seek", "value": 1})[1])     # backwards
    assert data["position_s"] == 1.0
    before = rig.driver.index
    data = as_json(dash.post("/api/replay/control", {"action": "step"})[1])
    assert rig.driver.index == before + 1 and data["position_s"] >= 2.0

    for body in ({"action": "speed", "value": 0}, {"action": "speed"}, {"action": "speed", "value": "fast"},
                 {"action": "speed", "value": 5000}, {"action": "seek", "value": -1}, {"action": "seek"},
                 {"action": "seek", "fraction": 2}, {"action": "seek", "value": True}):
        status, resp, _ = dash.post("/api/replay/control", body)
        assert status == 400 and "error" in as_json(resp), body
    dash.post("/api/replay/control", {"action": "play"})
    status, resp, _ = dash.post("/api/replay/control", {"action": "step"})
    assert status == 400 and "pause" in as_json(resp)["error"]


def test_replay_blocks_every_order_route_and_reports_it(dash):
    install_replay(dash.session)
    snap = as_json(dash.get("/api/state")[1])
    assert snap["order_entry_allowed"] is False and snap["order_entry_block"] == "Replay mode: order entry is disabled."
    assert snap["features"]["replay"]["active"] is True
    status, body, _ = dash.post("/api/orders/preflight", ticket())
    assert status == 403 and as_json(body) == {"ok": False, "error": "Replay mode: order entry is disabled."}
    for path, payload in (("/api/orders", {**ticket(), "confirm": "yes"}),
                          ("/api/orders/cancel", {"order_id": 1, "confirm": "yes"}),
                          ("/api/positions/close", {"contract_id": ES, "confirm": "yes"})):
        status, body, _ = dash.post(path, payload)
        assert status == 403, path
    assert dash.session.client.place_calls == [] and dash.session.client.cancel_calls == []
    assert feature.order_entry_blocked(Namespace(replay=None)) is None


def test_snapshot_reports_the_recorder_while_recording(dash, tmp_path):
    install(dash.session)
    rec = replay.Recorder(tmp_path / "live.ndjson", announce=None).start()
    dash.session.recorder = rec
    try:
        info = as_json(dash.get("/api/state")[1])["features"]["replay"]
        assert info["active"] is False
        assert info["recording"] == {"active": True, "path": str(tmp_path / "live.ndjson"), "lines": 0}
    finally:
        rec.stop()
    assert as_json(dash.get("/api/state")[1])["features"]["replay"]["recording"]["active"] is False


# -- CLI ---------------------------------------------------------------------------------------------------


def test_cli_record_replay_speed_and_loop_flags(tmp_path):
    rec = write_recording(tmp_path / "rec.ndjson")
    plain = cli.parse_args(["--port", "0"])
    assert plain.record is None and plain.replay is None and plain.speed == 1.0 and plain.replay_loop is False
    args = cli.parse_args(["--replay", str(rec), "--speed", "5", "--replay-loop", "--port", "0"])
    assert args.replay == str(rec) and args.speed == 5.0 and args.replay_loop is True
    assert cli.parse_args(["--replay", str(rec)]).speed == 1.0
    assert cli.parse_args(["--record", str(tmp_path / "new.ndjson.gz")]).record == str(tmp_path / "new.ndjson.gz")
    for bad in (["--record", str(tmp_path / "x.ndjson"), "--replay", str(rec)],     # both modes
                ["--replay", str(tmp_path / "missing.ndjson")],                       # no such file
                ["--record", str(rec)],                                               # would overwrite
                ["--replay", str(rec), "--speed", "0"], ["--replay", str(rec), "--speed", "-2"],
                ["--replay", str(rec), "--speed", "5000"], ["--replay", str(rec), "--speed", "nan"],
                ["--speed", "2"], ["--replay-loop"]):                                 # need --replay
        with pytest.raises(SystemExit):
            cli.parse_args(bad)


def test_startup_lines_name_the_mode_and_stay_ascii(dash, tmp_path):
    session = dash.session
    session.watch_ids = [ES]
    session.args = Namespace(unit="minute", unit_number=5, bars=300, refresh=60.0, refresh_focus=5.0,
                             replay="rec.ndjson", speed=5.0, replay_loop=True, record=None)
    session.replay = object()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.print_startup(session, "http://127.0.0.1:8773/")
    text = out.getvalue()
    text.encode("ascii")
    assert "Mode: REPLAY of rec.ndjson at 5x, looping" in text and "Order entry: disabled (replay mode)" in text
    assert "Practice Accounts only" not in text

    session.replay = None
    session.args.replay, session.args.record = None, "out.ndjson"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.print_startup(session, "http://127.0.0.1:8773/")
    assert "Mode: live, recording to out.ndjson" in out.getvalue() and "Order entry: enabled" in out.getvalue()


# -- Session wiring ------------------------------------------------------------------------------------------


def session_args(**extra):
    base = dict(unit="minute", unit_number=5, bars=300, refresh=60.0, refresh_focus=5.0, watch=["ES", "NQ"],
                since=None, account_id=None, features="none", replay=None, record=None, speed=1.0, replay_loop=False)
    base.update(extra)
    return Namespace(**base)


def test_session_replay_runs_offline_and_blocks_orders_even_with_features_none(tmp_path):
    path = write_recording(tmp_path / "rec.ndjson.gz")
    session = app.Session(session_args(replay=str(path), speed=50.0))
    assert isinstance(session.client, replay.ReplayClient) and session.recorder is None
    assert [app.feature_registry.name_of(m) for m in session.features] == ["replay"]   # forced on
    assert session.args.watch == [ES]                                                 # the recording's own list

    async def go():
        await session.start()
        await asyncio.sleep(0.5)
        status = feature.status(session)
        snapshot = session.state.snapshot()
        await session.stop()
        return status, snapshot

    status, snapshot = asyncio.run(go())
    assert status["active"] is True and status["position_s"] > 0
    assert snapshot["account"]["name"] == "PRACTICE7" and snapshot["connection"] == {"market": True, "user": True}
    assert snapshot["quotes"][ES]["last"] is not None and snapshot["focus"] == ES
    assert any(s.startswith("market SubscribeContractQuotes") for s in status["subscriptions"])
    assert feature.order_entry_blocked(session) == "Replay mode: order entry is disabled."


def test_session_record_observes_both_hubs_and_the_client(tmp_path, monkeypatch):
    class FakeClient:
        tokens = None

        def __init__(self):
            self._http = type("Http", (), {"observers": [], "add_observer": lambda s, cb: s.observers.append(cb)})()

        def close(self):
            pass

    out = tmp_path / "rec.ndjson"
    client = FakeClient()
    monkeypatch.setattr(app, "TopstepXClient", lambda: client)
    with contextlib.redirect_stdout(io.StringIO()) as said:
        session = app.Session(session_args(record=str(out)))
        assert session.recorder is not None and session.replay is None
        client._http.observers[0]({"path": "/Account/search", "request": {}, "error": None,
                                   "response": {"success": True, "accounts": []}})
        asyncio.run(session.market._event_handlers["GatewayQuote"]([ES, {"lastPrice": 1.0}]))
        asyncio.run(session.stop())
    assert "Recording to" in said.getvalue() and "Recording saved" in said.getvalue()
    rows = replay.Recording.load(out)
    assert len(rows.events) == 1 and len(rows.rest) == 1
