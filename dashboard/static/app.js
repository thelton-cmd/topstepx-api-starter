// TopstepX dashboard shell: polling loop, header, tiles, tables and module wiring.
// Every figure on this page is an approximation built from API data. Nothing here is trading advice.

import { mountChart } from "./chart.js";
import { mountDom } from "./dom.js";
import { mountTicket } from "./ticket.js";
import { loadSettings, mountSettings, saveSettings } from "./settings.js";

const POLL_MS = 1000;
const LOST_CONTACT = "Lost contact with the dashboard process. Is it still running in your terminal?";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const tokenMeta = document.querySelector('meta[name="dashboard-token"]');
const TOKEN = tokenMeta ? tokenMeta.getAttribute("content") || "" : "";

let latest = null;        // the newest snapshot from /api/state
let names = {};           // contract id -> display name
let contracts = {};       // contract id -> contract dict (tick_size etc.)
let mounted = false;      // chart/dom/ticket mounted after the first snapshot
let settingsUi = null;    // handle from mountSettings (addSection)
const features = [];      // feature modules loaded from /api/features
let accountKey = "";      // fingerprint of the account list behind the dropdown
let switching = false;    // true while POST /api/account is in flight
const listeners = [];     // api.onState callbacks
const settingsListeners = [];   // api.onSettings callbacks
const settings = loadSettings();   // page settings (confirmation mode, DOM size, chart overlays)

// -- formatting -----------------------------------------------------------------

function decimalsFor(tickSize) {
  const tick = Number(tickSize);
  if (!tick || tick <= 0) return null;
  const text = String(tick);
  if (text.includes("e")) return 4;
  const dot = text.indexOf(".");
  return dot < 0 ? 0 : Math.min(text.length - dot - 1, 6);
}

const fmt = {
  price(v, contractId) {
    if (v == null || v === "" || Number.isNaN(Number(v))) return "-";
    const d = contractId ? decimalsFor((contracts[contractId] || {}).tick_size) : null;
    const options = d == null
      ? { minimumFractionDigits: 2, maximumFractionDigits: 4 }
      : { minimumFractionDigits: d, maximumFractionDigits: d };
    return Number(v).toLocaleString("en-US", options);
  },
  money(v) {
    if (v == null || v === "" || Number.isNaN(Number(v))) return "-";
    const n = Number(v);
    return (n < 0 ? "-" : "") + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  },
  clock(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "-" : d.toISOString().slice(11, 19);
  },
  int(v) {
    return v == null || v === "" || Number.isNaN(Number(v)) ? "-" : Number(v).toLocaleString("en-US");
  },
  percent(v) {
    return v == null || v === "" || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(2) + "%";
  },
};

