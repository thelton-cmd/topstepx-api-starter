"""Feature: replay a recorded session (``--replay PATH``) and show the recording state (``--record PATH``).

- ``GET /api/replay/status``: ``{active, file, speed, playing, position_s, duration_s, clock, loop,
  subscriptions, ...}``. Not replaying: ``{"active": false, ...}``.
- ``POST /api/replay/control`` ``{action: "play"|"pause"|"speed"|"seek"|"step", value?}``. ``speed`` takes
  a number (multiplier), ``seek`` takes seconds (or ``"fraction": 0..1``), ``step`` dispatches the next
  recorded event while paused. Answers with the new status.
- ``snapshot`` adds ``recording: {active, path, lines}`` while ``--record`` is on.
- ``order_entry_blocked``: replay is a practice and testing tool; every order route answers 403.

The engine is ``dashboard/replay.py``. This module never sends, cancels or closes anything.
"""

from __future__ import annotations

NAME = "replay"
TITLE = "Replay"
DESCRIPTION = "Record the live stream to a file and replay it later at speed, with order entry disabled."
ORDER = 5
JS = "replay.js"
CSS = "replay.css"

BLOCK_REASON = "Replay mode: order entry is disabled."
ACTIONS = ("play", "pause", "speed", "seek", "step")
INACTIVE = {"active": False, "file": None, "speed": 1.0, "playing": False, "position_s": 0.0,
            "duration_s": 0.0, "clock": None, "loop": False, "subscriptions": []}


def _driver(session):
    return getattr(session, "replay", None)


def status(session) -> dict:
    driver = _driver(session)
    return driver.status() if driver is not None else dict(INACTIVE, subscriptions=[])


def order_entry_blocked(session) -> str | None:
    return BLOCK_REASON if _driver(session) is not None else None


def snapshot(session) -> dict:
    out = status(session)
    recorder = getattr(session, "recorder", None)
    if recorder is not None:
        out["recording"] = {"active": bool(recorder.active), "path": str(recorder.path), "lines": recorder.lines}
    return out


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        raise ValueError(f"{name} must be a number")
    return float(value)


def control(session, body: dict) -> tuple[int, dict]:
    action = body.get("action") if isinstance(body, dict) else None
    if action not in ACTIONS:
        raise ValueError("action must be one of: " + ", ".join(ACTIONS))
    driver = _driver(session)
    if driver is None:
        return 200, {**status(session), "ok": False, "note": "Replay mode is not active (start with --replay PATH)."}
    if action == "play":
        driver.play()
    elif action == "pause":
        driver.pause()
    elif action == "speed":
        driver.set_speed(_number(body.get("value"), "value"))
    elif action == "seek":
        if body.get("fraction") is not None:
            fraction = _number(body["fraction"], "fraction")
            if not 0 <= fraction <= 1:
                raise ValueError("fraction must be between 0 and 1")
            target = fraction * driver.duration
        else:
            target = _number(body.get("value"), "value")
            if target < 0:
                raise ValueError("value must be zero or more seconds")
        session.call_async(driver.seek(target), timeout=120.0)
    else:   # step
        if driver.playing:
            raise ValueError("pause before stepping")
        session.call_async(driver.step(), timeout=30.0)
    return 200, {**driver.status(), "ok": True}


def routes(session) -> dict:
    return {"GET": {"/api/replay/status": lambda query: (200, status(session))},
            "POST": {"/api/replay/control": lambda body: control(session, body)}}
