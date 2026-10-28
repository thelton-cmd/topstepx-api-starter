// pine.js - "Pine Script to Python": paste a TradingView Pine Script indicator, convert it to a
// dashboard indicator plugin, and save it so it appears in the chart's Indicators menu.
//
// The page only converts the user's own script into lines on a chart. It never sends orders and
// shows no advice; strategy() order logic is reported as "not converted". Talks to
// /api/pine/convert, /api/pine/save and /api/pine/examples (dashboard/features/pine.py).

const STORAGE_KEY = "dashboard.pine";
const NAME_RE = /^[a-z][a-z0-9_]{0,39}$/;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function token() {
  const meta = document.querySelector('meta[name="dashboard-token"]');
  return meta ? meta.getAttribute("content") : "";
}

// Like api.post, but keeps the whole error body (line and col of a syntax error).
async function call(method, path, body) {
  const options = { method, headers: { "X-Dashboard-Token": token() }, cache: "no-store" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
  if (!response.ok) {
    const error = new Error((data && data.error) || `${response.status} ${response.statusText || "request failed"}`);
    error.line = data && data.line ? Number(data.line) : 0;
    error.col = data && data.col ? Number(data.col) : 0;
    throw error;
  }
  return data;
}

function loadState() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {}; } catch (err) { return {}; }
}

function saveState(patch) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...loadState(), ...patch })); } catch (err) { /* private mode */ }
}

async function copyText(text, sourceNode) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) { /* fall back to a selection copy */ }
  try {
    const range = document.createRange();
    range.selectNodeContents(sourceNode);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    const ok = document.execCommand("copy");
    selection.removeAllRanges();
    return ok;
  } catch (err) {
    return false;
  }
}

function lineRange(text, line) {
  let start = 0;
  for (let i = 1; i < line; i += 1) {
    const next = text.indexOf("\n", start);
    if (next < 0) return null;
    start = next + 1;
  }
  const end = text.indexOf("\n", start);
  return [start, end < 0 ? text.length : end];
}

