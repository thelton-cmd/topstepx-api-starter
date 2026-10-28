// settings.js - page-wide settings behind the gear button in the header, kept in localStorage.
//
// These are conveniences for the trader using this page. None of them changes what the server
// enforces: orders still go only to a Practice Account the API marks simulated and tradable, and
// every order, cancel and close request still carries the page token and "confirm": "yes".
//
// Contract (see dashboard/ARCHITECTURE.md, "Frontend contract"):
//   loadSettings() -> settings object (mutated in place by the panel, so modules can keep a reference)
//   mountSettings(rootEl, settings, onChange) builds the gear button + panel; onChange(settings) runs
//   after every edit, after the object has been updated and saved.

const STORAGE_KEY = "dashboard.settings";

export const CONFIRM_MODES = [
  ["typed", "Review and type yes", "A summary opens. Type yes, then click Send. The default."],
  ["click", "Review and click Send", "A summary opens. One click sends."],
  ["oneclick", "One-click", "No summary. Ticket buttons and DOM clicks send immediately."],
];

const DEFAULTS = { confirm: "typed", domSize: 1, showFills: true, showOrders: true };

function normalize(raw) {
  const s = { ...DEFAULTS, ...(raw && typeof raw === "object" ? raw : {}) };
  if (!CONFIRM_MODES.some(([value]) => value === s.confirm)) s.confirm = DEFAULTS.confirm;
  const size = parseInt(s.domSize, 10);
  s.domSize = Number.isFinite(size) && size >= 1 ? Math.min(size, 50) : DEFAULTS.domSize;
  s.showFills = s.showFills !== false;
  s.showOrders = s.showOrders !== false;
  return s;
}

export function loadSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return normalize(raw ? JSON.parse(raw) : null);
  } catch (err) {
    return normalize(null);
  }
}

export function saveSettings(settings) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); } catch (err) { /* private mode etc. */ }
}

export function confirmModeLabel(mode) {
  const found = CONFIRM_MODES.find(([value]) => value === mode);
  return found ? found[1] : mode;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function mountSettings(rootEl, settings, onChange) {
  rootEl.classList.add("settings-wrap");
  rootEl.innerHTML = `
    <button type="button" class="settings-btn" aria-haspopup="dialog" aria-expanded="false" title="Dashboard settings">
      <span class="gear" aria-hidden="true">&#9881;</span> Settings
    </button>
    <div class="settings-pop" role="dialog" aria-label="Dashboard settings" hidden>
      <div class="set-group">
        <div class="set-title">Order confirmation</div>
        ${CONFIRM_MODES.map(([value, label, help]) => `
          <label class="set-radio"><input type="radio" name="confirm-mode" value="${esc(value)}">
            <span><b>${esc(label)}</b><small>${esc(help)}</small></span></label>`).join("")}
        <div class="set-note">Applies to orders, cancels and closes from the ticket, the DOM and the tables.
          Orders only ever go to a Practice Account the API marks simulated.</div>
      </div>
      <div class="set-group">
        <div class="set-title">DOM</div>
        <label class="set-row">Default order size <input type="number" class="set-dom-size" min="1" max="50" step="1"></label>
      </div>
      <div class="set-group">
        <div class="set-title">Chart</div>
        <label class="set-row"><input type="checkbox" class="set-fills"> Plot today's fills on the chart</label>
        <label class="set-row"><input type="checkbox" class="set-orders"> Show working orders and position on the chart</label>
      </div>
      <div class="set-extra"></div>
      <div class="set-foot"><button type="button" class="settings-reset">Reset to defaults</button></div>
    </div>`;
  const q = (sel) => rootEl.querySelector(sel);
  const btn = q(".settings-btn");
  const pop = q(".settings-pop");
  const radios = [...rootEl.querySelectorAll('input[name="confirm-mode"]')];
  const domSize = q(".set-dom-size");
  const fills = q(".set-fills");
  const orders = q(".set-orders");

  function sync() {
    for (const r of radios) r.checked = r.value === settings.confirm;
    if (document.activeElement !== domSize) domSize.value = String(settings.domSize);
    fills.checked = settings.showFills;
    orders.checked = settings.showOrders;
    btn.classList.toggle("oneclick", settings.confirm === "oneclick");
    btn.title = "Dashboard settings. Order confirmation: " + confirmModeLabel(settings.confirm);
  }

  function update(partial) {
    Object.assign(settings, normalize({ ...settings, ...partial }));
    saveSettings(settings);
    sync();
    if (typeof onChange === "function") onChange(settings);
  }

  for (const r of radios) r.addEventListener("change", () => { if (r.checked) update({ confirm: r.value }); });
  domSize.addEventListener("change", () => update({ domSize: domSize.value }));
  fills.addEventListener("change", () => update({ showFills: fills.checked }));
  orders.addEventListener("change", () => update({ showOrders: orders.checked }));
  q(".settings-reset").addEventListener("click", () => update({ ...DEFAULTS }));

  function setOpen(open) {
    pop.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.classList.toggle("open", open);
  }
  btn.addEventListener("click", () => setOpen(pop.hidden));
  document.addEventListener("click", (ev) => {
    if (!pop.hidden && !pop.contains(ev.target) && !btn.contains(ev.target)) setOpen(false);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !pop.hidden) {
      setOpen(false);
      btn.focus();
    }
  });

  // Feature modules add their own sections: {title, render(container, settings, update)}.
  const extra = q(".set-extra");
  const sections = [];
  function addSection(section) {
    if (!section || typeof section.render !== "function") return;
    const group = document.createElement("div");
    group.className = "set-group";
    const title = document.createElement("div");
    title.className = "set-title";
    title.textContent = String(section.title || "Feature");
    group.appendChild(title);
    const body = document.createElement("div");
    group.appendChild(body);
    extra.appendChild(group);
    sections.push({ section, body });
    try { section.render(body, settings, update); } catch (err) { console.error("settings section failed", err); }
  }

  sync();
  return { sync, setOpen, addSection };
}
