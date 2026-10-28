// alerts.js - page side of the alerts feature: price alerts you set, fill notifications, and your
// own guardrails (a daily loss number and a contract cap that pause order entry on this dashboard).
//
// Everything here is driven by numbers the trader types. Nothing is suggested, and nothing here
// is advice. The module only GETs and POSTs to /api/alerts/*; it never touches order routes.

const LIST_MS = 2000;      // refresh the alert list while the panel is open
const EVENTS_MS = 1000;    // poll new events (alerts, fills, tests) while the page is open
const MAX_FEED = 60;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export async function mount(api, info) {
  const settings = api.settings || {};
  settings.alerts = { desktop: false, sound: false, fills: true, open: false, ...(settings.alerts || {}) };
  const prefs = settings.alerts;
  const save = () => { if (typeof api.saveSettings === "function") api.saveSettings(); };

  // ---- header button + panel ----------------------------------------------------------
  const button = document.createElement("button");
  button.type = "button";
  button.className = "alerts-btn";
  button.title = "Alerts and guardrails";
  button.innerHTML = 'Alerts<span class="alerts-count"></span>';
  api.addHeaderTool(button);
  const countEl = button.querySelector(".alerts-count");

  const root = api.registerPanel({ id: "alerts", title: "Alerts and guardrails", span: 6, before: "feature-journal" });
  const section = root.closest("section");
  root.innerHTML = `
    <div class="al-note muted">Alert prices, the loss number and the contract cap are yours. Figures are approximations built from API data.</div>
    <div class="al-block">
      <div class="al-title">Guardrails</div>
      <form class="al-guard">
        <label>Max daily loss <input type="number" class="al-loss" min="0" step="any" placeholder="off"></label>
        <label>Max open contracts <input type="number" class="al-cap" min="1" step="1" placeholder="off"></label>
        <button type="submit">Save</button>
        <button type="button" class="al-clear">Clear pause</button>
      </form>
      <div class="al-guard-status muted"></div>
    </div>
    <div class="al-block">
      <div class="al-title">Add a price alert <span class="muted">(or Alt+click the chart)</span></div>
      <form class="al-add">
        <select class="al-contract" aria-label="Contract"></select>
        <input type="number" class="al-price" step="any" placeholder="price" required aria-label="Price">
        <select class="al-dir" aria-label="Direction">
          <option value="cross">crosses</option>
          <option value="above">moves above</option>
          <option value="below">moves below</option>
        </select>
        <input type="text" class="al-text" maxlength="120" placeholder="note (optional)" aria-label="Note">
        <label class="al-once"><input type="checkbox" class="al-once-box" checked> once</label>
        <button type="submit" class="primary">Add</button>
      </form>
    </div>
    <div class="al-block">
      <div class="al-title">Alerts <button type="button" class="al-test small">Test notification</button></div>
      <div class="al-list"></div>
    </div>
    <div class="al-block">
      <div class="al-title">Recent events</div>
      <div class="al-feed"></div>
    </div>
    <div class="al-error"></div>`;
  const q = (sel) => root.querySelector(sel);
  const el = {
    loss: q(".al-loss"), cap: q(".al-cap"), guardForm: q(".al-guard"), clear: q(".al-clear"), guardStatus: q(".al-guard-status"),
    addForm: q(".al-add"), contract: q(".al-contract"), price: q(".al-price"), dir: q(".al-dir"), text: q(".al-text"), once: q(".al-once-box"),
    list: q(".al-list"), feed: q(".al-feed"), test: q(".al-test"), error: q(".al-error"),
  };

  let listing = null;
  let lastSeq = null;        // events seen; null until the first poll (which only records the current seq)
  let listTimer = null;
  const feed = [];
  const lines = new Map();   // alert id -> IPriceLine on the chart

  function setOpen(open) {
    prefs.open = !!open;
    section.hidden = !prefs.open;
    button.classList.toggle("open", prefs.open);
    save();
    if (prefs.open) {
      refreshList();
      if (!listTimer) listTimer = setInterval(refreshList, LIST_MS);
    } else if (listTimer) {
      clearInterval(listTimer);
      listTimer = null;
    }
  }
  button.addEventListener("click", () => setOpen(section.hidden));

  function showError(text) {
    el.error.textContent = text || "";
    el.error.hidden = !text;
  }

  // ---- contracts for the add form -----------------------------------------------------------
  let contractKey = "";
  function syncContracts(snapshot) {
    const list = (snapshot && snapshot.contracts) || [];
    const key = list.map((c) => c.id).join("|");
    if (key === contractKey) return;
    contractKey = key;
    const current = el.contract.value;
    el.contract.innerHTML = list.map((c) => `<option value="${esc(c.id)}">${esc(c.name || c.id)}</option>`).join("");
    if (list.some((c) => c.id === current)) el.contract.value = current;
    else if (snapshot.focus && list.some((c) => c.id === snapshot.focus)) el.contract.value = snapshot.focus;
  }

  function tickFor(contractId) {
    const snap = api.snapshot();
    const c = snap && (snap.contracts || []).find((x) => x.id === contractId);
    const tick = c ? Number(c.tick_size) : NaN;
    return Number.isFinite(tick) && tick > 0 ? tick : null;
  }

  function decimals(tick) {
    if (!tick) return 2;
    const text = String(tick);
    const dot = text.indexOf(".");
    return dot < 0 ? 0 : Math.min(text.length - dot - 1, 6);
  }

  // ---- list + guard --------------------------------------------------------------------------
  async function refreshList() {
    try {
      listing = await api.get("/api/alerts");
      showError("");
    } catch (err) {
      showError("Could not load alerts: " + (err && err.message ? err.message : err));
      return;
    }
    renderList();
    renderGuard();
    drawLines();
  }

  function renderGuard() {
    if (!listing) return;
    const g = listing.guard || {};
    if (document.activeElement !== el.loss) el.loss.value = g.max_daily_loss == null ? "" : String(g.max_daily_loss);
    if (document.activeElement !== el.cap) el.cap.value = g.max_contracts == null ? "" : String(g.max_contracts);
    const totals = (listing.status && listing.status.totals) || {};
    const parts = [];
    parts.push("Today's P&L approx. " + api.fmt.money(totals.total) + ", open contracts " + api.fmt.int(totals.open_contracts) + ".");
    if (g.max_daily_loss == null && g.max_contracts == null) parts.push("No guard set.");
    if (listing.status && listing.status.blocked) parts.push(listing.status.blocked);
    if (listing.status && listing.status.save_error) parts.push("Could not save: " + listing.status.save_error);
    el.guardStatus.textContent = parts.join(" ");
    el.guardStatus.classList.toggle("blocked", !!(listing.status && listing.status.blocked));
    el.clear.hidden = !(g.paused_at);
  }

  function renderList() {
    const alerts = (listing && listing.alerts) || [];
    const armed = alerts.filter((a) => a.armed).length;
    countEl.textContent = alerts.length ? ` (${armed}/${alerts.length})` : "";
    if (!alerts.length) {
      el.list.innerHTML = '<div class="muted">No alerts yet.</div>';
      return;
    }
    el.list.innerHTML = `<table><thead><tr><th>Contract</th><th>Direction</th><th>Price</th><th>Note</th><th>State</th><th>Last</th><th></th></tr></thead><tbody>`
      + alerts.map((a) => {
        const tick = tickFor(a.contract_id);
        const price = Number(a.price).toFixed(decimals(tick));
        const state = a.armed ? "armed" : (a.once ? "fired" : "waiting to re-arm");
        const last = a.last_triggered ? api.fmt.clock(a.last_triggered) : "-";
        return `<tr data-id="${esc(a.id)}" class="${a.armed ? "" : "fired"}">`
          + `<td>${esc(a.name || a.contract_id)}</td><td>${esc(a.direction)}</td><td>${esc(price)}</td><td>${esc(a.note || "")}</td>`
          + `<td>${esc(state)}${a.trigger_count ? ` <span class="muted">x${a.trigger_count}</span>` : ""}</td><td>${esc(last)}</td>`
          + `<td class="al-actions">${a.armed ? "" : '<button type="button" class="small" data-act="rearm">Re-arm</button> '}<button type="button" class="small danger" data-act="delete">Delete</button></td></tr>`;
      }).join("") + "</tbody></table>";
  }

  el.list.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.closest("tr").dataset.id;
    try {
      if (btn.dataset.act === "delete") await api.post("/api/alerts/delete", { id });
      else await api.post("/api/alerts/update", { id, armed: true });
      await refreshList();
    } catch (err) {
      showError(err && err.message ? err.message : String(err));
    }
  });

  el.addForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {
      contract_id: el.contract.value,
      price: Number(el.price.value),
      direction: el.dir.value,
      note: el.text.value.trim(),
      once: el.once.checked,
    };
    try {
      await api.post("/api/alerts/add", body);
      el.price.value = "";
      el.text.value = "";
      await refreshList();
      api.notify("Alert added at " + body.price + ".");
    } catch (err) {
      showError(err && err.message ? err.message : String(err));
    }
  });

  el.guardForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {
      max_daily_loss: el.loss.value.trim() === "" ? null : Number(el.loss.value),
      max_contracts: el.cap.value.trim() === "" ? null : parseInt(el.cap.value, 10),
    };
    try {
      const res = await api.post("/api/alerts/guard", body);
      await refreshList();
      api.notify(res && res.blocked ? res.blocked : "Guard saved.", res && res.blocked ? "warn" : undefined);
    } catch (err) {
      showError(err && err.message ? err.message : String(err));
    }
  });

  el.clear.addEventListener("click", async () => {
    try {
      const res = await api.post("/api/alerts/guard/clear", {});
      await refreshList();
      api.notify(res && res.note ? res.note : "Pause cleared.", res && res.note ? "warn" : undefined);
    } catch (err) {
      showError(err && err.message ? err.message : String(err));
    }
  });

  el.test.addEventListener("click", async () => {
    try { await api.post("/api/alerts/test", {}); } catch (err) { showError(err && err.message ? err.message : String(err)); }
  });

  // ---- events: toast, sound, desktop notification ---------------------------------------------
  let audio = null;
  function beep() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      if (!audio) audio = new Ctx();
      const now = audio.currentTime;
      for (const [offset, freq] of [[0, 880], [0.16, 1175]]) {
        const osc = audio.createOscillator();
        const gain = audio.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + offset);
        gain.gain.exponentialRampToValueAtTime(0.2, now + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.14);
        osc.connect(gain).connect(audio.destination);
        osc.start(now + offset);
        osc.stop(now + offset + 0.16);
      }
    } catch (err) { /* audio is optional */ }
  }

  function desktop(text) {
    if (!prefs.desktop || typeof Notification === "undefined" || Notification.permission !== "granted") return;
    try { new Notification("TopstepX dashboard", { body: text, silent: true }); } catch (err) { /* optional */ }
  }

  function handleEvent(e) {
    if (!e || !e.kind) return;
    if (e.kind === "fill" && !prefs.fills) return;
    const text = e.text || e.kind;
    const kind = e.kind === "alert" ? "warn" : e.kind === "guard" ? "error" : undefined;
    api.notify(text, kind);
    if (prefs.sound) beep();
    desktop(text);
    feed.unshift(e);
    if (feed.length > MAX_FEED) feed.length = MAX_FEED;
    renderFeed();
    if (e.kind === "alert" && !section.hidden) refreshList();
    else if (e.kind === "alert") drawLinesSoon();
  }

  function renderFeed() {
    if (!feed.length) {
      el.feed.innerHTML = '<div class="muted">Nothing yet. Alerts, fills and test notifications show here.</div>';
      return;
    }
    el.feed.innerHTML = feed.map((e) => `<div class="al-ev ${esc(e.kind)}"><span class="muted">${esc(api.fmt.clock(e.time))}</span> ${esc(e.text || e.kind)}</div>`).join("");
  }
  renderFeed();

  async function pollEvents() {
    try {
      const data = await api.get("/api/alerts/events?after=" + encodeURIComponent(String(lastSeq == null ? 0 : lastSeq)));
      const events = (data && data.events) || [];
      if (lastSeq == null) {
        // First poll: do not replay history as notifications, just show it in the feed.
        for (const e of events.slice(-MAX_FEED)) feed.unshift(e);
        renderFeed();
      } else {
        for (const e of events) handleEvent(e);
      }
      if (data && typeof data.last_seq === "number") lastSeq = data.last_seq;
    } catch (err) { /* the next poll retries */ }
  }
  setInterval(pollEvents, EVENTS_MS);
  pollEvents();

  // ---- chart: alert lines and Alt+click to add ------------------------------------------------
  let drawTimer = null;
  function drawLinesSoon() {
    if (drawTimer) return;
    drawTimer = setTimeout(() => { drawTimer = null; drawLines(); }, 50);
  }

  function drawLines() {
    const chart = api.chart;
    if (!chart || !chart.candleSeries) return;
    const contract = chart.contract();
    const alerts = ((listing && listing.alerts) || []).filter((a) => a.contract_id === contract);
    const wanted = new Set(alerts.map((a) => a.id));
    for (const [id, line] of [...lines]) {
      if (!wanted.has(id)) {
        try { chart.candleSeries.removePriceLine(line); } catch (err) { /* already gone */ }
        lines.delete(id);
      }
    }
    const LC = chart.LC || {};
    const dotted = LC.LineStyle && LC.LineStyle.Dotted != null ? LC.LineStyle.Dotted : 1;
    for (const a of alerts) {
      const opts = {
        price: Number(a.price), color: a.armed ? "#f5b041" : "#8aa096", lineWidth: 1, lineStyle: dotted,
        axisLabelVisible: true, title: (a.note ? a.note : "alert") + (a.armed ? "" : " (fired)"),
      };
      const existing = lines.get(a.id);
      if (existing) existing.applyOptions(opts);
      else lines.set(a.id, chart.candleSeries.createPriceLine(opts));
    }
  }

  let altDown = false;
  document.addEventListener("keydown", (ev) => { if (ev.key === "Alt") altDown = true; });
  document.addEventListener("keyup", (ev) => { if (ev.key === "Alt") altDown = false; });
  window.addEventListener("blur", () => { altDown = false; });

  function wireChart() {
    const chart = api.chart;
    if (!chart || !chart.chart) return false;
    chart.onBars(() => drawLinesSoon());
    chart.chart.subscribeClick((param) => {
      const alt = (param && param.sourceEvent && param.sourceEvent.altKey) || altDown;
      if (!alt || !param || !param.point) return;
      const raw = chart.candleSeries.coordinateToPrice(param.point.y);
      if (raw == null || !Number.isFinite(raw)) return;
      const contract = chart.contract();
      const tick = tickFor(contract);
      const price = tick ? Math.round(raw / tick) * tick : raw;
      el.contract.value = contract;
      el.price.value = price.toFixed(decimals(tick));
      if (section.hidden) setOpen(true);
      el.text.focus();
      api.notify("Alert price set to " + el.price.value + ". Add a note and press Add.");
    });
    return true;
  }
  if (!wireChart()) {
    const retry = setInterval(() => { if (wireChart()) clearInterval(retry); }, 1000);
  }

  // ---- banner when the guard pauses order entry ------------------------------------------------
  const banner = document.createElement("div");
  banner.className = "al-banner";
  banner.hidden = true;
  document.body.appendChild(banner);

  api.onState((snapshot) => {
    syncContracts(snapshot);
    const mine = snapshot && snapshot.features && snapshot.features.alerts;
    if (mine) {
      if (!listing) countEl.textContent = mine.alerts_count ? ` (${mine.armed_count}/${mine.alerts_count})` : "";
      if (listing && mine.version !== listing.version && !section.hidden) refreshList();
      else if (listing && mine.version !== listing.version) refreshList();
    }
    const reason = snapshot && snapshot.order_entry_block;
    if (reason && mine && mine.blocked) {
      banner.hidden = false;
      banner.innerHTML = `<span>${esc(reason)}</span> <button type="button" class="small">Change guard</button>`;
      banner.querySelector("button").onclick = () => { setOpen(true); el.loss.focus(); section.scrollIntoView({ behavior: "smooth" }); };
    } else {
      banner.hidden = true;
    }
  });

  // ---- settings section -------------------------------------------------------------------------
  api.registerSettingsSection({
    title: "Alerts",
    render(container, all, update) {
      container.innerHTML = `
        <label class="set-row"><input type="checkbox" class="al-set-desktop"> Desktop notifications</label>
        <label class="set-row"><input type="checkbox" class="al-set-sound"> Sound</label>
        <label class="set-row"><input type="checkbox" class="al-set-fills"> Notify on fills</label>
        <div class="set-note">Alert levels and guard numbers are set in the Alerts panel.</div>`;
      const d = container.querySelector(".al-set-desktop");
      const s = container.querySelector(".al-set-sound");
      const f = container.querySelector(".al-set-fills");
      d.checked = prefs.desktop; s.checked = prefs.sound; f.checked = prefs.fills;
      d.addEventListener("change", async () => {
        if (d.checked && typeof Notification !== "undefined" && Notification.permission !== "granted") {
          try { await Notification.requestPermission(); } catch (err) { /* ignore */ }
          if (Notification.permission !== "granted") { d.checked = false; api.notify("Desktop notifications were not allowed by the browser.", "warn"); }
        }
        prefs.desktop = d.checked; update({ alerts: prefs });
      });
      s.addEventListener("change", () => { prefs.sound = s.checked; update({ alerts: prefs }); if (s.checked) beep(); });
      f.addEventListener("change", () => { prefs.fills = f.checked; update({ alerts: prefs }); });
    },
  });

  section.hidden = !prefs.open;
  if (prefs.open) setOpen(true);
  const first = api.snapshot();
  if (first) syncContracts(first);
}
