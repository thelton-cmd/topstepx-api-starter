// hotkeys.js - optional keyboard shortcuts for the order ticket (feature: trading_tools).
//
// Off by default. Settings > Hotkeys turns them on (stored as api.settings.hotkeys = {enabled}).
// Every shortcut goes through api.orders (ticket.js), so the confirmation mode the trader chose in
// Settings applies exactly as it does for the buttons, and the server's Practice-only guard and
// "confirm": "yes" rule still decide what is accepted. This module never POSTs anything itself
// and never acts without a key press.
//
// Keys only fire when shortcuts are on, the key comes with Shift and no Ctrl/Alt/Meta, it is not
// an auto-repeat, focus is not in an input/select/textarea, and no confirmation window or
// Settings menu is open. Escape (close the confirmation window) is handled by ticket.js.

const MAP = [
  ["Shift+F", "Flatten all: close every open position, then cancel every open order"],
  ["Shift+C", "Cancel all open orders"],
  ["Shift+B", "Buy market at the DOM size (plus your bracket settings)"],
  ["Shift+S", "Sell market at the DOM size (plus your bracket settings)"],
  ["Escape", "Close the confirmation window without sending"],
];

const CONFIRM_LABELS = { typed: "review + type yes", click: "review + click Send", oneclick: "one-click" };

export function mount(api) {
  const settings = api.settings || {};
  const enabled = () => !!(settings.hotkeys && settings.hotkeys.enabled === true);
  api.hotkeys = { active: true, enabled: enabled, map: MAP.map((m) => m.slice()) };

  const notify = (text, kind) => {
    if (typeof api.notify === "function") api.notify(text, kind);
  };

  function confirmLabel() {
    return CONFIRM_LABELS[settings.confirm] || CONFIRM_LABELS.typed;
  }

  function enabledText() {
    return "Hotkeys on: Shift+F flatten, Shift+C cancel all, Shift+B buy market, Shift+S sell market. Confirmation: " +
      confirmLabel() + ".";
  }

  // ---- Settings section ------------------------------------------------------------
  if (typeof api.registerSettingsSection === "function") {
    api.registerSettingsSection({
      title: "Hotkeys",
      render(container, s, update) {
        const row = document.createElement("label");
        row.className = "set-row";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.className = "set-hotkeys";
        row.appendChild(box);
        row.appendChild(document.createTextNode(" Enable keyboard shortcuts (off by default)"));
        container.appendChild(row);

        const table = document.createElement("table");
        table.className = "hotkeys-map";
        const body = document.createElement("tbody");
        for (const [key, what] of MAP) {
          const tr = document.createElement("tr");
          const k = document.createElement("td");
          const kbd = document.createElement("kbd");
          kbd.textContent = key;
          k.appendChild(kbd);
          const d = document.createElement("td");
          d.textContent = what;
          tr.appendChild(k);
          tr.appendChild(d);
          body.appendChild(tr);
        }
        table.appendChild(body);
        container.appendChild(table);

        const note = document.createElement("div");
        note.className = "set-note hotkeys-note";
        container.appendChild(note);

        const sync = () => {
          box.checked = enabled();
          const oneclick = settings.confirm === "oneclick";
          note.textContent = "Shortcuts use the order confirmation setting above (now: " + confirmLabel() + ")." +
            (oneclick ? " With One-click, a shortcut sends immediately with no summary." : "") +
            " They are ignored while typing in a field or while a confirmation window is open." +
            " Orders only ever go to a Practice Account.";
          note.classList.toggle("hotkeys-warn", oneclick);
        };
        box.addEventListener("change", () => {
          const on = box.checked;
          if (typeof update === "function") update({ hotkeys: { enabled: on } });
          else {
            settings.hotkeys = { enabled: on };
            if (typeof api.saveSettings === "function") api.saveSettings();
          }
          notify(on ? enabledText() : "Hotkeys off.", on ? "warn" : "");
        });
        sync();
        if (typeof api.onSettings === "function") api.onSettings(sync);
      },
    });
  }

  // ---- the key handler ---------------------------------------------------------------
  function typingIn(target) {
    const node = target instanceof Element ? target : null;
    const active = document.activeElement instanceof Element ? document.activeElement : null;
    const sel = "input, select, textarea, [contenteditable=''], [contenteditable='true']";
    return !!((node && node.closest(sel)) || (active && active.closest(sel)));
  }

  function blockedByUi() {
    if (document.querySelector(".modal-backdrop")) return true;         // a confirmation window is open
    const pop = document.querySelector(".settings-pop");
    return !!(pop && !pop.hidden);                                       // the Settings menu is open
  }

  function domSize() {
    if (api.dom && typeof api.dom.size === "function") {
      const n = Number(api.dom.size());
      if (Number.isInteger(n) && n >= 1) return Math.min(n, 50);
    }
    const n = parseInt(settings.domSize, 10);
    return Number.isFinite(n) && n >= 1 ? Math.min(n, 50) : 1;
  }

  function run(label, action) {
    const orders = api.orders;
    if (!orders) { notify(label + ": the order ticket is not ready.", "error"); return; }
    if (typeof orders.allowed === "function" && !orders.allowed()) {
      const snap = typeof api.snapshot === "function" ? api.snapshot() : null;
      const why = snap && snap.order_entry_allowed !== true
        ? "this account is read-only here (Practice Accounts only)."
        : "another request is still in progress.";
      notify(label + ": not sent - " + why, "warn");
      return;
    }
    const oneclick = settings.confirm === "oneclick";
    notify(label + (oneclick ? " - sending (one-click)." : " - review opened."), oneclick ? "warn" : "");
    let promise;
    try {
      promise = action(orders);
    } catch (err) {
      notify(label + ": " + (err && err.message ? err.message : "not sent."), "error");
      return;
    }
    if (promise && typeof promise.catch === "function") {
      promise.catch((err) => {
        const msg = err && err.message ? err.message : "Not sent.";
        notify(label + ": " + msg, msg === "Nothing was sent." ? "" : "warn");
      });
    }
  }

  const ACTIONS = {
    f: ["Shift+F flatten all", (o) => o.flatten()],
    c: ["Shift+C cancel all", (o) => o.cancelAll()],
    b: [null, (o) => o.place({ side: "buy", order_type: "market", size: domSize() })],
    s: [null, (o) => o.place({ side: "sell", order_type: "market", size: domSize() })],
  };

  document.addEventListener("keydown", (ev) => {
    if (!enabled()) return;
    if (!ev.shiftKey || ev.ctrlKey || ev.altKey || ev.metaKey || ev.isComposing) return;
    const key = typeof ev.key === "string" ? ev.key.toLowerCase() : "";
    const entry = ACTIONS[key];
    if (!entry) return;
    if (ev.repeat || typingIn(ev.target) || blockedByUi()) return;
    ev.preventDefault();
    const size = domSize();
    const label = entry[0] || ("Shift+" + key.toUpperCase() + " " + (key === "b" ? "BUY " : "SELL ") + size + " market");
    run(label, entry[1]);
  });

  if (enabled()) notify(enabledText() + " Turn them off in Settings > Hotkeys.", "warn");
}
