// Chart panel: Lightweight Charts candles + volume with EMA, VWAP and Supertrend overlays, the
// indicator plugins the server finds in dashboard/indicators_ext (GET /api/indicators) and an
// optional volume profile. Indicators are drawn with the parameters the user picks. They are
// lines on a chart, nothing more.

const STORAGE_KEY = "dashboard.chart";
const REFRESH_MS = 5000;              // full reload of /api/bars for the focus contract
const MIN_STATE_RELOAD_MS = 1500;     // throttle reloads triggered by snapshots (e.g. no bars yet -> 404)
const EMA_COLORS = ["#f5b041", "#5dade2", "#af7ac5", "#f1948a", "#82e0aa", "#f7dc6f", "#85c1e9", "#d7bde2"];
const VWAP_COLOR = "#c8d6e5";
const ST_UP = "#26a69a";
const ST_DOWN = "#ef5350";
const DEFAULTS = { emaOn: true, ema: "9,21", vwap: true, st: true, stPeriod: 10, stMult: 3, plugins: {}, profile: "" };
// Bar size is any whole number of seconds, minutes, hours or days (the server validates 1-1000
// units and 1-5000 bars); the quick buttons are shortcuts for the common minute sizes.
const BAR_UNITS = [["second", "sec"], ["minute", "min"], ["hour", "hour"], ["day", "day"]];
const QUICK_BARS = [["minute", 1, "1m"], ["minute", 5, "5m"], ["minute", 15, "15m"], ["hour", 1, "1h"], ["day", 1, "1D"]];
const MAX_UNIT_NUMBER = 1000;
const MAX_HISTORY = 5000;
const INITIAL_VISIBLE_BARS = 200;   // bars on screen when a contract or bar size first loads; scroll left for more
const HISTORY_EDGE_BARS = 40;       // ask for older bars when fewer than this many are left off-screen to the left
const MAX_LOADED_BARS = 20000;      // the server keeps this many per contract (the API's per-request maximum)

// Indicator plugins (dashboard/indicators_ext/*.py, listed by GET /api/indicators) and the volume profile.
const PLUGIN_CSS = "/static/features/indicators.css";
const PLUGIN_COLORS = ["#5dade2", "#f5b041", "#af7ac5", "#82e0aa", "#f1948a", "#f7dc6f"];
const PLUGIN_LIST_MS = 15000;       // re-scan the plugin folder this often (and whenever the menu opens)
const MAX_STORED_PLUGINS = 100;
const PRICE_PANE_PX = 420;          // .chart-canvas height in style.css; the price pane keeps about this much
const PLUGIN_PANE_PX = 120;         // each plugin drawn in its own pane adds this much below the price pane
const MARKER_POSITIONS = new Set(["aboveBar", "belowBar", "inBar"]);
const MARKER_SHAPES = new Set(["circle", "square", "arrowUp", "arrowDown"]);
const PROFILE_MODES = ["session", "bars"];
const PROFILE_WIDTH = 0.2;          // share of the price pane width used by the longest profile row
const PROFILE_MIN_ROW_PX = 3;       // thinner rows are merged so every drawn row stays visible
const PROFILE_FILL = "rgba(138, 160, 150, 0.28)";
const PROFILE_POC_FILL = "rgba(200, 214, 205, 0.55)";
const PROFILE_TEXT = "#d7e0dc";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function parsePeriods(text) {
  const seen = new Set();
  const out = [];
  for (const part of String(text || "").split(/[\s,;]+/)) {
    const n = parseInt(part, 10);
    if (Number.isFinite(n) && n > 0 && n <= 1000 && !seen.has(n)) {
      seen.add(n);
      out.push(n);
    }
  }
  return out.slice(0, EMA_COLORS.length);
}

// Stored plugin settings: {name: {on: bool, params: {param: number | bool | string}}}. Values are
// checked again by the server (parse_params), so this only keeps the shape sane.
function normalizePlugins(raw) {
  const out = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [name, entry] of Object.entries(raw).slice(0, MAX_STORED_PLUGINS)) {
    if (!name || name.length > 64 || !entry || typeof entry !== "object") continue;
    const params = {};
    const source = entry.params && typeof entry.params === "object" && !Array.isArray(entry.params) ? entry.params : {};
    for (const [key, value] of Object.entries(source)) {
      const ok = (typeof value === "number" && Number.isFinite(value)) || typeof value === "boolean"
        || (typeof value === "string" && value.length <= 200);
      if (key.length <= 64 && ok) params[key] = value;
    }
    out[name] = { on: entry.on === true, params };
  }
  return out;
}

function normalizeSettings(raw) {
  const s = { ...DEFAULTS, ...(raw && typeof raw === "object" ? raw : {}) };
  const period = parseInt(s.stPeriod, 10);
  const mult = parseFloat(s.stMult);
  return {
    emaOn: s.emaOn !== false,
    ema: parsePeriods(s.ema).join(","),
    vwap: !!s.vwap,
    st: !!s.st,
    stPeriod: Number.isFinite(period) && period > 0 ? Math.min(period, 500) : DEFAULTS.stPeriod,
    stMult: Number.isFinite(mult) && mult > 0 ? Math.min(Math.round(mult * 100) / 100, 20) : DEFAULTS.stMult,
    plugins: normalizePlugins(s.plugins),
    profile: PROFILE_MODES.includes(s.profile) ? s.profile : "",
  };
}

function loadSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return normalizeSettings(raw ? JSON.parse(raw) : null);
  } catch (err) {
    return normalizeSettings(null);
  }
}

function saveSettings(settings) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); } catch (err) { /* private mode etc. */ }
}

function barLabel(seconds) {
  const s = Number(seconds) || 0;
  if (!s) return "";
  const unit = (n, name) => `${n} ${name}${n === 1 ? "" : "s"}`;
  if (s % 86400 === 0) return unit(s / 86400, "day");
  if (s % 3600 === 0) return unit(s / 3600, "hour");
  if (s % 60 === 0) return unit(s / 60, "minute");
  return unit(s, "second");
}

function decimalsFor(tickSize) {
  const tick = Number(tickSize);
  if (!tick || tick <= 0) return null;
  const text = String(tick);
  if (text.includes("e")) return 4;
  const dot = text.indexOf(".");
  return dot < 0 ? 0 : Math.min(text.length - dot - 1, 6);
}