const signed = (v) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
const cell = (html, cls) => `<td class="${cls || ""}">${html}</td>`;
const table = (head, rows, empty) => rows.length
  ? `<div class="table-wrap"><table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
  : `<div class="empty">${esc(empty)}</div>`;

// -- the shared api object ----------------------------------------------------------

async function request(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (err) { data = null; }
  }
  if (!response.ok) {
    const message = data && data.error ? String(data.error) : `${response.status} ${response.statusText || "request failed"}`;
    throw new Error(message);
  }
  return data;
}

const api = {
  get(path) {
    return request(path, { headers: { "X-Dashboard-Token": TOKEN }, cache: "no-store" });
  },
  post(path, body) {
    return request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN },
      body: JSON.stringify(body || {}),
    });
  },
  onState(fn) {
    if (typeof fn === "function") listeners.push(fn);
  },
  focus() {
    return latest && latest.focus ? latest.focus : null;
  },
  async setFocus(contractId) {
    await api.post("/api/focus", { contract_id: contractId });
    await poll();
  },
  contractName(id) {
    return names[id] || (id == null ? "" : String(id));
  },
  fmt,
  settings,
  onSettings(fn) {
    if (typeof fn === "function") settingsListeners.push(fn);
  },
  saveSettings() {
    saveSettings(settings);
    notifySettings();
  },
  orders: null,   // set by ticket.js once it mounts: { place, cancel, close, allowed }
  chart: null,    // set by chart.js: { chart, candleSeries, LC, candles(), barSeconds(), contract(), onBars(fn) }
  dom: null,      // set by dom.js: { onSnapshot(fn), size(), focus() }
  snapshot() {
    return latest;
  },
  // -- extension points for feature modules (dashboard/static/features/*.js) --------------
  registerPanel({ id, title, span = 12, before = null }) {
    const section = document.createElement("section");
    section.className = `span-${span}`;
    section.id = `feature-${id}`;
    section.innerHTML = `<h2>${esc(title)}</h2><div class="feature-body"></div>`;
    const main = document.querySelector("main");
    const anchor = before ? document.getElementById(before) : null;
    if (anchor && anchor.parentNode === main) main.insertBefore(section, anchor); else main.appendChild(section);
    return section.querySelector(".feature-body");
  },
  addHeaderTool(node) {
    $("header-tools").appendChild(node);
    return node;
  },
  registerSettingsSection(section) {
    if (settingsUi && typeof settingsUi.addSection === "function") settingsUi.addSection(section);
  },
  notify(text, kind) {
    const box = document.createElement("div");
    box.className = "toast" + (kind ? " " + kind : "");
    box.textContent = String(text);
    $("toasts").appendChild(box);
    setTimeout(() => { box.classList.add("gone"); setTimeout(() => box.remove(), 400); }, 6000);
    return box;
  },
  showError(message) {
    showError(String(message));
  },
};

function notifySettings() {
  for (const fn of settingsListeners) {
    try { fn(settings); } catch (err) { console.error("onSettings listener failed", err); }
  }
}

// -- errors -----------------------------------------------------------------------

function showError(message) {
  const el = $("error");
  el.textContent = message;
  el.hidden = false;
}

function clearError() {
  $("error").hidden = true;
}

// -- rendering --------------------------------------------------------------------

function accountLabel(a) {
  return `${a.name || a.id} (${a.simulated ? "practice" : "funded/live"})`;
}

function badge(el, on) {
  el.className = "badge " + (on ? "on" : "off");
}

function renderHeader(s) {
  const a = s.account || {};
  const accounts = s.accounts || [];
  const select = $("account-select");
  const key = accounts.map((x) => `${x.id}:${x.name}:${x.simulated ? 1 : 0}:${x.can_trade ? 1 : 0}`).join("|");
  if (key !== accountKey) {
    accountKey = key;
    select.innerHTML = accounts.map((x) => `<option value="${esc(x.id)}">${esc(accountLabel(x))}</option>`).join("");
  }
  if (!switching && a.id != null && select.value !== String(a.id)) select.value = String(a.id);
  select.disabled = switching || !accounts.length;
  $("account-info").textContent = a.id == null
    ? "no account selected"
    : `balance ${fmt.money(a.balance)}${s.order_entry_allowed ? "" : " (read-only)"}`;
  const connection = s.connection || {};
  badge($("badge-market"), !!connection.market);
  badge($("badge-user"), !!connection.user);
  $("updated").textContent = "updated " + fmt.clock(s.generated_at) + " UTC";
  $("since").textContent = s.since ? String(s.since).slice(0, 16).replace("T", " ") + " UTC" : "-";
  if (s.last_error) showError(String(s.last_error)); else clearError();
}

function renderTiles(s) {
  const t = s.totals || {};
  const tiles = [
    ["Realized", t.realized, "money"],
    ["Fees + commissions", t.costs == null ? null : -t.costs, "money"],
    ["Unrealized", t.unrealized, "money"],
    ["Total P&L", t.total, "money"],
    ["Open contracts", t.open_contracts, "int"],
    ["Fills today", t.fills, "int"],
  ];
  let html = tiles.map(([label, v, kind]) => {
    const cls = kind === "money" ? signed(v) : "";
    const text = kind === "money" ? fmt.money(v) : fmt.int(v);
    return `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(text)}</div></div>`;
  }).join("");
  const pending = t.pending_prices || [];
  if (pending.length) {
    html += `<div class="tile"><div class="label">Waiting for prices</div><div class="value small muted">${esc(pending.map((id) => api.contractName(id)).join(", "))}</div></div>`;
  }
  $("tiles").innerHTML = html;
}

function renderQuotes(s) {
  const quotes = s.quotes || {};
  const rows = (s.contracts || []).map((c) => {
    const q = quotes[c.id] || {};
    const cls = c.id === s.focus ? "focus" : "";
    return `<tr class="${cls}" data-quote="${esc(c.id)}">`
      + cell(`<span class="link" data-focus="${esc(c.id)}" title="${esc(c.id)}">${esc(c.name || c.id)}</span> <span class="muted">${esc(c.description || "")}</span>`)
      + cell(fmt.price(q.bid, c.id))
      + cell(fmt.price(q.ask, c.id))
      + cell(fmt.price(q.last, c.id))
      + cell(fmt.price(q.change, c.id), signed(q.change))
      + cell(fmt.percent(q.change_percent), signed(q.change_percent))
      + cell(fmt.price(q.high, c.id))
      + cell(fmt.price(q.low, c.id))
      + cell(fmt.int(q.volume))
      + cell(fmt.clock(q.updated))
      + "</tr>";
  });
  $("quotes").innerHTML = table(
    ["Contract", "Bid", "Ask", "Last", "Chg", "Chg %", "High", "Low", "Volume", "Time"],
    rows,
    "No contracts watched yet. Add one with the form above. Outside market hours the stream may stay quiet.",
  );
}

function renderPositions(s) {
  const rows = (s.positions || []).map((p) => `<tr data-contract-id="${esc(p.contract_id)}">`
    + cell(esc(p.name || api.contractName(p.contract_id)))
    + cell(esc(p.side), p.side === "LONG" ? "pos" : "neg")
    + cell(fmt.int(p.contracts))
    + cell(fmt.price(p.average_price, p.contract_id))
    + cell(fmt.price(p.last, p.contract_id))
    + cell(fmt.money(p.unrealized), signed(p.unrealized))
    + cell(fmt.clock(p.opened))
    + "</tr>");
  // ticket.js appends a Close button cell to each row (and its own header cell) after every snapshot.
  $("positions").innerHTML = table(
    ["Contract", "Side", "Size", "Avg price", "Last", "Unrealized", "Opened"],
    rows,
    "No open positions.",
  );
}

function renderOrders(s) {
  const rows = (s.orders || []).map((o) => `<tr data-order-id="${esc(o.id)}">`
    + cell(esc(api.contractName(o.contract_id)))
    + cell(esc(o.side), o.side === "BUY" ? "pos" : "neg")
    + cell(esc(o.type))
    + cell(fmt.int(o.size))
    + cell(fmt.int(o.filled))
    + cell(fmt.price(o.limit_price, o.contract_id))
    + cell(fmt.price(o.stop_price, o.contract_id))
    + cell(esc(o.status))
    + cell(fmt.clock(o.created))
    + "</tr>");
  // ticket.js appends a Cancel button cell to each row (and its own header cell) after every snapshot.
  $("orders").innerHTML = table(
    ["Contract", "Side", "Type", "Size", "Filled", "Limit", "Stop", "Status", "Created"],
    rows,
    "No open orders.",
  );
}

function renderTrades(s) {
  const trades = [...(s.trades || [])].sort((a, b) => {
    const byTime = String(b.time || "").localeCompare(String(a.time || ""));
    return byTime !== 0 ? byTime : Number(b.id) - Number(a.id);
  });
  const rows = trades.map((f) => {
    const fees = Number(f.fees) || 0;
    const halfTurn = f.pnl == null;
    const net = halfTurn ? null : Number(f.pnl) - fees;
    return `<tr data-trade-id="${esc(f.id)}">`
      + cell(fmt.clock(f.time))
      + cell(esc(api.contractName(f.contract_id)))
      + cell(esc(f.side), f.side === "BUY" ? "pos" : "neg")
      + cell(fmt.int(f.size))
      + cell(fmt.price(f.price, f.contract_id))
      + cell(halfTurn ? '<span class="muted">half-turn</span>' : fmt.money(f.pnl), signed(f.pnl))
      + cell(fmt.money(fees))
      + cell(fmt.money(net), signed(net))
      + "</tr>";
  });
  $("trades").innerHTML = table(
    ["Time", "Contract", "Side", "Size", "Price", "P&L", "Fees", "Net"],
    rows,
    "No fills since the start of the day.",
  );
}

function render(s) {
  renderHeader(s);
  renderTiles(s);
  renderQuotes(s);
  renderPositions(s);
  renderOrders(s);
  renderTrades(s);
}

// -- modules ----------------------------------------------------------------------

function mountOne(rootId, fn) {
  try {
    fn();
  } catch (err) {
    console.error(`${rootId} failed to mount`, err);
    const el = $(rootId);
    if (el) el.innerHTML = `<div class="empty">${esc("This panel failed to start: " + (err && err.message ? err.message : err))}</div>`;
  }
}

function mountModules() {
  mountOne("chart-root", () => mountChart($("chart-root"), api));
  mountOne("dom-root", () => mountDom($("dom-root"), $("tape-root"), api));
  mountOne("ticket-root", () => mountTicket($("ticket-root"), api));
  loadFeatures();
}

// Feature page modules: GET /api/features lists them; each exports mount(api). A failing one is
// reported in the console and skipped, so one broken feature never takes the page down.
async function loadFeatures() {
  let list = [];
  try {
    const data = await api.get("/api/features");
    list = data && Array.isArray(data.features) ? data.features : [];
  } catch (err) {
    console.error("could not list features", err);
    return;
  }
  for (const f of list) {
    if (!f || !f.js) continue;
    if (f.css) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = "/static/features/" + encodeURIComponent(f.css);
      document.head.appendChild(link);
    }
    try {
      const mod = await import("/static/features/" + encodeURIComponent(f.js));
      if (typeof mod.mount === "function") await mod.mount(api, f);
      features.push({ ...f, module: mod });
    } catch (err) {
      console.error(`feature ${f.name} failed to mount`, err);
    }
  }
  if (latest) for (const fn of listeners) { try { fn(latest); } catch (err) { console.error("onState listener failed", err); } }
}

// -- snapshot handling ----------------------------------------------------------------

function apply(s) {
  // Responses can overlap (timer poll vs. a manual refresh); never go backwards in time.
  if (latest && s.generated_at && latest.generated_at && s.generated_at < latest.generated_at) return;
  latest = s;
  names = {};
  contracts = {};
  for (const c of s.contracts || []) {
    names[c.id] = c.name || c.id;
    contracts[c.id] = c;
  }
  render(s);
  if (!mounted) {
    mounted = true;
    mountModules();
  }
  for (const fn of listeners) {
    try { fn(s); } catch (err) { console.error("onState listener failed", err); }
  }
}

async function poll() {
  try {
    const snapshot = await api.get("/api/state");
    if (snapshot && snapshot.totals) apply(snapshot);
    return snapshot;
  } catch (err) {
    showError(LOST_CONTACT);
    return null;
  }
}

function schedule() {
  setTimeout(async () => {
    await poll();
    schedule();
  }, POLL_MS);
}

// -- user actions -----------------------------------------------------------------

$("account-select").addEventListener("change", async (event) => {
  const select = event.target;
  const accountId = Number(select.value);
  if (!accountId || (latest && latest.account && latest.account.id === accountId)) return;
  switching = true;
  select.disabled = true;
  try {
    const snapshot = await api.post("/api/account", { account_id: accountId });
    switching = false;
    if (snapshot && snapshot.totals) apply(snapshot); else await poll();
  } catch (err) {
    switching = false;
    showError("Could not switch account: " + err.message);
    if (latest) renderHeader(latest);
  } finally {
    switching = false;
    select.disabled = false;
  }
});

$("quotes").addEventListener("click", (event) => {
  const target = event.target.closest("[data-focus]");
  if (!target) return;
  const id = target.getAttribute("data-focus");
  if (!id || id === api.focus()) return;
  api.setFocus(id).catch((err) => showError("Could not change focus: " + err.message));
});

$("watch-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("watch-input");
  const status = $("watch-status");
  const symbol = input.value.trim();
  if (!symbol) return;
  status.textContent = "adding " + symbol + "...";
  try {
    const result = await api.post("/api/watch", { symbol });
    input.value = "";
    await poll();
    const id = result && result.contract_id ? result.contract_id : symbol;
    status.textContent = "watching " + api.contractName(id);
  } catch (err) {
    status.textContent = err.message;
  }
});

mountOne("settings-root", () => { settingsUi = mountSettings($("settings-root"), settings, notifySettings); });
poll().then(schedule);
