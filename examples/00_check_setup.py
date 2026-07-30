"""Setup checker — run this first.

Verifies, step by step, that everything is in place to use the kit:
.env file, credentials, network, and a real login. Each check prints
[OK] or [FAIL] with a fix hint.

    python examples/00_check_setup.py
"""

import sys
from pathlib import Path

OK, FAIL = "[OK]  ", "[FAIL]"


def main() -> int:
    failures = 0

    # ── [env-file] Is the .env file there? ──────────────────────────
    repo_root = Path(__file__).resolve().parent.parent
    env_file = repo_root / ".env"
    if env_file.exists():
        print(f"{OK} .env file found at {env_file}")
    else:
        print(f"{FAIL} No .env file at {env_file}")
        print("       Fix: copy .env.example .env  (then paste in your credentials)")
        return 1

    # ── [credentials] Do the credentials load? ──────────────────────
    try:
        from topstepx import load_settings

        settings = load_settings(str(env_file))
        print(f"{OK} Credentials loaded for user '{settings.username}'")
    except Exception as exc:
        print(f"{FAIL} {exc}")
        return 1

    # ── [network] Can we reach the gateway? ─────────────────────────
    import httpx

    try:
        httpx.get("https://api.topstepx.com", timeout=10.0)
        print(f"{OK} api.topstepx.com is reachable")
    except httpx.HTTPError as exc:
        print(f"{FAIL} Cannot reach api.topstepx.com: {exc}")
        print("       Fix: check your internet connection / firewall / VPN")
        failures += 1

    # ── [login] Does a real login succeed? ──────────────────────────
    try:
        from topstepx import TokenManager

        tokens = TokenManager(settings)
        tokens.login()
        print(f"{OK} Login succeeded — token valid until {tokens.expires_at}")
    except Exception as exc:
        print(f"{FAIL} Login failed: {exc}")
        failures += 1

    # ── [verdict] The verdict ───────────────────────────────────────
    if failures == 0:
        print("\nAll checks passed. Try:  python examples/04_stream_quotes.py")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
