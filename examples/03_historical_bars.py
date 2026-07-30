"""Example 03 — fetch historical bars (OHLCV).

Pulls the last 2 hours of 5-minute bars. Note: the History endpoint allows
50 requests per 30 seconds (other endpoints: 200 per 60s) and returns at
most 20,000 bars per request — the kit automatically backs off and retries
on HTTP 429, but keep your own polling modest.

    python examples/03_historical_bars.py                      # defaults to first "ES" match
    python examples/03_historical_bars.py CON.F.US.EP.U26      # specific contract
"""

import sys
from datetime import datetime, timedelta, timezone

from topstepx import BarUnit, TopstepXClient, TopstepXError


# ── [find-contract] Pick a contract ─────────────────────────────────
def main() -> None:
    with TopstepXClient() as client:
        if len(sys.argv) > 1:
            contract_id = sys.argv[1]
        else:
            contracts = client.search_contracts("ES")
            if not contracts:
                print("No ES contracts found — pass a contract ID explicitly.")
                return
            contract_id = contracts[0].id
            print(f"Using first ES match: {contract_id}\n")

        # ── [request-bars] Ask for two hours of 5-minute bars ───────
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=2)
        bars = client.get_bars(
            contract_id,
            start=start,
            end=end,
            unit=BarUnit.MINUTE,
            unit_number=5,
            limit=100,
        )

    # ── [print-bars] Print the table ────────────────────────────────
    if not bars:
        print("No bars returned — the market may be closed for this window.")
        return

    print(f"{len(bars)} x 5-minute bars for {contract_id}:\n")
    print(f"{'TIME (UTC)':<22} {'OPEN':>10} {'HIGH':>10} {'LOW':>10} {'CLOSE':>10} {'VOL':>8}")
    print("-" * 76)
    for bar in bars:
        t = bar.time.strftime("%Y-%m-%d %H:%M") if bar.time else "?"
        print(f"{t:<22} {bar.open:>10.2f} {bar.high:>10.2f} {bar.low:>10.2f} {bar.close:>10.2f} {bar.volume:>8.0f}")


# ── [errors] The safety net ─────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
