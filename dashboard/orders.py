"""Order ticket validation, preflight summary, the practice-only guard, and the send helpers.

This is the API-mechanics half of order entry. Every parameter comes from the browser form
exactly as the user typed it; nothing here chooses a side, a price, a size, a bracket distance
or a time. The rules mirror examples/07_place_order_demo.py: explicit parameters, a preflight
summary, and a typed "yes" (checked in server.py) before anything is sent.

Orders are only ever sent to an account the API marks ``simulated: true`` (a Practice
Account) that is also ``canTrade: true``. Funded/live accounts are refused here and again in
server.py, whatever the browser sends.

Brackets: the trader may type a stop distance and a target distance in ticks. They are sent as
the gateway's own ``stopLossBracket`` / ``takeProfitBracket`` objects (``{"ticks", "type"}``,
Swagger ``PlaceOrderBracket``). The gateway only accepts them when the account's bracket mode
is "Auto OCO Brackets" (TopstepX, Settings > Risk Settings); in the default "Position Brackets"
mode it rejects the order with errorCode 2. The spec does not document the sign of ``ticks``;
other API users report that the gateway wants them signed from the entry (a buy's stop below =
negative, its target above = positive; a sell the reverse) and refuses a wrong sign.
``bracket_requests`` is the one place that mapping lives.
"""

from __future__ import annotations

import math
import re
import sys
import traceback
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from topstepx import OrderSide, OrderType, TopstepXError

WARNING = "Topstep states that API orders are final and cannot be reviewed or reversed."
PRACTICE_ONLY = "Order entry is only enabled for Practice Accounts (the API marks them simulated)."
BRACKET_NOTE = (
    "Brackets are sent as the gateway's stopLossBracket / takeProfitBracket. They need the "
    "account's bracket mode set to Auto OCO Brackets in TopstepX (Settings > Risk Settings); in "
    "Position Brackets mode (the default) the gateway rejects the order. The gateway prices the "
    "legs from the actual fill."
)
MAX_SIZE = 50
MAX_BRACKET_TICKS = 1000
MAX_BATCH_ITEMS = 200  # flatten / cancel-all: more ids than this in one request is refused

SIDES = {"buy": OrderSide.BUY, "sell": OrderSide.SELL}
ORDER_TYPES = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop": OrderType.STOP,
    "stop_limit": OrderType.STOP_LIMIT,
}
# Which price fields each type requires; every other price field is forbidden.
REQUIRED_PRICES = {
    "market": set(),
    "limit": {"limit_price"},
    "stop": {"stop_price"},
    "stop_limit": {"limit_price", "stop_price"},
}
PRICE_LABELS = {"limit_price": "Limit price", "stop_price": "Stop price"}
TICK_LABELS = {"stop_ticks": "Stop (ticks)", "target_ticks": "Target (ticks)"}
# The order type of each bracket leg: the protective stop rests as a stop, the target as a limit.
BRACKET_TYPES = {"stop_loss": OrderType.STOP, "take_profit": OrderType.LIMIT}
# Gateway error code meaning "that position / order does not exist (any more)" on both
# Position/closeContract (PositionNotFound) and Order/cancel (OrderNotFound).
NOT_FOUND_CODE = 2


@dataclass
class OrderTicket:
    """Exactly what the browser sent, already type-checked."""

    account_id: int
    contract_id: str
    side: str  # "buy" | "sell"
    order_type: str  # "market" | "limit" | "stop" | "stop_limit"
    size: int
    limit_price: float | None = None
    stop_price: float | None = None
    stop_ticks: int | None = None  # protective stop distance from the entry, in ticks (None = no stop leg)
    target_ticks: int | None = None  # target distance from the entry, in ticks (None = no target leg)


# -- field parsing ---------------------------------------------------------------------


def _int_field(body: dict, name: str, label: str) -> int:
    value = body.get(name)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number.")
    return value


def _price_field(body: dict, name: str) -> float | None:
    value = body.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    label = PRICE_LABELS[name]
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise ValueError(f"{label} must be a number.") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return value


