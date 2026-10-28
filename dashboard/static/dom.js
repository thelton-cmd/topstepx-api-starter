// dom.js - DOM (depth-of-market) ladder with click trading, plus the tick tape, for the focused contract.
//
// Columns: your working buy orders | bid size | price | ask size | your working sell orders | session volume.
//   - Click a bid-size cell to buy at that price, an ask-size cell to sell. Below the market on the
//     buy side (or above it on the sell side) that is a limit order; past the market it is a stop.
//   - Click one of your orders on the ladder to cancel it. Your position shows at its average price.
//   - Click a price to copy it into the order ticket (never auto-submits).
// Every order goes through api.orders (ticket.js): the same preflight, the confirmation mode the
// trader chose in Settings, the bracket distances from Settings (api.orders.bracket(), added by
// ticket.js to every order), and the server's Practice-only guard. This module never POSTs itself.
//
// Contract (see dashboard/ARCHITECTURE.md, "Frontend contract"):
//   mountDom(domRootEl, tapeRootEl, api) is called once by app.js.
//   api.get(path) -> json (throws Error on !ok), api.onState(fn), api.focus(), api.fmt.*,
//   api.settings.domSize, api.orders (set by ticket.js; may be null until it mounts).

const DEPTH_MS = 250;
const TAPE_MS = 500;
const LEVELS = 15;
const MAX_TAPE_ROWS = 200;
const MAX_GRID_ROWS = 400;
const MANUAL_SCROLL_HOLD_MS = 5000;
const QUIET_NOTE = "Outside market hours the stream is quiet; that is normal.";

