// ticket.js - order ticket, brackets, Cancel/Close buttons, Flatten all / Cancel all, the
// confirmation flow, and api.orders for the DOM and the hotkeys feature.
//
// This mirrors examples/07_place_order_demo.py in the browser: every parameter is typed or clicked
// by the user, the server runs a preflight and returns a summary, and the user confirms the way
// they chose in Settings:
//   "typed"    review the summary and type yes, then Send (the default)
//   "click"    review the summary and click Send
//   "oneclick" no summary; the request goes straight out
// The ticket sends exactly what the user chose; it never suggests a side, size, price or bracket
// distance. The server additionally refuses anything that is not a Practice Account (simulated +
// can_trade) and anything without "confirm": "yes", so this module is a convenience layer, not the guard.
//
// Brackets: "Stop (ticks)" and "Target (ticks)" are distances from the entry the trader types
// (blank = no leg). They live in api.settings.bracket = {stopTicks, targetTicks} and ride along
// with every order from the ticket, the DOM and the hotkeys as stop_ticks / target_ticks. The
// server turns them into the gateway's stopLossBracket / takeProfitBracket and the review shows
// the legs before anything is sent.
//
// Contract (see dashboard/ARCHITECTURE.md, "Frontend contract"):
//   mountTicket(rootEl, api) is called once by app.js.
//   api.post(path, body) adds the X-Dashboard-Token header and throws Error(message) on !ok.
//   api.onState(fn) runs after every /api/state poll; api.focus(); api.contractName(id); api.fmt.*
//   api.settings is the live settings object ({confirm, domSize, bracket, hotkeys, ...}); api.onSettings(fn).
//   This module sets api.orders = { place(body), cancel(orderId), close(contractId), flatten(),
//   cancelAll(), allowed(), busy(), toolsAvailable(), bracket() }. Every call runs the same
//   confirmation flow and resolves with the server's reply, or rejects with the reason (already
//   shown in the ticket's status line), including "Nothing was sent." when the trader backs out.
//   app.js adds data-order-id to open-order rows in #orders and data-contract-id to position
//   rows in #positions, and re-renders both tables after every snapshot.

const READ_ONLY_TEXT =
  "Order entry is only enabled on Practice Accounts (the API marks them simulated). This account is read-only here.";
const MODAL_TITLE = "Review before sending";
const NOTHING_SENT = "Nothing was sent.";
const CANCEL_NOTE = "This sends a cancel request for the order above.";
const CLOSE_NOTE = "This sends a close request for the position above.";
const FLATTEN_NOTE =
  "This sends a close request for each position above, then a cancel request for each order above. " +
  "If a close fails, the orders on that contract are left working. Anything that opens after this summary is not included.";
const CANCEL_ALL_NOTE =
  "This sends a cancel request for each order above. Anything placed after this summary is not included.";
const TOOLS_MISSING =
  "Flatten all and Cancel all need the trading_tools feature (the dashboard was started without it).";
const BRACKET_HELP =
  "Distances from the entry in ticks; blank = no leg. Sent as the gateway's own brackets with every ticket, DOM and hotkey order. " +
  "They need Auto OCO Brackets mode in TopstepX (Settings > Risk Settings); in Position Brackets mode the gateway rejects the order.";
const BRACKET_SHORT_HELP =
  "Blank = no leg. Sent with every ticket, DOM and hotkey order; needs Auto OCO Brackets mode in TopstepX (Settings > Risk Settings).";
const MAX_BRACKET_TICKS = 1000;
const TYPES = [
  ["market", "Market"],
  ["limit", "Limit"],
  ["stop", "Stop"],
  ["stop_limit", "Stop limit"],
];
const TICK_FIELDS = [
  ["stopTicks", "Stop (ticks)"],
  ["targetTicks", "Target (ticks)"],
];

function usesLimit(type) { return type === "limit" || type === "stop_limit"; }
function usesStop(type) { return type === "stop" || type === "stop_limit"; }

// A bracket distance: null (none) or a whole number from 1 to MAX_BRACKET_TICKS; anything else is invalid.
function validTicks(v) {
  const n = typeof v === "number" ? v : Number(String(v == null ? "" : v).trim());
  return Number.isInteger(n) && n >= 1 && n <= MAX_BRACKET_TICKS ? n : null;
}
function parseTicksInput(raw) {
  const text = String(raw == null ? "" : raw).trim();
  if (!text) return { ok: true, value: null };
  if (!/^\d+$/.test(text)) return { ok: false, value: null };
  const n = validTicks(text);
  return n === null ? { ok: false, value: null } : { ok: true, value: n };
}

