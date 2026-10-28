// apilog.js - "Show me the API call": the TopstepX REST calls and SignalR hub traffic behind this page.
//
// Read-only. This module reads /api/apilog/* and can POST /api/apilog/clear and /api/apilog/capture,
// which only change what the dashboard keeps in memory. It never sends, changes or cancels an order.
// Secrets (API key, session token) are replaced with <redacted> by the server before anything is stored.
//
// Contract: dashboard/features/__init__.py and dashboard/ARCHITECTURE.md, "Features (extension points)".

const POLL_MS = 1000;
const MAX_ROWS = 500;
const MAX_RING = 200;
const DEFAULTS = { open: false, captureHub: true };
const NEAR_LIMIT_MINUTE = 150;   // of 200 requests / 60 s
const NEAR_LIMIT_BARS = 40;      // of 50 History/retrieveBars requests / 30 s

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function clockMs(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "-" : d.toISOString().slice(11, 23);
}

function bytes(n) {
  if (n == null || Number.isNaN(Number(n))) return "-";
  const v = Number(n);
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1024 / 1024).toFixed(1)} MB`;
}

function pretty(value) {
  try { return JSON.stringify(value, null, 2); } catch (err) { return String(value); }
}

function ago(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s)) return "-";
  if (s < 1) return "now";
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) { /* fall back to a textarea below */ }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.top = "-1000px";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
  area.remove();
  return ok;
}

export async function mount(api, info) {
  const prefs = () => {
    const raw = api.settings && typeof api.settings.apilog === "object" && api.settings.apilog ? api.settings.apilog : {};
    return { open: raw.open === true, captureHub: raw.captureHub !== false };
  };
  const savePrefs = (partial) => {
    api.settings.apilog = { ...DEFAULTS, ...prefs(), ...partial };
    api.saveSettings();
  };

  // -- header tool ---------------------------------------------------------------------------
  const tool = document.createElement("button");
  tool.type = "button";
  tool.className = "apilog-tool";
  tool.setAttribute("aria-pressed", "false");
  tool.innerHTML = 'API <span class="apilog-count">0</span>';
  tool.title = "Show the TopstepX API calls behind this page";
  api.addHeaderTool(tool);
  const countEl = tool.querySelector(".apilog-count");

  // -- panel state ---------------------------------------------------------------------------
  let body = null;            // feature-body div inside our section
  let section = null;
  let el = {};                // named elements inside the panel
  let tab = "rest";
  let paused = false;
  let timer = null;
  let busy = false;
  let lastSeq = 0;            // newest REST seq we have
  let hubSeq = 0;             // newest hub-ring seq we have
  const rows = new Map();     // seq -> {rec, row, detail, loaded}
  const order = [];           // seqs, oldest first
  const hubRows = new Map();  // key -> {row, detail}
  let hubKeys = "";
  const hubOpen = new Set();  // expanded hub stat keys
  const ringRows = [];        // newest first: {seq, row, detail}
  let lastCounters = null;
  let noMatch = false;        // the filter hides every row
  let syncingCapture = false; // a POST /api/apilog/capture is in flight

  function isOpen() { return !!section && !section.hidden; }

  function build() {
    body = api.registerPanel({ id: "apilog", title: info && info.title ? info.title : "API calls", span: 12, before: "feature-journal" });
    section = body.parentElement;
    section.classList.add("apilog-section");
    body.innerHTML = `
      <div class="apilog-intro muted">Every request this dashboard sends to the TopstepX REST API, and the SignalR hub
        traffic it receives. Click a row to see the exact JSON and copy it as curl or Python. Secrets are shown as
        <code>&lt;redacted&gt;</code>. This is for learning the API mechanics.</div>
      <div class="apilog-bar">
        <div class="apilog-tabs" role="tablist">
          <button type="button" role="tab" class="apilog-tab active" data-tab="rest" aria-selected="true">REST</button>
          <button type="button" role="tab" class="apilog-tab" data-tab="hub" aria-selected="false">Hub</button>
        </div>
        <input type="search" class="apilog-filter" placeholder="filter: path, field, status" aria-label="Filter API calls">
        <label class="apilog-errors-only"><input type="checkbox"> errors only</label>
        <span class="apilog-spacer"></span>
        <span class="apilog-status muted"></span>
        <button type="button" class="small apilog-pause">Pause</button>
        <button type="button" class="small apilog-clear" title="Empty the lists kept by the dashboard">Clear</button>
        <button type="button" class="small apilog-close" title="Hide this panel">Close</button>
      </div>
      <div class="apilog-pane" data-pane="rest">
        <div class="apilog-limits muted"></div>
        <div class="table-wrap apilog-scroll">
          <table class="apilog-table apilog-rest">
            <thead><tr><th>Time (UTC)</th><th>Path</th><th class="num">Status</th><th class="num">ms</th><th>Request</th><th>Result</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="empty apilog-empty">No REST calls recorded yet. They appear here as the dashboard makes them
          (the chart refreshes its bars every few seconds).</div>
      </div>
      <div class="apilog-pane" data-pane="hub" hidden>
        <div class="apilog-hub-note muted">Market events (quotes, trades, depth) arrive many times a second, so they are
          summarised: a count, a rate over the last 10 seconds and the last message. User-hub events and every
          subscribe/unsubscribe call are rare, so each one is listed below. In the kit,
          <code>market = MarketHub(client.tokens)</code> and <code>user = UserHub(client.tokens)</code>.</div>
        <div class="apilog-hub-off warn" hidden>Hub recording is off. Turn it on in Settings, under API calls.</div>
        <div class="table-wrap">
          <table class="apilog-table apilog-hub">
            <thead><tr><th>Hub</th><th>Dir</th><th>Event / method</th><th class="num">Count</th><th class="num">Rate/s</th><th>Last seen</th><th>Kit call</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="empty apilog-hub-empty">No hub messages recorded yet.</div>
        <h3 class="apilog-sub">User-hub events and subscriptions</h3>
        <div class="table-wrap apilog-scroll apilog-ring-wrap">
          <table class="apilog-table apilog-ring">
            <thead><tr><th>Time (UTC)</th><th>Hub</th><th>Dir</th><th>Message</th><th>Arguments</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="empty apilog-ring-empty">None yet. Switching account or focus contract sends subscribe calls.</div>
      </div>`;
    const q = (sel) => body.querySelector(sel);
    el = {
      tabs: [...body.querySelectorAll(".apilog-tab")],
      panes: [...body.querySelectorAll(".apilog-pane")],
      filter: q(".apilog-filter"),
      errorsOnly: q(".apilog-errors-only input"),
      status: q(".apilog-status"),
      pause: q(".apilog-pause"),
      clear: q(".apilog-clear"),
      close: q(".apilog-close"),
      limits: q(".apilog-limits"),
      restBody: q(".apilog-rest tbody"),
      restEmpty: q(".apilog-empty"),
      hubBody: q(".apilog-hub tbody"),
      hubEmpty: q(".apilog-hub-empty"),
      hubOff: q(".apilog-hub-off"),
      ringBody: q(".apilog-ring tbody"),
      ringEmpty: q(".apilog-ring-empty"),
    };
    for (const b of el.tabs) b.addEventListener("click", () => setTab(b.dataset.tab));
    el.filter.addEventListener("input", applyFilter);
    el.errorsOnly.addEventListener("change", applyFilter);
    el.pause.addEventListener("click", () => {
      paused = !paused;
      el.pause.textContent = paused ? "Resume" : "Pause";
      el.pause.classList.toggle("active", paused);
      setStatus(idleStatus());
      if (!paused) tick();
    });
    el.clear.addEventListener("click", clearAll);
    el.close.addEventListener("click", () => savePrefs({ open: false }));
    el.restBody.addEventListener("click", onRestClick);
    el.hubBody.addEventListener("click", onHubClick);
    el.ringBody.addEventListener("click", onRingClick);
    if (lastCounters) renderCounters(lastCounters);
  }

  function idleStatus() {
    return paused ? "paused" : noMatch ? "nothing matches the filter" : "";
  }

  function setStatus(text, kind) {
    if (!el.status) return;
    el.status.textContent = text || "";
    el.status.classList.toggle("error", kind === "error");
  }

  function setTab(name) {
    tab = name === "hub" ? "hub" : "rest";
    for (const b of el.tabs) {
      const on = b.dataset.tab === tab;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    }
    for (const p of el.panes) p.hidden = p.dataset.pane !== tab;
    el.filter.placeholder = tab === "rest" ? "filter: path, field, status" : "filter: event, method, argument";
    el.errorsOnly.parentElement.hidden = tab !== "rest";
    applyFilter();
    tick();
  }

  // -- open / close --------------------------------------------------------------------------
  function show(open, scroll) {
    if (open && !section) build();
    if (section) section.hidden = !open;
    tool.classList.toggle("open", open);
    tool.setAttribute("aria-pressed", open ? "true" : "false");
    if (open) {
      tick();
      if (scroll) section.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  tool.addEventListener("click", () => {
    const open = !isOpen();
    savePrefs({ open });
    if (open && section) section.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  // -- polling ---------------------------------------------------------------------------------
  function schedule() {
    if (timer) clearTimeout(timer);
    timer = isOpen() && !paused ? setTimeout(tick, POLL_MS) : null;
  }

  async function tick() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (!isOpen() || paused) return;
    if (busy) { schedule(); return; }
    busy = true;
    try {
      if (tab === "rest") await pollCalls(); else await pollHub();
      setStatus(idleStatus());
    } catch (err) {
      setStatus("Could not read the API log: " + (err && err.message ? err.message : err), "error");
    } finally {
      busy = false;
      schedule();
    }
  }

  async function pollCalls() {
    const limit = lastSeq === 0 ? MAX_ROWS : 200;
    const data = await api.get(`/api/apilog/calls?after=${lastSeq}&limit=${limit}&bodies=0`);
    if (!data) return;
    if (Number(data.last_seq) < lastSeq) {   // the dashboard restarted: start over
      resetRest();
      lastSeq = 0;
      return pollCalls();
    }
    const calls = Array.isArray(data.calls) ? data.calls : [];
    for (const rec of calls) addRow(rec);
    lastSeq = Math.max(lastSeq, Number(data.last_seq) || 0);
    trimRows();
    el.restEmpty.hidden = rows.size > 0;
    if (calls.length) applyFilter();
  }

  // -- REST table -----------------------------------------------------------------------------
  function rowHtml(rec) {
    const status = rec.status == null ? "-" : String(rec.status);
    const ms = rec.duration_ms == null ? "-" : Number(rec.duration_ms).toFixed(0);
    const result = rec.ok ? rec.result : (rec.result || rec.error || "error");
    return `<td class="apilog-time">${esc(clockMs(rec.time))}</td>`
      + `<td class="apilog-path"><span class="apilog-caret" aria-hidden="true">&#9656;</span>${esc(rec.path)}</td>`
      + `<td class="num">${esc(status)}</td>`
      + `<td class="num">${esc(ms)}</td>`
      + `<td><span class="apilog-ellipsis" title="${esc(rec.summary)}">${esc(rec.summary || "{}")}</span></td>`
      + `<td><span class="apilog-ellipsis" title="${esc(result)}">${esc(result)}</span>${rec.truncated ? ' <span class="apilog-tag" title="Shortened before storing">cut</span>' : ""}</td>`;
  }

  function addRow(rec) {
    if (rows.has(rec.seq)) return;
    const row = document.createElement("tr");
    row.className = "apilog-row" + (rec.ok ? "" : " apilog-err");
    row.dataset.seq = String(rec.seq);
    row.tabIndex = 0;
    row.innerHTML = rowHtml(rec);
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggleRest(rec.seq); }
    });
    el.restBody.insertBefore(row, el.restBody.firstChild);
    rows.set(rec.seq, { rec, row, detail: null, loaded: null });
    order.push(rec.seq);
  }

  function trimRows() {
    while (order.length > MAX_ROWS) {
      const seq = order.shift();
      const item = rows.get(seq);
      if (item) {
        item.row.remove();
        if (item.detail) item.detail.remove();
      }
      rows.delete(seq);
    }
  }

  function resetRest() {
    rows.clear();
    order.length = 0;
    el.restBody.innerHTML = "";
    el.restEmpty.hidden = false;
  }

  function matches(text) {
    const needle = el.filter.value.trim().toLowerCase();
    return !needle || text.toLowerCase().includes(needle);
  }

  function applyFilter() {
    if (!el.filter) return;
    if (tab === "rest") {
      const errorsOnly = el.errorsOnly.checked;
      let visible = 0;
      for (const item of rows.values()) {
        const r = item.rec;
        const hay = `${r.path} ${r.summary} ${r.result} ${r.status} ${r.error || ""}`;
        const show = matches(hay) && (!errorsOnly || !r.ok);
        item.row.hidden = !show;
        if (item.detail) item.detail.hidden = !show;
        if (show) visible += 1;
      }
      el.restEmpty.hidden = rows.size > 0;
      noMatch = rows.size > 0 && visible === 0;
      setStatus(idleStatus());
    } else {
      noMatch = false;
      for (const [key, item] of hubRows) {
        const show = matches(key.replace(/\|/g, " ") + " " + (item.row.dataset.kit || ""));
        item.row.hidden = !show;
        if (item.detail) item.detail.hidden = !show || !hubOpen.has(key);
      }
      for (const item of ringRows) {
        const show = matches(item.row.textContent);
        item.row.hidden = !show;
        if (item.detail) item.detail.hidden = !show || !item.open;
      }
    }
  }

  function onRestClick(ev) {
    if (ev.target.closest(".apilog-detail")) return;
    const row = ev.target.closest("tr.apilog-row");
    if (row) toggleRest(Number(row.dataset.seq));
  }

  function toggleRest(seq) {
    const item = rows.get(seq);
    if (!item) return;
    if (item.detail && !item.detail.hidden) {
      item.detail.hidden = true;
      item.row.classList.remove("expanded");
      return;
    }
    item.row.classList.add("expanded");
    if (!item.detail) {
      item.detail = document.createElement("tr");
      item.detail.className = "apilog-detail";
      item.detail.innerHTML = '<td colspan="6"><div class="apilog-detail-body muted">loading...</div></td>';
      item.row.after(item.detail);
      loadDetail(item);
    }
    item.detail.hidden = false;
  }

  async function loadDetail(item) {
    const box = item.detail.querySelector(".apilog-detail-body");
    const seq = item.rec.seq;
    try {
      const [rec, snip] = await Promise.all([
        api.get(`/api/apilog/call?seq=${seq}`),
        api.get(`/api/apilog/snippets?seq=${seq}`),
      ]);
      item.loaded = { rec, snip };
      box.classList.remove("muted");
      const meta = [`POST ${snip.url}`, rec.status == null ? "no HTTP status (the request did not get a reply)" : `HTTP ${rec.status}`,
        rec.duration_ms == null ? null : `${Number(rec.duration_ms).toFixed(0)} ms`,
        `request ${bytes(rec.request_bytes)}`, `response ${bytes(rec.response_bytes)}`,
        snip.method ? `kit: client.${snip.method}()` : "kit: client._http.post()"].filter(Boolean);
      box.innerHTML = `
        <div class="apilog-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>
        ${rec.error ? `<div class="apilog-error-text">${esc(rec.error)}</div>` : ""}
        ${rec.note ? `<div class="apilog-note">${esc(rec.note)}</div>` : ""}
        <div class="apilog-json">
          <div><div class="apilog-label">Request body (JSON sent)</div><pre>${esc(pretty(rec.request))}</pre></div>
          <div><div class="apilog-label">Response (JSON received)</div><pre>${esc(pretty(rec.response))}</pre></div>
        </div>
        <div class="apilog-actions">
          <button type="button" class="small" data-copy="curl">Copy as curl</button>
          <button type="button" class="small" data-copy="python">Copy as Python</button>
          <span class="apilog-copy-status muted">${snip.note ? esc(snip.note) : ""}</span>
        </div>
        <pre class="apilog-snippet" hidden></pre>`;
      for (const b of box.querySelectorAll("[data-copy]")) {
        b.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          const kind = b.dataset.copy;
          const text = kind === "curl" ? snip.curl : snip.python;
          const pre = box.querySelector(".apilog-snippet");
          pre.textContent = text;
          pre.hidden = false;
          const ok = await copyText(text);
          const statusEl = box.querySelector(".apilog-copy-status");
          statusEl.textContent = ok
            ? (kind === "curl" ? "curl copied. It expects your session token in $TOPSTEPX_TOKEN." : "Python copied. Run it from the repository folder with the kit installed.")
            : "Copy was blocked by the browser; select the text below instead.";
        });
      }
    } catch (err) {
      box.textContent = "Could not load this call: " + (err && err.message ? err.message : err);
    }
  }

  // -- Hub tab -----------------------------------------------------------------------------------
  async function pollHub() {
    const data = await api.get(`/api/apilog/hub?after=${hubSeq}`);
    if (!data) return;
    el.hubOff.hidden = data.capture_hub !== false;
    const events = Array.isArray(data.events) ? data.events : [];
    renderHubStats(events);
    if (Number(data.last_seq) < hubSeq) {   // restarted: start the ring over
      resetRing();
      hubSeq = 0;
      return;
    }
    const recent = Array.isArray(data.recent) ? data.recent : [];
    for (const m of recent) addRing(m);
    hubSeq = Math.max(hubSeq, Number(data.last_seq) || 0);
    while (ringRows.length > MAX_RING) {
      const old = ringRows.pop();
      old.row.remove();
      if (old.detail) old.detail.remove();
    }
    el.ringEmpty.hidden = ringRows.length > 0;
    if (recent.length) applyFilter();
  }

  const hubKey = (e) => `${e.hub}|${e.direction}|${e.name}`;

  function renderHubStats(events) {
    const keys = events.map(hubKey).join(",");
    if (keys !== hubKeys) {
      hubKeys = keys;
      hubRows.clear();
      el.hubBody.innerHTML = "";
      for (const e of events) {
        const key = hubKey(e);
        const row = document.createElement("tr");
        row.className = "apilog-row apilog-hubrow" + (e.kind === "summarised" ? " apilog-summarised" : "");
        row.dataset.key = key;
        row.tabIndex = 0;
        row.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggleHub(key); }
        });
        const detail = document.createElement("tr");
        detail.className = "apilog-detail";
        detail.hidden = !hubOpen.has(key);
        detail.innerHTML = '<td colspan="7"><div class="apilog-detail-body"><div class="apilog-label">Last message</div><pre></pre></div></td>';
        el.hubBody.appendChild(row);
        el.hubBody.appendChild(detail);
        hubRows.set(key, { row, detail });
      }
    }
    for (const e of events) {
      const item = hubRows.get(hubKey(e));
      if (!item) continue;
      const html = `<td>${esc(e.hub)}</td><td>${esc(e.direction)}</td>`
        + `<td class="apilog-path"><span class="apilog-caret" aria-hidden="true">&#9656;</span>${esc(e.name)}</td>`
        + `<td class="num">${esc(Number(e.count).toLocaleString("en-US"))}</td>`
        + `<td class="num">${esc(Number(e.rate).toFixed(1))}</td>`
        + `<td title="${esc(e.last_time)}">${esc(ago(e.age_seconds))}</td>`
        + `<td><code class="apilog-kit">${esc(e.kit || "-")}</code></td>`;
      if (item.row.dataset.html !== html) {
        item.row.innerHTML = html;
        item.row.dataset.html = html;
      }
      item.row.dataset.kit = e.kit || "";
      item.row.classList.toggle("expanded", hubOpen.has(hubKey(e)));
      if (hubOpen.has(hubKey(e))) {
        const pre = item.detail.querySelector("pre");
        const text = pretty(e.last_payload) + (e.note ? "\n\n" + e.note : "");
        if (pre.textContent !== text) pre.textContent = text;
      }
    }
    el.hubEmpty.hidden = events.length > 0;
    applyFilter();
  }

  function toggleHub(key) {
    if (hubOpen.has(key)) hubOpen.delete(key); else hubOpen.add(key);
    const item = hubRows.get(key);
    if (item) {
      item.detail.hidden = !hubOpen.has(key);
      item.row.classList.toggle("expanded", hubOpen.has(key));
    }
    if (hubOpen.has(key)) tick();
  }

  function onHubClick(ev) {
    if (ev.target.closest(".apilog-detail")) return;
    const row = ev.target.closest("tr.apilog-hubrow");
    if (row) toggleHub(row.dataset.key);
  }

  function addRing(m) {
    const row = document.createElement("tr");
    row.className = "apilog-row apilog-ringrow";
    row.dataset.seq = String(m.seq);
    row.tabIndex = 0;
    row.innerHTML = `<td class="apilog-time">${esc(clockMs(m.time))}</td><td>${esc(m.hub)}</td><td>${esc(m.direction)}</td>`
      + `<td class="apilog-path"><span class="apilog-caret" aria-hidden="true">&#9656;</span>${esc(m.name)}</td>`
      + `<td><span class="apilog-ellipsis" title="${esc(m.summary)}">${esc(m.summary || "(no arguments)")}</span></td>`;
    const detail = document.createElement("tr");
    detail.className = "apilog-detail";
    detail.hidden = true;
    detail.innerHTML = `<td colspan="5"><div class="apilog-detail-body">`
      + (m.kit ? `<div class="apilog-meta"><span>kit: <code>${esc(m.kit)}</code></span></div>` : "")
      + `<div class="apilog-label">Arguments</div><pre>${esc(pretty(m.args))}</pre>`
      + (m.note ? `<div class="apilog-note">${esc(m.note)}</div>` : "")
      + `</div></td>`;
    el.ringBody.insertBefore(detail, el.ringBody.firstChild);
    el.ringBody.insertBefore(row, detail);
    const item = { seq: m.seq, row, detail, open: false };
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggleRing(item); }
    });
    ringRows.unshift(item);
  }

  function toggleRing(item) {
    item.open = !item.open;
    item.detail.hidden = !item.open;
    item.row.classList.toggle("expanded", item.open);
  }

  function onRingClick(ev) {
    if (ev.target.closest(".apilog-detail")) return;
    const row = ev.target.closest("tr.apilog-ringrow");
    if (!row) return;
    const item = ringRows.find((r) => String(r.seq) === row.dataset.seq);
    if (item) toggleRing(item);
  }

  function resetRing() {
    ringRows.length = 0;
    el.ringBody.innerHTML = "";
    el.ringEmpty.hidden = false;
  }

  async function clearAll() {
    try {
      const data = await api.post("/api/apilog/clear", {});
      resetRest();
      resetRing();
      hubRows.clear();
      hubKeys = "";
      el.hubBody.innerHTML = "";
      el.hubEmpty.hidden = false;
      lastSeq = Number(data && data.last_seq) || lastSeq;
      hubSeq = Number(data && data.hub_last_seq) || hubSeq;
      setStatus("cleared");
    } catch (err) {
      setStatus("Could not clear: " + err.message, "error");
    }
  }

  // -- counters from /api/state ------------------------------------------------------------------
  function renderCounters(c) {
    const minute = Number(c.calls_last_minute) || 0;
    const bars = Number(c.bars_calls_last_30s) || 0;
    const rate = Number(c.hub_events_per_second) || 0;
    countEl.textContent = String(minute);
    const near = minute >= NEAR_LIMIT_MINUTE || bars >= NEAR_LIMIT_BARS;
    tool.classList.toggle("near-limit", near);
    tool.title = `${minute} REST calls in the last 60 s (most endpoints allow 200 per 60 s); `
      + `${bars} History/retrieveBars calls in the last 30 s (limit 50 per 30 s); `
      + `hub about ${rate.toFixed(1)} messages/s. Counts only this dashboard's calls. Click to show the API calls.`;
    if (el.limits) {
      el.limits.innerHTML = `Last 60 s: <b>${esc(minute)}</b> REST calls (most endpoints allow 200 per 60 s). `
        + `History/retrieveBars in the last 30 s: <b>${esc(bars)}</b> (limit 50 per 30 s). `
        + `Total since start: ${esc(Number(c.calls_total || 0).toLocaleString("en-US"))}. `
        + `Hub: about ${esc(rate.toFixed(1))} messages/s. These count only this dashboard's own calls.`;
      el.limits.classList.toggle("warn", near);
      el.limits.classList.toggle("muted", !near);
    }
  }

  api.onState((s) => {
    const c = s && s.features ? s.features.apilog : null;
    if (!c || c.error) return;
    lastCounters = c;
    renderCounters(c);
  });

  // -- settings ------------------------------------------------------------------------------------
  // The page setting decides whether the server records hub traffic: sent once on load (when it
  // differs from the server) and whenever the user changes it.
  let captureAgain = false;
  async function syncCapture(on) {
    if (syncingCapture) { captureAgain = true; return; }
    syncingCapture = true;
    try {
      await api.post("/api/apilog/capture", { hub: on });
    } catch (err) {
      api.notify("Could not change hub recording: " + err.message, "error");
    } finally {
      syncingCapture = false;
      if (captureAgain) {   // the setting changed again while the first request was in flight
        captureAgain = false;
        syncCapture(prefs().captureHub);
      }
    }
  }

  let setOpenBox = null;
  let setHubBox = null;
  api.registerSettingsSection({
    title: "API calls",
    render(container, settings, update) {
      container.innerHTML = `
        <label class="set-row"><input type="checkbox" class="apilog-set-open"> Show the API calls panel</label>
        <label class="set-row"><input type="checkbox" class="apilog-set-hub"> Record hub traffic (counts, rates, last message)</label>
        <div class="set-note">REST calls are always recorded (the newest 500, secrets redacted, kept in memory only).
          Hub recording adds a little work for every market message.</div>`;
      setOpenBox = container.querySelector(".apilog-set-open");
      setHubBox = container.querySelector(".apilog-set-hub");
      const p = prefs();
      setOpenBox.checked = p.open;
      setHubBox.checked = p.captureHub;
      setOpenBox.addEventListener("change", () => update({ apilog: { ...prefs(), open: setOpenBox.checked } }));
      setHubBox.addEventListener("change", () => update({ apilog: { ...prefs(), captureHub: setHubBox.checked } }));
    },
  });

  let applied = { open: null, captureHub: null };
  function applyPrefs(scroll) {
    const p = prefs();
    if (setOpenBox) setOpenBox.checked = p.open;
    if (setHubBox) setHubBox.checked = p.captureHub;
    if (p.open !== applied.open) show(p.open, scroll);
    if (applied.captureHub !== null && p.captureHub !== applied.captureHub) syncCapture(p.captureHub);
    applied = p;
  }
  api.onSettings(() => applyPrefs(false));

  applyPrefs(false);
  const snap = typeof api.snapshot === "function" ? api.snapshot() : null;
  const server = snap && snap.features && snap.features.apilog;
  if (server && typeof server.capture_hub === "boolean" && server.capture_hub !== prefs().captureHub) {
    syncCapture(prefs().captureHub);
  }
}
