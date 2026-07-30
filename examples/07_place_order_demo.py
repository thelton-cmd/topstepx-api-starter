"""Example 07 — place, list, and cancel an order (API mechanics demo).

==============================================================================
 This demonstrates the ORDER API MECHANICS only. Every parameter is supplied
 explicitly by you on the command line. Nothing here is trading advice, a
 trading signal, or a recommendation to enter or exit any position.
==============================================================================

Run against a practice account: every TopstepX Combine subscription includes
a free practice account — the safe place to exercise the API. The script
prints a full summary and requires you to type "yes" before anything is sent.

    python examples/07_place_order_demo.py --account-id 123 --contract-id CON.F.US.EP.U26 ^
        --side buy --type limit --size 1 --limit-price 5000.00

Then it lists your open orders and offers to cancel the one it placed.
"""

import argparse
import sys
from datetime import datetime, timezone

from topstepx import OrderSide, OrderType, TopstepXClient, TopstepXError

BANNER = __doc__.split("====")[1].strip("=\n")


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
    if args.type == "limit" and args.limit_price is None:
        parser.error("--limit-price is required for limit orders")
    if args.type == "stop" and args.stop_price is None:
        parser.error("--stop-price is required for stop orders")
    return args


# ── [summary-confirm] Show exactly what will be sent ────────────────
def main() -> None:
    args = parse_args()
    side = OrderSide.BUY if args.side == "buy" else OrderSide.SELL
    order_type = {"limit": OrderType.LIMIT, "market": OrderType.MARKET, "stop": OrderType.STOP}[args.type]

    print(BANNER)
    print("\nOrder summary (exactly what will be sent):")
    print(f"  account   : {args.account_id}")
    print(f"  contract  : {args.contract_id}")
    print(f"  side      : {args.side.upper()}")
    print(f"  type      : {args.type.upper()}")
    print(f"  size      : {args.size}")
    if args.limit_price is not None:
        print(f"  limit px  : {args.limit_price}")
    if args.stop_price is not None:
        print(f"  stop px   : {args.stop_price}")

    if input("\nType 'yes' to send this order: ").strip().lower() != "yes":
        print("Aborted — nothing was sent.")
        return

    # ── [place] Send it — only after a typed yes ────────────────────
    with TopstepXClient() as client:
        order_id = client.place_order(
            account_id=args.account_id,
            contract_id=args.contract_id,
            order_type=order_type,
            side=side,
            size=args.size,
            limit_price=args.limit_price,
            stop_price=args.stop_price,
        )
        print(f"\nOrder placed. orderId = {order_id}")

        # ── [list-cancel] The rest of the round trip ────────────────
        open_orders = client.search_open_orders(args.account_id)
        print(f"\nOpen orders on account {args.account_id}: {len(open_orders)}")
        for o in open_orders:
            print(f"  #{o.id}  {o.contract_id}  size={o.size}  limit={o.limit_price}  stop={o.stop_price}")

        if any(o.id == order_id for o in open_orders):
            if input(f"\nType 'yes' to cancel order #{order_id}: ").strip().lower() == "yes":
                client.cancel_order(args.account_id, order_id)
                print("Cancel request sent.")
            else:
                print("Order left open — manage it in the TopstepX platform.")
        else:
            print(f"\nOrder #{order_id} is not in the open list (it may have filled or been rejected).")
            print("Check recent trades / the platform UI. Recent orders today:")
            start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            for o in client.search_orders(args.account_id, start=start_of_day):
                print(f"  #{o.id}  status={o.status}  {o.contract_id}  size={o.size}")


# ── [errors] The safety net ─────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
