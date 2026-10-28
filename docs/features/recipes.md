# Prompt recipes

You set up the kit with one prompt. The **Recipes** panel shows the next ones: about 25
copy-paste prompts that extend the dashboard and the kit, such as a second chart, a volume
profile, a Pine Script converted into an indicator, price alerts, a daily loss lock, journal
exports and a dark/light theme. Each one names the files the agent will touch.

The recipes build tools. None of them is trading advice or a trading rule; where a recipe
automates something, the numbers and rules are yours and anything that can send orders starts on
your Practice Account.

## Using it

1. Click **Recipes** in the dashboard header. Click it again (or **Close**) to hide the panel.
2. Type in the search box to filter (for example `alert`, `CSV`, `Pine`). Click a group name to
   fold it; **Expand all** and **Collapse all** do every group at once.
3. Click **Copy prompt**, then paste it into Claude Code (or Codex) running on this computer in
   your `topstepx-api-starter` folder. **Show prompt** lets you read it first.

The agent reads `AGENTS.md` and follows its ground rules: your API key stays in `.env`, nothing is
sent to your account unless you ask for that specific action, and the numbers are always yours.
If your browser blocks the clipboard, the panel selects the prompt so you can press Ctrl+C.

## Adding your own

The recipes live in [`docs/RECIPES.md`](../RECIPES.md); the same file reads well on GitHub. Add
a recipe in the format described in the comment at its top, then check it:

```text
python -m dashboard.features.recipes
```

(Windows: `.venv\Scripts\python.exe -m dashboard.features.recipes`.) The panel re-reads the file
when it changes, so reopen it to see your recipe. `tests/test_feature_recipes.py` also checks
word counts, unique ids and wording.

## Details

- `GET /api/recipes` returns the parsed file as JSON. The feature only reads that Markdown file;
  it never touches your account, the hubs or orders.
- Start the dashboard without it with `--features` and a list that leaves it out, for example
  `python -m dashboard --features apilog`.