// A color Lightweight Charts can parse (it throws on anything else), or the fallback.
const colorCache = new Map();
function cssColor(value, fallback) {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text || text.length > 64) return fallback;
  if (!colorCache.has(text)) {
    let ok = false;
    try { ok = !/[;{}]|url\(|var\(/i.test(text) && !!(window.CSS && CSS.supports && CSS.supports("color", text)); } catch (err) { ok = false; }
    colorCache.set(text, ok);
  }
  return colorCache.get(text) ? text : fallback;
}

function clampNumber(value, min, max, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback;
}

// Plugin series as the page draws them: sorted, one point per time for lines/histograms, colors
// it can parse, marker fields Lightweight Charts accepts.
function sanitizeSeries(raw) {
  const out = [];
  const ids = new Set();
  (Array.isArray(raw) ? raw : []).forEach((s, i) => {
    if (!s || typeof s !== "object") return;
    const type = s.type === "histogram" || s.type === "markers" ? s.type : "line";
    const color = cssColor(s.color, PLUGIN_COLORS[i % PLUGIN_COLORS.length]);
    let id = String(s.id || `s${i}`).slice(0, 64);
    if (ids.has(id)) id = `${id}-${i}`;
    ids.add(id);
    let points;
    if (type === "markers") {
      points = [];
      for (const p of Array.isArray(s.points) ? s.points : []) {
        const time = Number(p && p.time);
        if (!Number.isFinite(time)) continue;
        const marker = {
          time,
          position: MARKER_POSITIONS.has(p.position) ? p.position : "aboveBar",
          shape: MARKER_SHAPES.has(p.shape) ? p.shape : "circle",
          color: cssColor(p.color, color),
        };
        if (p.text != null && p.text !== "") marker.text = String(p.text).slice(0, 40);
        if (p.size != null && Number.isFinite(Number(p.size))) marker.size = clampNumber(p.size, 0, 4, 1);
        points.push(marker);
      }
      points.sort((a, b) => a.time - b.time);
    } else {
      const byTime = new Map();
      for (const p of Array.isArray(s.points) ? s.points : []) {
        const time = Number(p && p.time);
        const value = Number(p && p.value);
        if (!Number.isFinite(time) || !Number.isFinite(value)) continue;
        const point = { time, value };
        const pointColor = p.color != null ? cssColor(p.color, null) : null;
        if (pointColor) point.color = pointColor;
        byTime.set(time, point);
      }
      points = [...byTime.values()].sort((a, b) => a.time - b.time);
    }
    out.push({
      id,
      label: String(s.label || s.id || `series ${i + 1}`).slice(0, 80),
      type,
      color,
      line_style: ["solid", "dashed", "dotted"].includes(s.line_style) ? s.line_style : "solid",
      line_width: Math.round(clampNumber(s.line_width, 1, 4, 1)),
      points,
    });
  });
  return out;
}

export function mountChart(rootEl, api) {
  if (!document.querySelector(`link[href="${PLUGIN_CSS}"]`)) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = PLUGIN_CSS;
    document.head.appendChild(link);
  }
  rootEl.innerHTML = `
    <div class="chart-controls">
      <label>Contract <select class="chart-contract" aria-label="Chart contract"></select></label>
      <label>Bar <input type="number" class="chart-tfn" min="1" max="1000" step="1" aria-label="Units per bar" title="How many units per candle (for example 7 minutes)"><select class="chart-tfu" aria-label="Bar unit" title="Unit for each candle"></select></label>
      <span class="chart-quick" role="group" aria-label="Quick bar sizes"></span>
      <label>History <input type="number" class="chart-bars" min="1" max="5000" step="100" aria-label="Bars to load" title="How many bars to load (1-5000)"> bars</label>
      <span class="chart-ind">
        <button type="button" class="chart-ind-btn" aria-haspopup="dialog" aria-expanded="false" title="Add or configure chart indicators">Indicators<span class="chart-ind-count"></span> <span class="caret">&#9662;</span></button>
        <div class="chart-ind-pop" role="dialog" aria-label="Indicators" hidden>
          <div class="ind-row">
            <label class="ind-name"><input type="checkbox" class="chart-ema-on"> EMA</label>
            <span class="ind-params"><label>periods <input type="text" class="chart-ema" placeholder="9,21" title="Comma separated, up to 8 (for example 9,21,50)"></label></span>
          </div>
          <div class="ind-row">
            <label class="ind-name"><input type="checkbox" class="chart-vwap"> VWAP</label>
            <span class="ind-params muted">session anchored, resets 22:00 UTC</span>
          </div>
          <div class="ind-row">
            <label class="ind-name"><input type="checkbox" class="chart-st"> Supertrend</label>
            <span class="ind-params">
              <label>period <input type="number" class="chart-st-period" min="1" max="500" step="1"></label>
              <label>multiplier <input type="number" class="chart-st-mult" min="0.1" max="20" step="0.1"></label>
            </span>
          </div>
          <div class="ind-plugins"></div>
          <div class="ind-section">Volume profile</div>
          <div class="ind-row ind-profile">
            <label class="ind-name"><input type="checkbox" class="chart-vp-session"> Session volume profile</label>
            <span class="ind-params muted">volume at each price from the depth feed</span>
          </div>
          <div class="ind-row ind-profile">
            <label class="ind-name"><input type="checkbox" class="chart-vp-bars"> Visible range volume profile (from bars)</label>
            <span class="ind-params muted">each bar's volume spread evenly over its high-low range</span>
          </div>
          <div class="ind-foot">
            <button type="button" class="chart-ind-reset">Reset to defaults</button>
            <span class="muted">Lines drawn with your parameters; not signals or recommendations.</span>
          </div>
        </div>
      </span>
      <span class="chart-barlabel"></span>
      <span class="chart-status"><span class="chart-status-main"></span><span class="chart-status-ind"></span></span>
    </div>
    <div class="chart-legend"></div>
    <div class="chart-canvas"></div>`;
  const q = (selector) => rootEl.querySelector(selector);
  const el = {
    select: q(".chart-contract"),
    tfn: q(".chart-tfn"),
    tfu: q(".chart-tfu"),
    quick: q(".chart-quick"),
    bars: q(".chart-bars"),
    indBtn: q(".chart-ind-btn"),
    indCount: q(".chart-ind-count"),
    indPop: q(".chart-ind-pop"),
    indReset: q(".chart-ind-reset"),
    emaOn: q(".chart-ema-on"),
    ema: q(".chart-ema"),
    vwap: q(".chart-vwap"),
    st: q(".chart-st"),
    stPeriod: q(".chart-st-period"),
    stMult: q(".chart-st-mult"),
    indPlugins: q(".ind-plugins"),
    vpSession: q(".chart-vp-session"),
    vpBars: q(".chart-vp-bars"),
    barLabel: q(".chart-barlabel"),
    statusMain: q(".chart-status-main"),
    statusInd: q(".chart-status-ind"),
    legend: q(".chart-legend"),
    canvas: q(".chart-canvas"),
  };

  const settings = loadSettings();

  function enabledPlugins() {
    return Object.keys(settings.plugins).filter((name) => settings.plugins[name].on);
  }

  function syncIndicatorUi() {
    el.emaOn.checked = settings.emaOn;
    el.ema.value = settings.ema;
    el.vwap.checked = settings.vwap;
    el.st.checked = settings.st;
    el.stPeriod.value = String(settings.stPeriod);
    el.stMult.value = String(settings.stMult);
    el.ema.disabled = !settings.emaOn;
    el.stPeriod.disabled = el.stMult.disabled = !settings.st;
    el.vpSession.checked = settings.profile === "session";
    el.vpBars.checked = settings.profile === "bars";
    const active = (settings.emaOn && parsePeriods(settings.ema).length ? 1 : 0) + (settings.vwap ? 1 : 0) + (settings.st ? 1 : 0)
      + enabledPlugins().length + (settings.profile ? 1 : 0);
    el.indCount.textContent = active ? ` (${active})` : "";
  }
  syncIndicatorUi();

  // The Indicators menu: a small panel under the button, closed by a click elsewhere or Escape.
  function setIndicatorsOpen(open) {
    el.indPop.hidden = !open;
    el.indBtn.setAttribute("aria-expanded", open ? "true" : "false");
    el.indBtn.classList.toggle("open", open);
  }
  el.indBtn.addEventListener("click", () => setIndicatorsOpen(el.indPop.hidden));
  document.addEventListener("click", (ev) => {
    if (!el.indPop.hidden && !el.indPop.contains(ev.target) && !el.indBtn.contains(ev.target)) setIndicatorsOpen(false);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !el.indPop.hidden) {
      setIndicatorsOpen(false);
      el.indBtn.focus();
    }
  });

  let contractKey = "";
  const tickSizes = {};

  // The status line has two parts: the chart's own (bars loaded, errors) and indicator problems
  // (a plugin's bad parameters or broken file), so one never hides the other.
  function setStatus(text, isError) {
    el.statusMain.textContent = text || "";
    el.statusMain.className = "chart-status-main" + (isError ? " neg" : "");
  }

  function setIndicatorIssues(issues) {
    const text = issues.join(" | ");
    el.statusInd.textContent = text;
    el.statusInd.title = text;
    el.statusInd.className = "chart-status-ind" + (text ? " neg" : "");
  }

  // Contract dropdown: the watched contracts, bound to the focus contract.
  function syncSelect(s) {
    const list = s.contracts || [];
    const key = list.map((c) => `${c.id}:${c.name}`).join("|");
    if (key !== contractKey) {
      contractKey = key;
      el.select.innerHTML = list.map((c) => `<option value="${esc(c.id)}">${esc(c.name || c.id)}</option>`).join("");
    }
    for (const c of list) tickSizes[c.id] = c.tick_size;
    if (s.focus && el.select.value !== s.focus && document.activeElement !== el.select) el.select.value = s.focus;
  }

  el.select.addEventListener("change", () => {
    const id = el.select.value;
    if (!id || id === api.focus()) return;
    api.setFocus(id).catch((err) => setStatus("Could not change focus: " + err.message, true));
  });

  // Bar size + history: the server fetches the bars, so a change is a POST and then a reload.
  el.tfu.innerHTML = BAR_UNITS.map(([unit, label]) => `<option value="${unit}">${label}</option>`).join("");
  el.quick.innerHTML = QUICK_BARS.map(([unit, n, label]) =>
    `<button type="button" class="chart-quick-btn" data-unit="${unit}" data-n="${n}" title="${esc(barLabel(unitSeconds(unit) * n))} per bar">${label}</button>`).join("");
  let timeframe = null;      // {unit, unit_number, bars} as the server last reported it
  let tfPending = false;

  function unitSeconds(unit) {
    return { second: 1, minute: 60, hour: 3600, day: 86400 }[unit] || 0;
  }

  function markQuick() {
    for (const btn of el.quick.querySelectorAll("button")) {
      const active = !!timeframe && btn.dataset.unit === timeframe.unit && Number(btn.dataset.n) === timeframe.unit_number;
      btn.classList.toggle("active", active);
    }
  }

  function syncTimeframe(tf) {
    if (!tf || typeof tf !== "object" || !tf.unit) return;
    timeframe = { unit: String(tf.unit), unit_number: Number(tf.unit_number), bars: Number(tf.bars) };
    if (!tfPending) {
      if (document.activeElement !== el.tfn) el.tfn.value = String(timeframe.unit_number);
      if (document.activeElement !== el.tfu) el.tfu.value = timeframe.unit;
      if (document.activeElement !== el.bars) el.bars.value = String(timeframe.bars);
    }
    markQuick();
  }

  function clampInt(text, min, max, fallback) {
    const n = parseInt(text, 10);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, n));
  }

  async function postTimeframe(unit, unitNumber, bars) {
    const body = { unit, unit_number: unitNumber, bars };
    if (timeframe && body.unit === timeframe.unit && body.unit_number === timeframe.unit_number && body.bars === timeframe.bars) {
      syncTimeframe(timeframe);   // nothing changed (e.g. the input was clamped back); just tidy the fields
      return;
    }
    tfPending = true;
    setStatus(`loading ${bars} x ${barLabel(unitSeconds(unit) * unitNumber)} bars...`);
    try {
      const result = await api.post("/api/timeframe", body);
      tfPending = false;
      needFull = true;
      syncTimeframe(result && result.timeframe ? result.timeframe : null);
    } catch (err) {
      tfPending = false;
      setStatus("Could not change bars: " + (err && err.message ? err.message : String(err)), true);
      if (timeframe) syncTimeframe(timeframe);
      return;
    }
    load();
  }

  function submitFields() {
    const fallbackN = timeframe ? timeframe.unit_number : 5;
    const fallbackBars = timeframe ? timeframe.bars : 1500;
    const unitNumber = clampInt(el.tfn.value, 1, MAX_UNIT_NUMBER, fallbackN);
    const bars = clampInt(el.bars.value, 1, MAX_HISTORY, fallbackBars);
    const unit = BAR_UNITS.some(([u]) => u === el.tfu.value) ? el.tfu.value : (timeframe ? timeframe.unit : "minute");
    el.tfn.value = String(unitNumber);
    el.bars.value = String(bars);
    postTimeframe(unit, unitNumber, bars);
  }
  el.tfn.addEventListener("change", submitFields);
  el.tfu.addEventListener("change", submitFields);
  el.bars.addEventListener("change", submitFields);
  for (const input of [el.tfn, el.bars]) {
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); input.blur(); submitFields(); } });
  }
  el.quick.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-unit]");
    if (!btn) return;
    const bars = clampInt(el.bars.value, 1, MAX_HISTORY, timeframe ? timeframe.bars : 1500);
    postTimeframe(btn.dataset.unit, Number(btn.dataset.n), bars);
  });

  const LC = window.LightweightCharts;
  if (!LC || typeof LC.createChart !== "function") {
    el.canvas.innerHTML = '<div class="chart-missing">Could not load the chart library (dashboard/static/vendor/lightweight-charts.standalone.production.js, with cdn.jsdelivr.net as the fallback). Reload the page; quotes, depth, tape and tables still work.</div>';
    for (const input of [el.tfn, el.tfu, el.bars, el.indBtn, el.emaOn, el.ema, el.vwap, el.st, el.stPeriod, el.stMult, el.indReset, el.vpSession, el.vpBars, ...el.quick.querySelectorAll("button")]) input.disabled = true;
    api.onState(syncSelect);
    return;
  }

  // -- chart and series ------------------------------------------------------------

  const chart = LC.createChart(el.canvas, {
    autoSize: true,
    layout: {
      background: { color: "#151c18" },
      textColor: "#d7e0dc",
      panes: { separatorColor: "#22302a", separatorHoverColor: "rgba(138, 160, 150, 0.25)" },
    },
    grid: { vertLines: { color: "#1b2420" }, horzLines: { color: "#1b2420" } },
    rightPriceScale: { borderColor: "#22302a" },
    timeScale: { borderColor: "#22302a", timeVisible: true, secondsVisible: false, rightOffset: 3 },
  });
  const candleSeries = chart.addSeries(LC.CandlestickSeries, {
    upColor: ST_UP, downColor: ST_DOWN, borderVisible: false, wickUpColor: ST_UP, wickDownColor: ST_DOWN,
  });
  const volumeSeries = chart.addSeries(LC.HistogramSeries, {
    priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false,
  });
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

  const dashed = LC.LineStyle && LC.LineStyle.Dashed != null ? LC.LineStyle.Dashed : 2;
  const solid = LC.LineStyle && LC.LineStyle.Solid != null ? LC.LineStyle.Solid : 0;
  const dotted = LC.LineStyle && LC.LineStyle.Dotted != null ? LC.LineStyle.Dotted : 1;
  const LINE_STYLES = { solid, dashed, dotted };
  const lineOptions = (color, isDashed) => ({
    color,
    lineWidth: 1,
    lineStyle: isDashed ? dashed : solid,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });

  const emaSeries = new Map();   // period (string) -> LineSeries
  let vwapSeries = null;
  let stSeries = null;

  // -- loading state ---------------------------------------------------------------

  let contract = null;       // contract whose bars are on screen
  let version = null;        // bars version from the last /api/bars response
  let barSeconds = 60;
  let candles = [];          // what is drawn right now (oldest first); nudging appends to it
  let volumeRows = [];
  let indicatorData = { ema: {}, vwap: [], supertrend: [] };
  let lastCandle = null;
  let serverLastTime = null; // newest candle time the server has sent; incremental loads start here
  let needFull = true;       // next load must fetch everything (new contract/bar size/indicators/history)
  let inflight = false;
  let wantReload = false;
  let lastLoadAt = 0;
  let historyBusy = false;
  const historyDone = new Set();   // "contract|barSeconds" keys with no older bars at the API
  let latestSnapshot = null;       // last /api/state snapshot, for fills, orders and position overlays
  let overlayKey = "";
  let markers = null;              // series-markers primitive (fills)
  const priceLines = new Map();    // key -> IPriceLine for working orders and the position

  function emaColor(period) {
    const index = parsePeriods(settings.ema).indexOf(Number(period));
    return EMA_COLORS[(index < 0 ? 0 : index) % EMA_COLORS.length];
  }

  // The legend lists the built-in lines, every plugin series on screen and the volume profile.
  let legendBuiltins = { ema: {}, vwap: null, supertrend: null };

  function renderLegend(indicators) {
    legendBuiltins = indicators;
    refreshLegend();
  }

  function swatchStyle(series) {
    const color = series.color;
    if (series.type === "line" && series.line_style === "dashed") return `background: repeating-linear-gradient(90deg, ${color} 0 4px, transparent 4px 6px)`;
    if (series.type === "line" && series.line_style === "dotted") return `background: repeating-linear-gradient(90deg, ${color} 0 2px, transparent 2px 4px)`;
    return `background: ${color}`;
  }

  function refreshLegend() {
    const indicators = legendBuiltins;
    const items = [];
    const periods = Object.keys(indicators.ema || {}).map(Number).filter((n) => n > 0).sort((a, b) => a - b);
    for (const p of periods) items.push([`background: ${emaColor(p)}`, `EMA ${p}`, ""]);
    if (indicators.vwap) items.push([`background: repeating-linear-gradient(90deg, ${VWAP_COLOR} 0 4px, transparent 4px 6px)`, "VWAP", ""]);
    if (indicators.supertrend) {
      items.push([`background: linear-gradient(90deg, ${ST_UP} 50%, ${ST_DOWN} 50%)`, `Supertrend ${settings.stPeriod} x ${settings.stMult}`, ""]);
    }
    for (const name of enabledPlugins()) {
      const st = pluginState.get(name);
      if (!st || !st.loaded) continue;
      for (const series of st.meta) items.push([swatchStyle(series), series.label, series.type === "markers" ? "dot" : series.type === "histogram" ? "block" : ""]);
    }
    if (settings.profile) {
      items.push([`background: ${PROFILE_POC_FILL}`, settings.profile === "session" ? "Session volume profile" : "Volume profile (visible bars)", "block"]);
    }
    el.legend.innerHTML = items.map(([style, text, shape]) =>
      `<span><i class="swatch${shape ? " " + shape : ""}" style="${esc(style)}"></i>${esc(text)}</span>`).join("");
  }

  function applyIndicators(indicators) {
    const ema = indicators.ema || {};
    for (const [period, series] of [...emaSeries]) {
      if (!(period in ema)) {
        chart.removeSeries(series);
        emaSeries.delete(period);
      }
    }
    for (const period of Object.keys(ema)) {
      let series = emaSeries.get(period);
      if (!series) {
        series = chart.addSeries(LC.LineSeries, lineOptions(emaColor(period), false));
        emaSeries.set(period, series);
      } else {
        series.applyOptions({ color: emaColor(period) });
      }
      series.setData(Array.isArray(ema[period]) ? ema[period] : []);
    }

    if (Array.isArray(indicators.vwap) && settings.vwap) {
      if (!vwapSeries) vwapSeries = chart.addSeries(LC.LineSeries, lineOptions(VWAP_COLOR, true));
      vwapSeries.setData(indicators.vwap);
    } else if (vwapSeries) {
      chart.removeSeries(vwapSeries);
      vwapSeries = null;
    }

    if (Array.isArray(indicators.supertrend) && settings.st) {
      if (!stSeries) stSeries = chart.addSeries(LC.LineSeries, { ...lineOptions(ST_UP, false), lineWidth: 2 });
      // One line whose points carry their own color, so it changes color where the direction flips.
      const points = indicators.supertrend
        .filter((p) => p && p.value != null)
        .map((p) => ({ time: p.time, value: p.value, color: p.direction === -1 ? ST_DOWN : ST_UP }));
      stSeries.setData(points);
    } else if (stSeries) {
      chart.removeSeries(stSeries);
      stSeries = null;
    }
    renderLegend({
      ema,
      vwap: vwapSeries ? indicators.vwap : null,
      supertrend: stSeries ? indicators.supertrend : null,
    });
  }

  // Keep what the user is looking at when the data set is replaced: map the visible logical
  // range from the old candle array onto the new one (bars may have been dropped on the left).
  function restoreView(range, oldCandles, newCandles) {
    if (!range || !oldCandles.length || !newCandles.length) return;
    let shift;
    if (newCandles[0].time <= oldCandles[0].time) {
      shift = newCandles.findIndex((c) => c.time >= oldCandles[0].time);
      if (shift < 0) shift = 0;
    } else {
      let dropped = 0;
      while (dropped < oldCandles.length && oldCandles[dropped].time < newCandles[0].time) dropped += 1;
      shift = -dropped;
    }
    chart.timeScale().setVisibleLogicalRange({ from: range.from + shift, to: range.to + shift });
  }

  // Show the newest bars, not the whole history: scrolling left loads more.
  function showRecent(count) {
    const timeScale = chart.timeScale();
    if (!count) return;
    if (count <= INITIAL_VISIBLE_BARS) timeScale.fitContent();
    else timeScale.setVisibleLogicalRange({ from: count - INITIAL_VISIBLE_BARS, to: count + 3 });
  }

  function finishLoad(data, focus) {
    contract = focus;
    version = data.version != null ? data.version : version;
    if (data.timeframe) syncTimeframe(data.timeframe);
    chart.applyOptions({ timeScale: { secondsVisible: barSeconds < 60 } });
    lastCandle = candles.length ? candles[candles.length - 1] : null;
    el.barLabel.textContent = barLabel(barSeconds) ? barLabel(barSeconds) + " per bar" : "";
    const span = candles.length > 1 ? ", " + spanLabel(candles[candles.length - 1].time - candles[0].time) : "";
    setStatus(`${candles.length} bars${span}, updated ${api.fmt.clock(new Date().toISOString())} UTC`);
    renderOverlays(true);   // bar times may have changed: re-snap fills, keep lines
    notifyBars();
  }

  function spanLabel(seconds) {
    const days = seconds / 86400;
    if (days >= 2) return `${Math.round(days)} days`;
    const hours = seconds / 3600;
    return hours >= 1 ? `${Math.round(hours)} hours` : `${Math.round(seconds / 60)} minutes`;
  }

  // A full payload: everything the server has for this contract and bar size.
  function applyBars(focus, data) {
    const newCandles = (Array.isArray(data.candles) ? data.candles : []).map((c) => ({ ...c }));
    const newBarSeconds = Number(data.bar_seconds) > 0 ? Number(data.bar_seconds) : barSeconds;
    const changedContract = contract !== focus || newBarSeconds !== barSeconds;   // new bar size: start fresh
    const timeScale = chart.timeScale();
    const range = changedContract ? null : timeScale.getVisibleLogicalRange();

    if (changedContract) {
      const d = decimalsFor(tickSizes[focus]);
      const tick = Number(tickSizes[focus]);
      if (d != null && tick > 0) candleSeries.applyOptions({ priceFormat: { type: "price", precision: d, minMove: tick } });
      if (contract !== focus) clearProfile();   // the old contract's prices mean nothing on this scale
    }
    volumeRows = Array.isArray(data.volume) ? data.volume.map((v) => ({ ...v })) : [];
    indicatorData = normalizeIndicators(data.indicators);
    candleSeries.setData(newCandles);
    volumeSeries.setData(volumeRows);
    applyIndicators(indicatorData);

    if (changedContract || !range) showRecent(newCandles.length);
    else restoreView(range, candles, newCandles);

    barSeconds = newBarSeconds;
    candles = newCandles;
    serverLastTime = newCandles.length ? newCandles[newCandles.length - 1].time : null;
    needFull = false;
    finishLoad(data, focus);
  }

  function normalizeIndicators(raw) {
    const src = raw && typeof raw === "object" ? raw : {};
    const ema = {};
    for (const [period, points] of Object.entries(src.ema || {})) ema[period] = Array.isArray(points) ? points.slice() : [];
    return { ema, vwap: Array.isArray(src.vwap) ? src.vwap.slice() : [], supertrend: Array.isArray(src.supertrend) ? src.supertrend.slice() : [] };
  }

  // Replace everything from the first incoming time onward, keep what is older.
  function mergeTail(rows, incoming) {
    if (!incoming.length) return rows;
    const cut = incoming[0].time;
    const kept = rows.filter((r) => r.time < cut);
    return kept.concat(incoming);
  }

  // An incremental payload: only candles/points at or after serverLastTime.
  function applyIncrement(focus, data) {
    const rows = (Array.isArray(data.candles) ? data.candles : []).map((c) => ({ ...c }));
    const timeScale = chart.timeScale();
    const range = timeScale.getVisibleLogicalRange();
    const oldCount = candles.length;
    const atLiveEdge = !range || range.to >= oldCount - 2;

    if (rows.length) {
      const newest = rows[rows.length - 1].time;
      // Keep bars the page started itself from live quotes when they are newer than the server's copy.
      const nudged = candles.filter((c) => c.time > newest && c.time >= serverLastTime);
      candles = mergeTail(candles, rows).concat(nudged);
      serverLastTime = newest;
      candleSeries.setData(candles);
    }
    const volume = Array.isArray(data.volume) ? data.volume.map((v) => ({ ...v })) : [];
    if (volume.length) {
      volumeRows = mergeTail(volumeRows, volume);
      volumeSeries.setData(volumeRows);
    }
    const inc = normalizeIndicators(data.indicators);
    for (const [period, points] of Object.entries(inc.ema)) indicatorData.ema[period] = mergeTail(indicatorData.ema[period] || [], points);
    for (const period of Object.keys(indicatorData.ema)) if (!(period in inc.ema)) delete indicatorData.ema[period];
    indicatorData.vwap = mergeTail(indicatorData.vwap, inc.vwap);
    indicatorData.supertrend = mergeTail(indicatorData.supertrend, inc.supertrend);
    applyIndicators(indicatorData);

    if (range) {
      if (atLiveEdge) timeScale.scrollToRealTime();
      else timeScale.setVisibleLogicalRange(range);
    }
    finishLoad(data, focus);
  }

  // Older bars, loaded when the user scrolls near the left edge.
  async function loadOlder() {
    const focus = contract;
    if (!focus || historyBusy || !candles.length) return;
    const key = `${focus}|${barSeconds}`;
    if (historyDone.has(key) || candles.length >= MAX_LOADED_BARS) return;
    historyBusy = true;
    setStatus(`loading older bars... (${candles.length} loaded)`);
    try {
      const result = await api.post("/api/history", { contract_id: focus, before: candles[0].time });
      if (contract !== focus) return;
      if (!result || !result.added) {
        historyDone.add(key);
        setStatus(`${candles.length} bars; no older bars available`);
      } else {
        needFull = true;
        await load();
      }
    } catch (err) {
      setStatus("Could not load older bars: " + (err && err.message ? err.message : String(err)), true);
    } finally {
      historyBusy = false;
    }
  }
  chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (range && range.from < HISTORY_EDGE_BARS && candles.length) loadOlder();
  });

  // -- indicator plugins ---------------------------------------------------------------
  // GET /api/indicators lists the plugin files; each enabled plugin's lines come from
  // GET /api/indicators/series, fetched next to /api/bars (everything on a full load, the tail
  // after the newest candle otherwise) and merged the same way as the built-in overlays.

  const plugins = {
    list: [],          // describe() rows from /api/indicators
    errors: [],        // [{name, file, error}] plugin files that failed to load
    available: true,   // false when the dashboard runs without the "indicators" feature
    listError: "",
    rowsKey: "",
    listing: false,
  };
  const pluginState = new Map();   // name -> what is loaded and drawn for that plugin

  function ensurePlugin(name) {
    let st = pluginState.get(name);
    if (!st) {
      st = {
        needFull: true, loaded: false, version: null, overlay: true, drawnOverlay: null, title: name,
        meta: [], data: new Map(), series: new Map(), kinds: new Map(), markers: null, markersHost: null, error: "",
      };
      pluginState.set(name, st);
    }
    return st;
  }

  const pluginInfo = (name) => plugins.list.find((p) => p.name === name) || null;
  const paramsKey = (name) => JSON.stringify((settings.plugins[name] || {}).params || {});

  function pluginTitle(name) {
    const info = pluginInfo(name);
    const st = pluginState.get(name);
    return (info && info.title) || (st && st.title) || name;
  }

  function clearPluginDrawing(st) {
    if (st.markers) {
      try { st.markers.detach(); } catch (err) { /* host series already removed */ }
      st.markers = null;
      st.markersHost = null;
    }
    for (const series of st.series.values()) {
      try { chart.removeSeries(series); } catch (err) { /* already gone */ }
    }
    st.series.clear();
    st.kinds.clear();
    st.drawnOverlay = null;
  }

  function forgetPluginData(st) {
    clearPluginDrawing(st);
    st.data = new Map();
    st.meta = [];
    st.loaded = false;
    st.needFull = true;
  }

  // One pane per plugin that is not an overlay: the chart grows by PLUGIN_PANE_PX for each so the
  // price pane keeps its height. Lightweight Charts removes a pane once its last series is gone.
  function syncPanes() {
    let panes = [];
    try { panes = chart.panes(); } catch (err) { return; }
    const extra = Math.max(0, panes.length - 1);
    const height = extra ? `${PRICE_PANE_PX + extra * PLUGIN_PANE_PX}px` : "";
    if (el.canvas.style.height !== height) el.canvas.style.height = height;
    if (extra) {
      panes[0].setStretchFactor(PRICE_PANE_PX / PLUGIN_PANE_PX);
      for (let i = 1; i < panes.length; i += 1) panes[i].setStretchFactor(1);
    }
    scheduleProfileDraw();
  }

  function pluginSeriesOptions(name, st, meta) {
    const base = { color: meta.color, priceLineVisible: false, lastValueVisible: !st.overlay, title: "" };
    if (meta.type === "histogram") return st.overlay ? { ...base, priceScaleId: `plugin:${name}` } : base;
    return { ...base, lineWidth: meta.line_width, lineStyle: LINE_STYLES[meta.line_style] ?? solid, crosshairMarkerVisible: false };
  }

  function drawPluginMarkers(st) {
    const rows = [];
    for (const meta of st.meta) if (meta.type === "markers") rows.push(...(st.data.get(meta.id) || []));
    if (!rows.length) {
      if (st.markers) st.markers.setMarkers([]);
      return;
    }
    if (typeof LC.createSeriesMarkers !== "function") return;
    // Markers sit on the candles for overlays, on the plugin's first line in its own pane otherwise;
    // only times that exist in the host series can carry a marker.
    let host = candleSeries;
    let times = new Set(candles.map((c) => c.time));
    if (!st.overlay) {
      const first = st.series.entries().next().value;
      if (first) {
        host = first[1];
        times = new Set((st.data.get(first[0]) || []).map((p) => p.time));
      }
    }
    if (st.markers && st.markersHost !== host) {
      try { st.markers.detach(); } catch (err) { /* ignore */ }
      st.markers = null;
    }
    if (!st.markers) {
      st.markers = LC.createSeriesMarkers(host, []);
      st.markersHost = host;
    }
    st.markers.setMarkers(rows.filter((m) => times.has(m.time)).sort((a, b) => a.time - b.time));
  }

  function drawPlugin(name) {
    const st = pluginState.get(name);
    if (!st) return;
    if (st.drawnOverlay !== null && st.drawnOverlay !== st.overlay) clearPluginDrawing(st);   // moved pane
    const wanted = new Map(st.meta.filter((m) => m.type !== "markers").map((m) => [m.id, m]));
    for (const [id, series] of [...st.series]) {
      const meta = wanted.get(id);
      if (meta && st.kinds.get(id) === meta.type) continue;
      if (st.markers && st.markersHost === series) {
        try { st.markers.detach(); } catch (err) { /* ignore */ }
        st.markers = null;
      }
      chart.removeSeries(series);
      st.series.delete(id);
      st.kinds.delete(id);
    }
    st.drawnOverlay = st.overlay;
    let pane = 0;
    if (!st.overlay) {
      const anchor = st.series.values().next().value;
      pane = anchor ? anchor.getPane().paneIndex() : chart.panes().length;   // a new pane at the bottom
    }
    for (const meta of wanted.values()) {
      let series = st.series.get(meta.id);
      const options = pluginSeriesOptions(name, st, meta);
      if (!series) {
        series = chart.addSeries(meta.type === "histogram" ? LC.HistogramSeries : LC.LineSeries, options, pane);
        if (st.overlay && meta.type === "histogram") series.priceScale().applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });
        if (!st.overlay) pane = series.getPane().paneIndex();
        st.series.set(meta.id, series);
        st.kinds.set(meta.id, meta.type);
      } else {
        series.applyOptions(options);
      }
      series.setData(st.data.get(meta.id) || []);
    }
    drawPluginMarkers(st);
  }

  function requestPlugins(focus, full, after) {
    if (!plugins.available) return [];
    return enabledPlugins().map((name) => {
      const st = ensurePlugin(name);
      const wantFull = full || st.needFull || !st.loaded || after == null;
      const key = paramsKey(name);
      const query = new URLSearchParams({ contract: focus, name, params: key });
      if (!wantFull) query.set("after", String(after));
      return api.get(`/api/indicators/series?${query.toString()}`).then(
        (data) => ({ name, full: wantFull, key, data }),
        (err) => ({ name, full: wantFull, key, error: err && err.message ? err.message : String(err) }));
    });
  }

  function syncClampedParams(name, clean) {
    const entry = settings.plugins[name];
    if (!entry || !clean || typeof clean !== "object") return;
    let changed = false;
    for (const [key, value] of Object.entries(entry.params)) {
      if (!(key in clean) || clean[key] === value) continue;
      entry.params[key] = clean[key];   // the server clamped it (for example a length above its maximum)
      changed = true;
      const input = el.indPlugins.querySelector(`[data-plugin="${CSS.escape(name)}"] [data-param="${CSS.escape(key)}"]`);
      if (input && document.activeElement !== input) {
        if (input.type === "checkbox") input.checked = !!clean[key]; else input.value = String(clean[key]);
      }
    }
    if (changed) saveSettings(settings);
  }

  function applyPluginResults(results) {
    for (const r of results) {
      const entry = settings.plugins[r.name];
      const st = pluginState.get(r.name);
      if (!entry || !entry.on || !st) continue;          // turned off while the request was out
      if (r.key !== paramsKey(r.name)) continue;          // parameters changed meanwhile; the next load has them
      if (r.error) {
        if (/no such route/i.test(r.error)) {
          plugins.available = false;   // the dashboard runs without the "indicators" feature
          st.error = "";
          renderPluginRows();
        } else if (!/no bars for that contract/i.test(r.error)) {
          st.error = r.error;
        }
        forgetPluginData(st);
        continue;
      }
      const data = r.data || {};
      if (!r.full && data.plugin_version !== st.version) {   // the plugin file was saved again: recompute it all
        st.needFull = true;
        wantReload = true;
        continue;
      }
      const meta = sanitizeSeries(data.series);
      if (!r.full) {
        const same = meta.length === st.meta.length && meta.every((m, i) => m.id === st.meta[i].id && m.type === st.meta[i].type);
        if (!same) {
          st.needFull = true;
          wantReload = true;
          continue;
        }
        for (const m of meta) st.data.set(m.id, mergeTail(st.data.get(m.id) || [], m.points));
      } else {
        st.data = new Map(meta.map((m) => [m.id, m.points]));
      }
      st.meta = meta.map(({ points, ...rest }) => rest);
      st.version = data.plugin_version ?? null;
      st.overlay = data.overlay !== false;
      st.title = data.title || r.name;
      st.error = "";
      st.loaded = true;
      st.needFull = false;
      syncClampedParams(r.name, data.params);
      drawPlugin(r.name);
    }
    syncPanes();
    refreshLegend();
    updateIndicatorIssues();
  }

  function updateIndicatorIssues() {
    const issues = [];
    const enabled = enabledPlugins();
    for (const name of enabled) {
      const st = pluginState.get(name);
      if (st && st.error) issues.push(`${pluginTitle(name)}: ${st.error}`);
    }
    const otherBroken = plugins.errors.filter((e) => !enabled.includes(e.name));
    if (otherBroken.length) {
      const files = otherBroken.map((e) => e.file || e.name).join(", ");
      issues.push(`${otherBroken.length === 1 ? "an indicator file" : otherBroken.length + " indicator files"} failed to load (${files}); see Indicators`);
    }
    if (plugins.listError) issues.push(`Indicator plugins: ${plugins.listError}`);
    if (settings.profile && vp.error) issues.push(`Volume profile: ${vp.error}`);
    setIndicatorIssues(issues);
  }

  // -- the plugin part of the Indicators menu --------------------------------------------

  function paramControl(spec, value) {
    const label = esc(spec.label || spec.name);
    const attrs = `data-param="${esc(spec.name)}"${spec.help ? ` title="${esc(spec.help)}"` : ""}`;
    if (spec.type === "bool") return `<label><input type="checkbox" ${attrs}${value ? " checked" : ""}> ${label}</label>`;
    if (spec.type === "int" || spec.type === "float") {
      const step = spec.step != null ? spec.step : spec.type === "int" ? 1 : "any";
      const range = (spec.min != null ? ` min="${esc(spec.min)}"` : "") + (spec.max != null ? ` max="${esc(spec.max)}"` : "");
      return `<label>${label} <input type="number" ${attrs}${range} step="${esc(step)}" value="${esc(value ?? "")}"></label>`;
    }
    if (Array.isArray(spec.choices) && spec.choices.length) {
      const options = spec.choices.map((c) => `<option value="${esc(c)}"${String(c) === String(value) ? " selected" : ""}>${esc(c)}</option>`).join("");
      return `<label>${label} <select ${attrs}>${options}</select></label>`;
    }
    const hex = /^#[0-9a-f]{6}$/i;
    if ((spec.format === "color" || hex.test(String(spec.default || ""))) && hex.test(String(value || ""))) {
      return `<label>${label} <input type="color" ${attrs} value="${esc(value)}"></label>`;
    }
    return `<label>${label} <input type="text" ${attrs} maxlength="200" value="${esc(value ?? "")}"></label>`;
  }

  function pluginRow(info) {
    const entry = settings.plugins[info.name] || { on: false, params: {} };
    const params = entry.on
      ? (Array.isArray(info.params) ? info.params : []).map((spec) => paramControl(spec, spec.name in entry.params ? entry.params[spec.name] : spec.default)).join("")
      : "";
    const pane = info.overlay === false ? ' <span class="ind-tag" title="Drawn in its own pane under the price chart">pane</span>' : "";
    const tip = [info.description, info.file ? `dashboard/indicators_ext/${info.file}` : ""].filter(Boolean).join("\n");
    return `<div class="ind-row ind-plugin" data-plugin="${esc(info.name)}">
      <label class="ind-name" title="${esc(tip)}"><input type="checkbox" class="plug-on"${entry.on ? " checked" : ""}> ${esc(info.title || info.name)}</label>${pane}
      <span class="ind-params">${params}</span>
    </div>`;
  }

  function renderPluginRows() {
    const missing = enabledPlugins().filter((name) => !pluginInfo(name));
    const key = JSON.stringify([plugins.available, plugins.listError, plugins.list, plugins.errors, missing,
      Object.entries(settings.plugins).map(([name, entry]) => [name, entry.on])]);
    if (key === plugins.rowsKey) return;   // unchanged: leave the inputs alone while someone types
    plugins.rowsKey = key;
    let html = '<div class="ind-section">Plugins <span class="ind-path">dashboard/indicators_ext</span></div>';
    if (!plugins.available) {
      html += '<div class="ind-note muted">Plugins are not loaded: this dashboard was started with --features that leaves out "indicators".</div>';
    } else if (!plugins.list.length && !missing.length && !plugins.errors.length) {
      html += '<div class="ind-note muted">No plugin files found. indicators_ext/sma.py shows the format.</div>';
    }
    for (const info of plugins.list) html += pluginRow(info);
    for (const name of missing) {
      const broken = plugins.errors.find((e) => e.name === name);
      html += `<div class="ind-row ind-plugin" data-plugin="${esc(name)}">
        <label class="ind-name"><input type="checkbox" class="plug-on" checked> ${esc(name)}</label>
        <span class="ind-params neg">${broken ? "file failed to load" : "no longer in dashboard/indicators_ext"}</span>
      </div>`;
    }
    for (const e of plugins.errors) {
      html += `<div class="ind-error"><b>${esc(e.file || e.name)}</b>: ${esc(e.error)}</div>`;
    }
    el.indPlugins.innerHTML = html;
  }

  async function refreshPluginList() {
    if (plugins.listing) return;
    plugins.listing = true;
    try {
      const data = await api.get("/api/indicators");
      plugins.available = true;
      plugins.listError = "";
      plugins.list = Array.isArray(data && data.plugins) ? data.plugins.filter((p) => p && typeof p.name === "string") : [];
      plugins.errors = Array.isArray(data && data.errors) ? data.errors.filter((e) => e && typeof e === "object") : [];
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      if (/no such route/i.test(message)) {
        plugins.available = false;
        plugins.list = [];
        plugins.errors = [];
      } else {
        plugins.listError = message;
      }
    } finally {
      plugins.listing = false;
    }
    // A plugin whose file was saved again recomputes everything on the next load.
    let reload = false;
    for (const info of plugins.list) {
      const st = pluginState.get(info.name);
      if (st && st.loaded && info.version !== st.version && settings.plugins[info.name] && settings.plugins[info.name].on) {
        st.needFull = true;
        reload = true;
      }
    }
    renderPluginRows();
    updateIndicatorIssues();
    if (reload) load();
  }

  function readParam(spec, input) {
    if (spec.type === "bool") return !!input.checked;
    if (spec.type === "int" || spec.type === "float") {
      const text = String(input.value).trim();
      const n = Number(text);
      // Anything that is not a number goes to the server as typed, so its message shows in the status line.
      return text !== "" && Number.isFinite(n) ? (spec.type === "int" ? Math.trunc(n) : n) : text;
    }
    return String(input.value).slice(0, 200);
  }

  function setPluginOn(name, on) {
    const entry = settings.plugins[name] || { on: false, params: {} };
    settings.plugins[name] = { on, params: entry.params || {} };
    saveSettings(settings);
    if (on) {
      const st = ensurePlugin(name);
      st.needFull = true;
      st.error = "";
    } else {
      const st = pluginState.get(name);
      if (st) clearPluginDrawing(st);
      pluginState.delete(name);
      syncPanes();
    }
    syncIndicatorUi();
    renderPluginRows();
    refreshLegend();
    updateIndicatorIssues();
    if (on) load();
  }

  function setPluginParam(name, spec, input) {
    const entry = settings.plugins[name];
    if (!entry) return;
    entry.params = { ...entry.params, [spec.name]: readParam(spec, input) };
    saveSettings(settings);
    ensurePlugin(name).needFull = true;   // new parameters: every point is recomputed
    load();
  }

  el.indPlugins.addEventListener("change", (ev) => {
    const row = ev.target.closest("[data-plugin]");
    if (!row) return;
    const name = row.dataset.plugin;
    if (ev.target.classList.contains("plug-on")) {
      setPluginOn(name, ev.target.checked);
      return;
    }
    const param = ev.target.dataset.param;
    const info = pluginInfo(name);
    const spec = param && info && Array.isArray(info.params) ? info.params.find((s) => s.name === param) : null;
    if (spec) setPluginParam(name, spec, ev.target);
  });
  el.indPlugins.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && ev.target.matches("input[data-param]")) {
      ev.preventDefault();
      ev.target.blur();   // commits the value (fires change)
    }
  });
  // The list is fetched again whenever the menu opens, so a plugin file saved a moment ago shows up.
  el.indBtn.addEventListener("click", () => { if (!el.indPop.hidden) refreshPluginList(); });

  // -- volume profile ---------------------------------------------------------------------
  // A horizontal histogram of volume at price on a <canvas> laid over the price pane, anchored
  // to its right edge. "session" uses the depth feed's volume at each price; "bars" spreads each
  // visible bar's volume over its high-low range (both computed by /api/indicators/profile).

  const vp = { rows: [], binSize: 0, tick: 0, error: "", inflight: false, again: false, raf: 0, timer: 0, canvas: null };

  function clearProfile() {
    vp.rows = [];
    vp.error = "";
    scheduleProfileDraw();
  }

  function scheduleProfileDraw() {
    if (vp.raf) return;
    vp.raf = requestAnimationFrame(() => {
      vp.raf = 0;
      drawProfile();
    });
  }

  function drawProfile() {
    if (!settings.profile || !vp.rows.length || !contract) {
      if (vp.canvas) vp.canvas.hidden = true;
      return;
    }
    if (!vp.canvas) {
      vp.canvas = document.createElement("canvas");
      vp.canvas.className = "vp-canvas";
      vp.canvas.setAttribute("aria-hidden", "true");
      el.canvas.appendChild(vp.canvas);
    }
    const canvas = vp.canvas;
    let width = 0;
    let height = 0;
    try {
      const size = chart.paneSize(0);
      width = size.width;
      height = size.height;
    } catch (err) {
      return;
    }
    if (!(width > 0 && height > 0)) return;
    canvas.hidden = false;
    const ratio = window.devicePixelRatio || 1;
    const pixelWidth = Math.round(width * ratio);
    const pixelHeight = Math.round(height * ratio);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const bin = vp.binSize > 0 ? vp.binSize : vp.tick;
    if (!(bin > 0)) return;
    const tick = vp.tick > 0 && vp.tick <= bin ? vp.tick : bin;
    const y0 = candleSeries.priceToCoordinate(vp.rows[0].price);
    const y1 = candleSeries.priceToCoordinate(vp.rows[0].price + bin);
    if (y0 == null || y1 == null) return;
    const binPx = Math.abs(y1 - y0);
    if (!(binPx > 0)) return;
    // Merge neighbouring rows until each drawn row is at least PROFILE_MIN_ROW_PX tall.
    const group = Math.max(1, Math.ceil(PROFILE_MIN_ROW_PX / binPx));
    const span = group * bin;
    const buckets = new Map();
    for (const row of vp.rows) {
      const key = Math.floor(Math.round(row.price / bin) / group);
      buckets.set(key, (buckets.get(key) || 0) + row.volume);
    }
    let most = 0;
    let pocKey = null;
    for (const [key, volume] of buckets) {
      if (volume > most) {
        most = volume;
        pocKey = key;
      }
    }
    if (!(most > 0)) return;
    const maxWidth = width * PROFILE_WIDTH;
    let poc = null;
    for (const [key, volume] of buckets) {
      // A row covers its prices from half a tick below the lowest to half a tick above the highest.
      const ya = candleSeries.priceToCoordinate(key * span - tick / 2);
      const yb = candleSeries.priceToCoordinate((key + 1) * span - tick / 2);
      if (ya == null || yb == null) continue;
      const top = Math.min(ya, yb);
      const rowHeight = Math.abs(ya - yb);
      if (top > height || top + rowHeight < 0) continue;
      const barWidth = Math.max(1, (volume / most) * maxWidth);
      const gap = rowHeight > 4 ? 1 : 0;
      ctx.fillStyle = key === pocKey ? PROFILE_POC_FILL : PROFILE_FILL;
      ctx.fillRect(width - barWidth, top + gap / 2, barWidth, Math.max(1, rowHeight - gap));
      if (key === pocKey) poc = { x: width - barWidth, y: top + rowHeight / 2, volume };
    }
    if (poc) {
      ctx.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillStyle = PROFILE_TEXT;
      ctx.fillText(api.fmt.int(Math.round(poc.volume)), poc.x - 4, Math.min(height - 7, Math.max(7, poc.y)));
    }
  }

  async function refreshProfile() {
    const mode = settings.profile;
    const focus = contract;
    if (!mode || !focus) return;
    if (vp.inflight) {
      vp.again = true;
      return;
    }
    vp.inflight = true;
    const query = new URLSearchParams({ contract: focus, source: mode === "bars" ? "bars" : "depth" });
    if (mode === "bars") {
      const range = chart.timeScale().getVisibleRange();
      if (range && Number.isFinite(Number(range.from)) && Number.isFinite(Number(range.to))) {
        query.set("from", String(Math.floor(Number(range.from))));
        query.set("to", String(Math.ceil(Number(range.to))));
      }
    }
    try {
      const data = await api.get(`/api/indicators/profile?${query.toString()}`);
      if (settings.profile === mode && contract === focus) {
        vp.rows = (Array.isArray(data && data.rows) ? data.rows : [])
          .map((r) => ({ price: Number(r.price), volume: Number(r.volume) }))
          .filter((r) => Number.isFinite(r.price) && Number.isFinite(r.volume) && r.volume > 0);
        vp.binSize = Number(data.bin_size) || 0;
        vp.tick = Number(data.tick_size) || 0;
        vp.error = "";
      }
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      vp.rows = [];
      vp.error = /no such route/i.test(message) ? "not available (the indicators feature is not loaded)" : /no bars/i.test(message) ? "" : message;
    } finally {
      vp.inflight = false;
    }
    scheduleProfileDraw();
    updateIndicatorIssues();
    if (vp.again) {
      vp.again = false;
      refreshProfile();
    }
  }

  function setProfile(mode) {
    settings.profile = PROFILE_MODES.includes(mode) ? mode : "";
    saveSettings(settings);
    vp.rows = [];
    vp.error = "";
    syncIndicatorUi();
    refreshLegend();
    updateIndicatorIssues();
    scheduleProfileDraw();
    if (settings.profile) refreshProfile();
  }
  el.vpSession.addEventListener("change", () => setProfile(el.vpSession.checked ? "session" : ""));
  el.vpBars.addEventListener("change", () => setProfile(el.vpBars.checked ? "bars" : ""));

  // Redraw when the view moves or resizes (the price scale also rescales while the mouse drags it,
  // which the crosshair callback and the slow timer cover); refetch the bars profile for the new range.
  chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
    if (!settings.profile) return;
    scheduleProfileDraw();
    if (settings.profile === "bars") {
      clearTimeout(vp.timer);
      vp.timer = setTimeout(refreshProfile, 300);
    }
  });
  chart.subscribeCrosshairMove(() => { if (settings.profile) scheduleProfileDraw(); });
  if (typeof ResizeObserver === "function") new ResizeObserver(() => scheduleProfileDraw()).observe(el.canvas);
  setInterval(() => { if (settings.profile) scheduleProfileDraw(); }, 1000);

  // -- loading ---------------------------------------------------------------------------

  async function load() {
    const focus = api.focus();
    if (!focus) {
      setStatus("no contract in focus");
      return;
    }
    if (inflight) {
      wantReload = true;
      return;
    }
    inflight = true;
    lastLoadAt = Date.now();
    const full = needFull || contract !== focus || !candles.length || serverLastTime == null;
    const params = new URLSearchParams({ contract: focus });
    // The server treats a missing ema/vwap/st as its default (on), so "off" is always sent explicitly.
    params.set("ema", settings.emaOn ? parsePeriods(settings.ema).join(",") : "");
    params.set("vwap", settings.vwap ? "1" : "0");
    params.set("st", settings.st ? `${settings.stPeriod},${settings.stMult}` : "0");
    const after = full ? null : serverLastTime;
    if (after != null) params.set("after", String(after));
    const pluginJobs = requestPlugins(focus, full, after);   // in parallel with the bars
    try {
      const data = await api.get(`/api/bars?${params.toString()}`);
      let applied = false;
      if (api.focus() === focus && data && data.candles) {
        const sameShape = Number(data.bar_seconds) === barSeconds
          && (data.oldest == null || !candles.length || data.oldest <= candles[0].time)
          && Number(data.count) >= candles.filter((c) => c.time <= serverLastTime).length;
        if (full) { applyBars(focus, data); applied = true; }
        else if (sameShape) { applyIncrement(focus, data); applied = true; }
        else { needFull = true; wantReload = true; }   // the server's data changed shape (bar size/cleared): refetch all
      }
      const results = await Promise.all(pluginJobs);
      if (applied && contract === focus) applyPluginResults(results);
      if (applied && settings.profile) refreshProfile();
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      setStatus(/no bars/i.test(message) || /404/.test(message) ? "waiting for bars..." : message, true);
    } finally {
      inflight = false;
    }
    if (wantReload) {
      wantReload = false;
      load();
    }
  }

  // Between loads, move the newest candle with the live last price (or start a new one).
  function nudgeLastCandle(quote) {
    if (!lastCandle || !quote || quote.last == null) return;
    const last = Number(quote.last);
    if (!Number.isFinite(last)) return;
    const now = Math.floor(Date.now() / 1000);
    const bucket = now - (now % barSeconds);
    if (bucket > lastCandle.time) {
      lastCandle = { time: bucket, open: last, high: last, low: last, close: last };
      candles.push(lastCandle);
    } else {
      lastCandle.close = last;
      lastCandle.high = Math.max(lastCandle.high, last);
      lastCandle.low = Math.min(lastCandle.low, last);
    }
    candleSeries.update(lastCandle);
  }

  // -- controls --------------------------------------------------------------------

  function applySettings(next) {
    const before = enabledPlugins();
    Object.assign(settings, normalizeSettings(next));
    for (const name of before) {   // "Reset to defaults" turns plugins off too
      if (!settings.plugins[name] || !settings.plugins[name].on) {
        const st = pluginState.get(name);
        if (st) clearPluginDrawing(st);
        pluginState.delete(name);
      }
    }
    syncPanes();
    syncIndicatorUi();
    renderPluginRows();
    saveSettings(settings);
    if (!settings.profile) clearProfile();
    refreshLegend();
    updateIndicatorIssues();
    needFull = true;   // indicator parameters changed: every point is recomputed
    load();
  }

  function readControls() {
    applySettings({
      ...settings,
      emaOn: el.emaOn.checked,
      ema: el.ema.value,
      vwap: el.vwap.checked,
      st: el.st.checked,
      stPeriod: el.stPeriod.value,
      stMult: el.stMult.value,
    });
  }
  for (const input of [el.emaOn, el.ema, el.vwap, el.st, el.stPeriod, el.stMult]) input.addEventListener("change", readControls);
  el.indReset.addEventListener("click", () => applySettings({ ...DEFAULTS }));

  // -- fills, working orders and position on the chart -------------------------------
  // Fills are the trader's own trades from /api/state (today's, for the focus contract); orders
  // and the position come from the same snapshot. Nothing here is computed or suggested.
  const pageSettings = api.settings || {};

  function candleTimeAtOrBefore(t) {
    if (!candles.length || t < candles[0].time) return null;
    let lo = 0;
    let hi = candles.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (candles[mid].time <= t) lo = mid; else hi = mid - 1;
    }
    return candles[lo].time;
  }

  function fillMarkers(s) {
    if (!pageSettings.showFills) return [];
    const out = [];
    for (const f of Array.isArray(s.trades) ? s.trades : []) {
      if (!f || f.contract_id !== contract || f.time == null) continue;
      const t = Math.floor(new Date(f.time).getTime() / 1000);
      if (!Number.isFinite(t)) continue;
      const when = candleTimeAtOrBefore(t);
      if (when === null) continue;
      const buy = String(f.side).toUpperCase() === "BUY";
      out.push({
        time: when,
        position: buy ? "belowBar" : "aboveBar",
        shape: buy ? "arrowUp" : "arrowDown",
        color: buy ? ST_UP : ST_DOWN,
        text: (buy ? "B " : "S ") + (f.size != null ? f.size : "") + " @ " + api.fmt.price(f.price, contract),
      });
    }
    out.sort((a, b) => a.time - b.time);
    return out;
  }

  function syncPriceLines(wanted) {
    for (const [key, line] of [...priceLines]) {
      if (!wanted.has(key)) {
        candleSeries.removePriceLine(line);
        priceLines.delete(key);
      }
    }
    for (const [key, opts] of wanted) {
      const existing = priceLines.get(key);
      if (existing) existing.applyOptions(opts);
      else priceLines.set(key, candleSeries.createPriceLine(opts));
    }
  }

  function renderOverlays(force) {
    const s = latestSnapshot;
    if (!s || !contract) return;
    const orders = pageSettings.showOrders ? (Array.isArray(s.orders) ? s.orders : []).filter((o) => o && o.contract_id === contract) : [];
    const position = pageSettings.showOrders ? (Array.isArray(s.positions) ? s.positions : []).find((p) => p && p.contract_id === contract) : null;
    const fills = fillMarkers(s);
    const key = JSON.stringify([contract, barSeconds, pageSettings.showFills, pageSettings.showOrders, fills,
      orders.map((o) => [o.id, o.side, o.type, o.size, o.filled, o.limit_price, o.stop_price]),
      position && [position.side, position.contracts, position.average_price]]);
    if (!force && key === overlayKey) return;
    overlayKey = key;

    if (LC.createSeriesMarkers) {
      if (!markers) markers = LC.createSeriesMarkers(candleSeries, []);
      markers.setMarkers(fills);
    }

    const wanted = new Map();
    const dashedStyle = dashed;
    for (const o of orders) {
      const price = o.limit_price != null ? Number(o.limit_price) : Number(o.stop_price);
      if (!Number.isFinite(price)) continue;
      const buy = String(o.side).toUpperCase() === "BUY";
      wanted.set("order:" + o.id, {
        price, color: buy ? ST_UP : ST_DOWN, lineWidth: 1, lineStyle: dashedStyle, axisLabelVisible: true,
        title: (buy ? "BUY " : "SELL ") + String(o.type || "").toLowerCase() + " " + (Number(o.size) - (Number(o.filled) || 0)),
      });
    }
    if (position && position.average_price != null && Number.isFinite(Number(position.average_price))) {
      const long = String(position.side).toUpperCase() === "LONG";
      wanted.set("position", {
        price: Number(position.average_price), color: long ? "#7fd1c7" : "#f28b82", lineWidth: 2, lineStyle: solid,
        axisLabelVisible: true, title: (long ? "LONG " : "SHORT ") + position.contracts,
      });
    }
    syncPriceLines(wanted);
  }
  if (typeof api.onSettings === "function") api.onSettings(() => renderOverlays(true));

  // -- hooks for feature modules -----------------------------------------------------
  const barListeners = [];
  api.chart = {
    chart,
    candleSeries,
    volumeSeries,
    LC,
    candles: () => candles,
    barSeconds: () => barSeconds,
    contract: () => contract,
    timeframe: () => timeframe,
    onBars(fn) { if (typeof fn === "function") barListeners.push(fn); },
    reload() { needFull = true; load(); },
  };
  function notifyBars() {
    for (const fn of barListeners) { try { fn({ contract, barSeconds, candles }); } catch (err) { console.error("onBars listener failed", err); } }
  }

  // -- wiring ----------------------------------------------------------------------

  api.onState((s) => {
    syncSelect(s);
    latestSnapshot = s;
    renderOverlays(false);
    const focus = s.focus;
    if (!focus) {
      setStatus("no contract in focus");
      return;
    }
    const known = (s.bars_versions || {})[focus];
    const stale = focus !== contract || (known != null && known !== version);
    if (stale) {
      if (Date.now() - lastLoadAt >= MIN_STATE_RELOAD_MS) load();
    } else {
      nudgeLastCandle((s.quotes || {})[focus]);
    }
  });
  renderPluginRows();
  refreshPluginList();
  setInterval(refreshPluginList, PLUGIN_LIST_MS);
  setInterval(load, REFRESH_MS);
  load();
}