export function mountTicket(rootEl, api) {
  const fmt = makeFmt(api);
  const settings = api.settings || { confirm: "typed" };
  const state = {
    snapshot: null,
    allowed: false,
    busy: false,
    side: null,
    focus: null,
    observed: { orders: null, positions: null },
    decoratePending: false,
    toolsAvailable: false,
    toolsChecked: false,
  };

  const mode = () => (settings.confirm === "click" || settings.confirm === "oneclick" ? settings.confirm : "typed");

  function bracket() {
    const b = settings.bracket && typeof settings.bracket === "object" ? settings.bracket : {};
    return { stopTicks: validTicks(b.stopTicks), targetTicks: validTicks(b.targetTicks) };
  }

  function saveBracket(partial) {
    settings.bracket = Object.assign(bracket(), partial);
    if (typeof api.saveSettings === "function") api.saveSettings();
  }

  // ---- ticket shell -------------------------------------------------------------
  const ticket = el("div", "ticket");
  const badgeRow = el("div", "ticket-badges");
  const hotkeysBadge = el("span", "badge on hotkeys-badge");
  hotkeysBadge.textContent = "Hotkeys on";
  badgeRow.appendChild(hotkeysBadge);
  badgeRow.style.display = "none";
  const accountLine = el("div", "account-line");
  const notice = el("div", "notice");
  notice.style.display = "none";

  const contractValue = el("span", "value");
  const contractField = fieldRow("Contract", contractValue);

  const buyBtn = el("button", "side-btn buy");
  buyBtn.type = "button";
  buyBtn.textContent = "BUY";
  buyBtn.setAttribute("aria-pressed", "false");
  const sellBtn = el("button", "side-btn sell");
  sellBtn.type = "button";
  sellBtn.textContent = "SELL";
  sellBtn.setAttribute("aria-pressed", "false");
  const sideWrap = el("div", "side");
  sideWrap.appendChild(buyBtn);
  sideWrap.appendChild(sellBtn);
  const sideField = fieldRow("Side", sideWrap);

  const typeSel = el("select", "order-type");
  for (const [value, text] of TYPES) {
    const opt = el("option");
    opt.value = value;
    opt.textContent = text;
    typeSel.appendChild(opt);
  }
  typeSel.value = "market";
  const typeField = fieldRow("Type", typeSel);

  const sizeInput = el("input", "size");
  sizeInput.type = "number";
  sizeInput.min = "1";
  sizeInput.max = "50";
  sizeInput.step = "1";
  sizeInput.value = "1";
  sizeInput.inputMode = "numeric";
  const sizeField = fieldRow("Size", sizeInput);

  const limitInput = el("input", "limit-price");
  limitInput.type = "number";
  limitInput.step = "any";
  limitInput.placeholder = "limit price";
  const limitField = fieldRow("Limit price", limitInput);

  const stopInput = el("input", "stop-price");
  stopInput.type = "number";
  stopInput.step = "any";
  stopInput.placeholder = "stop price";
  const stopField = fieldRow("Stop price", stopInput);

  // Bracket distances (persisted in settings.bracket; shared with the Settings section).
  const tickInputs = {};
  const tickFields = [];
  for (const [key, label] of TICK_FIELDS) {
    const input = ticksInput(key === "stopTicks" ? "bracket-stop" : "bracket-target", label);
    input.addEventListener("change", () => commitTicks(key, label, input));
    tickInputs[key] = input;
    tickFields.push(fieldRow(label, input));
  }
  const bracketNote = el("div", "muted bracket-note");
  bracketNote.style.fontSize = "12px";
  bracketNote.style.marginBottom = "6px";

  const reviewBtn = el("button", "primary review");
  reviewBtn.type = "button";
  const modeNote = el("span", "muted mode-note");
  const actionRow = el("div", "ticket-actions");
  actionRow.appendChild(reviewBtn);
  actionRow.appendChild(modeNote);

  const flattenBtn = el("button", "danger flatten-all");
  flattenBtn.type = "button";
  flattenBtn.textContent = "Flatten all";
  flattenBtn.title = "Close every open position on this account, then cancel every open order. Confirms per Settings.";
  const cancelAllBtn = el("button", "danger cancel-all");
  cancelAllBtn.type = "button";
  cancelAllBtn.textContent = "Cancel all";
  cancelAllBtn.title = "Cancel every open order on this account. Confirms per Settings.";
  const toolsRow = el("div", "ticket-actions ticket-tools");
  toolsRow.appendChild(flattenBtn);
  toolsRow.appendChild(cancelAllBtn);
  toolsRow.style.display = "none";

  const status = el("div", "status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");

  ticket.appendChild(badgeRow);
  ticket.appendChild(accountLine);
  ticket.appendChild(notice);
  ticket.appendChild(contractField);
  ticket.appendChild(sideField);
  ticket.appendChild(typeField);
  ticket.appendChild(sizeField);
  ticket.appendChild(limitField);
  ticket.appendChild(stopField);
  for (const f of tickFields) ticket.appendChild(f);
  ticket.appendChild(bracketNote);
  ticket.appendChild(actionRow);
  ticket.appendChild(toolsRow);
  ticket.appendChild(status);
  rootEl.appendChild(ticket);

  const controls = [buyBtn, sellBtn, typeSel, sizeInput, limitInput, stopInput, tickInputs.stopTicks,
    tickInputs.targetTicks, reviewBtn, flattenBtn, cancelAllBtn];

  // ---- field behaviour ----------------------------------------------------------
  function setSide(side) {
    state.side = side;
    buyBtn.classList.toggle("active", side === "buy");
    sellBtn.classList.toggle("active", side === "sell");
    buyBtn.setAttribute("aria-pressed", side === "buy" ? "true" : "false");
    sellBtn.setAttribute("aria-pressed", side === "sell" ? "true" : "false");
  }
  buyBtn.addEventListener("click", () => setSide("buy"));
  sellBtn.addEventListener("click", () => setSide("sell"));

  function updatePriceFields() {
    const type = typeSel.value;
    limitField.style.display = usesLimit(type) ? "" : "none";
    stopField.style.display = usesStop(type) ? "" : "none";
  }
  typeSel.addEventListener("change", updatePriceFields);
  updatePriceFields();

  function applyEnabled() {
    const off = !state.allowed || state.busy;
    for (const c of controls) c.disabled = off;
  }

  function bracketSummaryText() {
    const b = bracket();
    if (b.stopTicks === null && b.targetTicks === null) return "No brackets. " + BRACKET_SHORT_HELP;
    const parts = [];
    if (b.stopTicks !== null) parts.push("stop " + b.stopTicks + " ticks");
    if (b.targetTicks !== null) parts.push("target " + b.targetTicks + " ticks");
    return "Brackets on every order: " + parts.join(", ") + ". " + BRACKET_SHORT_HELP;
  }

  function syncBracketInputs() {
    const b = bracket();
    for (const [key] of TICK_FIELDS) {
      const input = tickInputs[key];
      if (document.activeElement !== input) input.value = b[key] === null ? "" : String(b[key]);
    }
    setText(bracketNote, bracketSummaryText());
  }

  function hotkeysOn() {
    return !!(settings.hotkeys && settings.hotkeys.enabled === true && api.hotkeys && api.hotkeys.active === true);
  }

  function updateBadge() {
    const on = hotkeysOn();
    badgeRow.style.display = on ? "" : "none";
    if (on) {
      const map = api.hotkeys && Array.isArray(api.hotkeys.map) ? api.hotkeys.map : [];
      hotkeysBadge.title = "Keyboard shortcuts are on (Settings > Hotkeys). " + map.map((m) => m[0] + " " + m[1]).join("; ");
    }
  }

  function applyMode() {
    const m = mode();
    reviewBtn.textContent = m === "oneclick" ? "Send order" : "Review order";
    reviewBtn.classList.toggle("danger", m === "oneclick");
    reviewBtn.classList.toggle("primary", m !== "oneclick");
    modeNote.textContent = m === "typed" ? "review, type yes" : m === "click" ? "review, click Send" : "one-click: sends immediately";
    syncBracketInputs();
    updateBadge();
  }
  applyMode();
  if (typeof api.onSettings === "function") api.onSettings(applyMode);

  function setStatus(text, isError) {
    status.textContent = text || "";
    status.classList.toggle("error", !!isError);
    status.classList.toggle("ok", !isError && !!text);
  }

  function commitTicks(key, label, input) {
    const parsed = parseTicksInput(input.value);
    if (!parsed.ok) {
      setStatus(label + " must be a whole number from 1 to " + MAX_BRACKET_TICKS + ", or blank for none.", true);
      const saved = bracket()[key];
      input.value = saved === null ? "" : String(saved);
      return;
    }
    saveBracket({ [key]: parsed.value });
    syncBracketInputs();
  }

  // dom-price: the ladder copies a clicked price into the price input. Never auto-submits.
  window.addEventListener("dom-price", (ev) => {
    if (!state.allowed) return;
    const price = ev && ev.detail ? Number(ev.detail.price) : NaN;
    if (!Number.isFinite(price)) return;
    const type = typeSel.value;
    const target = type === "stop" ? stopInput : limitInput;
    target.value = String(price);
  });

  // ---- Settings section: the same bracket distances -------------------------------
  if (typeof api.registerSettingsSection === "function") {
    api.registerSettingsSection({
      title: "Brackets",
      render(container) {
        const inputs = {};
        for (const [key, label] of TICK_FIELDS) {
          const row = el("label", "set-row");
          row.appendChild(document.createTextNode(label + " "));
          const input = ticksInput("set-" + (key === "stopTicks" ? "bracket-stop" : "bracket-target"), label);
          input.addEventListener("change", () => commitTicks(key, label, input));
          row.appendChild(input);
          container.appendChild(row);
          inputs[key] = input;
        }
        const clear = el("button", "small bracket-clear");
        clear.type = "button";
        clear.textContent = "Clear brackets";
        clear.addEventListener("click", () => saveBracket({ stopTicks: null, targetTicks: null }));
        container.appendChild(clear);
        const note = el("div", "set-note");
        note.textContent = BRACKET_HELP;
        container.appendChild(note);
        const sync = () => {
          const b = bracket();
          for (const [key] of TICK_FIELDS) {
            if (document.activeElement !== inputs[key]) inputs[key].value = b[key] === null ? "" : String(b[key]);
          }
        };
        sync();
        if (typeof api.onSettings === "function") api.onSettings(sync);
      },
    });
  }

  // ---- does the server have the trading_tools routes? --------------------------------
  function checkTools() {
    if (state.toolsChecked || typeof api.get !== "function") return;
    state.toolsChecked = true;
    api.get("/api/features").then((data) => {
      const list = data && Array.isArray(data.features) ? data.features : [];
      state.toolsAvailable = list.some((f) => f && f.name === "trading_tools");
      toolsRow.style.display = state.toolsAvailable ? "" : "none";
    }).catch(() => {
      state.toolsChecked = false; // try again after the next snapshot
    });
  }

  // ---- snapshot -> account line, contract line, table buttons --------------------
  api.onState((snapshot) => {
    if (!snapshot) return;
    state.snapshot = snapshot;
    state.allowed = snapshot.order_entry_allowed === true;
    state.focus = typeof api.focus === "function" ? api.focus() || null : snapshot.focus || null;

    const acct = snapshot.account;
    if (!acct) {
      setText(accountLine, "No account selected.");
    } else {
      const name = acct.name != null ? String(acct.name) : "account " + String(acct.id);
      setText(
        accountLine,
        "Account: " + name + (state.allowed ? " - order entry enabled (Practice Account)" : " - read-only")
      );
    }
    if (state.allowed) {
      notice.style.display = "none";
      setText(notice, "");
    } else {
      // A feature can pause order entry with its own reason (the trader's guard, replay mode).
      setText(notice, snapshot.order_entry_block ? String(snapshot.order_entry_block) : READ_ONLY_TEXT);
      notice.style.display = "";
    }

    if (state.focus) {
      const name = typeof api.contractName === "function" ? api.contractName(state.focus) : null;
      setText(contractValue, name && name !== state.focus ? name + " (" + state.focus + ")" : state.focus);
    } else {
      setText(contractValue, "none selected");
    }

    const counts = openLists();
    flattenBtn.title = "Close " + counts.positions.length + " open position(s), then cancel " + counts.orders.length +
      " open order(s) on this account. Confirms per Settings.";
    cancelAllBtn.title = "Cancel " + counts.orders.length + " open order(s) on this account. Confirms per Settings.";

    checkTools();
    updateBadge();
    applyEnabled();
    attachObservers();
    decorateTables();
  });

  // ---- building the request body ----------------------------------------------------
  function buildBody() {
    const type = typeSel.value;
    const size = parseInt(sizeInput.value, 10);
    if (!Number.isFinite(size)) throw new Error("Size must be a whole number from 1 to 50.");
    if (!state.side) throw new Error("Choose BUY or SELL first.");
    return completeBody({
      side: state.side,
      order_type: type,
      size: size,
      limit_price: usesLimit(type) ? parsePrice(limitInput.value, "Limit price") : null,
      stop_price: usesStop(type) ? parsePrice(stopInput.value, "Stop price") : null,
    });
  }

  // Fill in account, focus contract and the current bracket settings (a caller may override them).
  function completeBody(partial) {
    const acct = state.snapshot && state.snapshot.account;
    if (!acct) throw new Error("No account selected.");
    if (!state.focus) throw new Error("No contract selected.");
    const b = bracket();
    const stopTicks = Object.prototype.hasOwnProperty.call(partial, "stop_ticks") ? partial.stop_ticks : b.stopTicks;
    const targetTicks = Object.prototype.hasOwnProperty.call(partial, "target_ticks") ? partial.target_ticks : b.targetTicks;
    return {
      account_id: acct.id,
      contract_id: state.focus,
      side: partial.side,
      order_type: partial.order_type,
      size: partial.size,
      limit_price: partial.limit_price == null ? null : Number(partial.limit_price),
      stop_price: partial.stop_price == null ? null : Number(partial.stop_price),
      stop_ticks: stopTicks == null ? null : stopTicks,
      target_ticks: targetTicks == null ? null : targetTicks,
    };
  }

  function parsePrice(raw, label) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) return null; // the server reports a missing required price in plain English
    const n = Number(text);
    if (!Number.isFinite(n)) throw new Error(label + " is not a number.");
    return n;
  }

  // ---- the shared flow: preflight -> confirm (per setting) -> send ---------------------
  function bracketText(body) {
    const parts = [];
    if (body.stop_ticks != null) parts.push("stop " + body.stop_ticks + "t");
    if (body.target_ticks != null) parts.push("target " + body.target_ticks + "t");
    return parts.length ? " + " + parts.join(" / ") : "";
  }

  function orderText(body) {
    const price = body.order_type === "market" ? "" : body.order_type === "stop"
      ? " @ stop " + fmt.price(body.stop_price, body.contract_id)
      : " @ " + fmt.price(body.limit_price, body.contract_id) + (body.stop_price != null ? " (stop " + fmt.price(body.stop_price, body.contract_id) + ")" : "");
    return body.side.toUpperCase() + " " + body.size + " " + body.order_type.replace("_", " ") + price + bracketText(body);
  }

  async function withBusy(fn) {
    state.busy = true;
    applyEnabled();
    decorateTables();
    try {
      return await fn();
    } finally {
      state.busy = false;
      applyEnabled();
      decorateTables();
    }
  }

  // Run opts.onSend after the confirmation the user asked for in Settings. Resolves with what
  // onSend returned; rejects with its error, or with "Nothing was sent." if the trader backs out.
  function confirmThenSend(opts) {
    if (mode() === "oneclick") {
      return withBusy(() => opts.onSend());
    }
    return new Promise((resolve, reject) => {
      openModal({
        ...opts,
        requireTyping: mode() === "typed",
        onDone: (outcome) => {
          if (outcome && outcome.ok) resolve(outcome.value);
          else reject(outcome && outcome.error ? outcome.error : new Error(NOTHING_SENT));
        },
      });
    });
  }

  function guard() {
    if (!state.allowed) return new Error(READ_ONLY_TEXT);
    if (state.busy) return new Error("Another request is still in progress.");
    return null;
  }

  async function placeOrder(body) {
    const blocked = guard();
    if (blocked) throw blocked;
    if (mode() === "oneclick") {
      setStatus("Sending " + orderText(body) + "...", false);
      return withBusy(async () => {
        try {
          const res = await api.post("/api/orders", Object.assign({}, body, { confirm: "yes" }));
          const id = res && res.order_id != null ? res.order_id : "?";
          setStatus("Order placed: " + orderText(body) + ". orderId = " + String(id), false);
          return res;
        } catch (err) {
          setStatus(errorText(err), true);
          throw err;
        }
      });
    }
    setStatus("Checking with the server...", false);
    const summary = await withBusy(async () => {
      try {
        const res = await api.post("/api/orders/preflight", body);
        if (!res || res.ok !== true || !res.summary) {
          throw new Error(res && res.error ? String(res.error) : "Preflight failed.");
        }
        return res.summary;
      } catch (err) {
        setStatus(errorText(err), true);
        throw err;
      }
    });
    setStatus("", false);
    const warning = [summary.warning, summary.bracket_note].filter((x) => x != null && x !== "").map(String).join(" ");
    return confirmThenSend({
      rows: summaryRows(summary),
      warning: warning,
      sendLabel: "Send order",
      onSend: async () => {
        // Send back the tag the user just reviewed so the modal and the gateway see the same one.
        const payload = Object.assign({}, body, { confirm: "yes", custom_tag: summary.custom_tag });
        try {
          const res = await api.post("/api/orders", payload);
          const id = res && res.order_id != null ? res.order_id : "?";
          setStatus("Order placed: " + orderText(body) + ". orderId = " + String(id), false);
          return res;
        } catch (err) {
          setStatus(errorText(err), true);
          throw err;
        }
      },
    });
  }

  async function review() {
    if (!state.allowed || state.busy) return;
    let body;
    try {
      body = buildBody();
    } catch (err) {
      setStatus(err.message, true);
      return;
    }
    try {
      await placeOrder(body);
    } catch (err) {
      /* status already shows the reason */
    } finally {
      setSide(null);
    }
  }
  reviewBtn.addEventListener("click", review);

  function legText(leg, b, contractId) {
    if (!leg) return "none";
    let where;
    if (leg.price == null) where = "priced from the fill (no last price yet)";
    else if (b.approx) where = "~" + fmt.price(leg.price, contractId) + " (approx, from last " + fmt.price(b.reference_price, contractId) + ")";
    else where = fmt.price(leg.price, contractId) + " (from the " + (b.basis || "entry price") + " " + fmt.price(b.reference_price, contractId) + ")";
    return leg.ticks + " ticks " + (leg.direction || "") + " -> " + where;
  }

  function gatewayText(b) {
    const part = (name, leg) => leg
      ? name + " {ticks: " + leg.gateway_ticks + ", type: " + leg.type_code + " " + titleCase(leg.type) + "}"
      : null;
    return [part("stopLossBracket", b.stop_loss), part("takeProfitBracket", b.take_profit)].filter(Boolean).join(", ");
  }

  function summaryRows(s) {
    const cid = s.contract_id || state.focus;
    const rows = [
      ["Account", s.account],
      ["Simulated", s.simulated === true ? "YES" : String(s.simulated)],
      ["Contract", s.contract],
      ["Side", s.side],
      ["Type", s.type],
      ["Size", s.size],
    ];
    if (s.limit_price != null) rows.push(["Limit price", fmt.price(s.limit_price, cid)]);
    if (s.stop_price != null) rows.push(["Stop price", fmt.price(s.stop_price, cid)]);
    const b = s.brackets;
    if (b && typeof b === "object") {
      rows.push(["Stop leg", legText(b.stop_loss, b, cid)]);
      rows.push(["Target leg", legText(b.take_profit, b, cid)]);
      rows.push(["Gateway brackets", gatewayText(b)]);
    } else {
      rows.push(["Brackets", "none"]);
    }
    rows.push(["Tick size", s.tick_size]);
    rows.push(["Custom tag", s.custom_tag]);
    return rows;
  }

  // ---- Cancel / Close buttons in the tables -------------------------------------------
  function attachObservers() {
    for (const id of ["orders", "positions"]) {
      if (state.observed[id]) continue;
      const node = document.getElementById(id);
      if (!node) continue;
      const mo = new MutationObserver(scheduleDecorate);
      mo.observe(node, { childList: true, subtree: true });
      state.observed[id] = mo;
    }
  }

  function scheduleDecorate() {
    if (state.decoratePending) return;
    state.decoratePending = true;
    // setTimeout rather than requestAnimationFrame: rAF never fires in a background tab.
    setTimeout(() => {
      state.decoratePending = false;
      decorateTables();
    }, 0);
  }

  function decorateTables() {
    decorateRows("orders", "data-order-id", "Cancel", "cancel", (id) => cancelOrder(id).catch(() => {}));
    decorateRows("positions", "data-contract-id", "Close", "close", (id) => closePosition(id).catch(() => {}));
  }

  function decorateRows(containerId, attr, label, action, handler) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const rows = container.querySelectorAll("tr[" + attr + "]");
    const off = !state.allowed || state.busy;
    rows.forEach((tr) => {
      let btn = tr.querySelector("button[data-ticket-action]");
      if (!btn) {
        const td = el("td", "ticket-cell");
        btn = el("button", "danger small");
        btn.type = "button";
        btn.textContent = label;
        btn.dataset.ticketAction = action;
        btn.addEventListener("click", () => handler(tr.getAttribute(attr)));
        td.appendChild(btn);
        tr.appendChild(td);
      }
      btn.disabled = off;
    });
    const headRow = container.querySelector("thead tr");
    if (headRow) {
      const existing = headRow.querySelector("th[data-ticket-th]");
      if (rows.length && !existing) {
        const th = el("th");
        th.dataset.ticketTh = "1";
        headRow.appendChild(th);
      } else if (!rows.length && existing) {
        existing.remove();
      }
    }
  }

  function findOrder(orderId) {
    const list = state.snapshot && Array.isArray(state.snapshot.orders) ? state.snapshot.orders : [];
    return list.find((o) => o && String(o.id) === String(orderId)) || null;
  }

  function findPosition(contractId) {
    const list = state.snapshot && Array.isArray(state.snapshot.positions) ? state.snapshot.positions : [];
    return list.find((p) => p && String(p.contract_id) === String(contractId)) || null;
  }

  function contractLabel(id) {
    if (id == null) return "-";
    const name = typeof api.contractName === "function" ? api.contractName(id) : null;
    return name && name !== id ? String(name) + " (" + String(id) + ")" : String(id);
  }

  function cancelOrder(orderId) {
    if (!state.allowed || orderId == null) return Promise.reject(new Error(READ_ONLY_TEXT));
    const o = findOrder(orderId);
    const rows = [["Order id", orderId]];
    if (o) {
      rows.push(["Contract", contractLabel(o.contract_id)]);
      rows.push(["Side", o.side]);
      rows.push(["Type", o.type]);
      rows.push(["Size", o.size]);
      if (o.limit_price != null) rows.push(["Limit price", fmt.price(o.limit_price, o.contract_id)]);
      if (o.stop_price != null) rows.push(["Stop price", fmt.price(o.stop_price, o.contract_id)]);
    } else {
      rows.push(["Note", "This order is no longer in the latest snapshot."]);
    }
    const numeric = Number(orderId);
    const idValue = Number.isFinite(numeric) ? numeric : orderId;
    return confirmThenSend({
      rows: rows,
      warning: CANCEL_NOTE,
      sendLabel: "Send cancel",
      onSend: async () => {
        try {
          const res = await api.post("/api/orders/cancel", { order_id: idValue, confirm: "yes" });
          setStatus("Cancel request sent for order " + String(orderId) + ".", false);
          return res;
        } catch (err) {
          setStatus(errorText(err), true);
          throw err;
        }
      },
    });
  }

  function closePosition(contractId) {
    if (!state.allowed || !contractId) return Promise.reject(new Error(READ_ONLY_TEXT));
    const p = findPosition(contractId);
    const rows = [["Contract", contractLabel(contractId)]];
    if (p) {
      rows.push(["Side", p.side]);
      rows.push(["Size", p.size]);
      if (p.average_price != null) rows.push(["Average price", fmt.price(p.average_price, contractId)]);
    } else {
      rows.push(["Note", "This position is no longer in the latest snapshot."]);
    }
    return confirmThenSend({
      rows: rows,
      warning: CLOSE_NOTE,
      sendLabel: "Send close",
      onSend: async () => {
        try {
          const res = await api.post("/api/positions/close", { contract_id: String(contractId), confirm: "yes" });
          setStatus("Close request sent for " + contractLabel(contractId) + ".", false);
          return res;
        } catch (err) {
          setStatus(errorText(err), true);
          throw err;
        }
      },
    });
  }

  // ---- Flatten all / Cancel all (dashboard/features/trading_tools.py) ------------------
  function openLists() {
    const snap = state.snapshot || {};
    const positions = (Array.isArray(snap.positions) ? snap.positions : []).filter((p) => p && p.contract_id);
    const orders = (Array.isArray(snap.orders) ? snap.orders : []).filter((o) => o && o.id != null && Number.isFinite(Number(o.id)));
    return { positions: positions, orders: orders };
  }

  function positionText(p) {
    const size = p.contracts != null ? p.contracts : Math.abs(Number(p.size) || 0);
    return contractLabel(p.contract_id) + " " + String(p.side || "") + " " + String(size) +
      (p.average_price != null ? " @ " + fmt.price(p.average_price, p.contract_id) : "");
  }

  function orderRowText(o) {
    const price = o.limit_price != null ? " @ " + fmt.price(o.limit_price, o.contract_id)
      : o.stop_price != null ? " @ stop " + fmt.price(o.stop_price, o.contract_id) : "";
    return "#" + String(o.id) + " " + contractLabel(o.contract_id) + " " + String(o.side || "") + " " +
      String(o.type || "") + " " + String(o.size == null ? "" : o.size) + price + (o.status ? " (" + o.status + ")" : "");
  }

  function batchGate() {
    const blocked = guard();
    if (blocked) return blocked;
    if (!state.toolsAvailable) return new Error(TOOLS_MISSING);
    const acct = state.snapshot && state.snapshot.account;
    if (!acct) return new Error("No account selected.");
    return null;
  }

  function reportText(label, res) {
    if (!res || typeof res !== "object") return label + ": no report came back.";
    const positions = Array.isArray(res.positions) ? res.positions : [];
    const orders = Array.isArray(res.orders) ? res.orders : [];
    const sentClose = positions.filter((x) => x.status === "sent").length;
    const sentCancel = orders.filter((x) => x.status === "sent").length;
    const parts = [];
    if (res.action === "flatten") parts.push(sentClose + " close and " + sentCancel + " cancel request(s) sent");
    else parts.push(sentCancel + " cancel request(s) sent");
    const failures = positions.concat(orders).filter((x) => x.status === "failed");
    if (failures.length) {
      parts.push(failures.length + " failed (" + failures.map((x) =>
        (x.order_id != null ? "order #" + x.order_id : contractLabel(x.contract_id) + " close") + ": " + (x.error || "error")).join("; ") + ")");
    }
    const gone = positions.concat(orders).filter((x) => x.status === "not_found").length;
    if (gone) parts.push(gone + " already closed or cancelled");
    const kept = orders.filter((x) => x.status === "kept").length;
    if (kept) parts.push(kept + " order(s) left working because their position did not close");
    const ni = res.not_included || {};
    const late = (Array.isArray(ni.positions) ? ni.positions.length : 0) + (Array.isArray(ni.orders) ? ni.orders.length : 0);
    if (late) parts.push(late + " item(s) opened after the summary and were not included");
    return label + ": " + parts.join(". ") + ".";
  }

  async function sendBatch(path, body, label) {
    setStatus(label + ": sending...", false);
    try {
      const res = await api.post(path, body);
      const text = reportText(label, res);
      const clean = res && res.ok === true && !res.kept && !(res.not_included &&
        ((res.not_included.positions || []).length || (res.not_included.orders || []).length));
      setStatus(text, !(res && res.ok === true));
      if (typeof api.notify === "function") api.notify(text, res && res.ok === true ? (clean ? "" : "warn") : "error");
      return res;
    } catch (err) {
      setStatus(errorText(err), true);
      throw err;
    }
  }

  function flattenAll() {
    const blocked = batchGate();
    if (blocked) { setStatus(blocked.message, true); return Promise.reject(blocked); }
    const acct = state.snapshot.account;
    const lists = openLists();
    if (!lists.positions.length && !lists.orders.length) {
      const err = new Error("Nothing to flatten: no open positions or orders on this account.");
      setStatus(err.message, false);
      return Promise.reject(err);
    }
    const rows = [["Account", acct.name != null ? String(acct.name) : String(acct.id)]];
    for (const p of lists.positions) rows.push(["Close position", positionText(p)]);
    for (const o of lists.orders) rows.push(["Cancel order", orderRowText(o)]);
    const body = {
      account_id: acct.id,
      contract_ids: lists.positions.map((p) => String(p.contract_id)),
      order_ids: lists.orders.map((o) => Number(o.id)),
      confirm: "yes",
    };
    return confirmThenSend({
      title: "Flatten all - review",
      rows: rows,
      warning: FLATTEN_NOTE,
      sendLabel: "Send flatten",
      onSend: () => sendBatch("/api/positions/flatten", body, "Flatten all"),
    });
  }

  function cancelAllOrders() {
    const blocked = batchGate();
    if (blocked) { setStatus(blocked.message, true); return Promise.reject(blocked); }
    const acct = state.snapshot.account;
    const lists = openLists();
    if (!lists.orders.length) {
      const err = new Error("Nothing to cancel: no open orders on this account.");
      setStatus(err.message, false);
      return Promise.reject(err);
    }
    const rows = [["Account", acct.name != null ? String(acct.name) : String(acct.id)]];
    for (const o of lists.orders) rows.push(["Cancel order", orderRowText(o)]);
    const body = { account_id: acct.id, order_ids: lists.orders.map((o) => Number(o.id)), confirm: "yes" };
    return confirmThenSend({
      title: "Cancel all - review",
      rows: rows,
      warning: CANCEL_ALL_NOTE,
      sendLabel: "Send cancels",
      onSend: () => sendBatch("/api/orders/cancel_all", body, "Cancel all"),
    });
  }

  flattenBtn.addEventListener("click", () => { flattenAll().catch(() => {}); });
  cancelAllBtn.addEventListener("click", () => { cancelAllOrders().catch(() => {}); });

  // ---- api.orders: the DOM and the hotkeys trade through the same flow -----------------
  api.orders = {
    allowed: () => state.allowed && !state.busy,
    busy: () => state.busy,
    toolsAvailable: () => state.toolsAvailable,
    bracket: () => bracket(),
    // body: {side, order_type, size, limit_price?, stop_price?, stop_ticks?, target_ticks?};
    // account, contract and (unless given) the bracket settings are filled in.
    place: (partial) => {
      let body;
      try {
        body = completeBody(partial || {});
      } catch (err) {
        setStatus(err.message, true);
        return Promise.reject(err);
      }
      return placeOrder(body);
    },
    cancel: (orderId) => cancelOrder(orderId),
    close: (contractId) => closePosition(contractId),
    flatten: () => flattenAll(),
    cancelAll: () => cancelAllOrders(),
  };

  // ---- the confirmation modal (shared by every action) -----------------------------------
  let modal = null;

  // outcome: {ok: true, value} after a successful send, {ok: false, error} otherwise.
  function closeModal(outcome) {
    if (!modal) return;
    document.removeEventListener("keydown", modal.onKey);
    modal.backdrop.remove();
    const done = modal.onDone;
    modal = null;
    if (typeof done === "function") done(outcome);
  }

  function openModal(opts) {
    closeModal({ ok: false, error: new Error(NOTHING_SENT) });
    const requireTyping = opts.requireTyping !== false;
    const backdrop = el("div", "modal-backdrop");
    const box = el("div", "modal");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");

    const title = el("h3");
    title.textContent = opts.title || MODAL_TITLE;
    box.appendChild(title);

    const summary = el("div", "summary");
    const dl = el("dl");
    for (const [k, v] of opts.rows) {
      const dt = el("dt");
      dt.textContent = String(k);
      const dd = el("dd");
      dd.textContent = v === null || v === undefined ? "-" : String(v);
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    summary.appendChild(dl);
    box.appendChild(summary);

    const warn = el("p", "warning");
    warn.textContent = opts.warning || "";
    box.appendChild(warn);

    const actions = el("div", "actions");
    const input = el("input", "confirm-input");
    input.type = "text";
    input.placeholder = "type yes";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("aria-label", "Type yes to enable the send button");
    const send = el("button", "primary");
    send.type = "button";
    send.textContent = opts.sendLabel;
    send.disabled = requireTyping;
    const cancel = el("button", "cancel");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    if (requireTyping) actions.appendChild(input);
    actions.appendChild(send);
    actions.appendChild(cancel);
    box.appendChild(actions);

    const ready = () => !requireTyping || input.value === "yes";
    input.addEventListener("input", () => {
      send.disabled = !ready();
    });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && ready() && !send.disabled) {
        ev.preventDefault();
        send.click();
      }
    });
    cancel.addEventListener("click", () => {
      closeModal({ ok: false, error: new Error(NOTHING_SENT) });
      setStatus(NOTHING_SENT, false);
    });
    send.addEventListener("click", async () => {
      if (!ready() || send.disabled) return;
      send.disabled = true;
      cancel.disabled = true;
      input.disabled = true;
      let outcome;
      try {
        outcome = { ok: true, value: await withBusy(() => opts.onSend()) };
      } catch (err) {
        outcome = { ok: false, error: err }; /* status already shows the reason */
      } finally {
        closeModal(outcome);
      }
    });

    const onKey = (ev) => {
      if (ev.key === "Escape" && modal && !input.disabled) {
        closeModal({ ok: false, error: new Error(NOTHING_SENT) });
        setStatus(NOTHING_SENT, false);
      }
    };
    document.addEventListener("keydown", onKey);

    backdrop.appendChild(box);
    document.body.appendChild(backdrop);
    modal = { backdrop: backdrop, onKey: onKey, onDone: opts.onDone };
    if (requireTyping) input.focus(); else send.focus();
  }

  // ---- initial state ----------------------------------------------------------------
  setText(accountLine, "Waiting for the first snapshot...");
  setText(contractValue, "none selected");
  syncBracketInputs();
  applyEnabled();
}

