"""Example 02 — search for tradable contracts.

Contract IDs (like "CON.F.US.EP.U26") are what you pass to the
historical-bars and market-data APIs. This example searches by text
(the API returns up to 20 matches), or lists everything with --all.

    python examples/02_search_contracts.py          # searches "ES"
    python examples/02_search_contracts.py NQ       # searches "NQ"
    python examples/02_search_contracts.py --all    # every available contract
"""

import sys

from topstepx import TopstepXClient, TopstepXError


# ── [read-argument] What are we searching for? ──────────────────────
def main() -> None:
    search_text = sys.argv[1] if len(sys.argv) > 1 else "ES"

    # ── [search] Ask the API ────────────────────────────────────────
    with TopstepXClient() as client:
        if search_text == "--all":
            contracts = client.list_available_contracts()
        else:
            contracts = client.search_contracts(search_text)

    # ── [print-results] Print what came back ────────────────────────
    if not contracts:
        print(f"No contracts found for '{search_text}'. Try 'ES', 'NQ', 'CL', 'GC'...")
        return

    label = "available" if search_text == "--all" else f"matching '{search_text}'"
    print(f"{len(contracts)} contract(s) {label}:\n")
    print(f"{'ID':<28} {'NAME':<10} {'TICK':<8} DESCRIPTION")
    print("-" * 80)
    for c in contracts:
        print(f"{c.id:<28} {c.name:<10} {c.tick_size or '':<8} {c.description}")

    print("\nCopy a contract ID into examples 03-05 to fetch data for it.")


# ── [errors] The safety net ─────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
