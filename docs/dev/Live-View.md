---
description: Read each figure of narwhal dev watch and send concurrent requests with narwhal dev ask to see engines change roles.
---

# Reading the live view

`narwhal dev watch` redraws a view of a launched instance once a second, from the router's [`GET /narwhal/state`](../http-api/05-Live-State.md) and [`GET /metrics`](../telemetry/03-Metrics-and-Control.md). It runs until you press Ctrl+C.

```text
╭─────────────────────────────────────── narwhal dev watch ────────────────────────────────────────╮
│ /home/you/runs/dev      http://127.0.0.1:18000   ERNIE-4.5-0.3B                         │
│ 4 prefill  4 decode   2 slots per engine   figures cover the last 25 s                           │
│                                                                                                  │
│  engine   role      requests             state                                                   │
│  ─────────────────────────────────────────────                                                   │
│  n1       prefill   ━━━━━━━━━━━━━━━━ 0   live                                                    │
│  n2       prefill   ━━━━━━━━━━━━━━━━ 0   live                                                    │
│  n3       prefill   ━━━━━━━━━━━━━━━━ 0   live                                                    │
│  n4       prefill   ━━━━━━━━━━━━━━━━ 0   live                                                    │
│  n5       decode    ━━━━━━━━━━━━╸━━━ 4   live                                                    │
│  n6       decode    ━━━━━━━━━╸━━━━━━ 3   live                                                    │
│  n7       decode    ━━━━━━━━━━━━╸━━━ 4   live                                                    │
│  n8       decode    ━━━━━━━━━━━━━━━━ 5   live                                                    │
│                                                                                                  │
│                                                                                                  │
│                              median   95th percentile   trend                                    │
│  ────────────────────────────────────────────────────────────────────────────────                │
│  time to first token           62ms             119ms    ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄                │
│  time per output token         16ms             160ms    ▁▃▄▅▆▇████▇▇▇▇▇▇▇▇▇▇████                │
│  requests per second           29.9                      ▃▃▁▃▄▃▄▄▄▅▅▆▆▆▇▇▇███████                │
│  output tokens per second   1,204.5                      ▁▆▄▆▆▆▆▇▇▇▇▇▇███████████                │
│                                                                                                  │
│ served 735   failed 0   refused, rejected or expired 0                                           │
│ role switches 0                                                                                  │
╰───────────────────────────────────────── Ctrl+C to exit ─────────────────────────────────────────╯
```

In a terminal the view takes the whole window, colours prefill engines cyan and decode engines magenta, and highlights an engine in the frame where it switches role. Ctrl+C gives the terminal back as it was. Piped to a file, `watch` writes each frame as plain lines with no terminal control codes.

## Fields

| Figure | Meaning |
| --- | --- |
| `4 prefill  4 decode` | Engines in each role now. |
| `2 slots per engine` | Requests each engine computes at once. More requests on an engine wait in its queue. |
| `role` | `prefill` engines compute prompts. `decode` engines generate replies from the KV cache a prefill engine hands over. |
| `requests` | Requests the router holds on the engine, queued or running. The bars share one scale, the slots per engine or the busiest engine's requests, whichever is larger. |
| `state` | `live`, or `draining`, `quarantined` or `ejected` when the router has taken the engine out of placement. |
| `time to first token` | Time from a request's arrival at the router to the end of its prefill. |
| `time per output token` | A request's mean time per output token after its prefill, including the wait for a decode engine. |
| `requests per second`, `output tokens per second` | Completed requests and their output tokens per second. Tokens are counted when a request completes, so one long reply arrives in a single frame. |
| `trend` | One mark per frame for the last 60 frames, from the lowest value shown to the highest. |
| `served`, `failed` | Requests completed and requests an engine failed, since the router started. |
| `refused, rejected or expired` | Requests refused by admission, rejected for capacity or router readiness, or expired at a deadline or queue timeout. |
| `role switches` | Engines the router moved to the other role since it started, with the five most recent. |

The second line states the window the latency and throughput cover, which grows to 30 seconds. The latency rows are the median and the 95th percentile of the requests that completed in it, read from the router's histograms. A percentile inside a histogram bucket is interpolated, so a figure moves in steps when few requests completed.

`watch` reads the times the router records. `narwhal dev ask` reports the time from sending a request to the first chunk of reply text, which adds the KV handoff, the wait for a decode engine and the first decoded token to the router's figure. Its time per output token starts at that first chunk.

## Sending concurrent requests

`narwhal dev ask --repeat N` sends N prompts from a built-in set of twelve questions. `--concurrency` sets how many are in flight, and `--max-tokens` bounds each reply.

To load the decode engines, ask for long replies:

```bash
narwhal dev ask --repeat 2000 --concurrency 8 --max-tokens 200
```

With the installed templates, the router decides a role switch from the last 30 seconds of load and needs ten arrivals in that window. A short burst leaves the roles as they are.

After a switch, the engine counts at the top of the view change, the frame highlights the engine that switched, and `role switches` goes up by one.
## Single-frame JSON output

`narwhal dev watch --once` takes two readings one `--interval` apart and prints that frame as JSON:

```bash
narwhal dev watch --once --interval 5
```

`--format json` implies `--once` and wraps the frame in the [versioned command result](../Command-Results.md).
