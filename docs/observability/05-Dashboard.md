---
description: Read the Narwhal Orchestrator dashboard panel by panel, from the SLO headline to token throughput.
---

# Read the dashboard

The **Narwhal Orchestrator** dashboard reads from top to bottom: the SLO headline row, the engine table beside fleet events, pool assignments beside engine role history, request outcomes, latency, pool pressure and retries, and token throughput. The **Router** selector picks the router whose metrics fill the panels, and **Engine detail** filters the engine table and the role history.

## Headline row

<div class="narwhal-panel-row stats" markdown>

![Goodput block with 100.0% within SLO and 2.94K requests.](../assets/observability/headline-goodput.png)

![Load block with 0.0% unserved and 2.87K offered.](../assets/observability/headline-load.png)

![TTFT p95 block at 15% of the SLO, 306 ms.](../assets/observability/headline-ttft-p95.png)

![TPOT p95 block at 34% of the SLO, 15.4 ms.](../assets/observability/headline-tpot-p95.png)

![Engines block with no engines out of service and 4 flips.](../assets/observability/headline-engines.png)

![Router block with admission Ready and no firing alerts.](../assets/observability/headline-router.png)

</div>

Each block pairs a status value with a context value. The status value turns green, amber or red against its target; the context value stays neutral. Goodput, Load, TTFT p95 and TPOT p95 cover the displayed interval, and the charts below show how each changes within it. Engines and Router show the current state, and **Flips** counts role flips in the displayed interval.

| Block | Status value | Green | Amber | Red | Context value |
| --- | --- | :---: | :---: | :---: | --- |
| Goodput | Share of requests that ended within both the TTFT and TPOT SLOs | 95% or more | 90% to 95% | below 90% | Number of those requests |
| Load | Share of requests that ended refused or rejected at admission or by failure or deadline expiry | below 1% | 1% to 5% | 5% or more | Number of offered requests |
| TTFT p95 | 95th-percentile time to first token as a share of its SLO | below 80% | 80% to 100% | 100% or more | The p95 value |
| TPOT p95 | 95th-percentile time per output token as a share of its SLO | below 80% | 80% to 100% | 100% or more | The p95 value |
| Engines | Engines out of service | 0 | | 1 or more | Role flips |
| Router | Admission readiness | Ready | | Not ready or Offline | Firing Narwhal alerts, red at 1 or more |

Goodput counts refused, rejected, failed, expired and cancelled requests as misses and counts each request when it ends.

## Engines and fleet events

<div class="narwhal-panel-row wide-first" markdown>

![Engines table with eight Serving engines, three on prefill and five on decode, with resident and running bars on the decode engines and low KV cache use.](../assets/observability/engines.png)

![Fleet events timeline with one NarwhalUnservedRising warning near the end of a role-flip run.](../assets/observability/fleet-events.png)

</div>

The engine table lists one row per engine, with the most serious state first and then by role. **Fleet events** sits to its right and shows the Narwhal alerts that fired in the displayed interval, one row per alert and engine: red while a page alert fires and amber while a warning fires.

| Column | Shows |
| --- | --- |
| Role | Current pool assignment: Prefill, Decode or Colocated |
| State | The most serious condition of the engine, from the table below |
| Resident | Narwhal requests currently on the engine, as a bar scaled to the busiest engine |
| vLLM running | Requests vLLM reports as running, as a bar scaled to the busiest engine |
| KV cache | vLLM KV cache use, amber from 80% and red from 95% |
| Prefix hits | Share of prompt tokens served from the engine's prefix cache |
| Tokens/s | Output tokens per second on decode and colocated engines, and prompt tokens per second the engine prefilled on prefill engines |

| State | Meaning | Takes new work |
| --- | --- | :---: |
| Serving | Normal operation | Yes |
| Switching | The engine still finishes requests from its previous role after a flip | Yes |
| Backlogged | vLLM holds waiting requests because its batch or KV cache is full | Yes |
| Probation | Latency drift evidence penalises the engine in placement | Yes |
| Verifying | A breaker verification probe is in flight | Yes |
| Draining | An operator drain or recovery validation holds the engine out of placement | No |
| Ejected | The breaker removed the engine from placement | No |
| Validating | Readmission checks run against the engine | No |
| Blocked | A readmission or automatic recovery check failed and the engine waits for an operator | No |
| Unreachable | The engine's metrics endpoint fails to answer Prometheus | No |
| Restarting | The engine is unreachable while a drain holds it, for example during a wave restart | No |

The **Engines** block in the headline row counts engines in the last six states as out of service, and the **Router** block counts the alerts firing now.

## Pool assignments and engine role history

<div class="narwhal-panel-row" markdown>

![Pool assignments swinging between seven prefill engines and seven decode engines as the workload changes.](../assets/observability/pool-assignments.png)

![Engine role history in which each of eight engines alternates between Prefill and Decode across the run.](../assets/observability/engine-role-history.png)

</div>

**Pool assignments** plots engines per pool and ejected engines over time. **Engine role history** sits to its right.

In **Engine role history**, each row is one engine over the displayed interval, coloured by its role. A colour change marks a role flip. Draining, Ejected, Validating, Blocked, Unreachable and Restarting periods replace the role for their duration, in the same colours as the **State** column.

## Request outcomes

![Request outcomes during a role-flip run: completed follows offered except at each workload swing, where predictive refusals rise until role control moves engines to the busier pool.](../assets/observability/request-outcomes.png)

The **offered** line shows arrivals per second. Each terminal outcome (completed, failed, refused, rejected, expired, cancelled and invalid) has its own line drawn from zero. The gap between **offered** and **completed** is the unserved load. A marker on the time axis shows when a Narwhal alert started firing: amber for a warning, red for a page. Hover a marker for the alert and engine.

## Latency

<div class="narwhal-panel-row" markdown>

![Time to first token with p95 reaching and p99 crossing the SLO line during two prefill-heavy swings.](../assets/observability/time-to-first-token.png)

![Time per output token with p50, p95 and p99 below the SLO line for the whole run.](../assets/observability/time-per-output-token.png)

![Request waiting time with seat time p95 near 20 seconds in the decode-heavy phases and queue wait near zero.](../assets/observability/request-waiting-time.png)

</div>

**Time to first token** and **Time per output token** plot p50, p95 and p99 for the selected router against its SLO line. **Request waiting time** plots the p95 time a request waits for admission and dispatch, and the p95 time it holds an admission seat.

## Pool pressure and retries

<div class="narwhal-panel-row" markdown>

![Pool pressure with prefill reaching 0.95 of its target during a prefill-heavy swing.](../assets/observability/pool-pressure.png)

![Retries and early exits at zero for the whole run.](../assets/observability/retries.png)

</div>

**Pool pressure** plots each pool's load as a fraction of its latency target; values near 1 mean the pool is at its SLO limit. **Retries and early exits** plots retry attempts per second and requests per second that ended before input sizing.

## Token throughput

![Token throughput with prefilled prompt tokens near 115K per second in the prefill-heavy phases and output tokens rising in the decode-heavy phases.](../assets/observability/token-throughput.png)

**Token throughput** plots prompt tokens per second that engines prefilled and router-observed output tokens per second. Each prompt token counts once, on the engine that prefilled it.

[![Next: GPU telemetry, alerts, and recovery](https://img.shields.io/badge/next-GPU%20telemetry%2C%20alerts%2C%20and%20recovery-0f766e)](03-Telemetry-and-Recovery.md)