def _ticks_message(name: str) -> str:
    return f"{TICK_LABELS[name]} must be a whole number from 1 to {MAX_BRACKET_TICKS}, or blank for none."


def _ticks_field(body: dict, name: str) -> int | None:
    """A bracket distance: blank/None = no leg, else a whole number of ticks from 1 to 1000."""
    value = body.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValueError(_ticks_message(name))
        value = int(text)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_BRACKET_TICKS:
        raise ValueError(_ticks_message(name))
    return value


def _choice_field(body: dict, name: str, label: str, choices: dict) -> str:
    value = body.get(name)
    if not isinstance(value, str) or value.strip().lower() not in choices:
        options = ", ".join(choices)
        raise ValueError(f"{label} must be one of: {options}.")
    return value.strip().lower()


def parse_ticket(body: dict) -> OrderTicket:
    """Turn the browser's JSON into an OrderTicket, or raise ValueError with a plain message."""
    if not isinstance(body, dict):
        raise ValueError("The order ticket must be a JSON object.")
    contract_id = body.get("contract_id")
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("Choose a contract.")
    ticket = OrderTicket(
        account_id=_int_field(body, "account_id", "Account id"),
        contract_id=contract_id.strip(),
        side=_choice_field(body, "side", "Side", SIDES),
        order_type=_choice_field(body, "order_type", "Order type", ORDER_TYPES),
        size=_int_field(body, "size", "Size"),
        limit_price=_price_field(body, "limit_price"),
        stop_price=_price_field(body, "stop_price"),
        stop_ticks=_ticks_field(body, "stop_ticks"),
        target_ticks=_ticks_field(body, "target_ticks"),
    )
    return ticket


# -- preflight -------------------------------------------------------------------------


def _require_tick_aligned(price: float | None, tick_size: float | None, label: str) -> None:
    """Reject prices that cannot be represented as whole contract ticks.

    Copied from examples/_safety.require_tick_aligned so the dashboard package does not
    depend on the examples directory being on sys.path.
    """
    if price is None:
        return
    if tick_size is None or tick_size <= 0:
        raise ValueError("The API did not return a usable tick size for this contract.")
    try:
        ticks = Decimal(str(price)) / Decimal(str(tick_size))
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError(f"Could not validate {label} against tick size {tick_size}.") from exc
    if ticks != ticks.to_integral_value():
        raise ValueError(f"{label} {price} is not aligned to this contract's {tick_size} tick size.")


def _is_practice(account: object) -> bool:
    return (
        isinstance(account, dict)
        and account.get("simulated") is True
        and account.get("can_trade") is True
    )


def _require_practice(account: object) -> dict:
    """The one rule that never bends: only a simulated, tradable account may send anything."""
    if not isinstance(account, dict) or account.get("id") is None:
        raise PermissionError("No account is selected.")
    if not _is_practice(account):
        raise PermissionError(PRACTICE_ONLY)
    return account


def _usable_price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def last_price_from(snapshot: object, contract_id: str) -> float | None:
    """The last traded price for a contract from a state snapshot's ``quotes``, or None."""
    quotes = snapshot.get("quotes") if isinstance(snapshot, dict) else None
    quote = quotes.get(contract_id) if isinstance(quotes, dict) else None
    return _usable_price(quote.get("last")) if isinstance(quote, dict) else None


def has_brackets(ticket: OrderTicket) -> bool:
    return ticket.stop_ticks is not None or ticket.target_ticks is not None


def bracket_requests(ticket: OrderTicket) -> tuple[dict | None, dict | None]:
    """The gateway's ``stopLossBracket`` and ``takeProfitBracket`` objects (None = not sent).

    The trader types distances; the gateway wants ticks signed from the entry: a buy's stop
    is below (negative) and its target above (positive), a sell the reverse. Types: the stop
    leg is a Stop order (4), the target leg a Limit order (1).
    """
    direction = 1 if ticket.side == "buy" else -1
    stop_loss = None
    take_profit = None
    if ticket.stop_ticks is not None:
        stop_loss = {"ticks": -direction * ticket.stop_ticks, "type": int(BRACKET_TYPES["stop_loss"])}
    if ticket.target_ticks is not None:
        take_profit = {"ticks": direction * ticket.target_ticks, "type": int(BRACKET_TYPES["take_profit"])}
    return stop_loss, take_profit


