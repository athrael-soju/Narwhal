---
glightbox: true
description: Read the Narwhal Orchestrator dashboard from the service-level headline down to individual engines.
---

# Read the dashboard

The **Narwhal Orchestrator** dashboard answers three questions in order: whether clients receive the promised service level, how the fleet's engines and pools carry the load, and where latency or failures come from. Open it through the tunnel in [Access the dashboard from a workstation](02-Access.md#access-the-dashboard-from-a-workstation).

## Selectors

The **Router** selector scopes every panel built from router metrics. It defaults to All, and the shipped scrape configuration holds one router per data source, so the dashboard shows that router as soon as it opens.

The **Engine detail** selector narrows the **Engines** headline block, the engine table and **Engine role history** to the chosen engines. It also defaults to All.

## Headline row

The headline row summarises the displayed interval. Each block pairs a status value, coloured against its target, with a context value that gives its scale.

<div class="narwhal-panel-row stats" markdown>

![Goodput block with 99.6% within SLO and 37.59K requests.](../assets/observability/headline-goodput.png)

![Load block with 0.4% unserved and 37.79K offered.](../assets/observability/headline-load.png)

![TTFT p95 block at 15% of the SLO, 313 ms.](../assets/observability/headline-ttft-p95.png)

![TPOT p95 block at 34% of the SLO, 15.4 ms.](../assets/observability/headline-tpot-p95.png)

![Engines block with 0 engines out of service and 0 flips.](../assets/observability/headline-engines.png)

![Router block with admission Ready and 0 firing alerts.](../assets/observability/headline-router.png)

</div>

**Goodput** reports the share of ended requests that completed within both the TTFT and TPOT SLOs. Every request that ended counts in the denominator: completed, failed, refused, rejected, expired and cancelled. The **Requests** value beside it counts the requests that met both SLOs.

**Load** reports the **Unserved** share: refused, rejected, failed and expired requests over the same total. **Offered** counts the requests clients sent in the interval.

**TTFT p95** and **TPOT p95** show the 95th-percentile time to first token and time per output token over the interval, as a share of the SLO and in seconds.

