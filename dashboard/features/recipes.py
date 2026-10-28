"""Prompt recipes: copy-paste prompts that extend the dashboard and the kit.

The recipes live in ``docs/RECIPES.md`` (format in the comment at the top of that file).
``GET /api/recipes`` parses it into::

    {"source": "docs/RECIPES.md", "count": 25,
     "groups": [{"id", "title", "recipes": [{"id", "title", "summary", "prompt", "touches"}]}]}

The file is re-read whenever it changes on disk, so a recipe you add shows up the next time the
panel opens. The page side (``static/features/recipes.js``) adds a "Recipes" header button and a
"Prompt recipes" panel with search and a Copy prompt button.

Read-only: this feature only reads a Markdown file. It never touches the account, the hubs or
orders. Check the file from a terminal with ``python -m dashboard.features.recipes``.
"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

NAME = "recipes"
TITLE = "Prompt recipes"
DESCRIPTION = "Copy-paste prompts for Claude Code or Codex that extend this dashboard and the kit."
ORDER = 90
JS = "recipes.js"
CSS = "recipes.css"

ROOT = Path(__file__).resolve().parents[2]
RECIPES_PATH = ROOT / "docs" / "RECIPES.md"
SOURCE_LABEL = "docs/RECIPES.md"

TOUCHES_PREFIX = "**What the agent will touch:**"
FENCE_OPEN = "```text"
FENCE = "```"


class RecipeFormatError(ValueError):
    """docs/RECIPES.md does not follow the format; the message names the line."""

    def __init__(self, line: int, message: str):
        super().__init__(f"line {line}: {message}")
        self.line = line


def slugify(title: str) -> str:
    """Recipe/group id: lowercase, apostrophes dropped, every other run of characters outside a-z0-9
    becomes one dash ("Export today's fills" -> "export-todays-fills")."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower().replace("'", "")).strip("-")


def _heading(line: str, level: int) -> str | None:
    prefix = "#" * level + " "
    if line.startswith(prefix) and not line.startswith(prefix + "#"):
        return line[len(prefix):].strip()
    return None


