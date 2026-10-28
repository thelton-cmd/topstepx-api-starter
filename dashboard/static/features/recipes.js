// recipes.js - the "Recipes" header button and the "Prompt recipes" panel.
//
// Shows the copy-paste prompts from docs/RECIPES.md (served by GET /api/recipes, see
// dashboard/features/recipes.py): a search box, one collapsible list per group, and a
// "Copy prompt" button on every recipe. Read-only: this module only GETs /api/recipes. It never
// sends, cancels or closes anything. All styles live in recipes.css.

const STORE_KEY = "dashboard.recipes";   // {open: bool, collapsed: {groupId: true}}
const COPIED_TEXT = "Copied. Paste it into Claude Code in your repo folder.";
const BLOCKED_TEXT = "The browser blocked copying. The prompt is selected: press Ctrl+C (Cmd+C on a Mac).";

function loadPrefs() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    const prefs = raw ? JSON.parse(raw) : null;
    if (prefs && typeof prefs === "object") {
      return { open: prefs.open === true, collapsed: prefs.collapsed && typeof prefs.collapsed === "object" ? prefs.collapsed : {} };
    }
  } catch (err) { /* private mode or bad JSON: start fresh */ }
  return { open: false, collapsed: {} };
}

function savePrefs(prefs) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(prefs)); } catch (err) { /* private mode etc. */ }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

// `code` spans in summaries and touch lines become <code>; everything else stays plain text.
function appendInline(parent, text) {
  String(text || "").split("`").forEach((part, index) => {
    if (!part) return;
    if (index % 2 === 1) parent.appendChild(el("code", null, part));
    else parent.appendChild(document.createTextNode(part));
  });
}

function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms))]);
}

// Clipboard API first (127.0.0.1 is a secure context), then the textarea + execCommand fallback.
async function copyText(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function" && window.isSecureContext) {
    try {
      await withTimeout(navigator.clipboard.writeText(text), 1500);
      return true;
    } catch (err) { /* permission or focus: try the fallback */ }
  }
  const active = document.activeElement;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.className = "recipes-copy-area";
  document.body.appendChild(area);
  area.select();
  area.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
  area.remove();
  if (active && typeof active.focus === "function") active.focus();
  return ok;
}

