---
description: Read each figure of narwhal dev watch and send concurrent requests with narwhal dev ask to see engines change roles.
---

# Reading the live view

`narwhal dev watch` redraws a view of a launched instance once a second, from the router's [`GET /narwhal/state`](../http-api/05-Live-State.md) and [`GET /metrics`](../telemetry/03-Metrics-and-Control.md). It runs until you press Ctrl+C.

```text
╭─────────────────────────────────────── narwhal dev watch ────────────────────────────────────────╮
│  ▗▖                                                                                              │
│   ▝▐▖▖      ▄▖                                                                                   │
│      ▀▐▌▄▟▛▀▀▀▜█▄        Narwhal                                                                 │
│        ▘▜█▄▖ █  ▀█▄▖     ERNIE-4.5-0.3B   http://127.0.0.1:18000   /home/you/runs/dev            │
│    ▗▄    ▗▖      ▐██▙     6 prefill  2 decode    2 slots per engine   figures cover the last 30… │
│  ▝███▌   ▟▌    ▗▖▟█▀▀▘                                                                           │
│     ▜▙▄▝▀▀   ▄▟██▛▘                                                                              │
│       ▀▀██████▛▀▘                                                                                │
│                                                                                                  │
│  engine   role      requests         state   role, last 50 s                                     │
│  ──────────────────────────────────────────────────────────────────────────────────────────────  │
│  n1       prefill   ━━━━━━━━━━━━ 0   live    ───██████████████████████████████┃████████████████  │
│  n2       prefill   ━━━━━━━━━━━━ 0   live    ───██████████┃████████████████████┃███████████████  │
│  n3       prefill   ━━━━━━━━━━━━ 0   live    ───███████████████████████████████████████████████  │
│  n4       prefill   ━━━━━━━━━━━━ 0   live    ───██████████████████████┃█████████┃██████████████  │
│  n5       prefill   ━━━━━━━━━━━━ 0   live    ───███████████████████████████████████┃███████████  │
│  n6       prefill   ━━━━━━━━━━━━ 0   live    ───█████████████████████████████████████████████┃█  │
│  n7       decode    ━━━━━━━━━━━━ 0   live    ───███████████████████████████████████████████████  │
│  n8       decode    ━━━━━━━━━━━━ 0   live    ───███████████████████████████████████████████████  │
│                                                                                                  │
│                                                                                                  │
│                             median   95th percentile   trend                                     │
│  ──────────────────────────────────────────────────────────────────────────────────────────────  │
│  time to first token          89ms             5.02s   ▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▇▇▇▇▇▇▇▇▇▇▇▇████▇▆▄▂▁▁▁▁▁  │
│  time per output token        73ms             148ms   ▃▃▄▄▃▄▄▄▄▄▄▄▄▄▄▄▃▃▃▂▂▂▁▁▁▂▂▁▁▁▃▄▅▅▇▇▇▇▇▇  │
│  requests per second           5.4                     ▅▅▅▅▆▅▅▅▄▄▄▄▄▄▄▄▄▄▄▄▄▄▃▃▃▂▂▁▁▁▂▂▂▃▆████▇  │
│  output tokens per second     73.1                     ▅▆▇▇█▇▆▆▅▆▇█▇▇██▇██▇▇▇██▇▇▆▆▆▅▅▄▄▄▄▄▄▃▂▁  │
│                                                                                                  │
│ served 241   failed 0   refused, rejected or expired 0                                           │
│ role switches 8   n6 → prefill 1 s ago   n5 → prefill 12 s ago   n4 → prefill 15 s ago   n2 → p… │
│ controller  held at 6P:2D instead of 7P:1D: projected decode pressure blocks consolidation       │
╰───────────────────────────────────────── Ctrl+C to exit ─────────────────────────────────────────╯
```

The frame above came from eight engines on a CPU-only machine, after long replies and then long prompts. In a terminal the view takes the whole window, colours prefill engines cyan and decode engines magenta, and highlights an engine in the frame where it switches role. The Narwhal logo shows while the whole frame fits the terminal's height. Ctrl+C gives the terminal back as it was. Piped to a file, `watch` writes each frame as plain lines with no terminal control codes.

## Fields

| Figure | Meaning |
| --- | --- |
| `6 prefill  2 decode` | Engines in each role now. |
| `2 slots per engine` | Requests each engine computes at once. More requests on an engine wait in its queue. |
| `role` | `prefill` engines compute prompts. `decode` engines generate replies from the KV cache a prefill engine hands over. |
| `requests` | Requests the router holds on the engine, queued or running. The bars share one scale, the slots per engine or the busiest engine's requests, whichever is larger. |
| `state` | `live`, or `draining`, `quarantined` or `ejected` when the router has taken the engine out of placement. |
| `role, last 50 s` | The engine's role in each frame, newest at the right, with a bar where it changed role. The column fills the terminal's width and draws a grey line until enough frames arrive, and its heading gives the seconds that width covers at the current `--interval`. |
| `time to first token` | Time from a request's arrival at the router to the end of its prefill. |
| `time per output token` | A request's mean time per output token after its prefill, including the wait for a decode engine. |
| `requests per second`, `output tokens per second` | Completed requests and their output tokens per second. Tokens are counted when a request completes, so one long reply arrives in a single frame. |
| `trend` | One mark per frame, newest at the right, from the lowest value shown to the highest. The row fills the terminal's width, up to 240 frames, and draws a grey baseline until enough frames arrive. |
| `served`, `failed` | Requests completed and requests an engine failed, since the router started. |
| `refused, rejected or expired` | Requests refused by admission, rejected for capacity or router readiness, or expired at a deadline or queue timeout. |
| `role switches` | Engines the router moved to the other role since it started, with the five most recent, newest first. A switch made while the view was open shows how long ago it happened. |
| `controller` | The role controller's last decision: `moved to` the split it applied, or its result (such as `held` or `blocked`) at the current split instead of the split it weighed, then its reason. |

The second line states the window the latency and throughput cover, which grows to 30 seconds. The latency rows are the median and the 95th percentile of the requests that completed in it, read from the router's histograms. A percentile inside a histogram bucket is interpolated, so a figure moves in steps when few requests completed.

`watch` reads the times the router records. `narwhal dev ask` reports the time from sending a request to the first chunk of reply text, which adds the KV handoff, the wait for a decode engine and the first decoded token to the router's figure. Its time per output token starts at that first chunk.

## Sending concurrent requests

`narwhal dev ask --repeat N` sends N prompts from a built-in set of twelve questions. `--concurrency` sets how many are in flight, and `--max-tokens` bounds each reply. The [`narwhal dev` reference](../cli/Dev.md#options) lists the options of `ask` and `watch`.

To load the decode engines, ask for long replies:

```bash
narwhal dev ask --repeat 2000 --concurrency 8 --max-tokens 200
```

With the installed templates, the router decides a role switch from the last 30 seconds of load and needs ten arrivals in that window. A short burst leaves the roles as they are.

After a switch, the engine counts at the top of the view change, the frame highlights the engine that switched, its role history shows a bar, and `role switches` goes up by one.
## Single-frame JSON output

`narwhal dev watch --once` takes two readings one `--interval` apart and prints that frame as JSON. Its `controller` field holds the last decision's `result`, the split it weighed (`prefill`, `decode`), the split at the time (`current_prefill`, `current_decode`) and its `reason`, or `null` before the first decision:

```bash
narwhal dev watch --once --interval 5
```

`--format json` implies `--once` and wraps the frame in the [versioned command result](../Command-Results.md).
