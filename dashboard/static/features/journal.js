// journal.js - "Journal and session stats": your fills paired into round trips, with daily totals,
// stats, a weekday x hour grid, MFE/MAE per round trip and a CSV export.
//
// Read-only. Everything shown is an approximation built from the fills and bars the API returned
// (FIFO pairing, UTC days). Nothing here is advice or a signal.

const STORAGE_KEY = "dashboard.journal";
const DAY_CHOICES = [1, 5, 20, 60];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function load() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {}; } catch (err) { return {}; }
}

function save(patch) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...load(), ...patch })); } catch (err) { /* private mode */ }
}

function localOffsetMinutes() {
  return -new Date().getTimezoneOffset();
}

function offsetLabel(minutes) {
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  return `UTC${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

function duration(seconds) {
  if (seconds == null) return "-";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

function signClass(v) {
  return v > 0 ? "pos" : v < 0 ? "neg" : "";
}

export function mount(api) {
  const prefs = load();
  const button = el("button", "journal-btn", "Journal");
  button.type = "button";
  button.title = "Round trips, daily totals and stats from your fills";
  button.setAttribute("aria-expanded", "false");
  api.addHeaderTool(button);

  const body = api.registerPanel({ id: "journal", title: "Journal and session stats", span: 12 });
  const section = body.parentElement;
  section.classList.add("journal-section");
  body.classList.add("journal");

  // -- controls ----------------------------------------------------------------------------
  const controls = el("div", "jr-controls");
  const daysSelect = el("select");
  daysSelect.setAttribute("aria-label", "Days back");
  for (const d of DAY_CHOICES) {
    const o = el("option", null, d === 1 ? "Last 1 day" : `Last ${d} days`);
    o.value = String(d);
    daysSelect.appendChild(o);
  }
  daysSelect.value = DAY_CHOICES.includes(prefs.days) ? String(prefs.days) : "5";

  const tzSelect = el("select");
  tzSelect.setAttribute("aria-label", "Heatmap time zone");
  const local = localOffsetMinutes();
  const tzChoices = [0];
  if (local !== 0) tzChoices.push(local);
  for (const m of tzChoices) {
    const o = el("option", null, m === 0 ? "Heatmap in UTC" : `Heatmap in your clock (${offsetLabel(m)})`);
    o.value = String(m);
    tzSelect.appendChild(o);
  }
  tzSelect.value = tzChoices.includes(prefs.tz) ? String(prefs.tz) : "0";

  const refresh = el("button", "small", "Refresh");
  refresh.type = "button";
  const exportBtn = el("button", "small", "Export CSV");
  exportBtn.type = "button";
  exportBtn.title = "Writes one CSV of these round trips into the kit's journal/ folder";
  const status = el("span", "muted jr-status", "");
  controls.append(daysSelect, tzSelect, refresh, exportBtn, status);

  const note = el("p", "muted jr-note", "Approximations from API data (FIFO pairing, UTC days). Not advice or a signal.");
  const tiles = el("div", "jr-tiles");
  const tablesRow = el("div", "jr-cols");
  const dailyBox = el("div", "jr-box");
  const heatBox = el("div", "jr-box");
  tablesRow.append(dailyBox, heatBox);
  const tripBox = el("div", "jr-box");
  body.append(controls, note, tiles, tablesRow, tripBox);

  // -- rendering ------------------------------------------------------------------------------
  const money = (v) => api.fmt.money(v);

  function tile(label, value, cls, title) {
    const t = el("div", "jr-tile");
    t.append(el("div", "jr-tile-label", label), el("div", "jr-tile-value " + (cls || ""), value));
    if (title) t.title = title;
    return t;
  }

  function renderTiles(s) {
    tiles.textContent = "";
    tiles.append(
      tile("Round trips", s.round_trips),
      tile("Closed with a gain", s.closed_with_gain, "pos"),
      tile("Closed with a loss", s.closed_with_loss, "neg"),
      tile("Closed flat", s.flat),
      tile("Gross", money(s.gross), signClass(s.gross)),
      tile("Fees", money(s.fees)),
      tile("Net after fees", money(s.net), signClass(s.net)),
      tile("Avg net per round trip", s.avg_net == null ? "-" : money(s.avg_net), signClass(s.avg_net)),
      tile("Avg hold", duration(s.avg_hold_seconds)),
      tile("Largest gain", s.largest_gain == null ? "-" : money(s.largest_gain), "pos"),
      tile("Largest loss", s.largest_loss == null ? "-" : money(s.largest_loss), "neg"),
      tile("Longest run of gains", s.longest_gain_run),
      tile("Longest run of losses", s.longest_loss_run),
    );
  }

  function table(headers, rows) {
    const wrap = el("div", "table-wrap");
    const t = el("table");
    const head = el("tr");
    for (const h of headers) head.appendChild(el("th", null, h));
    const thead = el("thead");
    thead.appendChild(head);
    const tbody = el("tbody");
    for (const r of rows) tbody.appendChild(r);
    t.append(thead, tbody);
    wrap.appendChild(t);
    return wrap;
  }

  function cell(text, cls) {
    return el("td", cls || "", text);
  }

  function renderDaily(daily, contracts) {
    dailyBox.textContent = "";
    dailyBox.appendChild(el("h3", null, "Per day (UTC) and per contract"));
    if (!daily.length) {
      dailyBox.appendChild(el("div", "empty", "No round trips closed in this window."));
      return;
    }
    const rows = daily.slice().reverse().map((d) => {
      const tr = el("tr");
      tr.append(cell(d.date), cell(d.round_trips), cell(d.gains), cell(d.losses), cell(money(d.gross)),
        cell(money(d.fees)), cell(money(d.net), signClass(d.net)));
      return tr;
    });
    dailyBox.appendChild(table(["Day", "Round trips", "Gain", "Loss", "Gross", "Fees", "Net"], rows));
    const crow = (contracts || []).map((c) => {
      const tr = el("tr");
      tr.append(cell(api.contractName(c.contract_id)), cell(c.round_trips), cell(c.gains), cell(c.losses),
        cell(money(c.gross)), cell(money(c.fees)), cell(money(c.net), signClass(c.net)));
      return tr;
    });
    if (crow.length) dailyBox.appendChild(table(["Contract", "Round trips", "Gain", "Loss", "Gross", "Fees", "Net"], crow));
  }

  function renderHeatmap(h) {
    heatBox.textContent = "";
    heatBox.appendChild(el("h3", null, `Net by weekday and hour closed (${offsetLabel(h.offset_minutes)})`));
    let peak = 0;
    for (const row of h.net) for (const v of row) peak = Math.max(peak, Math.abs(v));
    const grid = el("div", "jr-heat");
    grid.appendChild(el("span", "jr-heat-corner", ""));
    for (let hour = 0; hour < 24; hour++) grid.appendChild(el("span", "jr-heat-hour", hour % 3 === 0 ? hour : ""));
    for (let d = 0; d < 7; d++) {
      grid.appendChild(el("span", "jr-heat-day", WEEKDAYS[d]));
      for (let hour = 0; hour < 24; hour++) {
        const count = h.count[d][hour];
        const net = h.net[d][hour];
        const c = el("span", "jr-heat-cell");
        if (count) {
          const alpha = peak ? 0.18 + 0.72 * (Math.abs(net) / peak) : 0.18;
          c.style.background = `${net >= 0 ? "rgba(38,166,154," : "rgba(239,83,80,"}${alpha.toFixed(2)})`;
        }
        c.title = `${WEEKDAYS[d]} ${String(hour).padStart(2, "0")}:00: ${count} round trip${count === 1 ? "" : "s"}, net ${money(net)}`;
        grid.appendChild(c);
      }
    }
    heatBox.appendChild(grid);
  }

  function pointsText(v) {
    return v == null ? "-" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 4 });
  }

  async function showExcursion(trip, btn, out) {
    btn.disabled = true;
    out.textContent = "loading...";
    try {
      const r = await api.get(`/api/journal/excursion?id=${encodeURIComponent(trip.id)}&days=${daysSelect.value}`);
      if (r.mfe_points == null) {
        out.textContent = r.note || "no bars";
      } else {
        const m = r.money_available ? ` (${money(r.mfe_money)} / -${money(r.mae_money)})` : " (points only)";
        out.textContent = `MFE ${pointsText(r.mfe_points)} / MAE ${pointsText(r.mae_points)} pts${m}`;
      }
      out.title = r.note || "";
    } catch (err) {
      out.textContent = "";
      api.notify(err.message || "Could not load MFE/MAE.", "warn");
    } finally {
      btn.disabled = false;
    }
  }

  function renderTrips(trips, extra) {
    tripBox.textContent = "";
    tripBox.appendChild(el("h3", null, "Round trips (newest first)"));
    if (!trips.length) {
      tripBox.appendChild(el("div", "empty", "No round trips in this window."));
    } else {
      const rows = trips.slice().reverse().slice(0, 300).map((t) => {
        const tr = el("tr");
        const act = el("td", "jr-excursion");
        const btn = el("button", "small", "MFE/MAE");
        btn.type = "button";
        btn.title = "Loads 1-minute bars for this round trip's window (one API call, then cached)";
        const out = el("span", "muted");
        btn.addEventListener("click", () => showExcursion(t, btn, out));
        act.append(btn, document.createTextNode(" "), out);
        tr.append(cell(api.fmt.clock(t.exit_time)), cell(api.contractName(t.contract_id)), cell(t.side),
          cell(t.size), cell(api.fmt.price(t.entry_price, t.contract_id)), cell(api.fmt.price(t.exit_price, t.contract_id)),
          cell(duration(t.hold_seconds)), cell(money(t.pnl)), cell(money(t.fees)), cell(money(t.net), signClass(t.net)), act);
        tr.children[0].title = `${t.entry_time} -> ${t.exit_time}`;
        return tr;
      });
      tripBox.appendChild(table(["Closed (UTC)", "Contract", "Side", "Size", "Entry", "Exit", "Held", "Gross", "Fees", "Net", "MFE/MAE"], rows));
    }
    if (extra) tripBox.appendChild(el("p", "muted jr-fine", extra));
  }

  let loading = false;
  async function reload() {
    if (loading) return;
    loading = true;
    status.textContent = "loading...";
    try {
      const d = await api.get(`/api/journal?days=${daysSelect.value}&tz=${tzSelect.value}`);
      renderTiles(d.stats);
      renderDaily(d.daily, d.stats.contracts);
      renderHeatmap(d.heatmap);
      const bits = [];
      if (d.unmatched_fills) bits.push(`${d.unmatched_fills} closing fill(s) began before this window and are not paired.`);
      if (d.open_lots && d.open_lots.length) bits.push(`${d.open_lots.length} open lot(s) are not counted until they close.`);
      renderTrips(d.round_trips, bits.join(" "));
      note.textContent = d.note;
      status.textContent = `${d.trades_count} fill(s), ${d.round_trips.length} round trip(s)`;
    } catch (err) {
      status.textContent = "";
      api.notify(err.message || "Could not load the journal.", "warn");
    } finally {
      loading = false;
    }
  }

  async function exportCsv() {
    exportBtn.disabled = true;
    try {
      const r = await api.post("/api/journal/export", { days: Number(daysSelect.value) });
      api.notify(`Saved ${r.rows} round trip(s) to ${r.path}`);
    } catch (err) {
      api.notify(err.message || "Export failed.", "warn");
    } finally {
      exportBtn.disabled = false;
    }
  }

  function setOpen(open) {
    section.hidden = !open;
    button.classList.toggle("open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
    save({ open });
    if (open) reload();
  }

  daysSelect.addEventListener("change", () => { save({ days: Number(daysSelect.value) }); reload(); });
  tzSelect.addEventListener("change", () => { save({ tz: Number(tzSelect.value) }); reload(); });
  refresh.addEventListener("click", reload);
  exportBtn.addEventListener("click", exportCsv);
  button.addEventListener("click", () => {
    const opening = section.hidden;
    setOpen(opening);
    if (opening) section.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  setOpen(prefs.open === true);
}