// ---- helpers ----------------------------------------------------------------------

function el(tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

function ticksInput(className, label) {
  const input = el("input", className);
  input.type = "number";
  input.min = "1";
  input.max = String(MAX_BRACKET_TICKS);
  input.step = "1";
  input.placeholder = "none";
  input.inputMode = "numeric";
  input.title = label + ": distance from the entry in ticks, blank for none";
  return input;
}

function setText(node, text) {
  const next = text == null ? "" : String(text);
  if (node.textContent !== next) node.textContent = next;
}

function titleCase(text) {
  const s = String(text == null ? "" : text).toLowerCase().replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function fieldRow(labelText, control) {
  const wrap = el("div", "field");
  const label = el("span", "label");
  label.textContent = labelText;
  wrap.appendChild(label);
  wrap.appendChild(control);
  return wrap;
}

function errorText(err) {
  if (!err) return "Request failed.";
  const msg = err.message != null ? String(err.message) : String(err);
  return msg || "Request failed.";
}

function makeFmt(api) {
  const f = (api && api.fmt) || {};
  return {
    price(v, contractId) {
      if (v === null || v === undefined) return "-";
      if (typeof f.price === "function") {
        try { return String(f.price(v, contractId)); } catch (err) { /* fall through */ }
      }
      const n = Number(v);
      return Number.isFinite(n) ? String(n) : String(v);
    },
  };
}
