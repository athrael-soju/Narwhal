---
description: Engine monitoring, failure handling, readmission and durable control-plane state in Narwhal.
---

# Failure, readmission, and state

## Monitoring and readiness

Monitor pass stages:

| Stage | Work |
| --- | --- |
| `controller` | Role-controller logic |
| `health` | Health checks |
| `drains` | Drain settlement |
| `rollover` | Interval rollover |
| `readmission` | Readmission and [peer release](#peer-memory-release) rounds |
| `liveness` | Liveness probes |
| `handoff` | State handoff persistence |
| `telemetry` | Floor-state refresh and loop logging |

Prefix residency refresh from attestation sidecars runs on its own `residency` loop every `controller.monitor_interval_s`, and its failures count toward the monitor-failure streak.

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

Engines that share a host and exchange KV through CUDA IPC map each other's KV memory. A stopped engine's GPU memory stays allocated while any peer holds that mapping, and the engine can restart on its GPU once every peer releases it.

Release takes three parts:

| Part | Behavior |
| --- | --- |
| vLLM NIXL connector | A consume request evicts each producer that has been idle for longer than `engine_ttl` and removes its NIXL agent. |
| Engine launcher | For engines with CUDA IPC peers, sets `engine_ttl` to 60 seconds and `UCX_CUDA_IPC_CACHE` to `n`. A `UCX_CUDA_IPC_CACHE` value in `runtime.environment` takes precedence. |
| UCX 1.22 or later in the engine image | With `UCX_CUDA_IPC_CACHE=n`, unmaps the producer's memory when NIXL removes its agent. |

The image check records the UCX version bundled with the image's NIXL build in `checked.json` as `ucx_version`, and records `peer_release: true` when the engine has no CUDA IPC peers or meets both UCX conditions. The attestation reports both values under `launch`.

Under `recovery.engine_restart_policy` `individual`, the router sends peer release rounds for each ejected engine and for each engine whose lifecycle drain has completed. A round sends one transfer probe through every live engine that can consume KV, from another live producer. Rounds run 65, 125, 245, 485, 965, 1925 and 3845 seconds after the ejection or drain while the engine stays out of placement, so later rounds also cover longer backend `engine_ttl` values. Consumers that a round misses receive another probe 5 seconds later. `/narwhal/state` reports each engine's rounds in [`peer_release`](../http-api/05-Live-State.md#peer_release).

A restarted serving engine waits before vLLM measures free GPU memory. Every 5 seconds for up to 180 seconds, it compares free memory with the share that `--gpu-memory-utilization` requests, and logs `waiting for KV peers to release it` while memory is short. A restart issued right after a crash therefore starts once the first release round frees the memory.

With `peer_release: false`, peers keep the stopped engine's memory mapped until they restart. `narwhal-check` warns about host-sharing engines whose attestation lacks `launch.peer_release: true`, and a [wave restart](../operate/03-Restart-Engines.md#8-restart-an-engine-wave) recovers the crashed engine.

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

| Event | Result |
| --- | --- |
| Successful health or inference probe that meets the profile-match rule | Quarantine ends early. |
| Deadline passes | Candidate selection releases the engine. |
| Another engine's ejection or drain leaves the engine uncovered | The quarantine or inference-probe hold ends and the engine returns to placement. |

## Readmission and drains

Lifecycle readmission requires a complete `engine_contract`.

Automatic recovery starts these checks once the ejected engine's `/health` returns HTTP 200 and its attestation sidecar responds. An engine that fails automatic recovery stays blocked until readmission. While another engine stays in placement, other ejected engines keep recovering individually. After every placement peer is lost, recovery runs as one [whole wave](../operate/03-Restart-Engines.md#75-recover-loss-of-every-placement-peer), and a blocked member holds it.

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
