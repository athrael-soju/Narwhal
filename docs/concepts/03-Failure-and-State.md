# Failure, readmission, and state

## Monitoring and readiness

A monitor pass runs these stages independently:

| Stage | Work |
| --- | --- |
| `controller` | Role-controller logic |
| `health` | Health checks |
| `drains` | Drain settlement |
| `rollover` | Interval rollover |
| `readmission` | Readmission |
| `liveness` | Liveness probes |
| `residency` | Prefix residency refresh from attestation sidecars |
| `handoff` | State handoff persistence |
| `telemetry` | Floor-state refresh and loop logging |

If a stage raises an exception, Narwhal records it and continues with the remaining stages. Each pass with a failed stage extends the monitor-failure streak.

When the streak reaches `controller.monitor_failure_limit`, the router enters a degraded state. It stops admitting new requests, and `/ready` returns HTTP 503 with the reason `monitoring degraded: <stage> <class>`. Admitted requests and engine monitoring passes carry on. A standby router counts the readiness failure toward its takeover threshold.

A successful pass clears the streak and reopens admission. A router restart resets the monitor-failure counters.

## Engine failure handling

### Connection pools

| Pool | Traffic | Bound |
| --- | --- | --- |
| Data | Prefill, decode, and token counting | `serving.max_connections` |
| Reserved control | Health probes and inference probes | `engine.control_connections` |

### Failure evidence

Narwhal keeps a failure streak for each engine and class. When a streak reaches `recovery.eject_after`, the action depends on the class:

| Failure                                                                 | Class              | Action                                         |
| ----------------------------------------------------------------------- | ------------------ | ---------------------------------------------- |
| Connection error                                                        | `connection`       | Eject the engine                               |
| Transport timeout                                                       | `timeout`          | Run a health probe                             |
| First-token deadline, mid-stream silence, or invalid stream termination | `stream`           | Pause new requests and run an inference probe  |
| HTTP 408 or 429                                                         | `overload`         | Run a health probe                             |
| Other HTTP 5xx response                                                 | `inference_status` | Pause new requests and run an inference probe  |
| Unreadable KV handoff from prefill                                      | `kv_handoff`       | Pause new requests and run an inference probe  |

Pausing an engine places it under an inference-verification hold. Admission sends new work to other eligible engines until the hold lifts.

Recovery probes clear evidence and holds only when the loaded profiles match the live process generation. The rest of this page calls this the profile-match rule.

The inference probe runs a prefill leg and a decode leg:

| Probe result | Effect |
| --- | --- |
| Inconclusive leg | The hold stays; engine monitoring schedules another probe. |
| Failed prefill or decode leg | Narwhal ejects the engine. |
| Success | Clears recorded inference failures and the hold, under the profile-match rule. |

### Liveness

Liveness has its own per-engine miss counter. Narwhal ejects an engine after `recovery.liveness_misses` consecutive failed liveness probes.

A successful liveness health probe resets the counter. For a quarantined engine, it also lifts quarantine and clears failure evidence, under the profile-match rule.

### Last-engine protection

Performance-drift and temporary-quarantine holds never remove the last eligible engine from placement; only a confirmed failure does.

At zero serving capacity, `/ready` and new completion requests return HTTP 503. Engine monitoring keeps running recovery probes.

### Clearing failure evidence

| Successful response                        | Failure evidence cleared                     |
| ------------------------------------------ | -------------------------------------------- |
| 4xx response other than 408 or 429         | Connection and inference-status              |
| Health 200                                 | Connection, timeout, overload, and liveness  |
| Prefill with an extracted KV handoff       | Connection, inference-status, and KV-handoff |
| Decode stream reaching its terminal marker | Connection, inference-status, and stream     |

### Failure quarantine

When `recovery.failure_quarantine_s` is set above `0`, a failed engine stays quarantined until the deadline. A successful health or inference probe ends quarantine early, under the profile-match rule. Otherwise candidate selection releases the engine once the deadline passes.

## Readmission and drains

Lifecycle readmission requires a complete `engine_contract`. It runs these checks in order: health, attestation, loaded profile generations, model identity, a direct completion probe (the `generation` check), a role-permitted KV transfer, and a final health check.

After a planned restart, the engine's process start must be newer than the one in its drain record.

Profile checks cover every loaded variant. With `engine_contract`, profile digests must match verified attestation. Without it, automatic recovery checks health or inference and matches profiles to the live process identity.

A profile mismatch keeps an engine excluded from recovery, readmission, and takeover. The running router keeps its loaded profiles until restart.

An operator drain survives healthy responses, resume, and takeover. Only readmission clears it.

## Serving saturation and retries

Each admitted request gets one prefill and one decode attempt. When every admission seat is taken, new requests are refused at once.

[Bounded serving](../configuration/02-Serving-and-Role-Control.md#4-request-admission-and-bounded-serving) can queue or retry within the request's original deadline. Each retry obtains a fresh KV handoff ([How a request executes](01-Request-and-Topology.md#how-a-request-executes)).

## Durable control-plane state

On every monitor pass the active router writes a versioned state handoff. It records engine roles, ejections, lifecycle holds, inference-verification holds, consolidation risk, counters, and the lease holder.

### Resume validation

`narwhal-serve --resume` applies a saved state handoff when its schema, engine set, and engine restart policy match the configured fleet.

With `engine_contract` configured, resume also requires an accepted process identity for each engine the saved state handoff counts as available.

To load fresh measurements and keep lifecycle holds and drain identities, follow [Activate replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles).

### Atomic state handoff writes

Each writer fills a process-unique temporary file with the complete handoff and renames it over the destination, so concurrent writers always leave one complete document. If a write fails, its temporary file is deleted and the previous handoff stays in place.

### Warm standby and the lease

A warm standby router follows the active router's state handoff and serves traffic after it acquires the shared lease. Load balancers find the serving router through `/ready`.

The previous lease holder fences itself before its local lease expires.
