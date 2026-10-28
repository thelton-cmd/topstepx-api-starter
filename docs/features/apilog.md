# API calls ("Show me the API call")

Every panel on the dashboard is built from TopstepX API calls: the chart from
`History/retrieveBars`, the account list from `Account/search`, the quotes, depth and tape from
the SignalR market hub, and your orders and fills from the user hub. The **API calls** panel shows
those calls as they happen, so any click on the page can become a lesson in how the API works.

This feature is for learning the API mechanics. It only observes: it never sends, changes or
cancels an order, and nothing it shows is trading advice.

## Opening it

- Click **API** in the header. The number next to it is how many REST calls the dashboard made in
  the last 60 seconds. Click again (or **Close** in the panel) to hide it.
- Or open **Settings** and tick **Show the API calls panel**. The page remembers your choice.
- Start the dashboard with only this feature: `python -m dashboard --features apilog`
  (Windows: `.venv\Scripts\python.exe -m dashboard --features apilog`).

## The REST tab

One row per request the dashboard sent to `https://api.topstepx.com/api`, newest on top:
time (UTC), path, HTTP status, how long it took in milliseconds, the fields that were sent, and a
short summary of what came back (for example `1,500 bars` or `errorCode 3: ...`).

- Type in the filter box to find a path, a field or a status code. **Errors only** keeps the
  failed calls, which are tinted red (an HTTP error, a network error, or a reply with
  `success: false`).
- Click a row to see the exact JSON request body and the JSON response, pretty-printed.
- **Pause** stops the list from moving while you read. **Clear** empties the lists.
- The line above the table counts this dashboard's own calls against the API's rate limits
  (200 requests per 60 seconds for most endpoints, 50 per 30 seconds for `History/retrieveBars`).
  Other programs using the same API key also count toward those limits, so treat it as an
  approximation. The header number turns amber when the dashboard alone gets close to a limit.

### Copy as curl / Copy as Python

Expand a row and use the buttons. The copied text also appears under the buttons, so you can
select it by hand if the browser blocks the clipboard.

- **Copy as Python** gives code that repeats the call with this kit. When the path has a
  `TopstepXClient` method (for example `/Contract/search` -> `client.search_contracts(...)`), the
  snippet calls that method with the same arguments, converting times to `datetime` and codes to
  the kit's enums (`BarUnit.MINUTE`, `OrderType.LIMIT`). Otherwise it uses the raw transport,
  `client._http.post(path, body)`. Save it as a `.py` file in this repository and run it with the
  virtual environment's Python; `TopstepXClient()` reads your credentials from `.env`.
- **Copy as curl** gives a `curl` command for bash, zsh or Git Bash. It sends the session token
  from the `TOPSTEPX_TOKEN` environment variable of your own terminal (a token comes from the
  `/Auth/loginKey` call; see the [ProjectX Gateway docs](https://gateway.docs.projectx.com/)). In
  Windows PowerShell, call `curl.exe` and adjust the quoting, or use Git Bash.

Never paste your API key or a session token into a chat, an issue or a screenshot. Snippets for
the order endpoints (`/Order/place`, `/Order/cancel`, `/Order/modify`, `/Position/closeContract`,
`/Position/partialCloseContract`) start with a warning line: running them sends a real order
request, so use them only on a Practice Account.

## The Hub tab

The dashboard keeps two SignalR connections open: the market hub (`MarketHub` in the kit) and the
user hub (`UserHub`).

- **Market events** (`GatewayQuote`, `GatewayTrade`, `GatewayDepth`) can arrive dozens of times a
  second, so they are summarised: a count, a rate per second over the last 10 seconds, when the
  last one arrived and the last message itself (click a row to see it).
- **User-hub events** (`GatewayUserAccount`, `GatewayUserOrder`, `GatewayUserPosition`,
  `GatewayUserTrade`) and every **subscribe/unsubscribe call** the dashboard sends (direction
  `out`, for example `SubscribeContractQuotes`) are rare, so each one is also listed in the table
  below, newest first. Switching the account or the focus contract shows the unsubscribe and
  subscribe calls it makes.
- The **Kit call** column names the kit method behind each message, for example
  `market.on_quote(callback)` or `await user.subscribe_orders(7)`.

Recording hub traffic costs a little work for every market message. Turn it off in Settings
(**Record hub traffic**) if you do not need it; REST calls are always recorded.

## What it redacts

Before anything is stored, every field named `apiKey`, `token`, `password` or `authorization`
(in any letter case, at any depth of the JSON, plus close variants such as `api_key`,
`newToken` and `access_token`) is replaced with `<redacted>`. Strings that look like a session
token (a JWT), `Bearer ...` values and `access_token=` query parameters are masked too. HTTP headers
are never recorded, so the `Authorization: Bearer` header the kit sends is never seen.

The dashboard's own login (`/Auth/loginKey`) goes through the kit's token manager, not through the
REST transport this panel watches, so it does not appear in the list; if it ever did, its
`apiKey` and `token` would show as `<redacted>`.

## What it keeps, and for how long

Everything lives in the dashboard process's memory and is gone when you stop it. Nothing is
written to disk.

- The newest 500 REST calls. A request or response body over about 20 KB is shortened before it
  is stored: long lists keep their first items and end with a note such as
  `... 1,300 more items not stored`, and the expanded row says how large the original was.
- Per hub event: the count, the rate, the last message (shortened to about 4 KB) and when it
  arrived. The newest 200 user-hub events and subscribe/unsubscribe calls.
- **Clear** empties the lists. The 60-second counter keeps counting, because the API's rate limit
  counts those calls too.

## Routes (for people extending the dashboard)

| Method | Path | Returns |
|---|---|---|
| GET | `/api/apilog/calls?after=<seq>&limit=<n>&bodies=0\|1` | the newest `limit` (max 500) calls with `seq > after`, oldest first, plus `last_seq`; `bodies=0` leaves out the request/response JSON |
| GET | `/api/apilog/call?seq=<n>` | one call with its request and response |
| GET | `/api/apilog/snippets?seq=<n>` | `{url, method, curl, python}` for one call |
| GET | `/api/apilog/hub?after=<seq>` | `events` (per hub/direction/name figures) and `recent` (listed messages with `seq > after`, oldest first) |
| POST | `/api/apilog/clear` | empties the lists |
| POST | `/api/apilog/capture` | `{"hub": true\|false}` turns hub recording on or off |

The header counters come from `GET /api/state` under `features.apilog`. POSTs need the page's
token like every other dashboard POST. The server side is `dashboard/features/apilog.py`; the page
side is `dashboard/static/features/apilog.js` and `apilog.css`.
