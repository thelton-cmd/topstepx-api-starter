# Alerts and guardrails

The **Alerts** button in the header opens a panel with three things, all driven by numbers you
type. Nothing here has a suggested value, and nothing here is advice.

## Price alerts

Add an alert with a contract, a price, a direction (crosses, moves above, moves below) and an
optional note, or **Alt+click the chart** at the price you want. Alerts are checked against every
quote using the previous and the new price, so a gap straight through the level still fires.

- A **once** alert fires one time and then shows as fired; press Re-arm to use it again.
- A repeating alert re-arms by itself once price is back on the other side of its level.
- Alerts for the focused contract are drawn on the chart as dotted lines labelled with the note.

When an alert fires you get a toast, optionally a sound and a desktop notification (turn those on
under Settings > Alerts; the browser asks for permission once), and a line in the Recent events
feed.

## Fill notifications

Every fill on the selected account shows as "Filled BUY 1 ESZ6 @ 7,765.25" the same way. Turn it
off under Settings > Alerts if you do not want it.

## Guardrails

Two numbers of your own: a **max daily loss** and a **max open contracts**. When today's P&L (an
approximation built from API data: realized plus unrealized minus fees) reaches minus your number,
or the open contract count is above your cap, the dashboard pauses order entry: a banner appears,
the ticket and DOM refuse to send, and the server answers every order request with the reason.
The pause stays until you change or clear the guard yourself, even if the figure recovers.

This pause applies to this dashboard only. Topstep's own account rules are enforced by Topstep.

## Where it is saved

Alerts and the guard live in `%LOCALAPPDATA%\topstepx\alerts.json` (Windows) or
`~/.topstepx/alerts.json`, or in the file named by the `TOPSTEPX_ALERTS_FILE` environment
variable. Nothing else is written there.

Routes: `GET /api/alerts`, `GET /api/alerts/events?after=`, `POST /api/alerts/add|update|delete`,
`POST /api/alerts/guard`, `POST /api/alerts/guard/clear`, `POST /api/alerts/test`.
