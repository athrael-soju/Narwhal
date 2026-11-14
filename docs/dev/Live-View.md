---
description: Read each figure of narwhal dev watch and send concurrent requests with narwhal dev ask to see engines change roles.
---

# Reading the live view

`narwhal dev watch` redraws a view of a launched instance once a second, from the router's [`GET /narwhal/state`](../http-api/05-Live-State.md) and [`GET /metrics`](../telemetry/03-Metrics-and-Control.md). It runs until you press Ctrl+C.

```text
narwhal dev watch  http://127.0.0.1:18000
1 prefill, 3 decode   figures cover the last 4 s

engine  role     requests  state
n1      decode          3  live
n2      prefill         0  live
n3      decode          2  live
n4      decode          3  live

                       median    95th percentile
time to first token       77ms      217ms
time per output token     24ms      190ms

throughput  3.9 requests/s   255.1 output tokens/s
served 125   failed 0   refused, rejected or expired 0
role switches 1   n1 to decode
```

## Fields

| Figure | Meaning |
| --- | --- |
| `1 prefill, 3 decode` | Engines in each role now. |
| `role` | `prefill` engines compute prompts. `decode` engines generate replies from the KV cache a prefill engine hands over. |
| `requests` | Requests the router holds on the engine, queued or running. |
| `state` | `live`, or `draining`, `quarantined` or `ejected` when the router has taken the engine out of placement. |
| `time to first token` | Time from a request's arrival at the router to the end of its prefill. |
| `time per output token` | A request's mean time per output token after its prefill, including the wait for a decode engine. |
| `throughput` | Completed requests and their output tokens per second. Tokens are counted when a request completes, so one long reply arrives in a single frame. |
| `served`, `failed` | Requests completed and requests an engine failed, since the router started. |
| `refused, rejected or expired` | Requests refused by admission, rejected for capacity or router readiness, or expired at a deadline or queue timeout. |
| `role switches` | Engines the router moved to the other role since it started, with the five most recent. |

The second line states the window the latency and throughput cover, which grows to 30 seconds. The latency rows are the median and the 95th percentile of the requests that completed in it, read from the router's histograms. A percentile inside a histogram bucket is interpolated, so a figure moves in steps when few requests completed.

`watch` reads the times the router records. `narwhal dev ask` reports the time from sending a request to the first chunk of reply text, which adds the KV handoff, the wait for a decode engine and the first decoded token to the router's figure. Its time per output token starts at that first chunk.

## Sending concurrent requests

`narwhal dev ask --repeat N` sends N prompts from a built-in set of twelve questions. `--concurrency` sets how many are in flight, and `--max-tokens` bounds each reply.

To load the decode engines, ask for long replies:

```bash
narwhal dev ask --repeat 600 --concurrency 8 --max-tokens 200
```

On the measured host, a four-engine fleet with either installed model filled its decode engines, and the router moved one prefill engine to the decode role after 15 to 25 seconds. The view then shows `1 prefill, 3 decode` and the switch in its last line.

With the installed templates, the router decides a switch from the load of the last 30 seconds and needs ten arrivals in that window, so a short burst leaves the roles unchanged.

## Single-frame JSON output

`narwhal dev watch --once` takes two readings one `--interval` apart and prints that frame as JSON:

```bash
narwhal dev watch --once --interval 5
```

With `--format json` the frame arrives inside the [versioned command result](../Command-Results.md).

Output sent to a file or a pipe carries one frame after another with no terminal control codes.
