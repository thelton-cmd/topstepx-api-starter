"""Offline tests for the prompt-recipes feature: the docs/RECIPES.md parser, the content rules
every recipe follows, the GET /api/recipes route, and the page files."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from dashboard import features as feature_registry
from dashboard.features import recipes
from tests.test_dashboard_server import as_json, dash, static_dir  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]
STATIC_FEATURES = ROOT / "dashboard" / "static" / "features"

GROUPS = [
    "Charts and indicators",
    "Data and exports",
    "Alerts and risk guardrails",
    "Journal and review",
    "Dashboard layout and workflow",
    "API learning",
    "Building your own tools",
]

# Words and phrases no recipe may use: recipes build tools, they never suggest trades or results.
BANNED = [
    "profitable", "profit", "profits", "edge", "edges", "signal", "signals", "setup", "setups",
    "guarantee", "guaranteed", "guarantees", "buy the dip", "sell the rip", "can't lose", "cannot lose",
    "risk-free", "risk free", "sure thing", "make money", "money-making", "winning strategy",
    "take profit", "overbought", "oversold", "breakout", "alpha",
]
BANNED_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in BANNED) + r")(?![\w-])", re.IGNORECASE)

# Flags a recipe asks the agent to create (everything else must already exist in the kit).
NEW_FLAGS = {"--record", "--replay", "--speed", "--date", "--out-dir"}


@pytest.fixture(scope="module")
def parsed() -> dict:
    return recipes.parse_recipes((ROOT / "docs" / "RECIPES.md").read_text(encoding="utf-8"))


def all_recipes(data: dict) -> list[dict]:
    return [r for g in data["groups"] for r in g["recipes"]]


def recipe_text(recipe: dict) -> str:
    return "\n".join(recipe[k] for k in ("title", "summary", "prompt", "touches"))


# -- the real file -----------------------------------------------------------------------------


def test_real_file_has_every_group_and_20_to_25_recipes(parsed):
    assert [g["title"] for g in parsed["groups"]] == GROUPS
    assert all(g["recipes"] for g in parsed["groups"])
    assert 20 <= recipes.recipe_count(parsed) <= 25


def test_every_recipe_has_all_fields_and_a_unique_id(parsed):
    items = all_recipes(parsed)
    for r in items:
        assert set(r) == {"id", "title", "summary", "prompt", "touches"}
        assert all(isinstance(r[k], str) and r[k].strip() for k in r), r["id"]
        assert r["id"] == recipes.slugify(r["title"]) and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", r["id"])
        assert "\n" not in r["summary"] and "\n" not in r["touches"]
    ids = [r["id"] for r in items]
    assert len(ids) == len(set(ids))
    assert len({g["id"] for g in parsed["groups"]}) == len(parsed["groups"])


def test_prompts_are_short_plain_ascii_and_written_to_the_agent(parsed):
    for r in all_recipes(parsed):
        words = len(r["prompt"].split())
        assert words <= 120, f"{r['id']}: {words} words"
        assert r["prompt"].isascii(), r["id"]   # pastes cleanly into any terminal
        assert re.search(r"\b(me|my|I)\b", r["prompt"]), f"{r['id']} should be written from the trader to the agent"


def test_no_recipe_uses_banned_words(parsed):
    hits = {r["id"]: BANNED_RE.findall(recipe_text(r)) for r in all_recipes(parsed)}
    assert {k: v for k, v in hits.items() if v} == {}


def test_recipes_that_can_send_default_to_the_practice_account(parsed):
    sends = re.compile(r"ORDER_ROUTES|place_order|api\.orders\.place|\bsend", re.IGNORECASE)
    safe = ("Practice Account", "never send", "sends nothing", "without sending", "ask me first")
    for r in all_recipes(parsed):
        if sends.search(r["prompt"]):
            assert any(s in r["prompt"] for s in safe), r["id"]
        if "ORDER_ROUTES" in r["prompt"]:   # order plumbing: guarded route, tested offline only
            assert "Practice Account" in r["prompt"] and "mocked client" in r["prompt"], r["id"]


def test_there_are_ask_recipes_that_change_nothing(parsed):
    asks = [r for r in all_recipes(parsed) if r["title"].startswith("Ask:")]
    assert 2 <= len(asks) <= 3
    for r in asks:
        assert r["prompt"].rstrip().endswith("?") or "do not" in r["prompt"].lower()
        assert r["touches"].startswith("nothing")


def test_requested_recipes_are_present(parsed):
    text = "\n".join(recipe_text(r) for r in all_recipes(parsed))
    for needle in ("second chart", "2-hour", "every evening", "Notification API", "daily loss", "example 09",
                   "volume profile", "presets", "tools.pine2py", "--record", "--replay", "order ticket",
                   "tools/my_rule.py", "dark and light", "hotkey",
                   "bracket", "Flatten"):
        assert needle.lower() in text.lower(), needle


def test_files_and_flags_named_in_recipes_are_real(parsed):
    kit_sources = "\n".join(p.read_text(encoding="utf-8") for p in [ROOT / "dashboard" / "cli.py", *sorted((ROOT / "examples").glob("*.py"))])
    css_vars = set(re.findall(r"(--[a-z0-9-]+)\s*:", (ROOT / "dashboard" / "static" / "style.css").read_text(encoding="utf-8")))
    path_re = re.compile(r"\b((?:dashboard|examples|tools|tests|docs|topstepx)/[\w./<>-]*\w)")
    for r in all_recipes(parsed):
        text = r["prompt"] + "\n" + r["touches"]
        for raw in path_re.findall(text):
            path = ROOT / raw
            if re.fullmatch(r"examples/(0\d|10)_\w+\.py", raw):
                assert path.is_file(), f"{r['id']}: {raw} does not exist"
            elif "." in raw.rsplit("/", 1)[-1]:   # a file: new files are fine, but only in a real folder
                assert path.is_file() or path.parent.is_dir(), f"{r['id']}: no folder for {raw}"
            else:
                assert path.is_dir() or path.parent.is_dir(), f"{r['id']}: {raw} is not a folder in the kit"
        for flag in set(re.findall(r"(?<![\w-])--[a-z](?:[a-z0-9-]*[a-z0-9])?", r["prompt"])):
            if flag in NEW_FLAGS or flag in css_vars or flag == "--help":
                continue
            assert f'"{flag}"' in kit_sources, f"{r['id']}: {flag} is not a flag of the dashboard or an example"


# -- the parser on small inputs ----------------------------------------------------------------

GOOD = """<!-- format comment, with a ## heading inside that is ignored -->