export function mount(api) {
  const button = el("button", "pine-btn", "Pine Script");
  button.type = "button";
  button.title = "Convert a TradingView Pine Script indicator into a dashboard indicator";
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", "feature-pine");
  api.addHeaderTool(button);

  const body = api.registerPanel({ id: "pine", title: "Pine Script to Python", span: 12 });
  const section = body.parentElement;
  section.classList.add("pine-section");
  body.classList.add("pine");

  const intro = el("p", "pine-intro",
    "Paste a Pine Script (v4 or v5) indicator. It is converted into a Python indicator that appears in the chart's " +
    "Indicators menu, drawn with the settings you pick. Plots, shapes, inputs, functions and the usual ta.* calls " +
    "are supported; arrays, drawing objects, request.security and strategy order logic are not.");

  const cols = el("div", "pine-cols");
  const left = el("div", "pine-col");
  const right = el("div", "pine-col");

  const bar = el("div", "pine-bar");
  const exampleLabel = el("label", "pine-field", "Example ");
  const examples = el("select", "pine-examples");
  examples.setAttribute("aria-label", "Example scripts");
  examples.appendChild(new Option("Choose an example...", ""));
  exampleLabel.appendChild(examples);
  const nameLabel = el("label", "pine-field", "Name ");
  const nameInput = el("input", "pine-name");
  nameInput.type = "text";
  nameInput.placeholder = "my_indicator";
  nameInput.maxLength = 40;
  nameInput.spellcheck = false;
  nameInput.setAttribute("aria-label", "Indicator name (file name)");
  nameLabel.appendChild(nameInput);
  bar.append(exampleLabel, nameLabel);

  const source = el("textarea", "pine-source");
  source.spellcheck = false;
  source.rows = 16;
  source.placeholder = '//@version=5\nindicator("My indicator", overlay=true)\nplot(ta.sma(close, 20), "SMA 20")';
  source.setAttribute("aria-label", "Pine Script source");

  const actions = el("div", "pine-actions");
  const convertBtn = el("button", "primary", "Convert");
  convertBtn.type = "button";
  const copyBtn = el("button", null, "Copy code");
  copyBtn.type = "button";
  copyBtn.disabled = true;
  const saveBtn = el("button", null, "Save as indicator");
  saveBtn.type = "button";
  saveBtn.disabled = true;
  const status = el("span", "pine-status muted", "");
  status.setAttribute("aria-live", "polite");
  actions.append(convertBtn, copyBtn, saveBtn, status);
  left.append(bar, source, actions);

  const messages = el("div", "pine-messages");
  const codeHead = el("div", "pine-code-head");
  codeHead.appendChild(el("span", "pine-label", "Generated Python"));
  const meta = el("span", "muted pine-meta", "");
  codeHead.appendChild(meta);
  const code = el("pre", "pine-code", "");
  code.tabIndex = 0;
  right.append(messages, codeHead, code);
  cols.append(left, right);
  body.append(intro, cols);

  let converted = null;     // the last successful conversion of the text now in the box

  function setStatus(text, kind) {
    status.textContent = text || "";
    status.className = "pine-status" + (kind ? " " + kind : " muted");
  }

  function showMessages(items, kind, title) {
    messages.textContent = "";
    if (!items || !items.length) return;
    const box = el("div", "pine-note " + kind);
    if (title) box.appendChild(el("div", "pine-note-title", title));
    const list = el("ul");
    for (const item of items) list.appendChild(el("li", null, item));
    box.appendChild(list);
    messages.appendChild(box);
  }

  function resetResult() {
    converted = null;
    code.textContent = "";
    meta.textContent = "";
    copyBtn.disabled = true;
    saveBtn.disabled = true;
  }

  function suggestedName() {
    const typed = nameInput.value.trim();
    return typed || (converted ? converted.name : "");
  }

  async function convert() {
    const text = source.value;
    if (!text.trim()) {
      setStatus("Paste a script or pick an example first.", "warn");
      return;
    }
    convertBtn.disabled = true;
    setStatus("Converting...");
    try {
      const typed = nameInput.value.trim();
      const data = await call("POST", "/api/pine/convert", typed ? { source: text, name: typed } : { source: text });
      converted = { source: text, name: data.name, title: data.title };
      code.textContent = data.code;
      meta.textContent = `${data.title} (${data.overlay ? "on the price chart" : "own pane"})`;
      copyBtn.disabled = false;
      saveBtn.disabled = false;
      if (!nameInput.value.trim()) nameInput.placeholder = data.name;
      const warnings = data.warnings || [];
      showMessages(warnings, "warn", warnings.length ? "Converted, with notes" : "");
      setStatus(warnings.length ? `Converted with ${warnings.length} note${warnings.length === 1 ? "" : "s"}.` : "Converted.", "ok");
    } catch (err) {
      resetResult();
      const where = err.line ? `Line ${err.line}${err.col ? `, column ${err.col}` : ""}: ` : "";
      showMessages([where + err.message], "bad", "This script cannot be converted yet");
      setStatus("Not converted.", "bad");
      if (err.line) {
        const range = lineRange(text, err.line);
        if (range) {
          source.focus();
          source.setSelectionRange(range[0], range[1]);
        }
      }
    } finally {
      convertBtn.disabled = false;
    }
  }

  async function save() {
    const name = suggestedName();
    if (!NAME_RE.test(name)) {
      setStatus("Name: a lowercase letter, then lowercase letters, digits or _ (40 at most).", "warn");
      nameInput.focus();
      return;
    }
    saveBtn.disabled = true;
    setStatus("Saving...");
    try {
      const data = await call("POST", "/api/pine/save", { source: source.value, name });
      showMessages(data.warnings || [], "warn", (data.warnings || []).length ? "Saved, with notes" : "");
      setStatus(`Saved ${data.name}.`, "ok");
      if (api.chart && typeof api.chart.reload === "function") {
        try { api.chart.reload(); } catch (err) { /* the chart reloads on its own next refresh */ }
      }
      api.notify(`Saved ${data.name}: open the Indicators menu to add it`);
    } catch (err) {
      const where = err.line ? `Line ${err.line}${err.col ? `, column ${err.col}` : ""}: ` : "";
      showMessages([where + err.message], "bad", "Not saved");
      setStatus("Not saved.", "bad");
    } finally {
      saveBtn.disabled = !converted;
    }
  }

  convertBtn.addEventListener("click", convert);
  saveBtn.addEventListener("click", save);
  copyBtn.addEventListener("click", async () => {
    const ok = await copyText(code.textContent, code);
    api.notify(ok ? "Python code copied." : "Copy failed; select the code and copy it by hand.", ok ? "" : "warn");
  });
  source.addEventListener("input", () => {
    if (converted && converted.source !== source.value) {
      saveBtn.disabled = false;      // saving converts again, so an edited script can still be saved
    }
    saveState({ source: source.value.slice(0, 20000) });
  });
  nameInput.addEventListener("input", () => saveState({ name: nameInput.value }));

  let exampleList = [];
  examples.addEventListener("change", () => {
    const chosen = exampleList.find((e) => e.name === examples.value);
    if (!chosen) return;
    source.value = chosen.source;
    nameInput.value = chosen.name;
    saveState({ source: source.value, name: chosen.name });
    resetResult();
    showMessages([], "warn");
    setStatus("Example loaded. Press Convert.");
  });

  async function loadExamples() {
    try {
      const data = await call("GET", "/api/pine/examples");
      exampleList = (data && data.examples) || [];
      for (const e of exampleList) examples.appendChild(new Option(e.title || e.name, e.name));
    } catch (err) {
      examples.disabled = true;
      examples.title = "Examples are not available: " + err.message;
    }
  }

  function setOpen(open) {
    section.hidden = !open;
    button.classList.toggle("open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
    saveState({ open });
  }

  button.addEventListener("click", () => {
    const opening = section.hidden;
    setOpen(opening);
    if (opening) section.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  const saved = loadState();
  if (typeof saved.source === "string") source.value = saved.source;
  if (typeof saved.name === "string") nameInput.value = saved.name;
  setOpen(saved.open === true);
  loadExamples();
}
