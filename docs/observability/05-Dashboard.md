---
description: Read the Narwhal Orchestrator dashboard panel by panel, from the SLO headline to token throughput.
---

# Read the dashboard

Open the **Narwhal Orchestrator** dashboard through the tunnel in [Access the dashboard from a workstation](02-Access.md#access-the-dashboard-from-a-workstation).

## Selectors

| Selector | Filters | Default |
| --- | --- | --- |
| Router | Panels built from router metrics | All |
| Engine detail | The **Engines** headline block, the **Engines** table and **Engine role history** | All |

## Headline row

<div class="narwhal-panel-row stats" markdown>

[![Goodput block with 100.0% within SLO and 2.94K requests.](../assets/observability/headline-goodput.png)](../assets/observability/headline-goodput.png)

[![Load block with 0.0% unserved and 2.87K offered.](../assets/observability/headline-load.png)](../assets/observability/headline-load.png)

[![TTFT p95 block at 15% of the SLO, 306 ms.](../assets/observability/headline-ttft-p95.png)](../assets/observability/headline-ttft-p95.png)

[![TPOT p95 block at 34% of the SLO, 15.4 ms.](../assets/observability/headline-tpot-p95.png)](../assets/observability/headline-tpot-p95.png)

[![Engines block with 0 engines out of service and 4 flips.](../assets/observability/headline-engines.png)](../assets/observability/headline-engines.png)

[![Router block with admission Ready and 0 firing alerts.](../assets/observability/headline-router.png)](../assets/observability/headline-router.png)

</div>

| Block | Status value | Context value |
| --- | --- | --- |
| Goodput | **Within SLO**: requests completed within both the TTFT and TPOT SLOs, as a share of the completed, failed, refused, rejected, expired and cancelled requests that ended in the displayed interval | **Requests**: number of requests completed within both SLOs |
| Load | **Unserved**: refused, rejected, failed and expired requests, as a share of the same total | **Offered**: requests offered in the displayed interval |
| TTFT p95 | **Of SLO**: 95th-percentile time to first token over the displayed interval, as a share of the TTFT SLO | **p95**: the 95th-percentile time to first token |
| TPOT p95 | **Of SLO**: 95th-percentile time per output token over the displayed interval, as a share of the TPOT SLO | **p95**: the 95th-percentile time per output token |
| Engines | **Out of service**: engines currently in an [out-of-service state](#engine-states) | **Flips**: role flips in the displayed interval |
| Router | **Admission**: current router admission readiness | **Alerts**: Narwhal alerts currently firing |

The status values and **Alerts** take these colours:

| Value | Green | Amber | Red |
| --- | --- | --- | --- |
| Within SLO | 95% or more | 90% to 95% | below 90% |
| Unserved | below 1% | 1% to 5% | 5% or more |
| Of SLO | below 80% | 80% to 100% | 100% or more |
| Out of service | 0 | | 1 or more |
| Admission | Ready | | Not ready or Offline |
| Alerts | 0 | | 1 or more |

## Engines and fleet events

<div class="narwhal-panel-row wide-first" markdown>

[![Engines table with eight Serving engines, three on prefill and five on decode, with resident and running bars on the decode engines and low KV cache use.](../assets/observability/engines.png)](../assets/observability/engines.png)

[![Fleet events timeline with one NarwhalUnservedRising warning near the end of a role-flip run.](../assets/observability/fleet-events.png)](../assets/observability/fleet-events.png)

</div>

**Engines** lists one row per engine, most serious state first, then by role.

| Column | Shows |
| --- | --- |
| Engine | Engine ID (`iid`) |
| Role | Current pool assignment: Prefill, Decode or Colocated |
| State | The most serious [engine state](#engine-states) |
| Resident | Narwhal requests currently on the engine, as a bar scaled to the busiest engine |
| vLLM running | Requests vLLM reports as running, as a bar scaled to the busiest engine |
| KV cache | vLLM KV cache use, amber from 80% and red from 95% |
| Prefix hits | Share of prompt tokens served from the engine's prefix cache |
| Tokens/s | Output tokens per second on decode and colocated engines, and prompt tokens per second the engine prefilled on prefill engines |

**Fleet events** has one row per Narwhal alert and engine, with a bar in the severity colour while the alert fires:

| Severity | Bar colour |
| --- | --- |
| Page | Red |
| Warning | Amber |

## Pool assignments and engine role history

<div class="narwhal-panel-row" markdown>

[![Pool assignments swinging between seven prefill engines and seven decode engines as the workload changes.](../assets/observability/pool-assignments.png)](../assets/observability/pool-assignments.png)

[![Engine role history in which each of eight engines alternates between Prefill and Decode across the run.](../assets/observability/engine-role-history.png)](../assets/observability/engine-role-history.png)

</div>

**Pool assignments** plots engines per pool and ejected engines over time:

| Line | Shows |
| --- | --- |
| Teal | Prefill engines |
| Blue | Decode engines |
| Red | Ejected engines |

**Engine role history** shows each engine as one row, coloured by role or, while the engine is out of service, by its [state](#engine-states).

A change between Prefill and Decode marks a role flip.

## Request outcomes

[![Request outcomes during a role-flip run: completed follows offered except at each workload swing, where predictive refusals rise until role control moves engines to the busier pool.](../assets/observability/request-outcomes.png)](../assets/observability/request-outcomes.png)

**Request outcomes** plots each series as its own line from zero:

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

Severity-coloured markers on the time axis mark each Narwhal alert as it starts firing.

Hover a marker for the alert and engine.

## Latency

<div class="narwhal-panel-row" markdown>

[![Time to first token with p95 reaching and p99 crossing the SLO line during two prefill-heavy swings.](../assets/observability/time-to-first-token.png)](../assets/observability/time-to-first-token.png)

[![Time per output token with p50, p95 and p99 below the SLO line for the whole run.](../assets/observability/time-per-output-token.png)](../assets/observability/time-per-output-token.png)

[![Request waiting time with seat time p95 near 20 seconds in the decode-heavy phases and queue wait near zero.](../assets/observability/request-waiting-time.png)](../assets/observability/request-waiting-time.png)

</div>

| Panel | Plots |
| --- | --- |
| Time to first token | **p50**, **p95** and **p99** of the time from router arrival to prefill completion, against the selected router's **SLO** line |
| Time per output token | **p50**, **p95** and **p99** of the mean decode interval per completed request, against the selected router's **SLO** line |
| Request waiting time | **queue wait p95**, the time a request waits for admission and dispatch, and **seat time p95**, the time it holds an admission seat |

## Pool pressure and retries

<div class="narwhal-panel-row" markdown>

[![Pool pressure with prefill reaching 0.95 of its target during a prefill-heavy swing.](../assets/observability/pool-pressure.png)](../assets/observability/pool-pressure.png)

[![Retries and early exits at zero for the whole run.](../assets/observability/retries.png)](../assets/observability/retries.png)

</div>

| Panel | Plots |
| --- | --- |
| Pool pressure | **prefill** and **decode** load as a fraction of each pool's SLO target, with a line at 1 for the target |
| Retries and early exits | **retry attempts** per second, and **ended before sizing**: requests per second that ended before input sizing |

## Token throughput

[![Token throughput with prefilled prompt tokens near 115K per second in the prefill-heavy phases and output tokens rising in the decode-heavy phases.](../assets/observability/token-throughput.png)](../assets/observability/token-throughput.png)

| Series | Plots |
| --- | --- |
| Prefill/s | Engine prompt tokens per second from vLLM's `local_compute` and `local_cache_hit` sources when an engine reports them, otherwise from `vllm:prompt_tokens_total` |
| Decode/s | Output tokens per second the router observed |

## Engine states

The **State** column shows these states, listed from least to most serious:

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