def bracket_legs(ticket: OrderTicket, tick_size: float | None, last_price: float | None = None) -> dict | None:
    """Describe the bracket legs for the preflight summary, or None when the ticket has none.

    Leg prices are measured from the order's own price (limit price for limit and stop-limit
    entries, stop price for stop entries). A market entry has no price of its own, so its legs
    are measured from the last traded price and marked approximate. Either way the gateway
    prices the legs from the actual fill; these are the prices if the entry fills there.
    Arithmetic is done in Decimal on the tick grid, so every leg price is tick aligned.
    """
    if not has_brackets(ticket):
        return None
    for name in ("stop_ticks", "target_ticks"):
        value = getattr(ticket, name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or not 1 <= value <= MAX_BRACKET_TICKS):
            raise ValueError(_ticks_message(name))
    if tick_size is None or isinstance(tick_size, bool) or not isinstance(tick_size, (int, float)) or tick_size <= 0:
        raise ValueError("The API did not return a usable tick size for this contract, so brackets cannot be checked.")
    tick = Decimal(str(tick_size))

    approx = False
    if ticket.order_type in ("limit", "stop_limit"):
        reference, basis = ticket.limit_price, "limit price"
    elif ticket.order_type == "stop":
        reference, basis = ticket.stop_price, "stop price"
    else:
        reference, basis, approx = _usable_price(last_price), "last price", True
    ref = None
    if reference is not None:
        ref = Decimal(str(reference))
        if approx:  # a last price off the grid is snapped to the nearest tick
            ref = (ref / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick

    stop_loss, take_profit = bracket_requests(ticket)
    direction = 1 if ticket.side == "buy" else -1
    out: dict = {
        "reference_price": float(ref) if ref is not None else None,
        "basis": basis,
        "approx": approx,
        "stop_loss": None,
        "take_profit": None,
    }
    for key, ticks, request, leg_direction, label in (
        ("stop_loss", ticket.stop_ticks, stop_loss, -direction, "Stop"),
        ("take_profit", ticket.target_ticks, take_profit, direction, "Target"),
    ):
        if ticks is None:
            continue
        price = None
        if ref is not None:
            value = ref + leg_direction * ticks * tick
            if value <= 0:
                raise ValueError(f"{label} of {ticks} ticks would put that leg at or below zero for this contract.")
            price = float(value)
        out[key] = {
            "ticks": ticks,
            "gateway_ticks": request["ticks"],
            "type": BRACKET_TYPES[key].name,
            "type_code": request["type"],
            "direction": "above" if leg_direction > 0 else "below",
            "price": price,
        }
    return out


TAG_PATTERN = re.compile(r"^dash-[0-9a-f]{32}$")


def preflight(ticket: OrderTicket, account: dict | None, contract: dict | None,
              custom_tag: str | None = None, last_price: float | None = None) -> dict:
    """Check everything that can be checked offline and return the summary the page shows.

    Raises ValueError with a plain message for any problem. The summary is exactly what
    ``send_order`` will send. ``last_price`` is only used to show approximate bracket leg
    prices for a market entry.
    """
    if not isinstance(account, dict) or account.get("id") is None:
        raise ValueError("No account is selected.")
    if not _is_practice(account):
        raise ValueError(PRACTICE_ONLY)
    if account["id"] != ticket.account_id:
        raise ValueError("The ticket's account does not match the selected account. Reload the page.")

    if not isinstance(contract, dict) or not contract.get("id"):
        raise ValueError(f"Contract {ticket.contract_id} is not on the watch list.")
    if contract["id"] != ticket.contract_id:
        raise ValueError("The ticket's contract does not match the contract looked up for it.")
    if contract.get("active_contract") is False:
        raise ValueError(f"Contract {contract.get('name') or contract['id']} is not active.")

    if ticket.size <= 0:
        raise ValueError("Size must be at least 1.")
    if ticket.size > MAX_SIZE:
        raise ValueError(f"Size must be {MAX_SIZE} or fewer contracts.")

    if ticket.side not in SIDES:
        raise ValueError("Side must be one of: buy, sell.")
    if ticket.order_type not in ORDER_TYPES:
        raise ValueError("Order type must be one of: market, limit, stop, stop_limit.")
    prices = {"limit_price": ticket.limit_price, "stop_price": ticket.stop_price}
    required = REQUIRED_PRICES[ticket.order_type]
    type_label = ticket.order_type.replace("_", " ").upper()
    for name, value in prices.items():
        if name in required and value is None:
            raise ValueError(f"{type_label} orders need a {PRICE_LABELS[name].lower()}.")
        if name not in required and value is not None:
            raise ValueError(f"{type_label} orders do not take a {PRICE_LABELS[name].lower()}.")
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError(f"{PRICE_LABELS[name]} must be a positive number.")

    tick_size = contract.get("tick_size")
    _require_tick_aligned(ticket.limit_price, tick_size, "Limit price")
    _require_tick_aligned(ticket.stop_price, tick_size, "Stop price")
    brackets = bracket_legs(ticket, tick_size, last_price)

    summary = {
        "account": account.get("name") or str(account["id"]),
        "account_id": account["id"],
        "simulated": True,
        "can_trade": True,
        "contract": contract.get("name") or contract["id"],
        "contract_id": contract["id"],
        "side": SIDES[ticket.side].name,
        "type": ORDER_TYPES[ticket.order_type].name,
        "size": ticket.size,
        "limit_price": ticket.limit_price,
        "stop_price": ticket.stop_price,
        "tick_size": tick_size,
        "stop_ticks": ticket.stop_ticks,
        "target_ticks": ticket.target_ticks,
        "brackets": brackets,
        "custom_tag": custom_tag if custom_tag and TAG_PATTERN.match(custom_tag) else f"dash-{uuid.uuid4().hex}",
        "warning": WARNING,
    }
    if brackets is not None:
        summary["bracket_note"] = BRACKET_NOTE
    return summary


# -- send helpers (server.py has already checked confirm == "yes") -----------------------


def _brackets_match(summary: dict, stop_loss: dict | None, take_profit: dict | None) -> bool:
    """The summary the trader saw must describe exactly the bracket objects about to be sent."""
    shown = summary.get("brackets")
    if stop_loss is None and take_profit is None:
        return shown is None
    if not isinstance(shown, dict):
        return False
    for key, request in (("stop_loss", stop_loss), ("take_profit", take_profit)):
        leg = shown.get(key)
        if request is None:
            if leg is not None:
                return False
        elif (not isinstance(leg, dict) or leg.get("gateway_ticks") != request["ticks"]
              or leg.get("type_code") != request["type"]):
            return False
    return True


def send_order(client, ticket: OrderTicket, summary: dict) -> int:
    """Place the order described by a preflight summary. Returns the gateway's order id."""
    if not isinstance(summary, dict) or summary.get("account_id") != ticket.account_id:
        raise PermissionError("The preflight summary does not belong to this ticket.")
    if summary.get("simulated") is not True or summary.get("can_trade") is not True:
        raise PermissionError(PRACTICE_ONLY)
    if ticket.side not in SIDES or ticket.order_type not in ORDER_TYPES:
        raise ValueError("The ticket did not pass preflight.")
    stop_loss, take_profit = bracket_requests(ticket)
    if not _brackets_match(summary, stop_loss, take_profit):
        raise PermissionError("The preflight summary does not show these brackets. Review the order again.")
    return client.place_order(
        account_id=ticket.account_id,
        contract_id=ticket.contract_id,
        order_type=ORDER_TYPES[ticket.order_type],
        side=SIDES[ticket.side],
        size=ticket.size,
        limit_price=ticket.limit_price,
        stop_price=ticket.stop_price,
        custom_tag=summary["custom_tag"],
        stop_loss_bracket=stop_loss,
        take_profit_bracket=take_profit,
    )


def cancel_order(client, account: dict, order_id: int) -> None:
    account = _require_practice(account)
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
        raise ValueError("Order id must be a positive whole number.")
    client.cancel_order(account["id"], order_id)


def close_position(client, account: dict, contract_id: str) -> None:
    account = _require_practice(account)
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("Choose a contract.")
    client.close_position(account["id"], contract_id.strip())


# -- flatten all / cancel all (one explicit user action, many requests) --------------------


def parse_id_list(body: dict, name: str, kind: type) -> list:
    """The contract ids (``kind=str``) or order ids (``kind=int``) the trader reviewed.

    Missing or null means an empty list. Duplicates are dropped; order is kept.
    """
    value = body.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list.")
    if len(value) > MAX_BATCH_ITEMS:
        raise ValueError(f"{name} lists more than {MAX_BATCH_ITEMS} items.")
    out: list = []
    for item in value:
        if kind is str:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{name} must hold contract ids.")
            item = item.strip()
        elif isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValueError(f"{name} must hold positive whole-number order ids.")
        if item not in out:
            out.append(item)
    return out


def _attempt(fn) -> tuple[str, str | None]:
    """Run one request and return (status, error). Never raises: a batch continues past failures."""
    try:
        fn()
    except TopstepXError as exc:
        if getattr(exc, "error_code", None) == NOT_FOUND_CODE:
            return "not_found", str(exc)
        return "failed", str(exc)
    except (ValueError, PermissionError) as exc:
        return "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad item never stops the rest
        traceback.print_exc(file=sys.stderr)
        return "failed", f"Unexpected {type(exc).__name__}; see the terminal."
    return "sent", None


def _cancel_each(client, account: dict, orders: list[dict], keep_contracts: set[str]) -> list[dict]:
    results = []
    for order in orders:
        order_id = order.get("id")
        contract_id = order.get("contract_id")
        item = {"order_id": order_id, "contract_id": contract_id}
        if contract_id is not None and contract_id in keep_contracts:
            item.update(status="kept", error="Left working because the close on this contract failed.")
        else:
            status, error = _attempt(lambda: cancel_order(client, account, order_id))
            item["status"] = status
            if error:
                item["error"] = error
        results.append(item)
    return results


def _report(action: str, account: dict, positions: list[dict], orders: list[dict]) -> dict:
    counts = {"sent": 0, "failed": 0, "not_found": 0, "kept": 0}
    for item in positions + orders:
        counts[item["status"]] += 1
    return {
        "ok": counts["failed"] == 0,
        "action": action,
        "account": account.get("name") or str(account["id"]),
        "account_id": account["id"],
        "positions": positions,
        "orders": orders,
        **counts,
    }


def flatten_account(client, account: dict, contract_ids: list[str], orders: list[dict]) -> dict:
    """Close each listed position, then cancel each listed order; report every item.

    Positions are closed first, so a protective stop stays in place until its position is
    closed. If a close fails, the orders on that contract are left working (reported as
    "kept") instead of leaving that position without them. Failures never stop the batch.
    ``orders`` are dicts with ``id`` and ``contract_id`` (the rows the trader reviewed).
    """
    account = _require_practice(account)
    closed: list[dict] = []
    failed_contracts: set[str] = set()
    for contract_id in contract_ids:
        status, error = _attempt(lambda: close_position(client, account, contract_id))
        item = {"contract_id": contract_id, "status": status}
        if error:
            item["error"] = error
        if status == "failed":
            failed_contracts.add(contract_id)
        closed.append(item)
    cancelled = _cancel_each(client, account, orders, failed_contracts)
    return _report("flatten", account, closed, cancelled)


def cancel_orders(client, account: dict, orders: list[dict]) -> dict:
    """Cancel each listed order; report every item. Failures never stop the batch."""
    account = _require_practice(account)
    return _report("cancel_all", account, [], _cancel_each(client, account, orders, set()))
