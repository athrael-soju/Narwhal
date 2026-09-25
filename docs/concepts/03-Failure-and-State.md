# Failure, readmission, and state

## Monitoring and readiness

A monitor pass executes several operational stages independently:

- controller logic;
- health checks;
- drain settlement;
- interval rollover;
- readmission;
- liveness;
- telemetry;
- handoff persistence.

Narwhal records a stage error, increments the relevant counters, and continues the remaining stages of the monitor pass.

Any pass containing at least one stage exception increments the consecutive monitor-failure streak.

When that streak reaches `controller.monitor_failure_limit`, the router stops accepting new admissions.

`/ready` returns HTTP 503 with:

```text
monitoring degraded: <stage> <class>
```

Existing requests continue to completion.

A standby treats this readiness failure as evidence toward its takeover threshold.

Monitoring remains active while the router is degraded. One fully successful pass clears the failure streak and reopens admission.

Restarting the router starts with fresh monitor-failure counters.

## Engine failure handling

### Connection pools

Prefill, decode, and token counting use the data connection pool bounded by `serving.max_connections`.

Health checks, suspect verification, and readmission use the reserved control pool bounded by `engine.control_connections`.

### Failure evidence

Narwhal tracks consecutive failures separately by engine and failure class.

When a streak reaches `recovery.eject_after`, the response depends on the class.

| Failure                                                                 | Class              | Action                                      |
| ----------------------------------------------------------------------- | ------------------ | ------------------------------------------- |
| Connection error                                                        | `connection`       | Eject the engine                            |
| Transport timeout                                                       | `timeout`          | Run a health probe                          |
| First-token deadline, mid-stream silence, or invalid stream termination | `stream`           | Pause new requests and probe prefill/decode |
| HTTP 408 or 429                                                         | `overload`         | Run a health probe                          |
| Other HTTP 5xx response                                                 | `inference_status` | Pause new requests and probe prefill/decode |
| Unreadable KV handoff from prefill                                        | `kv_handoff`       | Pause new requests and probe prefill/decode |

Narwhal pauses new requests to an engine by applying an inference-verification hold before probing prefill and decode.

An inconclusive inference-probe leg leaves the inference-verification hold active. The monitor schedules another probe while admission sends new work to eligible peers.

A successful inference probe clears recorded inference failures.

A failed prefill or decode probe leg ejects the engine.

### Liveness

Liveness has its own per-engine miss counter.

Any successful health response resets that counter.

Narwhal ejects an engine after `recovery.liveness_misses` consecutive silent sweeps.

### Last-engine protection

Performance-drift and temporary-quarantine holds preserve the last eligible engine. A confirmed failure can remove it. When serving capacity is exhausted, `/ready` and new completion requests return HTTP 503 while recovery probes continue.

### Clearing failure evidence

| Successful response                        | Failure evidence cleared                     |
| ------------------------------------------ | -------------------------------------------- |
| 4xx response other than 408 or 429         | Connection and inference-status              |
| Health 200                                 | Connection, timeout, overload, and liveness  |
| Prefill with an extracted handoff          | Connection, inference-status, and KV-handoff |
| Decode stream reaching its terminal marker | Connection, inference-status, and stream     |

### Failure quarantine

When `recovery.failure_quarantine_s` is configured, a failed engine remains quarantined until the deadline unless a successful health or inference check clears the hold first.

Candidate selection automatically expires the quarantine when its deadline passes.

A successful inference probe also clears the separate inference-verification hold.

## Readmission and drains

Before returning an engine to placement, Narwhal verifies:

- health;
- attestation;
- model identity;
- generation identity;
- role-permitted KV transfer;
- final health.

Planned maintenance adds a newer-process requirement.

Operator drains survive:

- successful health responses;
- router resume;
- standby takeover.

Successful readmission clears an operator drain.

## Serving saturation and retries

The default serving policy reports saturation after one prefill/decode attempt.

[Bounded serving](../configuration/02-Serving-and-Role-Control.md#4-request-admission-and-bounded-serving) can queue or retry within the request’s original deadline.

Each retry acquires fresh KV ownership.

## Durable control-plane state

On every monitor pass, the active router writes a versioned handoff containing:

- engine roles;
- ejections;
- lifecycle holds;
- inference-verification holds;
- consolidation risk;
- counters;
- lease ownership.

### Resume validation

`narwhal-serve --resume` applies a saved handoff when both of these match the configured fleet:

- handoff schema;
- engine set.

### Atomic handoff writes

Handoff persistence uses atomic rename.

Each writer:

1. creates a process-unique temporary file;
2. writes the complete handoff;
3. renames that file over the destination.

Concurrent writers therefore leave one complete final document.

If a write fails, Narwhal removes its temporary file and leaves the previous destination intact.

### Warm standby and lease ownership

A warm standby follows the active router's handoff and waits to serve traffic until it acquires the shared lease.

The previous lease holder fences itself before its local lease expires.

Load balancers determine the current serving owner through `/ready`.