def parse_recipes(text: str) -> dict:
    """Parse the recipe Markdown into ``{"groups": [...]}``. Pure; raises RecipeFormatError.

    Grammar (blank lines allowed between the parts, nothing else):
        intro ... (skipped, up to the first "## " outside a fence or HTML comment)
        "## Group"   then one or more recipes:
        "### Title"  /  one summary line  /  "```text" ... "```"  /  "**What the agent will touch:** ..."
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    n = len(lines)
    i = 0

    # -- intro: skip to the first group heading (ignoring fences and HTML comments) --------
    in_fence = in_comment = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if in_comment:
            in_comment = "-->" not in line
        elif in_fence:
            in_fence = stripped != FENCE
        elif stripped.startswith("<!--"):
            in_comment = "-->" not in stripped[4:]
        elif stripped.startswith(FENCE):
            in_fence = True
        elif _heading(line, 2) is not None:
            break
        i += 1
    if i >= n:
        raise RecipeFormatError(n, 'no "## " group heading found')

    def skip_blank(k: int) -> int:
        while k < n and not lines[k].strip():
            k += 1
        return k

    groups: list[dict] = []
    seen_ids: dict[str, int] = {}
    group_ids: set[str] = set()
    while i < n:
        i = skip_blank(i)
        if i >= n:
            break
        title = _heading(lines[i], 2)
        if title is None:
            raise RecipeFormatError(i + 1, 'expected a "## " group heading')
        if not title:
            raise RecipeFormatError(i + 1, "group title is empty")
        group = {"id": slugify(title), "title": title, "recipes": []}
        if group["id"] in group_ids:
            raise RecipeFormatError(i + 1, f"duplicate group {title!r}")
        group_ids.add(group["id"])
        group_line = i + 1
        i += 1
        while True:
            i = skip_blank(i)
            if i >= n or _heading(lines[i], 2) is not None:
                break
            recipe_title = _heading(lines[i], 3)
            if recipe_title is None:
                raise RecipeFormatError(i + 1, 'expected a "### " recipe title (no other text is allowed in a group)')
            if not recipe_title:
                raise RecipeFormatError(i + 1, "recipe title is empty")
            title_line = i + 1
            recipe_id = slugify(recipe_title)
            if not recipe_id:
                raise RecipeFormatError(title_line, f"recipe title {recipe_title!r} gives an empty id")
            if recipe_id in seen_ids:
                raise RecipeFormatError(title_line, f"duplicate recipe id {recipe_id!r} (first used on line {seen_ids[recipe_id]})")
            seen_ids[recipe_id] = title_line

            # summary: exactly one line
            i = skip_blank(i + 1)
            if i >= n:
                raise RecipeFormatError(n, f"recipe {recipe_title!r} has no summary line")
            summary = lines[i].strip()
            if summary.startswith(("#", FENCE, TOUCHES_PREFIX)):
                raise RecipeFormatError(i + 1, f"recipe {recipe_title!r} needs a one-line summary before its prompt")
            i += 1
            if i < n and lines[i].strip():
                raise RecipeFormatError(i + 1, "the summary must be a single line followed by a blank line")

            # prompt: one ```text fence
            i = skip_blank(i)
            if i >= n or lines[i].strip() != FENCE_OPEN:
                raise RecipeFormatError(min(i, n - 1) + 1, f'recipe {recipe_title!r}: expected the prompt fence "{FENCE_OPEN}"')
            fence_line = i + 1
            i += 1
            body: list[str] = []
            while i < n and lines[i].strip() != FENCE:
                body.append(lines[i].rstrip())
                i += 1
            if i >= n:
                raise RecipeFormatError(fence_line, f"recipe {recipe_title!r}: the prompt fence is never closed")
            prompt = "\n".join(body).strip("\n")
            if not prompt.strip():
                raise RecipeFormatError(fence_line, f"recipe {recipe_title!r}: the prompt is empty")
            i += 1

            # touches line
            i = skip_blank(i)
            if i >= n or not lines[i].strip().startswith(TOUCHES_PREFIX):
                raise RecipeFormatError(min(i, n - 1) + 1, f'recipe {recipe_title!r}: expected a line starting with "{TOUCHES_PREFIX}"')
            touches = lines[i].strip()[len(TOUCHES_PREFIX):].strip()
            if not touches:
                raise RecipeFormatError(i + 1, f"recipe {recipe_title!r}: the touch line is empty")
            i += 1
            if i < n and lines[i].strip() and _heading(lines[i], 2) is None and _heading(lines[i], 3) is None:
                raise RecipeFormatError(i + 1, "nothing may follow the touch line inside a recipe")

            group["recipes"].append({"id": recipe_id, "title": recipe_title, "summary": summary,
                                     "prompt": prompt, "touches": touches})
        if not group["recipes"]:
            raise RecipeFormatError(group_line, f"group {title!r} has no recipes")
        groups.append(group)
    return {"groups": groups}


def recipe_count(data: dict) -> int:
    return sum(len(g["recipes"]) for g in data.get("groups", []))


# -- loading with a cache keyed on the file's mtime and size ---------------------------------

_cache_lock = threading.Lock()
_cache: dict = {"key": None, "data": None}


def load_recipes(path: Path | str | None = None) -> dict:
    """Parse the recipe file, re-reading it only when it changed. FileNotFoundError if missing."""
    target = Path(path) if path is not None else RECIPES_PATH
    stat = target.stat()
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["data"]
    data = parse_recipes(target.read_text(encoding="utf-8"))
    with _cache_lock:
        _cache["key"], _cache["data"] = key, data
    return data


def get_recipes(query: dict) -> tuple[int, dict]:
    """GET /api/recipes."""
    try:
        data = load_recipes()
    except FileNotFoundError:
        return 404, {"error": f"{SOURCE_LABEL} is missing, so there are no recipes to show."}
    except RecipeFormatError as exc:
        return 500, {"error": f"{SOURCE_LABEL} does not follow the recipe format ({exc}). "
                              "Run: python -m dashboard.features.recipes"}
    return 200, {"source": SOURCE_LABEL, "count": recipe_count(data), "groups": data["groups"]}


def routes(session) -> dict:
    return {"GET": {"/api/recipes": get_recipes}}


def main(argv: list[str] | None = None) -> int:
    """Check docs/RECIPES.md and print one line per group. ASCII only."""
    args = list(sys.argv[1:] if argv is None else argv)
    path = Path(args[0]) if args else RECIPES_PATH
    try:
        data = parse_recipes(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Not found: {path}")
        return 1
    except RecipeFormatError as exc:
        print(f"{path}: {exc}")
        return 1
    for group in data["groups"]:
        title = group["title"].encode("ascii", "replace").decode("ascii")
        print(f"{len(group['recipes']):3d}  {title}")
    print(f"OK: {recipe_count(data)} recipes in {len(data['groups'])} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
