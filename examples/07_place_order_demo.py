"""Example 07 — place, list, and cancel an order (API mechanics demo).

==============================================================================
 This demonstrates the ORDER API MECHANICS only. Every parameter is supplied
 explicitly by you on the command line. Nothing here is trading advice, a
 trading signal, or a recommendation to enter or exit any position.
==============================================================================

This script refuses funded/live accounts. Activate the free Practice Account
available with an active Trading Combine before you begin. It verifies the
account, contract, size, and price tick before showing a full summary and
requiring you to type "yes".

    python examples/07_place_order_demo.py --account-id 123 --contract-id CON.F.US.EP.Z26 --side buy --type limit --size 1 --limit-price 5000.00

Then it lists your open orders and offers to cancel the one it placed.
"""

import argparse
import sys
import uuid
from datetime import datetime, timezone

from topstepx import OrderSide, OrderType, TopstepXClient, TopstepXError
from _safety import choose_active_contract, choose_practice_account, require_tick_aligned

BANNER = __doc__.split("=" * 78)[1].strip("=\n")


# ── [arguments] Every parameter is explicit ─────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Order API mechanics demo (explicit parameters only)")
    parser.add_argument("--account-id", type=int, required=True, help="your account id (see example 06)")
    parser.add_argument("--contract-id", required=True, help="contract id (see example 02)")
    parser.add_argument("--side", choices=["buy", "sell"], required=True)
    parser.add_argument("--type", choices=["limit", "market", "stop"], required=True)
    parser.add_argument("--size", type=int, required=True, help="number of contracts")
    parser.add_argument("--limit-price", type=float, help="required for limit orders")
    parser.add_argument("--stop-price", type=float, help="required for stop orders")
    args = parser.parse_args()
    if args.size <= 0:
        parser.error("--size must be greater than zero")
    if args.type == "limit" and args.limit_price is None:
        parser.error("--limit-price is required for limit orders")
    if args.type == "stop" and args.stop_price is None:
        parser.error("--stop-price is required for stop orders")
    if args.type == "market" and (args.limit_price is not None or args.stop_price is not None):
        parser.error("market orders do not accept --limit-price or --stop-price")
    if args.type == "limit" and args.stop_price is not None:
        parser.error("limit orders do not accept --stop-price")
    if args.type == "stop" and args.limit_price is not None:
        parser.error("stop orders do not accept --limit-price")
    if args.limit_price is not None and args.limit_price <= 0:
        parser.error("--limit-price must be greater than zero")
    if args.stop_price is not None and args.stop_price <= 0:
        parser.error("--stop-price must be greater than zero")
    return args


# ── [summary-confirm] Show exactly what will be sent ────────────────
def main() -> None:
    args = parse_args()
    side = OrderSide.BUY if args.side == "buy" else OrderSide.SELL
    order_type = {"limit": OrderType.LIMIT, "market": OrderType.MARKET, "stop": OrderType.STOP}[args.type]

    with TopstepXClient() as client:
        account = choose_practice_account(client, args.account_id)
        contract = choose_active_contract(client, args.contract_id)
        require_tick_aligned(args.limit_price, contract.tick_size, "Limit price")
        require_tick_aligned(args.stop_price, contract.tick_size, "Stop price")
        custom_tag = f"starter-{uuid.uuid4().hex}"

        print(BANNER)
        print("\nPreflight passed:")
        print(f"  account   : {account.id} ({account.name})")
        print("  simulated : YES — funded/live accounts are blocked")
        print(f"  can trade : {account.can_trade}")
        print(f"  contract  : {contract.id} ({contract.name})")
        print(f"  active    : {contract.active_contract}")
        print(f"  tick size : {contract.tick_size}")
        print("\nOrder summary (exactly what will be sent):")
        print(f"  side      : {args.side.upper()}")
        print(f"  type      : {args.type.upper()}")
        print(f"  size      : {args.size}")
        if args.limit_price is not None:
            print(f"  limit px  : {args.limit_price}")
        if args.stop_price is not None:
            print(f"  stop px   : {args.stop_price}")
        print(f"  custom tag: {custom_tag}")
        print("\nTopstep states that API orders are final and cannot be reviewed or reversed.")

        if input("Type 'yes' to send this Practice Account order: ").strip().lower() != "yes":
            print("Aborted — nothing was sent.")
            return

        # ── [place] Send it — only after validation and a typed yes ──
        order_id = client.place_order(
            account_id=account.id,
            contract_id=contract.id,
            order_type=order_type,
            side=side,
            size=args.size,
            limit_price=args.limit_price,
            stop_price=args.stop_price,
            custom_tag=custom_tag,
        )
        print(f"\nOrder placed. orderId = {order_id}")

        # ── [list-cancel] The rest of the round trip ────────────────
        open_orders = client.search_open_orders(account.id)
        print(f"\nOpen orders on Practice Account {account.id}: {len(open_orders)}")
        for o in open_orders:
            print(f"  #{o.id}  {o.contract_id}  size={o.size}  limit={o.limit_price}  stop={o.stop_price}")

        if any(o.id == order_id for o in open_orders):
            if input(f"\nType 'yes' to cancel order #{order_id}: ").strip().lower() == "yes":
                client.cancel_order(account.id, order_id)
                print("Cancel request sent.")
            else:
                print("Order left open — manage it in the TopstepX platform.")
        else:
            print(f"\nOrder #{order_id} is not in the open list (it may have filled or been rejected).")
            print("Check recent trades / the platform UI. Recent orders today:")
            start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            for o in client.search_orders(account.id, start=start_of_day):
                print(f"  #{o.id}  status={o.status}  {o.contract_id}  size={o.size}")


# ── [errors] The safety net ─────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        print(f"Cannot continue: {exc}")
        sys.exit(1)
