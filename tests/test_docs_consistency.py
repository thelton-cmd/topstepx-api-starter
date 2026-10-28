"""Regression checks for beginner-facing safety instructions.

The public kit ships without the docs site, deploy files, or marketplace
code, so checks against those files skip when the files are absent.
"""

import ast
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
HAS_SITE = (ROOT / "site").is_dir()
needs_site = pytest.mark.skipif(not HAS_SITE, reason="docs site is not part of the public kit")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_cross_platform_setup_commands_are_consistent():
    readme = read("README.md")
    env_example = read(".env.example")

    assert r".\.venv\Scripts\Activate.ps1" in readme
    assert "python3 -m venv .venv" in readme
    assert "Copy-Item .env.example .env" in env_example


@needs_site
def test_site_setup_commands_match_readme():
    setup = read("site/src/content/setup.ts")
    quickstart = read("site/src/content/quickstart.ts")

    assert r".\\.venv\\Scripts\\Activate.ps1" in setup
    assert r".\\.venv\\Scripts\\Activate.ps1" in quickstart
    assert "python3 -m venv .venv" in quickstart


@needs_site
def test_hosted_site_cannot_proxy_topstepx_credentials_or_hubs():
    nginx = read("nginx.conf")
    playground = read("site/src/pages/Playground.tsx")

    assert "proxy_pass https://api.topstepx.com" not in nginx
    assert "proxy_pass https://rtc.topstepx.com" not in nginx
    assert "location ^~ /api/" in nginx and "location ^~ /hubs/" in nginx
    assert "proxy_pass https://topstepx-market-api.fly.dev" in nginx
    assert "LOOPBACK_HOSTS" in playground
    assert "localPlayground ? <CredentialsForm" in playground


def test_examples_never_take_the_first_fuzzy_contract_match():
    files = [
        "examples/03_historical_bars.py",
        "examples/04_stream_quotes.py",
        "examples/05_stream_depth.py",
        "examples/08_chart_bars.py",
        "examples/licensed_tool_demo.py",
        "README.md",
        "AGENTS.md",
        "site/src/content/quickstart.ts",
        "site/src/content/api/realtime.ts",
    ]
    unsafe = re.compile(r"search_contracts\([^\n]+\)\[0\]")
    existing = [path for path in files if (ROOT / path).exists()]
    assert not {path: unsafe.findall(read(path)) for path in existing if unsafe.search(read(path))}


def test_beginner_order_example_enforces_preflight():
    source = read("examples/07_place_order_demo.py")
    assert "choose_practice_account(client, args.account_id)" in source
    assert "choose_active_contract(client, args.contract_id)" in source
    assert "args.size <= 0" in source
    assert "require_tick_aligned" in source
    assert source.index("Preflight passed") < source.index("Type 'yes'")


def test_readme_lists_every_numbered_example():
    readme = read("README.md")
    numbered = sorted(p.name for p in (ROOT / "examples").glob("[0-9][0-9]_*.py"))
    assert numbered, "no numbered examples found"
    assert [name for name in numbered if f"`{name}`" not in readme] == []


def test_example_output_survives_windows_cp1252_consoles():
    # Piped output on Windows (agent shells, schedulers) is encoded as cp1252;
    # printing a character outside it (an arrow, a check mark) raises mid-run.
    unencodable = []
    for path in sorted((ROOT / "examples").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in {"print", "input"}:
                for const in ast.walk(node):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        try:
                            const.value.encode("cp1252")
                        except UnicodeEncodeError:
                            unencodable.append(f"{path.name}:{const.lineno}")
    assert unencodable == []


def test_read_only_recipes_never_send_orders():
    for path in ("examples/08_chart_bars.py", "examples/09_trade_journal.py", "examples/10_risk_monitor.py"):
        source = read(path)
        for call in ("place_order", "cancel_order", "modify_order", "close_position", "partial_close_position"):
            assert call not in source, f"{path} must stay read-only but mentions {call}"


def test_package_root_does_not_depend_on_marketplace_licensing():
    # The public kit ships without topstepx/licensing.py while the marketplace is on hold.
    assert "licensing" not in read("topstepx/__init__.py")


@needs_site
def test_licensing_module_is_documented_and_fail_closed():
    assert "topstepx/licensing.py" in read("site/src/content/modules.ts")
    assert "fails closed" in read("topstepx/licensing.py")