export function mountDom(domRootEl, tapeRootEl, api) {
  const fmt = makeFmt(api);
  const settings = api.settings || { domSize: 1 };

  // ---- ladder shell -------------------------------------------------------------
  const tools = el("div", "dom-tools");
  const sizeLabel = el("label");
  sizeLabel.textContent = "Size ";
  const sizeInput = el("input", "dom-size");
  sizeInput.type = "number";
  sizeInput.min = "1";
  sizeInput.max = "50";
  sizeInput.step = "1";
  sizeInput.value = String(settings.domSize || 1);
  sizeInput.title = "Contracts per DOM click";
  sizeLabel.appendChild(sizeInput);
  const hint = el("span", "dom-hint muted");
  const domStatus = el("span", "dom-status");
  domStatus.setAttribute("role", "status");
  tools.appendChild(sizeLabel);
  tools.appendChild(hint);
  tools.appendChild(domStatus);
  const header = el("div", "dom-header");
  const domPlaceholder = el("div", "placeholder");
  const ladder = el("div", "ladder");
  domRootEl.appendChild(tools);
  domRootEl.appendChild(header);
  domRootEl.appendChild(domPlaceholder);
  domRootEl.appendChild(ladder);

  sizeInput.addEventListener("change", () => {
    const n = parseInt(sizeInput.value, 10);
    const size = Number.isFinite(n) && n >= 1 ? Math.min(n, 50) : 1;
    sizeInput.value = String(size);
    settings.domSize = size;
    if (typeof api.saveSettings === "function") api.saveSettings();
  });
  if (typeof api.onSettings === "function") {
    api.onSettings(() => {
      if (document.activeElement !== sizeInput) sizeInput.value = String(settings.domSize || 1);
      updateHint();
    });
  }

  // ---- tape shell ---------------------------------------------------------------
  const tapePlaceholder = el("div", "placeholder");
  const tape = el("div", "tape");
  const tapeHead = el("div", "row head");
  for (const text of ["time", "price", "size", "side"]) {
    const cell = el("span");
    cell.textContent = text;
    tapeHead.appendChild(cell);
  }
  tape.appendChild(tapeHead);
  tapeRootEl.appendChild(tapePlaceholder);
  tapeRootEl.appendChild(tape);

  const state = {
    focus: null,
    contracts: [],
    tickSize: null,
    depthVersion: null,
    hasLadder: false,
    lastSnap: null,
    lastSeq: 0,
    tapeCount: 0,
    manualScrollAt: 0,
    expectedScrollTop: null,
    depthBusy: false,
    tapeBusy: false,
    scroller: null,
    allowed: false,
    orders: [],        // working orders for the focus contract
    position: null,    // open position for the focus contract
    overlayKey: "",
  };

  function tradingOn() {
    return state.allowed && api.orders && typeof api.orders.place === "function";
  }

  function updateHint() {
    if (!state.focus) { hint.textContent = ""; return; }
    if (!state.allowed) { hint.textContent = "read-only account: click a price to copy it into the ticket"; return; }
    const m = settings.confirm === "oneclick" ? "one-click" : settings.confirm === "click" ? "review, click Send" : "review, type yes";
    const b = bracketText();
    hint.textContent = "click bid size to buy, ask size to sell (" + m + (b ? "; brackets: " + b.slice(3) : "") + "); click your order to cancel";
  }

  function setDomStatus(text, isError) {
    domStatus.textContent = text || "";
    domStatus.classList.toggle("error", !!isError);
  }

  // ---- snapshot: focus, tick size, orders, position ------------------------------
  api.onState((snapshot) => {
    if (!snapshot) return;
    state.contracts = Array.isArray(snapshot.contracts) ? snapshot.contracts : [];
    state.allowed = snapshot.order_entry_allowed === true;
    syncFocus();
    const orders = (Array.isArray(snapshot.orders) ? snapshot.orders : []).filter((o) => o && o.contract_id === state.focus);
    const position = (Array.isArray(snapshot.positions) ? snapshot.positions : []).find((p) => p && p.contract_id === state.focus) || null;
    const key = JSON.stringify([state.allowed, orders.map((o) => [o.id, o.side, o.type, o.size, o.filled, o.limit_price, o.stop_price]),
      position && [position.side, position.contracts, position.average_price]]);
    state.orders = orders;
    state.position = position;
    updateHint();
    if (key !== state.overlayKey) {
      state.overlayKey = key;
      if (state.lastSnap) renderLadder(state.lastSnap);   // orders changed: redraw the ladder markers
    }
  });

  function syncFocus() {
    const focus = typeof api.focus === "function" ? api.focus() : null;
    const next = focus || null;
    if (next !== state.focus) {
      state.focus = next;
      resetForFocus();
    }
    const c = state.contracts.find((x) => x && x.id === state.focus);
    const tick = c && c.tick_size != null ? Number(c.tick_size) : null;
    state.tickSize = Number.isFinite(tick) && tick > 0 ? tick : null;
  }

  function resetForFocus() {
    state.depthVersion = null;
    state.hasLadder = false;
    state.lastSnap = null;
    state.lastSeq = 0;
    state.tapeCount = 0;
    state.manualScrollAt = 0;
    state.expectedScrollTop = null;
    state.overlayKey = "";
    ladder.replaceChildren();
    tape.replaceChildren(tapeHead);
    header.textContent = "";
    setDomStatus("");
    showPlaceholder(domPlaceholder, "No depth yet");
    showPlaceholder(tapePlaceholder, "No ticks yet");
    updateHint();
  }

  function focusLabel() {
    if (!state.focus) return "";
    const name = typeof api.contractName === "function" ? api.contractName(state.focus) : null;
    return name && name !== state.focus ? name + " (" + state.focus + ")" : state.focus;
  }

  function showPlaceholder(target, what) {
    const label = focusLabel();
    target.textContent = state.focus
      ? what + " for " + label + ". " + QUIET_NOTE
      : "Pick a contract to watch.";
    target.style.display = "";
  }

  function hidePlaceholder(target) {
    target.style.display = "none";
  }

  // ---- manual scroll detection --------------------------------------------------
  function onScroll(ev) {
    const target = ev.currentTarget;
    if (state.scroller && target !== state.scroller) return;
    if (state.expectedScrollTop !== null && Math.abs(target.scrollTop - state.expectedScrollTop) <= 1) {
      return; // our own programmatic scroll
    }
    state.manualScrollAt = Date.now();
  }
  ladder.addEventListener("scroll", onScroll);
  domRootEl.addEventListener("scroll", onScroll);
  ["wheel", "touchmove", "mousedown"].forEach((type) => {
    domRootEl.addEventListener(type, () => { state.manualScrollAt = Date.now(); }, { passive: true });
  });

  function userScrolledRecently() {
    return Date.now() - state.manualScrollAt < MANUAL_SCROLL_HOLD_MS;
  }

  function scroller() {
    if (state.scroller) return state.scroller;
    const rootStyle = getComputedStyle(domRootEl);
    if (rootStyle.overflowY === "auto" || rootStyle.overflowY === "scroll") {
      state.scroller = domRootEl;
    } else {
      const cs = getComputedStyle(ladder);
      if (cs.overflowY === "visible" && cs.maxHeight === "none") {
        ladder.style.overflowY = "auto";
        ladder.style.maxHeight = "60vh";
      }
      state.scroller = ladder;
    }
    return state.scroller;
  }

  function setScrollTop(value) {
    const sc = scroller();
    sc.scrollTop = value;
    state.expectedScrollTop = sc.scrollTop; // read back the clamped value
  }

  function offsetWithin(node, sc) {
    return node.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;
  }

  // ---- clicks: price copy, click trading, cancel -----------------------------------
  ladder.addEventListener("click", (ev) => {
    const target = ev.target instanceof Element ? ev.target : null;
    if (!target || !ladder.contains(target)) return;
    const priceCell = target.closest(".price");
    if (priceCell) {
      const price = Number(priceCell.dataset.price);
      if (Number.isFinite(price)) window.dispatchEvent(new CustomEvent("dom-price", { detail: { price: price } }));
      return;
    }
    const orderCell = target.closest(".ord[data-order-id]");
    if (orderCell) {
      cancelFromLadder(orderCell.dataset.orderId);
      return;
    }
    const sideCell = target.closest(".bid.clickable, .ask.clickable");
    if (sideCell) {
      const price = Number(sideCell.dataset.price);
      if (Number.isFinite(price)) tradeFromLadder(sideCell.classList.contains("bid") ? "buy" : "sell", price);
    }
  });

  function orderSize() {
    const n = parseInt(sizeInput.value, 10);
    return Number.isFinite(n) && n >= 1 ? Math.min(n, 50) : 1;
  }

  // " + stop 10t / target 20t" for the brackets ticket.js attaches to every order ("" when none).
  function bracketText() {
    const b = api.orders && typeof api.orders.bracket === "function" ? api.orders.bracket() : null;
    if (!b) return "";
    const parts = [];
    if (b.stopTicks != null) parts.push("stop " + b.stopTicks + "t");
    if (b.targetTicks != null) parts.push("target " + b.targetTicks + "t");
    return parts.length ? " + " + parts.join(" / ") : "";
  }

  function tradeFromLadder(side, price) {
    if (!tradingOn() || !state.lastSnap) return;
    if (!api.orders.allowed()) { setDomStatus("Another request is still in progress.", true); return; }
    const snap = state.lastSnap;
    const bestBid = numOrNull(snap.best_bid);
    const bestAsk = numOrNull(snap.best_ask);
    const last = numOrNull(snap.last_price);
    // A buy below the offer rests as a limit; a buy above it is a stop (and vice versa for sells).
    const ref = side === "buy" ? (bestAsk !== null ? bestAsk : last) : (bestBid !== null ? bestBid : last);
    let type;
    if (ref === null) type = "limit";
    else if (side === "buy") type = price <= ref + 1e-9 ? "limit" : "stop";
    else type = price >= ref - 1e-9 ? "limit" : "stop";
    const body = { side: side, order_type: type, size: orderSize() };
    if (type === "limit") body.limit_price = price; else body.stop_price = price;
    const text = side.toUpperCase() + " " + body.size + " " + type + " @ " + fmt.price(price, state.focus) + bracketText();
    setDomStatus(text + "...");
    api.orders.place(body)
      .then(() => setDomStatus("Sent: " + text))
      .catch((err) => notSent(err));
  }

  // Backing out of the review is not an error; anything else is shown in red.
  function notSent(err) {
    const msg = err && err.message ? err.message : "Not sent.";
    setDomStatus(msg, msg !== "Nothing was sent.");
  }

  function cancelFromLadder(orderId) {
    if (!tradingOn() || orderId == null) return;
    if (!api.orders.allowed()) { setDomStatus("Another request is still in progress.", true); return; }
    setDomStatus("Cancel order " + orderId + "...");
    api.orders.cancel(orderId)
      .then(() => setDomStatus("Cancel sent for order " + orderId))
      .catch((err) => notSent(err));
  }

  // ---- depth polling ------------------------------------------------------------
  async function pollDepth() {
    syncFocus();
    if (state.depthBusy || !state.focus) return;
    state.depthBusy = true;
    const focusAtRequest = state.focus;
    try {
      const snap = await api.get("/api/depth?contract=" + encodeURIComponent(focusAtRequest) + "&levels=" + LEVELS);
      if (focusAtRequest !== state.focus) return; // focus changed while waiting
      if (!snap || typeof snap !== "object") return;
      if (state.depthVersion !== null && snap.version === state.depthVersion) return;
      state.depthVersion = snap.version == null ? null : snap.version;
      state.lastSnap = snap;
      renderLadder(snap);
      for (const fn of snapshotListeners) { try { fn(snap, focusAtRequest); } catch (err) { console.error("dom onSnapshot listener failed", err); } }
    } catch (err) {
      if (focusAtRequest === state.focus && !state.hasLadder) {
        showPlaceholder(domPlaceholder, "No depth yet");
      }
    } finally {
      state.depthBusy = false;
    }
  }

  function renderLadder(snap) {
    const bids = Array.isArray(snap.bids) ? snap.bids : [];
    const asks = Array.isArray(snap.asks) ? snap.asks : [];
    const bestBid = numOrNull(snap.best_bid);
    const bestAsk = numOrNull(snap.best_ask);
    const lastPrice = numOrNull(snap.last_price);
    const lastSize = numOrNull(snap.last_size);

    if (!bids.length && !asks.length && bestBid === null && bestAsk === null) {
      ladder.replaceChildren();
      header.textContent = "";
      state.hasLadder = false;
      showPlaceholder(domPlaceholder, "No depth yet");
      return;
    }

    const tick = state.tickSize;
    const dec = tick ? decimalsOf(tick) : null;
    const key = (p) => (dec === null ? String(p) : Number(p).toFixed(dec));
    const label = (p) => (dec === null ? fmt.price(p) : Number(p).toFixed(dec));
    const snapKey = (p) => (tick ? key(Math.round(p / tick) * tick) : key(p));   // nearest grid price

    const bidMap = new Map();
    for (const lvl of bids) {
      const p = numOrNull(lvl && lvl.price);
      const s = numOrNull(lvl && lvl.size);
      if (p !== null && s !== null) bidMap.set(key(p), { price: p, size: s });
    }
    const askMap = new Map();
    for (const lvl of asks) {
      const p = numOrNull(lvl && lvl.price);
      const s = numOrNull(lvl && lvl.size);
      if (p !== null && s !== null) askMap.set(key(p), { price: p, size: s });
    }
    const tradedMap = new Map();
    let maxTraded = 0;
    for (const t of Array.isArray(snap.traded) ? snap.traded : []) {
      const p = numOrNull(t && t.price);
      const v = numOrNull(t && t.volume);
      if (p !== null && v !== null) {
        tradedMap.set(key(p), v);
        if (v > maxTraded) maxTraded = v;
      }
    }

    // Working orders and the position, keyed by their (grid-snapped) price.
    const orderMap = new Map();   // key -> {buy: [orders], sell: [orders]}
    for (const o of state.orders) {
      const p = numOrNull(o.limit_price != null ? o.limit_price : o.stop_price);
      if (p === null) continue;
      const k = snapKey(p);
      if (!orderMap.has(k)) orderMap.set(k, { buy: [], sell: [] });
      orderMap.get(k)[String(o.side).toUpperCase() === "BUY" ? "buy" : "sell"].push(o);
    }
    const pos = state.position && numOrNull(state.position.average_price) !== null ? state.position : null;
    const posKey = pos ? snapKey(numOrNull(pos.average_price)) : null;

    // Build the list of prices to show, highest first. Make sure orders and the position are on it.
    let prices = [];
    const anchorTop = bestAsk !== null ? bestAsk : bestBid;
    const anchorBottom = bestBid !== null ? bestBid : bestAsk;
    let gridOk = false;
    if (tick && anchorTop !== null && anchorBottom !== null) {
      let top = anchorTop + LEVELS * tick;
      let bottom = anchorBottom - LEVELS * tick;
      for (const k of [...orderMap.keys(), ...(posKey ? [posKey] : [])]) {
        const p = Number(k);
        if (p > top) top = Math.min(p + 2 * tick, anchorTop + MAX_GRID_ROWS * tick / 2);
        if (p < bottom) bottom = Math.max(p - 2 * tick, anchorBottom - MAX_GRID_ROWS * tick / 2);
      }
      const n = Math.round((top - bottom) / tick);
      if (n >= 0 && n <= MAX_GRID_ROWS) {
        for (let i = n; i >= 0; i--) {
          prices.push(roundTo(bottom + i * tick, dec));
        }
        gridOk = true;
      }
    }
    if (!gridOk) {
      // Fallback: only the levels the server returned, deduplicated, highest first.
      const seen = new Set();
      const all = [];
      for (const v of askMap.values()) { if (!seen.has(key(v.price))) { seen.add(key(v.price)); all.push(v.price); } }
      for (const v of bidMap.values()) { if (!seen.has(key(v.price))) { seen.add(key(v.price)); all.push(v.price); } }
      if (lastPrice !== null && !seen.has(key(lastPrice))) all.push(lastPrice);
      all.sort((a, b) => b - a);
      prices = all;
    }

    // Sizes per row and the largest size shown (for bar widths).
    const rows = [];
    let maxSize = 0;
    for (const p of prices) {
      const k = key(p);
      let bidSize = null;
      let askSize = null;
      if (bestBid === null || p <= bestBid + 1e-9) {
        const b = bidMap.get(k);
        if (b) bidSize = b.size;
      }
      if (bestAsk === null || p >= bestAsk - 1e-9) {
        const a = askMap.get(k);
        if (a) askSize = a.size;
      }
      if (bidSize !== null && bidSize > maxSize) maxSize = bidSize;
      if (askSize !== null && askSize > maxSize) maxSize = askSize;
      rows.push({ price: p, key: k, bidSize: bidSize, askSize: askSize, traded: tradedMap.get(k), orders: orderMap.get(k) || null });
    }

    const lastKey = lastPrice === null ? null : key(lastPrice);
    const bestBidKey = bestBid === null ? null : key(bestBid);
    const bestAskKey = bestAsk === null ? null : key(bestAsk);
    const clickable = tradingOn();
    const brackets = clickable ? bracketText() : "";

    const frag = document.createDocumentFragment();
    const head = el("div", "row head");
    for (const [text, cls] of [["buys", "ord"], ["bid size", "bid"], ["price", "price"], ["ask size", "ask"], ["sells", "ord"], ["vol", "vol"]]) {
      const cell = el("div", cls);
      cell.textContent = text;
      head.appendChild(cell);
    }
    frag.appendChild(head);
    let bestBidRow = null;
    let bestAskRow = null;
    for (const r of rows) {
      const row = el("div", "row");
      row.dataset.price = r.key;
      if (r.key === lastKey) row.classList.add("last");
      if (r.key === posKey) row.classList.add(String(pos.side).toUpperCase() === "LONG" ? "pos-long" : "pos-short");

      row.appendChild(orderCell("buy", r, pos, posKey));

      const bidCell = el("div", "bid");
      bidCell.dataset.price = String(r.price);
      if (clickable) {
        bidCell.classList.add("clickable");
        const isLimit = bestAsk === null || r.price <= bestAsk + 1e-9;
        bidCell.title = "Buy " + orderSize() + " " + (isLimit ? "limit" : "stop") + " @ " + label(r.price) + brackets;
      }
      if (r.bidSize !== null) {
        const bar = el("div", "bar");
        bar.style.width = pct(r.bidSize, maxSize);
        bidCell.appendChild(bar);
        const txt = el("span", "size");
        txt.textContent = fmt.size(r.bidSize);
        bidCell.appendChild(txt);
      }

      const priceCell = el("div", "price");
      priceCell.dataset.price = String(r.price);
      priceCell.title = "Click to copy this price into the order ticket";
      const priceTxt = el("span", "value");
      priceTxt.textContent = label(r.price);
      priceCell.appendChild(priceTxt);
      if (r.key === lastKey && lastSize !== null) {
        const ls = el("span", "last-size");
        ls.textContent = " x" + fmt.size(lastSize);
        priceCell.appendChild(ls);
      }

      const askCell = el("div", "ask");
      askCell.dataset.price = String(r.price);
      if (clickable) {
        askCell.classList.add("clickable");
        const isLimit = bestBid === null || r.price >= bestBid - 1e-9;
        askCell.title = "Sell " + orderSize() + " " + (isLimit ? "limit" : "stop") + " @ " + label(r.price) + brackets;
      }
      if (r.askSize !== null) {
        const bar = el("div", "bar");
        bar.style.width = pct(r.askSize, maxSize);
        askCell.appendChild(bar);
        const txt = el("span", "size");
        txt.textContent = fmt.size(r.askSize);
        askCell.appendChild(txt);
      }

      row.appendChild(bidCell);
      row.appendChild(priceCell);
      row.appendChild(askCell);
      row.appendChild(orderCell("sell", r, pos, posKey));

      const volCell = el("div", "vol");
      if (r.traded != null && r.traded > 0) {
        const bar = el("div", "bar");
        bar.style.width = pct(r.traded, maxTraded);
        volCell.appendChild(bar);
        const txt = el("span", "size");
        txt.textContent = fmt.size(r.traded);
        volCell.appendChild(txt);
        volCell.title = fmt.size(r.traded) + " traded at " + label(r.price) + " this session";
      }
      row.appendChild(volCell);

      frag.appendChild(row);
      if (r.key === bestBidKey) bestBidRow = row;
      if (r.key === bestAskKey) bestAskRow = row;
    }

    // Header line: best bid / best ask / last / session high-low.
    const parts = [];
    parts.push("Bid " + (bestBid === null ? "-" : label(bestBid)));
    parts.push("Ask " + (bestAsk === null ? "-" : label(bestAsk)));
    let lastText = "Last " + (lastPrice === null ? "-" : label(lastPrice));
    if (lastPrice !== null && lastSize !== null) lastText += " x" + fmt.size(lastSize);
    parts.push(lastText);
    const hi = numOrNull(snap.high);
    const lo = numOrNull(snap.low);
    parts.push("H " + (hi === null ? "-" : label(hi)) + "  L " + (lo === null ? "-" : label(lo)));
    if (pos) parts.push((String(pos.side).toUpperCase() === "LONG" ? "Long " : "Short ") + fmt.size(pos.contracts) + " @ " + label(numOrNull(pos.average_price)));
    header.textContent = parts.join("   ");

    const sc = scroller();
    const previousScroll = sc.scrollTop;
    ladder.replaceChildren(frag);
    hidePlaceholder(domPlaceholder);
    state.hasLadder = true;

    const anchorRows = [bestAskRow, bestBidRow].filter((x) => x);
    if (userScrolledRecently() || !anchorRows.length) {
      setScrollTop(previousScroll);
    } else {
      const tops = anchorRows.map((x) => offsetWithin(x, sc));
      const bottoms = anchorRows.map((x, i) => tops[i] + x.offsetHeight);
      const mid = (Math.min.apply(null, tops) + Math.max.apply(null, bottoms)) / 2;
      setScrollTop(Math.max(0, mid - sc.clientHeight / 2));
    }
  }

  // One of the two outer columns: the trader's working orders on that side, and the position badge.
  function orderCell(side, r, pos, posKey) {
    const cell = el("div", "ord " + side);
    const list = r.orders ? r.orders[side] : [];
    if (list && list.length) {
      const total = list.reduce((sum, o) => sum + (Number(o.size) || 0) - (Number(o.filled) || 0), 0);
      const chip = el("span", "chip");
      chip.textContent = fmt.size(total > 0 ? total : list.length);
      cell.appendChild(chip);
      cell.dataset.orderId = String(list[0].id);
      cell.title = list.map((o) => o.side + " " + o.type + " " + o.size + (o.filled ? " (" + o.filled + " filled)" : "") + " #" + o.id).join("\n")
        + "\nClick to cancel" + (list.length > 1 ? " order #" + list[0].id : "");
      cell.classList.add("has-order");
    }
    if (pos && r.key === posKey) {
      const isLong = String(pos.side).toUpperCase() === "LONG";
      if ((side === "buy") === isLong) {
        const badge = el("span", "pos");
        badge.textContent = (isLong ? "L" : "S") + fmt.size(pos.contracts);
        badge.title = (isLong ? "Long " : "Short ") + fmt.size(pos.contracts) + " @ " + fmt.price(pos.average_price, state.focus);
        cell.appendChild(badge);
      }
    }
    return cell;
  }

  // ---- tape polling -------------------------------------------------------------
  async function pollTape() {
    syncFocus();
    if (state.tapeBusy || !state.focus) return;
    state.tapeBusy = true;
    const focusAtRequest = state.focus;
    const after = state.lastSeq;
    try {
      const data = await api.get(
        "/api/tape?contract=" + encodeURIComponent(focusAtRequest) + "&after=" + encodeURIComponent(String(after))
      );
      if (focusAtRequest !== state.focus) return;
      if (!data || typeof data !== "object") return;
      const events = Array.isArray(data.events) ? data.events : [];
      if (typeof data.last_seq === "number" && Number.isFinite(data.last_seq)) {
        state.lastSeq = Math.max(state.lastSeq, data.last_seq);
      }
      for (const e of events) {
        if (e && typeof e.seq === "number" && e.seq > state.lastSeq) state.lastSeq = e.seq;
      }
      if (events.length) appendTape(events);
      else if (!state.tapeCount) showPlaceholder(tapePlaceholder, "No ticks yet");
    } catch (err) {
      if (focusAtRequest === state.focus && !state.tapeCount) {
        showPlaceholder(tapePlaceholder, "No ticks yet");
      }
    } finally {
      state.tapeBusy = false;
    }
  }

  function appendTape(events) {
    // Events arrive oldest first; newest must end up on top, so walk them in reverse and
    // build a fragment whose first child is the newest, then insert it before the list head.
    const frag = document.createDocumentFragment();
    let added = 0;
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (!e || typeof e !== "object") continue;
      const row = buildTapeRow(e);
      if (!row) continue;
      frag.appendChild(row);
      added++;
    }
    if (!added) return;
    tape.insertBefore(frag, tapeHead.nextSibling);
    while (tape.childElementCount - 1 > MAX_TAPE_ROWS) {
      tape.removeChild(tape.lastElementChild);
    }
    state.tapeCount = tape.childElementCount - 1;
    hidePlaceholder(tapePlaceholder);
  }

  function buildTapeRow(e) {
    const row = el("div", "row");
    const time = el("span", "time");
    time.textContent = fmt.clock(e.time);
    row.appendChild(time);
    if (e.kind === "trade") {
      const side = typeof e.side === "string" ? e.side.toUpperCase() : "";
      if (side === "BUY") row.classList.add("buy");
      else if (side === "SELL") row.classList.add("sell");
      const price = el("span", "price");
      price.textContent = fmt.price(e.price, state.focus);
      const size = el("span", "size");
      size.textContent = fmt.size(e.size);
      const sideEl = el("span", "side");
      sideEl.textContent = side || "-";
      row.appendChild(price);
      row.appendChild(size);
      row.appendChild(sideEl);
      return row;
    }
    if (e.kind === "quote") {
      row.classList.add("quote");
      const bidAsk = el("span", "bid-ask");
      bidAsk.textContent = "bid " + fmt.price(e.bid, state.focus) + " / ask " + fmt.price(e.ask, state.focus);
      const last = el("span", "last");
      last.textContent = "last " + fmt.price(e.price, state.focus);
      const tag = el("span", "side");
      tag.textContent = "quote";
      row.appendChild(bidAsk);
      row.appendChild(last);
      row.appendChild(tag);
      return row;
    }
    return null;
  }

  // ---- hooks for feature modules ----------------------------------------------------
  const snapshotListeners = [];
  api.dom = {
    onSnapshot(fn) { if (typeof fn === "function") snapshotListeners.push(fn); },
    size: () => orderSize(),
    focus: () => state.focus,
    last: () => state.lastSnap,
  };

  // ---- start -----------------------------------------------------------------------
  resetForFocus();
  setInterval(pollDepth, DEPTH_MS);
  setInterval(pollTape, TAPE_MS);
  pollDepth();
  pollTape();
}