# Title

Intro text.

```text
## not a group either (inside a fence)
```

## First group

### Copy today's fills

One line summary with `code`.

```text
Do the thing for me.

Second paragraph.
```

**What the agent will touch:** a feature module `dashboard/features/x.py`.

### Ask: what is a quote?

Another summary.

```text
Explain it to me?
```

**What the agent will touch:** nothing.

## Second group

### Third

Summary.

```text
Prompt.
```
**What the agent will touch:** `tools/x.py`
"""


def test_parser_reads_groups_recipes_and_fields():
    data = recipes.parse_recipes(GOOD)
    assert [g["id"] for g in data["groups"]] == ["first-group", "second-group"]
    first = data["groups"][0]["recipes"]
    assert first[0] == {"id": "copy-todays-fills", "title": "Copy today's fills",
                        "summary": "One line summary with `code`.",
                        "prompt": "Do the thing for me.\n\nSecond paragraph.",
                        "touches": "a feature module `dashboard/features/x.py`."}
    assert first[1]["id"] == "ask-what-is-a-quote" and first[1]["prompt"] == "Explain it to me?"
    assert data["groups"][1]["recipes"][0]["touches"] == "`tools/x.py`"
    assert recipes.recipe_count(data) == 3
    assert recipes.parse_recipes(GOOD.replace("\n", "\r\n")) == data   # Windows line endings


@pytest.mark.parametrize("broken, message", [
    ("# Only an intro\n", "no \"## \" group heading"),
    ("## G\n\nstray text\n", "expected a \"### \" recipe title"),
    ("## G\n", "has no recipes"),
    ("## G\n\n### R\n\n```text\nx\n```\n", "one-line summary"),
    ("## G\n\n### R\n\nline one\nline two\n\n```text\nx\n```\n**What the agent will touch:** y\n", "single line"),
    ("## G\n\n### R\n\nsum\n\n```python\nx\n```\n**What the agent will touch:** y\n", "prompt fence"),
    ("## G\n\n### R\n\nsum\n\n```text\nx\n", "never closed"),
    ("## G\n\n### R\n\nsum\n\n```text\n\n```\n**What the agent will touch:** y\n", "prompt is empty"),
    ("## G\n\n### R\n\nsum\n\n```text\nx\n```\n\nTouches: y\n", "What the agent will touch"),
    ("## G\n\n### R\n\nsum\n\n```text\nx\n```\n**What the agent will touch:**\n", "touch line is empty"),
    ("## G\n\n### R\n\nsum\n\n```text\nx\n```\n**What the agent will touch:** y\nextra\n", "nothing may follow"),
    ("## G\n\n### R\n\ns\n\n```text\nx\n```\n**What the agent will touch:** y\n\n### R!\n\ns\n\n```text\nx\n```\n"
     "**What the agent will touch:** y\n", "duplicate recipe id"),
    ("## G\n\n### R\n\ns\n\n```text\nx\n```\n**What the agent will touch:** y\n\n## G\n", "duplicate group"),
])
def test_parser_is_strict_and_names_the_line(broken, message):
    with pytest.raises(recipes.RecipeFormatError) as info:
        recipes.parse_recipes(broken)
    assert message in str(info.value) and str(info.value).startswith("line ")


def test_slugify():
    assert recipes.slugify("Export today's fills to CSV every evening") == "export-todays-fills-to-csv-every-evening"
    assert recipes.slugify("Ask: the quote stream vs. the trade stream") == "ask-the-quote-stream-vs-the-trade-stream"
    assert recipes.slugify("Start on 2-hour bars") == "start-on-2-hour-bars"


def test_load_recipes_rereads_the_file_only_when_it_changes(tmp_path, monkeypatch):
    path = tmp_path / "RECIPES.md"
    path.write_text(GOOD, encoding="utf-8")
    first = recipes.load_recipes(path)
    assert recipes.load_recipes(path) is first                  # cached
    path.write_text(GOOD.replace("### Third", "### Fourth"), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))   # coarse file clocks
    second = recipes.load_recipes(path)
    assert second is not first and second["groups"][1]["recipes"][0]["id"] == "fourth"


def test_cli_check_prints_ascii(capsys, tmp_path):
    assert recipes.main([]) == 0
    out = capsys.readouterr().out
    assert out.isascii() and "OK: " in out and "Charts and indicators" in out
    bad = tmp_path / "bad.md"
    bad.write_text("## G\n", encoding="utf-8")
    assert recipes.main([str(bad)]) == 1 and "has no recipes" in capsys.readouterr().out
    assert recipes.main([str(tmp_path / "missing.md")]) == 1


# -- the feature module and its route -------------------------------------------------------------


def test_feature_is_discovered_and_its_page_files_exist():
    assert feature_registry.describe(recipes) == {
        "name": "recipes", "title": "Prompt recipes", "description": recipes.DESCRIPTION,
        "js": "recipes.js", "css": "recipes.css", "order": 90,
    }
    assert "recipes" in [feature_registry.name_of(m) for m in feature_registry.discover("recipes")]
    assert feature_registry.order_routes([recipes]) == set()   # read-only: no order routes
    js = (STATIC_FEATURES / "recipes.js").read_text(encoding="utf-8")
    assert (STATIC_FEATURES / "recipes.css").is_file()
    assert "export function mount" in js and 'api.registerPanel({ id: "recipes", title: "Prompt recipes"' in js
    assert "api.addHeaderTool" in js and "navigator.clipboard.writeText" in js and 'execCommand("copy")' in js
    assert "Copied. Paste it into Claude Code in your repo folder." in js
    assert "api.post" not in js and "/api/orders" not in js and "api.orders" not in js   # never sends anything


def install(session):
    session.features = [recipes]
    session.feature_routes = feature_registry.collect_routes([recipes], session)


def test_route_serves_the_parsed_file(dash, parsed):
    install(dash.session)
    status, body, headers = dash.get("/api/recipes")
    assert status == 200 and headers["Content-Type"] == "application/json"
    payload = as_json(body)
    assert payload["groups"] == parsed["groups"]
    assert payload["count"] == recipes.recipe_count(parsed) and payload["source"] == "docs/RECIPES.md"
    listing = as_json(dash.get("/api/features")[1])["features"]
    assert listing == [feature_registry.describe(recipes)]
    assert "recipes" not in as_json(dash.get("/api/state")[1]).get("features", {})   # no snapshot bloat


def test_route_reports_a_missing_or_broken_file(dash, tmp_path, monkeypatch):
    install(dash.session)
    monkeypatch.setattr(recipes, "RECIPES_PATH", tmp_path / "missing.md")
    status, body, _ = dash.get("/api/recipes")
    assert status == 404 and "missing" in as_json(body)["error"]
    broken = tmp_path / "RECIPES.md"
    broken.write_text("## Group without recipes\n", encoding="utf-8")
    monkeypatch.setattr(recipes, "RECIPES_PATH", broken)
    status, body, _ = dash.get("/api/recipes")
    assert status == 500 and "line 1" in as_json(body)["error"] and "python -m dashboard.features.recipes" in as_json(body)["error"]
