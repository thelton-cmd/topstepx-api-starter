# Record and replay

Record the live data stream to a file, then replay it later at any speed. Replay is a practice and
testing tool: it shows you what the dashboard (chart, ladder, tape, positions) did with data you
already saw, and it works with no network. It is not advice about anything.

## Record

```
.venv\Scripts\python.exe -m dashboard --record recordings\es-session.ndjson.gz
```

- Every message on both streams (quotes, depth, trades, and your account, order, position and fill
  events) is appended to the file, along with the messages this dashboard sent to subscribe.
- The lookups the page needs to start offline are saved too: accounts, contract search, available
  contracts, chart bars, and the starting positions, open orders and fills.
- A name ending in `.gz` is compressed. The file is flushed about once a second and when you stop
  the dashboard (Ctrl+C or `--seconds`), so a crash loses at most a second.
- `--record` refuses to overwrite an existing file. While recording, the header shows `REC <path>`.
- Tokens, API keys and passwords are never written. **The file does contain your account data**
  (balances, positions, fills). Keep it private. `charts/` and `journal/` are git-ignored but a new
  `recordings/` folder is not, so add it to `.gitignore` before you commit.

## Replay

```
.venv\Scripts\python.exe -m dashboard --replay recordings\es-session.ndjson.gz --speed 10 --replay-loop
```

- `--speed N` (above 0, default 1) multiplies the recorded pace. `--replay-loop` starts over at the
  end. `--record` and `--replay` cannot be combined.
- The dashboard starts on the recorded watch list, with the contract that had depth first. Its
  streams are the recorded ones; no login or network is needed. If a lookup is missing from the
  recording and your `.env` has credentials, contract and account lookups fall back to the live
  API; otherwise the page shows a note.
- Order entry is disabled in replay: the ticket is read-only and every order route answers 403
  ("Replay mode: order entry is disabled."). This holds even with `--features none`.
- A banner at the top shows `REPLAY <file> - <replayed clock> - order entry disabled` with Play and
  Pause, speed buttons (1x 2x 5x 10x 20x), a position slider (drag to seek), the position and
  length, and Step while paused.
- Seeking backwards starts again from the beginning and fast-forwards without pacing, so the
  ladder, tape, positions, orders and fills match that moment. The chart grows with the replayed
  clock, but only at the bar size the recording was made with (change bar size later and the page
  says no bars were recorded for it).
- Depth and trades exist only for the contract that was focused while recording; other contracts
  have quotes. Positions, orders and fills come from the recorded events and the recorded starting
  snapshot, never from your live account.

## Routes

| Method | Path | |
|---|---|---|
| GET | `/api/replay/status` | `{active, file, speed, playing, position_s, duration_s, clock, loop, subscriptions}`; `active: false` outside replay |
| POST | `/api/replay/control` | `{"action": "play" \| "pause" \| "speed" \| "seek" \| "step", "value": n}`; `seek` takes seconds or `"fraction": 0..1`; `step` only while paused; answers with the new status |

## File format

One JSON object per line: a `meta` line, then hub messages
`{"t", "wall", "hub": "market"|"user", "dir": "in"|"out", "name", "args"}` and REST answers
`{"t", "wall", "kind": "rest", "path", "request", "response"}`. `t` is seconds since the recording
started. A recording cut short (the process was killed) still loads up to the last complete line.