// ---- helpers ----------------------------------------------------------------------

function el(tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

function numOrNull(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function decimalsOf(tick) {
  // Number of decimals needed to print prices on this tick grid (0.25 -> 2, 0.01 -> 2, 1 -> 0).
  let s = String(tick);
  if (s.indexOf("e-") >= 0) {
    const exp = parseInt(s.split("e-")[1], 10);
    return Number.isFinite(exp) ? Math.min(exp + 2, 10) : 2;
  }
  const dot = s.indexOf(".");
  if (dot < 0) return 0;
  return Math.min(s.length - dot - 1, 10);
}

function roundTo(value, dec) {
  if (dec === null || dec === undefined) return value;
  return Number(Number(value).toFixed(dec));
}

function pct(size, maxSize) {
  if (!maxSize || maxSize <= 0 || !Number.isFinite(size) || size <= 0) return "0%";
  return Math.max(2, Math.min(100, (size / maxSize) * 100)).toFixed(1) + "%";
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
      return Number.isFinite(n) ? n.toFixed(2) : "-";
    },
    clock(iso) {
      if (!iso) return "-";
      if (typeof f.clock === "function") {
        try { return String(f.clock(iso)); } catch (err) { /* fall through */ }
      }
      const d = new Date(iso);
      return isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString();
    },
    size(v) {
      if (v === null || v === undefined) return "-";
      const n = Number(v);
      if (!Number.isFinite(n)) return "-";
      return Number.isInteger(n) ? String(n) : n.toLocaleString();
    },
  };
}
