---
description: Engine monitoring, failure handling, readmission and durable control-plane state in Narwhal.
---

# Failure, readmission, and state

## Monitoring and readiness

Monitoring stages:

| Stage | Work |
| --- | --- |
| `controller` | Role-controller logic |
| `health` | Health checks |
| `drains` | Drain settlement |
| `rollover` | Interval rollover |
| `readmission` | Readmission and [peer release](#peer-memory-release) rounds |
| `liveness` | Liveness probes |
| `residency` | Prefix residency refresh from attestation sidecars, on its own loop every `controller.monitor_interval_s` |
| `handoff` | State handoff persistence |
| `telemetry` | Floor-state refresh and loop logging |

Each pass with a failed stage extends the monitor-failure streak.

When the streak reaches `controller.monitor_failure_limit`, the router enters a degraded state:

| Item | Degraded behavior |
| --- | --- |
| New requests | Admission stops. |
| `/ready` | HTTP 503 with the reason `monitoring degraded: <stage> <class>` |
| Admitted requests | Continue. |
| Engine monitoring passes | Continue. |
| Standby router | Counts the readiness failure toward its takeover threshold. |

These events reset the streak:

| Event | Effect |
| --- | --- |
| Successful pass | Clears the streak and reopens admission. |
| Router restart | Resets the monitor-failure counters. |

## Engine failure handling

### Connection pools

| Pool | Traffic | Bound |
| --- | --- | --- |
| Data, one per engine | Prefill, decode, and token counting | `serving.max_connections` per pool |
| Reserved control | Health probes and inference probes | `engine.control_connections` |

### Failure evidence

When an engine's failure streak for one class reaches `recovery.eject_after`, the action depends on the class:

| Failure | Class | Action |
| --- | --- | --- |
| Connection error | `connection` | Eject the engine |
| Transport timeout | `timeout` | Run a health probe |
| First-token deadline while the engine emits other output | `overload` | Run a health probe |
| First-token deadline from a silent engine, mid-stream silence, or invalid stream termination | `stream` | Pause new requests on a covered engine and run an inference probe |
| HTTP 408 or 429 | `overload` | Run a health probe |
| Other HTTP 5xx response | `inference_status` | Pause new requests on a covered engine and run an inference probe |
| Unreadable KV handoff from prefill | `kv_handoff` | Pause new requests on a covered engine and run an inference probe |

The role-coverage rule counts an engine as covered when every role it places stays placeable through other live engines:

| Engine | Covered when |
| --- | --- |
| Pinned | Another live engine holds its role or is unpinned. |
| Unpinned | Prefill and decode each have another live engine that holds that role or is unpinned. |

The profile-match rule requires loaded profiles that match the live process generation before a recovery probe clears evidence and holds.

The inference probe runs a prefill leg and a decode leg:

| Probe result | Effect |
| --- | --- |
| Inconclusive leg | Engine monitoring keeps the hold and schedules another probe. |
| Failed leg on a covered engine | Narwhal ejects the engine. |
| Failed leg on an uncovered engine | The engine stays in placement. |
| Success | Clears recorded inference failures and the hold, under the profile-match rule. |

### Liveness

| Liveness probe result | Effect |
| --- | --- |
| `recovery.liveness_misses` consecutive failures | Narwhal ejects the engine. |
| Success | Resets the engine's liveness miss count. |
| Success on a quarantined engine that meets the profile-match rule | Lifts quarantine and clears failure evidence. |

### Peer memory release

Engines that share a host and exchange KV through CUDA IPC map each other's KV memory.

A stopped engine's GPU memory stays allocated while a host peer holds that mapping.

| Part | Behavior |
| --- | --- |
| vLLM NIXL connector | A consume request evicts each producer idle for longer than `engine_ttl` and removes its NIXL agent. |
| Engine launcher | Sets `engine_ttl` to 60 seconds for engines with CUDA IPC peers when `runtime.environment` sets `UCX_CUDA_IPC_CACHE` to `n`. |
| UCX 1.22 or later in the engine image | Unmaps the producer's memory when NIXL removes its agent, with the CUDA IPC cache off. |
| Router peer release rounds | Send each live KV consumer a transfer probe from another live producer. |
| Engine startup | Waits up to 180 seconds for its GPU memory before vLLM measures it. |

`UCX_CUDA_IPC_CACHE` in `runtime.environment` selects the trade-off for CUDA IPC engines:

| `UCX_CUDA_IPC_CACHE` | Stopped peer's GPU memory | Transfers from a restarted or idle-evicted producer |
| --- | --- | --- |
| `y`, the UCX default | Stays mapped until a wave restart | Through the cached mapping |
| `n`, with UCX 1.22 or later | Released by peer release rounds | Map and unmap the producer's KV memory on each transfer |

`launch.peer_release` in an engine's attestation reports whether that engine releases a stopped peer's memory:

| Engine | `peer_release` |
| --- | --- |
| Engine with zero CUDA IPC peers | `true` |
| UCX 1.22 or later with the CUDA IPC cache off | `true` |
| Any other CUDA IPC engine | `false` |

The image check records the same value in `checked.json`, with the bundled UCX version in `ucx_version`.

#### Release rounds

Under `recovery.engine_restart_policy` `individual`, the router schedules release rounds for each engine out of placement:

| Engine state | Rounds start |
| --- | --- |
| Ejected | At ejection |
| Lifecycle state `drained`, `deadline_exceeded`, `validating` or `blocked` | At each change of state |

| Round | Seconds after the start |
| :---: | :---: |
| 1 | 65 |
| 2 | 125 |
| 3 | 245 |
| 4 | 485 |
| 5 | 965 |
| 6 | 1925 |
| 7 | 3845 |

| Probe outcome | Next attempt |
| --- | --- |
| Producer or consumer leg failed | One retry 5 seconds later, through the next producer |
| Zero other live producers for the consumer | Next round, with a warning in the router log |

`/narwhal/state` reports each engine's rounds in [`peer_release`](../http-api/05-Live-State.md#peer_release).

#### Startup wait

A restarted serving engine polls free GPU memory every 5 seconds.

It starts vLLM once free memory reaches the share that `--gpu-memory-utilization` requests, or after 180 seconds.

While memory is short, the engine logs `waiting for KV peers to release it`.

#### Whole-wave fallback

`narwhal-check` warns about each host-sharing producer with a host peer in either case:

| Host peer | Effect |
| --- | --- |
| Lacks `launch.peer_release: true` | Keeps the stopped producer's memory mapped |
| Has zero other producers for its release probe | Receives zero release probes |

A [wave restart](../operate/03-Restart-Engines.md#8-restart-an-engine-wave) recovers a crashed engine in both cases.

### Last-engine protection

| Uncovered engine | Placement |
| --- | --- |
| Performance-drift, temporary-quarantine, or inference-probe hold | Stays in placement |
| Temporary-quarantine or inference-probe hold after another engine's ejection or drain | Returns to placement |
| Failed health or inference probe | Stays in placement |
| Connection-error or liveness ejection | Leaves placement |

At zero serving capacity, `/ready` and new completion requests return HTTP 503.

### Clearing failure evidence

| Successful response | Failure evidence cleared |
| --- | --- |
| 4xx response other than 408 or 429 | Connection and inference-status |
| Health 200 | Connection, timeout, overload, and liveness |
| Prefill with an extracted KV handoff | Connection, inference-status, and KV-handoff |
| Decode stream reaching its terminal marker | Connection, inference-status, and stream |

### Failure quarantine

With `recovery.failure_quarantine_s` above `0`, a failed engine's placement depends on its role coverage:

| Failed engine | Placement |
| --- | --- |
| Covered | Quarantined until the deadline |
| Uncovered | Stays in placement |

Quarantine end events:

| Event | Result |
| --- | --- |
| Successful health or inference probe that meets the profile-match rule | Quarantine ends early. |
| Deadline passes | Candidate selection releases the engine. |
| Another engine's ejection or drain leaves the engine uncovered | The quarantine or inference-probe hold ends and the engine returns to placement. |

## Readmission and drains

Lifecycle readmission requires a complete `engine_contract`.

Automatic recovery with `engine_contract` set, under `recovery.engine_restart_policy` `individual`:

| Condition | Behavior |
| --- | --- |
| The ejected engine's `/health` returns HTTP 200 and its attestation sidecar responds | Automatic recovery runs the readmission checks. |
| The engine fails automatic recovery | The engine stays blocked until an operator requests readmission. |
| A blocked engine waits while another engine stays in placement | Each other ejected engine recovers individually. |
| Zero engines remain in placement | Recovery runs as one [whole wave](../operate/03-Restart-Engines.md#75-recover-loss-of-every-placement-peer). |
| A whole-wave member is blocked | The member holds the wave. |

Readmission checks, in order:

1. Health.
2. Attestation.
3. Loaded profile generations.
4. Model identity.
5. Process identity, with a process start newer than the drain record for a planned restart.
6. The `generation` check, a direct completion probe.
7. A role-permitted KV transfer.
8. A final health check.

Profile checks cover every loaded variant:

| Fleet configuration | Profile check |
| --- | --- |
| `engine_contract` set | Profile digests match the verified attestation's `launch_digest` when the sidecar reports one, otherwise its `attestation_digest`. |
| `engine_contract` unset | Automatic recovery checks health or inference and matches profiles to the live process identity. |

A profile mismatch excludes an engine from recovery, readmission, and takeover.

The running router keeps its loaded profiles until restart.

An operator drain survives healthy responses, resume, and takeover until readmission clears it.

## Serving saturation and retries

| Default | Behavior |
| --- | --- |
| `serving.max_attempts` at `1` | One prefill and one decode attempt per admitted request |
| `serving.queue_capacity` at `0` | Immediate refusal of new requests when every admission seat is occupied |

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

[Activate replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles) loads fresh measurements and keeps lifecycle holds and drain identities.

### Atomic state handoff writes

| Case | Destination |
| --- | --- |
| Concurrent writers | One complete document |
| Failed write | The previous handoff |

### Warm standby and the lease

A warm standby router serves traffic when it holds the shared lease.

Load balancers find the serving router through `/ready`.
