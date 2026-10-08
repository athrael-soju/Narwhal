---
description: Engine monitoring, failure handling, readmission and durable control-plane state in Narwhal.
---

# Failure, readmission, and state

## Monitoring and readiness

Each engine monitoring pass runs these stages:

| Stage | Work |
| --- | --- |
| `controller` | Role-controller logic |
| `health` | Health checks |
| `drains` | Drain settlement |
| `rollover` | Interval rollover |
| `readmission` | Readmission and [peer release](#peer-memory-release) rounds |
| `liveness` | Liveness probes |
| `residency` | Prefix residency refresh from attestation sidecars |
| `handoff` | State handoff persistence |
| `telemetry` | Floor-state refresh and loop logging |

Each pass with a failed stage extends the monitor-failure streak.

When the streak reaches `controller.monitor_failure_limit`, the router enters a degraded state. Admission of new requests stops, and `/ready` returns HTTP 503 with the reason `monitoring degraded: <stage> <class>`. Admitted requests and engine monitoring passes continue. A standby router counts the readiness failure toward its takeover threshold.

A successful pass clears the streak and reopens admission. A router restart resets the monitor-failure counters.

## Engine failure handling

### Connection pools

The router reaches engines through two kinds of connection pool:

| Pool | Scope | Carries | Bound | Client |
| --- | --- | --- | --- | --- |
| Data | One pool per engine | Token counting, prefill and decode | `serving.max_connections` open connections to each engine, and up to half that number kept alive | HTTP/1.1 on event-loop transports with the `httptools` parser |
| Control | One pool shared by all engines | Health probes and inference probes | `engine.control_connections` open connections across all engines, and up to half that number kept alive | HTTPX |

A data leg waits for a connection to its own engine only, so a busy engine does not hold connections that another engine's legs need. Health and inference probes for every engine share the control pool.

Both pools reuse a kept-alive connection for 5 s after its last response. A connection the engine has closed leaves the pool before reuse.

### Failure evidence

When an engine's failure streak for one class reaches `recovery.eject_after`, the action depends on the class:

| Failure | Class | Action |
| --- | --- | --- |
| Connection error or connect timeout | `connection` | Eject the engine |
| Transport timeout | `timeout` | Run a health probe |
| First-token deadline while the engine emits other output | `overload` | Run a health probe |
| First-token deadline from a silent engine, mid-stream silence, or invalid stream termination | `stream` | Run an inference probe under a placement hold |
| HTTP 408 or 429 | `overload` | Run a health probe |
| Other HTTP 5xx response | `inference_status` | Run an inference probe under a placement hold |
| Unreadable KV handoff from prefill | `kv_handoff` | Run an inference probe under a placement hold |

The role-coverage rule counts an engine as covered when every role it places stays placeable through other live engines. A pinned engine is covered when another live engine holds its role or is unpinned. An unpinned engine is covered when prefill and decode each have another live engine that holds that role or is unpinned.

The profile-match rule requires loaded profiles that match the live process generation before a recovery probe clears evidence and holds.

The inference probe runs a prefill leg and a decode leg. On an inconclusive leg, engine monitoring keeps the hold and schedules another probe. When a leg fails, Narwhal ejects a covered engine, and an uncovered engine stays in placement. A successful probe clears recorded inference failures and the hold, under the profile-match rule.

### Liveness

After `recovery.liveness_misses` consecutive liveness probe failures, Narwhal ejects the engine. A successful liveness probe resets the engine's miss count. On a quarantined engine that meets the profile-match rule, a successful probe also lifts quarantine and clears failure evidence.

### Peer memory release

Engines that share a host and exchange KV through CUDA IPC map each other's KV memory. A stopped engine's GPU memory stays allocated while a host peer holds that mapping.

On each consume request, the vLLM NIXL connector removes the NIXL agent of each producer idle for longer than `engine_ttl`. The engine launcher sets `engine_ttl` to 60 seconds for engines with CUDA IPC peers when `runtime.environment` sets `UCX_CUDA_IPC_CACHE` to `n`. With the CUDA IPC cache off, UCX 1.22 or later in the engine image unmaps the producer's memory when NIXL removes its agent.

Router peer release rounds send each live KV consumer a transfer probe from another live producer.

`UCX_CUDA_IPC_CACHE` in `runtime.environment` selects the trade-off for CUDA IPC engines. At `y`, the UCX default, a stopped peer's GPU memory stays mapped until a wave restart, and transfers from a restarted or idle-evicted producer go through the cached mapping. At `n`, with UCX 1.22 or later, peer release rounds release that memory, and each transfer from a restarted or idle-evicted producer maps and unmaps the producer's KV memory.

`launch.peer_release` in an engine's attestation reports whether that engine releases a stopped peer's memory, and the image check records the same value in [`checked.json`](../configuration/05-Engine-Launch.md#16-runtime-launch-records-and-image-verification). The value is `true` for an engine with zero CUDA IPC peers, and for an engine with UCX 1.22 or later and the CUDA IPC cache off. Every other CUDA IPC engine reports `false`.

#### Release rounds

Under `recovery.engine_restart_policy` `individual`, the router schedules release rounds for each engine out of placement. Rounds start at ejection for an ejected engine, and at each change of state for an engine in lifecycle state `drained`, `deadline_exceeded`, `validating` or `blocked`.

Each round runs at a fixed offset from that start:

| Round | Seconds after the start |
| :---: | :---: |
| 1 | 65 |
| 2 | 125 |
| 3 | 245 |
| 4 | 485 |
| 5 | 965 |
| 6 | 1925 |
| 7 | 3845 |

When a producer or consumer leg fails, the router retries once 5 seconds later through the next producer. When the consumer has zero other live producers, the router logs a warning and waits for the next round.

`/narwhal/state` reports each engine's rounds in [`peer_release`](../http-api/05-Live-State.md#peer_release).

#### Startup wait

Before vLLM measures GPU memory, a serving engine waits until free memory reaches the share that `--gpu-memory-utilization` requests, for up to 180 seconds.

While memory is short, the engine checks every 5 seconds and logs `waiting for KV peers to release it`.

#### Whole-wave fallback

`narwhal-check` warns about each host-sharing producer with a host peer in two cases. A host peer whose `launch.peer_release` attestation is `false`, missing, or unreadable keeps the stopped producer's memory mapped. A host peer with zero other producers for its release probe receives zero release probes.

A [wave restart](../operate/03-Restart-Engines.md#8-restarting-an-engine-wave) recovers a crashed engine in both cases.

### Last-engine protection

An uncovered engine stays in placement under a performance-drift, temporary-quarantine, or inference-probe hold, and after a failed health or inference probe. An engine under a temporary-quarantine or inference-probe hold returns to placement when another engine's ejection or drain leaves it uncovered. A connection-error or liveness ejection takes an uncovered engine out of placement.

At zero serving capacity, `/ready` and new completion requests return HTTP 503.

### Clearing failure evidence

Each successful response clears these failure-evidence classes:

| Successful response | Failure evidence cleared |
| --- | --- |
| 4xx response other than 408 or 429 | Connection and inference-status |
| Health 200 | Connection, timeout, overload, and liveness |
| Prefill with an extracted KV handoff | Connection, inference-status, and KV-handoff |
| Decode stream reaching its terminal marker | Connection, inference-status, and stream |

### Failure quarantine

With `recovery.failure_quarantine_s` above `0`, a failed [covered](#failure-evidence) engine enters quarantine until the deadline. When the deadline passes, candidate selection releases the engine.

A successful health or inference probe that meets the profile-match rule ends quarantine early. Quarantine also ends when another engine's ejection or drain leaves the engine uncovered.

## Readmission and drains

Lifecycle readmission requires a complete `engine_contract`.

With `engine_contract` set, under `recovery.engine_restart_policy` `individual`, automatic recovery runs the readmission checks when the ejected engine's `/health` returns HTTP 200 and its attestation sidecar responds. An engine that fails automatic recovery stays blocked until an operator requests readmission. While a blocked engine waits and another engine stays in placement, each other ejected engine recovers individually.

When zero engines remain in placement, recovery runs as one [whole wave](../operate/03-Restart-Engines.md#75-recovering-loss-of-every-placement-peer). A blocked whole-wave member holds the wave.

Readmission runs the [lifecycle readmission checks](../http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) in order.

Profile checks cover every loaded variant. With `engine_contract` set, profile digests must match the verified attestation's `launch_digest` when the sidecar reports one, otherwise its `attestation_digest`. With `engine_contract` unset, automatic recovery checks health or inference and matches profiles to the live process identity.

A profile mismatch excludes an engine from recovery, readmission, and takeover.

The running router keeps its loaded profiles until restart.

An operator drain survives healthy responses, resume, and takeover until readmission clears it.

## Serving saturation and retries

With the default `serving.max_attempts` of `1`, each admitted request gets one prefill and one decode attempt. With the default `serving.queue_capacity` of `0`, the router refuses new requests immediately when every admission seat is occupied.

[Bounded serving](../configuration/02-Serving-and-Role-Control.md#4-request-admission-and-bounded-serving) can queue a request or [retry](01-Request-and-Topology.md#how-a-request-executes) it within its original deadline.

## Durable control-plane state

The state handoff records:

- engine roles
- ejections
- lifecycle holds
- inference-verification holds
- consolidation risk
- counters
- the lease holder

### Resume validation

`narwhal-serve --resume` applies a saved state handoff when these match the configured fleet:

- schema
- engine set
- engine restart policy

With `engine_contract` configured, resume requires an accepted process identity for each engine the saved state handoff counts as available.

[Activating replacement profiles](../operate/03-Restart-Engines.md#activating-replacement-profiles) loads fresh measurements and keeps lifecycle holds and drain identities.

### Atomic state handoff writes

Concurrent writers leave one complete document at the destination. A failed write leaves the previous handoff in place.

### Warm standby and the lease

A warm standby router serves traffic when it holds the shared lease.

Load balancers find the serving router through `/ready`.
