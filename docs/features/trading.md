# Trading tools: brackets, Flatten all, Cancel all, hotkeys

These tools live in the order ticket of the local dashboard (`python -m dashboard`). They only
ever send anything on a **Practice Account** (the API marks it `simulated: true` and
`canTrade: true`). On a funded or live account the ticket is read-only and the server refuses
every one of these requests, whatever the page sends.

Every action goes through the same confirmation you picked in **Settings > Order confirmation**:

| Setting | What happens |
|---|---|
| Review and type yes (default) | A summary opens; type `yes`, then click Send |
| Review and click Send | A summary opens; one click sends |
| One-click | No summary; the request goes out at once |

Topstep states that API orders are final and cannot be reviewed or reversed. Test everything on
a Practice Account first. Nothing in this guide is a suggestion about when, what or how much to
trade; every number below is one you type yourself.

## Brackets (stop and target in ticks)

The ticket has two fields, **Stop (ticks)** and **Target (ticks)**, and the same two fields are
under **Settings > Brackets**. Each is a distance from the entry, in ticks, from 1 to 1000.
Leave a field blank for no leg. Both start blank.

- What you type is saved in this browser and is attached to **every** order you send from the
  ticket, the DOM ladder, and the hotkeys, until you clear it ("Clear brackets" in Settings, or
  empty the fields).
- The review shows each leg: how many ticks, above or below, the price it works out to, and the
  exact fields sent to the gateway (`stopLossBracket` / `takeProfitBracket`).
- Leg prices are measured from your order's own price: the limit price for limit and stop-limit
  orders, the stop price for stop orders. A market order has no price of its own, so its legs are
  shown from the last traded price and marked "approx". In every case the gateway prices the legs
  from the actual fill.
- The stop leg is sent as a Stop order and the target leg as a Limit order.

**Your account must be in Auto OCO Brackets mode.** In TopstepX open Settings > Risk Settings
and choose "Auto OCO Brackets". In the default "Position Brackets" mode the gateway rejects any
order that carries brackets with the message "Brackets cannot be used with Position Brackets. You
must enable Auto OCO Brackets." (Per the gateway docs, the rejected order still gets an id.)
TopstepX requires you to be flat to switch modes.

Notes from the gateway documentation and from other API users, worth knowing before you rely on
brackets:

- The API spec does not document the sign of `ticks`. The dashboard sends them signed from the
  entry (a buy's stop is negative and its target positive; a sell's the reverse), which is what
  other API users report the gateway expects. You type plain distances; the review shows the
  signed values that will be sent.
- TopstepX may enforce a minimum bracket distance. The dashboard does not guess one: if the
  gateway refuses a value, its message is shown in the ticket.
- Auto OCO legs cancel each other when one of them fills. Closing the position some other way
  (Close, Flatten, an opposite order) does not cancel them, so check your open orders afterwards.
  Flatten all cancels the open orders it lists.
- Legs on an entry that has not filled yet wait in the "Suspended" state.

## Flatten all and Cancel all

Two red buttons under the ticket (they appear when the `trading_tools` feature is loaded, which
it is unless you start the dashboard with `--features none`):

- **Flatten all** lists every open position and every open order the dashboard shows for the
  selected account. After you confirm, it sends a close request for each listed position, then a
  cancel request for each listed order.
- **Cancel all** lists every open order and sends a cancel request for each.

What to expect:

- Only what was listed in the summary is sent. Anything that opens after the summary appeared is
  not touched; the result says how many such items there were, so you can press again.
- Positions are closed before orders are cancelled, so a protective stop stays in place until its
  position is closed. If a close fails, the orders on that contract are left working and the
  result says so.
- One failure never stops the rest. The result line (and a notice in the corner) lists what was
  sent, what failed and why, and what was already closed or cancelled.
- "Sent" means the gateway accepted the request. Watch the positions and orders tables to see
  the outcome.

## Hotkeys

Keyboard shortcuts are **off** until you turn them on in **Settings > Hotkeys**, which also shows
the map. While they are on, the ticket shows a "Hotkeys on" badge.

| Keys | Action |
|---|---|
| Shift+F | Flatten all |
| Shift+C | Cancel all |
| Shift+B | Buy market, size = the DOM size, plus your bracket settings |
| Shift+S | Sell market, size = the DOM size, plus your bracket settings |
| Escape | Close the confirmation window without sending |

- A shortcut does exactly what the matching button does, through the same confirmation setting.
  With **One-click** confirmation a shortcut sends immediately with no summary; the Hotkeys
  settings say so in orange when that combination is on.
- Shortcuts are ignored while you are typing in a field, while a confirmation window or the
  Settings menu is open, when a key is held down (auto-repeat), and when Ctrl, Alt or Cmd is held.
- A small notice in the corner confirms every shortcut that fires.

## For developers

- Server: `dashboard/features/trading_tools.py` (routes `POST /api/positions/flatten` and
  `POST /api/orders/cancel_all`, both in its `ORDER_ROUTES`, so the server applies the
  Practice-only + `"confirm": "yes"` guard) and `dashboard/orders.py` (`bracket_requests`,
  `bracket_legs`, `flatten_account`, `cancel_orders`).
- Page: `dashboard/static/ticket.js` (bracket fields, buttons, `api.orders.flatten()` /
  `api.orders.cancelAll()`) and `dashboard/static/features/hotkeys.js`.
- Tests (offline, mocked API): `tests/test_dashboard_orders.py`, `tests/test_dashboard_server.py`,
  `tests/test_feature_trading_tools.py`. Live order testing is yours to do, in the browser, on a
  Practice Account.