**Engines** counts engines in an [out-of-service state](#engine-states) and the role flips in the interval. **Router** shows admission readiness and the Narwhal alerts firing now.

The status values take these colours:

| Value | Green | Amber | Red |
| --- | --- | --- | --- |
| Within SLO | 95% or more | 90% to 95% | below 90% |
| Unserved | below 1% | 1% to 5% | 5% or more |
| Of SLO | below 80% | 80% to 100% | 100% or more |
| Out of service | 0 | | 1 or more |
| Admission | Ready | | Not ready or Offline |
| Alerts | 0 | | 1 or more |

## Engines and engine role history

The engine table shows each engine's current role, state and load. **Engine role history** beside it shows how those roles changed over the interval, one row per engine in the same engine ID order.

<div class="narwhal-panel-row" markdown>

![Engines table with eight Serving engines in ID order, three on prefill and five on decode, with resident and running bars on the decode engines and KV cache use below 7%.](../assets/observability/engines.png)

![Engine role history with steady prefill and decode roles, and two unreachable periods on n8.](../assets/observability/engine-role-history.png)

</div>

Read the table from left to right. **Role** and **State** say what the engine does now and whether it serves placement. The bars compare engines with each other: **Resident** and **vLLM running** scale to the busiest engine, so a decode engine with a short bar holds less work than its peers.

| Column | Shows |
| --- | --- |
| Engine | Engine ID (`iid`) |
| Role | Current pool assignment: Prefill, Decode or Colocated |
| State | The most serious [engine state](#engine-states) |
| Resident | Narwhal requests currently on the engine |
| vLLM running | Requests vLLM reports as running |
| KV cache | vLLM KV cache use, amber from 80% and red from 95% |
| Prefix hits | Share of prompt tokens served from the engine's prefix cache |
| Tokens/s | Output tokens per second on decode and colocated engines, and prompt tokens per second the engine prefilled on prefill engines |

In **Engine role history**, teal marks Prefill and blue marks Decode. A change between them marks a role flip. While an engine is out of service, its row takes the colour of its [state](#engine-states) from the table.

## Request outcomes and fleet events

**Request outcomes** shows what happened to client requests, and **Fleet events** shows which alerts fired at the same time.

<div class="narwhal-panel-row" markdown>

![Request outcomes near 30 requests per second, with completed following offered and page markers where n8 dropped out.](../assets/observability/request-outcomes.png)

![Fleet events timeline with NarwhalEngineDown and NarwhalEngineEjected pages for n8.](../assets/observability/fleet-events.png)

</div>

In normal operation, **completed** tracks **offered** and the other series stay near zero. A gap between the two lines points to the series that rises in it:

| Series | Requests per second |
| --- | --- |
| offered | Original completion requests received, including early refusals |
| completed | Requests completed successfully |
| failed | Requests that ended in an error |
| refused (predictive) | Requests predictive admission refused above the TTFT budget |
| rejected (capacity) | Requests rejected by authentication or a concurrency limit |
| expired | Requests terminated by their admission or total deadline |
| cancelled | Requests their client abandoned before completion |
| invalid | Malformed or unsupported client requests rejected before dispatch |

Dashed markers on **Request outcomes** mark each Narwhal alert as it starts firing, red for pages and amber for warnings. Hover a marker for the alert and engine.

**Fleet events** gives each Narwhal alert and engine its own row, with a red bar while a page fires and an amber bar while a warning fires.

## Pool assignments

**Pool assignments** shows how many engines serve each phase over time.

<div class="narwhal-panel-row" markdown>

![Pool assignments during a role-control run: the prefill pool grows from three engines to seven, drops to one, then returns to seven as decode mirrors it.](../assets/observability/pool-assignments.png)

</div>

The teal line counts prefill engines and the blue line counts decode engines. A step in both lines marks a role flip. The red line counts engines the breaker ejected.

## Latency and token throughput

These panels show how long requests take and how much work the engines complete.

<div class="narwhal-panel-row" markdown>

![Time to first token with p50, p95 and p99 below the SLO line until a load spike at the end of the window.](../assets/observability/time-to-first-token.png)

![Time per output token with p50, p95 and p99 below the SLO line for the whole window.](../assets/observability/time-per-output-token.png)

![Token throughput with prefilled prompt tokens near 17K per second and output tokens near 8K per second.](../assets/observability/token-throughput.png)

</div>

**Time to first token** plots the p50, p95 and p99 time from router arrival to prefill completion. **Time per output token** plots the same percentiles of the mean decode interval per completed request. Both draw the selected router's SLO as a line. When p95 rises above it, more than 5% of requests in that window missed the SLO.

**Token throughput** plots **Prefill/s**, the prompt tokens engines prefilled per second, and **Decode/s**, the output tokens per second the router observed. Prefill/s uses vLLM's `local_compute` and `local_cache_hit` prompt token sources when an engine reports them, otherwise `vllm:prompt_tokens_total`.

## Pool pressure, request waiting time and retries

These panels show where pressure builds before it reaches clients.

<div class="narwhal-panel-row" markdown>

![Pool pressure with decode load crossing its target in short spikes and prefill load near zero.](../assets/observability/pool-pressure.png)

![Request waiting time with seat time p95 near 6 seconds and queue wait near zero.](../assets/observability/request-waiting-time.png)

![Retries and early exits at zero until requests begin ending before sizing at the end of the window.](../assets/observability/retries.png)

</div>

**Pool pressure** plots prefill and decode load as a fraction of each pool's SLO target. The line at 1 marks the target. Reactive expansion starts when a pool's load reaches `controller.thresholds.expand`, which defaults to 1.0.

**Request waiting time** separates two waits. **Queue wait p95** is the time a request waits for admission and dispatch. **Seat time p95** is the time it holds an admission seat.

**Retries and early exits** plots retry attempts per second, and requests per second that ended before input sizing.

## Engine states

The **State** column in the engine table shows the most serious state that applies, listed here from least to most serious:

| State | Meaning | Out of service |
| --- | --- | --- |
| Serving | Normal operation | No |
| Switching | The engine finishes requests from its previous role after a flip | No |
| Backlogged | vLLM reports waiting requests | No |
| Probation | Latency drift evidence penalises the engine in placement | No |
| Verifying | A breaker verification probe is in flight | No |
| Draining | An operator drain holds the engine out of placement | Yes |
| Ejected | The breaker removed the engine from placement | Yes |
| Validating | Readmission checks run against the engine | Yes |
| Blocked | The engine waits for an operator after a failed readmission or recovery check, or during a whole-wave restart hold | Yes |
| Unreachable | The engine's metrics endpoint fails to answer Prometheus | Yes |
| Restarting | The engine is unreachable while a drain holds it | Yes |

[![Next: GPU telemetry, alerts, and recovery](https://img.shields.io/badge/next-GPU%20telemetry%2C%20alerts%2C%20and%20recovery-0f766e)](03-Telemetry-and-Recovery.md)
