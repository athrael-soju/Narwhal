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

Recovery probes require loaded profiles that match the live process generation before they clear evidence and holds. This requirement is the profile-match rule.

The inference probe runs a prefill leg and a decode leg:

| Probe result | Effect |
| --- | --- |
| Inconclusive leg | The hold stays, and engine monitoring schedules another probe. |
| Failed prefill or decode leg | Narwhal ejects the engine. |
| Success | Clears recorded inference failures and the hold, under the profile-match rule. |

### Liveness

Liveness has its own per-engine miss counter. Narwhal ejects an engine after `recovery.liveness_misses` consecutive failed liveness probes.

A successful liveness health probe resets the counter. For a quarantined engine that meets the profile-match rule, the probe also lifts quarantine and clears failure evidence.

### Last-engine protection

The last eligible engine stays in placement under performance-drift and temporary-quarantine holds. A confirmed failure removes it.

At zero serving capacity, `/ready` and new completion requests return HTTP 503. Engine monitoring keeps running recovery probes.

### Clearing failure evidence

| Successful response                        | Failure evidence cleared                     |
| ------------------------------------------ | -------------------------------------------- |
| 4xx response other than 408 or 429         | Connection and inference-status              |
| Health 200                                 | Connection, timeout, overload, and liveness  |
| Prefill with an extracted KV handoff       | Connection, inference-status, and KV-handoff |
| Decode stream reaching its terminal marker | Connection, inference-status, and stream     |

### Failure quarantine

With `recovery.failure_quarantine_s` above `0`, a failed engine stays quarantined until the deadline.

| Event | Result |
| --- | --- |
| Successful health or inference probe that meets the profile-match rule | Quarantine ends early. |
| Deadline passes | Candidate selection releases the engine. |

## Readmission and drains

Lifecycle readmission requires a complete `engine_contract`. It runs these checks in order:

1. Health.
2. Attestation.
3. Loaded profile generations.
4. Model identity.
5. A direct completion probe (the `generation` check).
6. A role-permitted KV transfer.
7. A final health check.

After a planned restart, the engine's process start must be newer than the one in its drain record.

Profile checks cover every loaded variant:

| Fleet configuration | Profile check |
| --- | --- |
| `engine_contract` set | Profile digests match verified attestation. |
| `engine_contract` unset | Automatic recovery checks health or inference and matches profiles to the live process identity. |

A profile mismatch keeps an engine excluded from recovery, readmission, and takeover. The running router keeps its loaded profiles until restart.

An operator drain survives healthy responses, resume, and takeover. Only readmission clears it.

## Serving saturation and retries

Each admitted request gets one prefill and one decode attempt. When every admission seat is occupied, new requests are refused at once.

[Bounded serving](../configuration/02-Serving-and-Role-Control.md#4-request-admission-and-bounded-serving) can queue or retry within the request's original deadline. Each [retry](01-Request-and-Topology.md#how-a-request-executes) obtains a fresh KV handoff.

## Durable control-plane state

On every monitor pass the active router writes a versioned state handoff. It records engine roles, ejections, lifecycle holds, inference-verification holds, consolidation risk, counters, and the lease holder.

### Resume validation

`narwhal-serve --resume` applies a saved state handoff when its schema, engine set, and engine restart policy match the configured fleet.

With `engine_contract` configured, resume also requires an accepted process identity for each engine the saved state handoff counts as available.

To load fresh measurements and keep lifecycle holds and drain identities, follow [Activate replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles).

### Atomic state handoff writes

Each write renames a complete handoff from a process-unique temporary file over the destination.

| Case | Destination |
| --- | --- |
| Concurrent writers | One complete document |
| Failed write | The previous handoff. Narwhal deletes the temporary file. |

### Warm standby and the lease

A warm standby router follows the active router's state handoff and serves traffic after it acquires the shared lease. Load balancers find the serving router through `/ready`.

The previous lease holder fences itself before its local lease expires.
