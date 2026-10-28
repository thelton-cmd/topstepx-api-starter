"""Shared beginner-safe selection and preflight helpers for the examples."""

from __future__ import annotations

import re
import sys
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Sequence, TypeVar

if TYPE_CHECKING:
    from topstepx import Account, Contract, TopstepXClient

T = TypeVar("T")

# Futures month codes: a contract named "ESZ26" is root "ES" + month "Z" + year "26".
_MONTH_CODES = "FGHJKMNQUVXZ"


def is_root_symbol_match(contract_name: str, value: str) -> bool:
    """True when ``contract_name`` is ``value`` plus a month code and year (ES -> ESZ26)."""
    pattern = re.escape(value) + rf"[{_MONTH_CODES}]\d{{1,2}}"
    return re.fullmatch(pattern, contract_name, flags=re.IGNORECASE) is not None


def _pick_one(options: Sequence[T], heading: str, describe, hint: str) -> T:
    """Ask the user to pick from a numbered list — or explain how to skip the prompt.

    When stdin is not an interactive terminal (an AI coding agent, a
    scheduler, a pipe), there is nobody to answer, so raise instead of
    waiting forever.
    """
    lines = [f"  {index:>2}. {describe(option)}" for index, option in enumerate(options, start=1)]
    if not sys.stdin.isatty():
        raise ValueError(f"{heading}\n" + "\n".join(lines) + f"\n{hint}")

    print(heading)
    print("\n".join(lines))
    while True:
        try:
            choice = input(f"Enter 1-{len(options)} (or press Ctrl+C to stop): ").strip()
        except EOFError:  # Windows reports the null device as a TTY, so isatty() can miss it
            raise ValueError(f"{heading}\n" + "\n".join(lines) + f"\n{hint}") from None
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("Enter one of the numbers shown above.")


def choose_active_contract(
    client: "TopstepXClient", value: str = "ES", *, live: bool = False
) -> "Contract":
    """Resolve an exact contract ID, a root symbol (ES, NQ, ...), or a user choice."""
    value = value.strip()
    if value.upper().startswith("CON."):
        contract = client.get_contract(value)
        if not contract.id:
            raise ValueError(f"Contract '{value}' was not found.")
        if contract.active_contract is not True:
            raise ValueError(
                f"Contract '{value}' is not active. Run example 02 and choose an active contract."
            )
        return contract

    matches = [c for c in client.search_contracts(value, live=live) if c.active_contract is True]
    if not matches:
        raise ValueError(
            f"No active contracts found for '{value}'. Run example 02 or pass an exact contract ID."
        )

    exact = [c for c in matches if c.name.casefold() == value.casefold()]
    if len(exact) == 1:
        return exact[0]
    # "ES" means the active E-mini S&P contract (ESZ26), not MES or other text matches.
    roots = [c for c in matches if is_root_symbol_match(c.name, value)]
    if len(roots) == 1:
        return roots[0]
    if len(matches) == 1:
        return matches[0]

    return _pick_one(
        roots or matches,
        f"'{value}' matched several active contracts. Choose one:",
        lambda c: f"{c.name:<10} {c.id}  {c.description}",
        "Pass an exact contract ID instead, e.g. "
        f"{(roots or matches)[0].id} (run example 02 to list them).",
    )


def choose_account(client: "TopstepXClient", requested_id: int | None = None) -> "Account":
    """Return any active account — for READ-ONLY tools that never send orders."""
    accounts = client.search_accounts(only_active=True)
    if requested_id is not None:
        account = next((a for a in accounts if a.id == requested_id), None)
        if account is None:
            raise ValueError(f"Active account {requested_id} was not found.")
        return account
    if not accounts:
        raise ValueError("No active accounts were found for this login.")
    if len(accounts) == 1:
        return accounts[0]
    return _pick_one(
        accounts,
        "Choose an account:",
        lambda a: f"[{a.id}] {a.name}  {'practice' if a.simulated else 'funded/live'}",
        "Pass --account-id with one of the IDs above.",
    )


def choose_practice_account(
    client: "TopstepXClient", requested_id: int | None = None
) -> "Account":
    """Return a tradable simulated account, never a funded/live account."""
    accounts = client.search_accounts(only_active=True)
    practice = [a for a in accounts if a.simulated is True and a.can_trade is True]

    if requested_id is not None:
        account = next((a for a in accounts if a.id == requested_id), None)
        if account is None:
            raise ValueError(f"Active account {requested_id} was not found.")
        if account.simulated is not True:
            raise ValueError(
                f"Account {requested_id} is not marked simulated. This beginner example only "
                "runs with a Practice Account."
            )
        if account.can_trade is not True:
            raise ValueError(f"Practice account {requested_id} is not currently allowed to trade.")
        return account

    if not practice:
        raise ValueError(
            "No active, tradable Practice Account was found. Activate one from the Topstep "
            "Dashboard under Accounts > Add-ons, then run this example again."
        )
    if len(practice) == 1:
        return practice[0]

    return _pick_one(
        practice,
        "Choose an active Practice Account:",
        lambda a: f"[{a.id}] {a.name}  balance={a.balance}",
        "Pass --account-id with one of the IDs above.",
    )


def require_tick_aligned(price: float | None, tick_size: float | None, label: str) -> None:
    """Reject prices that cannot be represented as whole contract ticks."""
    if price is None:
        return
    if tick_size is None or tick_size <= 0:
        raise ValueError("The API did not return a usable tick size for this contract.")
    try:
        ticks = Decimal(str(price)) / Decimal(str(tick_size))
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError(f"Could not validate {label} against tick size {tick_size}.") from exc
    if ticks != ticks.to_integral_value():
        raise ValueError(
            f"{label} {price} is not aligned to this contract's {tick_size} tick size."
        )
