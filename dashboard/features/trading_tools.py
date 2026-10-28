"""Trading tools: "Flatten all" / "Cancel all" for the selected Practice Account, plus the page
module for optional keyboard shortcuts (off by default; see docs/features/trading.md).

Both routes are explicit user actions: the page lists exactly what it will close or cancel, the
trader confirms the way they chose in Settings, and the request carries the ids they reviewed.
Both paths are in ORDER_ROUTES, so the server refuses them unless the selected account is a
Practice Account (simulated + can_trade) and the body has "confirm": "yes"; the helpers in
dashboard/orders.py check the account again before every request.

POST /api/positions/flatten   {"account_id", "contract_ids": [...], "order_ids": [...], "confirm": "yes"}
    Closes each listed position (Position/closeContract), then cancels each listed order
    (Order/cancel). If a close fails, that contract's orders are left working and reported as
    "kept". Failures never stop the batch.
POST /api/orders/cancel_all   {"account_id", "order_ids": [...], "confirm": "yes"}
    Cancels each listed order.

Both answer 200 with a report: {"ok", "action", "account", "account_id", "positions": [...],
"orders": [...], "sent", "failed", "not_found", "kept", "not_included": {"positions", "orders"}}.
Each item has a "status": "sent" (the gateway accepted the request), "failed" (with "error"),
"not_found" (the gateway says it was already closed / cancelled) or "kept". "not_included"
lists what the dashboard sees open now that was not in the reviewed list (it opened after the
summary was shown); nothing is sent for those.

This module never sends anything on its own initiative: no timers, no hub callbacks.
"""

from __future__ import annotations

from .. import orders

NAME = "trading_tools"
TITLE = "Trading tools"
DESCRIPTION = ("Flatten all / Cancel all for the selected Practice Account, and optional keyboard "
               "shortcuts (off by default).")
ORDER = 40
JS = "hotkeys.js"
CSS = "hotkeys.css"
FLATTEN_PATH = "/api/positions/flatten"
CANCEL_ALL_PATH = "/api/orders/cancel_all"
ORDER_ROUTES = {FLATTEN_PATH, CANCEL_ALL_PATH}


def _account(session, body: dict) -> dict:
    """The selected account, which must be the one the page asked about."""
    account = session.state.selected_account()
    if account is None:
        raise ValueError("No account is selected.")
    account_id = body.get("account_id")
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        raise ValueError("account_id must be a whole number.")
    if account_id != account["id"]:
        raise ValueError("The request names a different account than the one selected on the page. Reload the page.")
    return account


def _open_now(session) -> tuple[list[dict], list[dict]]:
    snap = session.state.snapshot()
    positions = [p for p in snap.get("positions") or [] if isinstance(p, dict) and p.get("contract_id")]
    open_orders = [o for o in snap.get("orders") or [] if isinstance(o, dict) and o.get("id") is not None]
    return positions, open_orders


def _reviewed_orders(order_ids: list[int], open_orders: list[dict]) -> list[dict]:
    """Rows for the reviewed ids; an id the dashboard no longer sees is still attempted."""
    by_id = {o["id"]: o for o in open_orders}
    return [{"id": oid, "contract_id": (by_id.get(oid) or {}).get("contract_id")} for oid in order_ids]


def flatten(session, body: dict) -> tuple[int, dict]:
    account = _account(session, body)
    contract_ids = orders.parse_id_list(body, "contract_ids", str)
    order_ids = orders.parse_id_list(body, "order_ids", int)
    if not contract_ids and not order_ids:
        raise ValueError("Nothing to flatten: no positions or orders were listed.")
    positions, open_orders = _open_now(session)
    report = orders.flatten_account(session.client, account, contract_ids,
                                    _reviewed_orders(order_ids, open_orders))
    report["not_included"] = {
        "positions": [p["contract_id"] for p in positions if p["contract_id"] not in contract_ids],
        "orders": [o["id"] for o in open_orders if o["id"] not in order_ids],
    }
    return 200, report


def cancel_all(session, body: dict) -> tuple[int, dict]:
    account = _account(session, body)
    order_ids = orders.parse_id_list(body, "order_ids", int)
    if not order_ids:
        raise ValueError("Nothing to cancel: no orders were listed.")
    _positions, open_orders = _open_now(session)
    report = orders.cancel_orders(session.client, account, _reviewed_orders(order_ids, open_orders))
    report["not_included"] = {
        "positions": [],
        "orders": [o["id"] for o in open_orders if o["id"] not in order_ids],
    }
    return 200, report


def routes(session) -> dict:
    return {
        "POST": {
            FLATTEN_PATH: lambda body: flatten(session, body),
            CANCEL_ALL_PATH: lambda body: cancel_all(session, body),
        }
    }