function selectContents(node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

export function mount(api) {
  const prefs = loadPrefs();
  let data = null;        // the /api/recipes payload once loaded
  let loading = null;     // the in-flight load promise
  const cards = [];       // {node, groupId, haystack}
  const groupNodes = [];  // {node, id, countEl, total}

  // -- header button ----------------------------------------------------------------------
  const button = el("button", "recipes-tool", "Recipes");
  button.type = "button";
  button.title = "Copy-paste prompts that extend this dashboard and the kit";
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", "feature-recipes");
  button.dataset.featureTool = "recipes";
  api.addHeaderTool(button);

  // -- panel --------------------------------------------------------------------------------
  const body = api.registerPanel({ id: "recipes", title: "Prompt recipes", span: 12 });
  const section = body.closest("section") || body.parentNode;
  section.classList.add("recipes-panel");
  section.hidden = true;
  // Open it at the top of the page, where the header button is.
  const main = section.parentNode;
  if (main && main.firstElementChild && main.firstElementChild !== section) main.insertBefore(section, main.firstElementChild);

  const root = el("div", "recipes");
  const intro = el("p", "recipes-intro");
  appendInline(intro, "Copy a prompt and paste it into Claude Code (or Codex) running on this computer in your repo folder. "
    + "The agent follows `AGENTS.md`: your API key stays in `.env`, the numbers and rules are yours, and anything that can "
    + "send orders starts on your Practice Account. These prompts build tools; none of them is trading advice.");
  root.appendChild(intro);

  const bar = el("div", "recipes-bar");
  const search = el("input", "recipes-search");
  search.type = "search";
  search.placeholder = "Search recipes: alert, CSV, Pine, theme...";
  search.setAttribute("aria-label", "Search recipes");
  search.autocomplete = "off";
  const count = el("span", "recipes-count");
  const expandBtn = el("button", "small", "Expand all");
  expandBtn.type = "button";
  const collapseBtn = el("button", "small", "Collapse all");
  collapseBtn.type = "button";
  const closeBtn = el("button", "small recipes-close", "Close");
  closeBtn.type = "button";
  bar.append(search, count, expandBtn, collapseBtn, closeBtn);
  root.appendChild(bar);

  const status = el("div", "status recipes-status");
  const list = el("div", "recipes-groups");
  const empty = el("div", "empty recipes-empty", "No recipe matches. Try fewer or different words.");
  empty.hidden = true;
  const foot = el("p", "recipes-foot");
  appendInline(foot, "Add your own in `docs/RECIPES.md` (format in the comment at its top); this panel re-reads it when it changes.");
  root.append(status, list, empty, foot);
  body.appendChild(root);

  // -- rendering ------------------------------------------------------------------------------
  function render(payload) {
    list.textContent = "";
    cards.length = 0;
    groupNodes.length = 0;
    for (const group of payload.groups || []) {
      const details = el("details", "recipes-group");
      details.dataset.group = group.id;
      details.open = !prefs.collapsed[group.id];
      const summary = el("summary");
      summary.appendChild(el("span", "recipes-group-title", group.title));
      const groupCount = el("span", "recipes-group-count");
      summary.appendChild(groupCount);
      details.appendChild(summary);
      details.addEventListener("toggle", () => {
        if (search.value.trim()) return;   // searching opens groups by itself; keep the saved choice
        if (details.open) delete prefs.collapsed[group.id]; else prefs.collapsed[group.id] = true;
        savePrefs(prefs);
      });

      const ul = el("ul", "recipes-list");
      for (const recipe of group.recipes || []) {
        const li = el("li", "recipe");
        li.dataset.id = recipe.id;
        const head = el("div", "recipe-head");
        head.appendChild(el("h3", "recipe-title", recipe.title));
        const copy = el("button", "small primary recipe-copy", "Copy prompt");
        copy.type = "button";
        copy.setAttribute("aria-label", "Copy prompt: " + recipe.title);
        head.appendChild(copy);
        li.appendChild(head);

        const summaryLine = el("p", "recipe-summary");
        appendInline(summaryLine, recipe.summary);
        li.appendChild(summaryLine);

        const promptBox = el("details", "recipe-prompt");
        promptBox.appendChild(el("summary", null, "Show prompt"));
        const pre = el("pre", null, recipe.prompt);
        promptBox.appendChild(pre);
        li.appendChild(promptBox);

        const touches = el("p", "recipe-touches");
        touches.appendChild(el("span", "recipe-touches-label", "Touches: "));
        appendInline(touches, recipe.touches);
        li.appendChild(touches);

        copy.addEventListener("click", async () => {
          const ok = await copyText(recipe.prompt);
          if (ok) {
            api.notify(COPIED_TEXT);
            copy.textContent = "Copied";
            copy.classList.add("done");
            setTimeout(() => { copy.textContent = "Copy prompt"; copy.classList.remove("done"); }, 1500);
          } else {
            promptBox.open = true;
            selectContents(pre);
            api.notify(BLOCKED_TEXT, "warn");
          }
        });

        ul.appendChild(li);
        const haystack = [group.title, recipe.title, recipe.summary, recipe.prompt, recipe.touches].join(" ").toLowerCase();
        cards.push({ node: li, groupId: group.id, haystack });
      }
      details.appendChild(ul);
      list.appendChild(details);
      groupNodes.push({ node: details, id: group.id, countEl: groupCount, total: (group.recipes || []).length });
    }
    applyFilter();
  }

  function applyFilter() {
    const terms = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    const shown = {};
    let total = 0;
    for (const card of cards) {
      const match = terms.every((t) => card.haystack.includes(t));
      card.node.hidden = !match;
      if (match) {
        shown[card.groupId] = (shown[card.groupId] || 0) + 1;
        total += 1;
      }
    }
    for (const g of groupNodes) {
      const n = shown[g.id] || 0;
      g.node.hidden = n === 0;
      g.countEl.textContent = terms.length ? `${n} of ${g.total}` : String(g.total);
      if (terms.length) g.node.open = n > 0;
      else g.node.open = !prefs.collapsed[g.id];
    }
    const all = cards.length;
    count.textContent = terms.length ? `${total} of ${all} recipes` : `${all} recipes`;
    empty.hidden = !(terms.length && total === 0);
  }

  async function ensureLoaded() {
    if (data) return data;
    if (!loading) {
      status.className = "status recipes-status";
      status.textContent = "Loading recipes...";
      loading = api.get("/api/recipes")
        .then((payload) => {
          data = payload || { groups: [] };
          status.textContent = "";
          render(data);
          return data;
        })
        .catch((err) => {
          status.className = "status recipes-status error";
          status.textContent = "Could not load the recipes: " + (err && err.message ? err.message : err);
          return null;
        })
        .finally(() => { loading = null; });
    }
    return loading;
  }

  function setOpen(open, { focus = false, scroll = false } = {}) {
    section.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.classList.toggle("open", open);
    prefs.open = open;
    savePrefs(prefs);
    if (!open) return;
    ensureLoaded();
    if (scroll) section.scrollIntoView({ block: "start", behavior: "smooth" });
    if (focus) search.focus({ preventScroll: true });
  }

  button.addEventListener("click", () => setOpen(section.hidden, { focus: true, scroll: true }));
  closeBtn.addEventListener("click", () => { setOpen(false); button.focus(); });
  search.addEventListener("input", applyFilter);
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && search.value) {
      ev.preventDefault();
      ev.stopPropagation();
      search.value = "";
      applyFilter();
    }
  });
  expandBtn.addEventListener("click", () => {
    search.value = "";
    prefs.collapsed = {};
    savePrefs(prefs);
    applyFilter();
  });
  collapseBtn.addEventListener("click", () => {
    search.value = "";
    prefs.collapsed = {};
    for (const g of groupNodes) prefs.collapsed[g.id] = true;
    savePrefs(prefs);
    applyFilter();
  });

  if (prefs.open) setOpen(true);
}
