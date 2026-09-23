# HA handoff and engine lifecycle

## HA handoff

### `GET /narwhal/handoff`

Returns a fresh control-plane handoff document.

The endpoint contains state required for standby takeover and operator tooling and should be exposed only on the trusted control network.

### Handoff fields

| Field               | Meaning                                                                                                    |
| ------------------- | ---------------------------------------------------------------------------------------------------------- |
| `schema`            | `narwhal.handoff`                                                                                          |
| `schema_version`    | Handoff schema version; this release writes `1`                                                            |
| `at`                | Unix wall-clock timestamp                                                                                  |
| `run`               | Request-journal run ID of the writing process                                                              |
| `model`             | Configured served model                                                                                    |
| `epoch`             | Lease epoch; zero when HA fencing is disabled                                                              |
| `holder`            | Unique lease-holder token; empty when HA fencing is disabled                                               |
| `engines`           | Sorted configured engine IDs                                                                               |
| `roles`             | Engine ID mapped to `prefill` or `decode`                                                                  |
| `ejected`           | Engines currently excluded by the breaker                                                                  |
| `inference_sources` | Suspect engines mapped to producer IDs required for verification; empty producer ID requests a local probe |
| `counters`          | `served`, `failed`, `unserved`, `refused`, `rejected`, `cancelled` totals                                  |
| `lifecycle`         | Durable drain, validation, wave state, restart policy, and accepted process starts                         |
| `demand_risk`       | Latest consolidation-risk event, or `null`                                                                 |

`demand_risk` contains:

- `kind`
- elapsed `age_s`
- per-kind counts

A receiving router reanchors `age_s` to its own clock and begins gathering new arrival evidence.

Package and Git provenance are stored in the request-journal header.

### Restored and process-local state

A handoff restores these persisted totals:

- `served`
- `failed`
- `unserved`
- `refused`
- `rejected`
- `cancelled`

The replacement process starts fresh process-local state for:

- resident tracking
- flip history
- role-change counters
- controller-decision counters
- latency histograms
- floor history
- monitoring-failure counters

Narwhal writes the handoff to:

```text
recovery.state_path
```

for:

```text
narwhal-serve --resume
```

Warm standbys retrieve the same state through `/narwhal/handoff`.

---

## Lifecycle API

Narwhal exposes explicit lifecycle state and actions for planned engine restarts.

### `GET /narwhal/lifecycle`

Returns:

```text
narwhal.lifecycle
```

schema version `1`.

`router.controls_fleet` distinguishes:

- active lease holder
- standby router
- fenced router

Each engine record exposes:

- `state`
- `draining`
- scheduler eligibility in `accepts_new`
- `ready_to_stop`
- resident prefill count
- resident decode count
- deadline
- wave ID
- old process-start timestamp
- new process-start timestamp
- validation checks
- error state

The wave record reports:

- whether router-wide readiness has been withdrawn
- whether every wave member is safe for the external supervisor to stop

Top-level `engine_restart_policy` contains the configured restart policy.

`process_starts` maps engine IDs to the last accepted process-start timestamps.

Top-level `error` is empty on success. For a non-2xx response, it contains the rejected action's error.

---

## Draining engines

### `POST /narwhal/lifecycle/drain`

Starts a lifecycle drain for:

- one engine, or
- the entire fleet as one wave

Narwhal removes every target from placement before recording process identity.

A whole-fleet drain also withdraws router `/ready`.

Example:

```json
{
  "engines": ["e0"],
  "deadline_s": 300
}
```

Failure semantics:

|  HTTP | Meaning                                                                   |
| ----: | ------------------------------------------------------------------------- |
| `409` | Unsafe lifecycle request shape                                            |
| `503` | Narwhal could not capture process identity; the drain hold remains active |

The hold is deliberately retained after process-identity failure so the affected engine is not silently returned to placement.

---

## Readmitting engines

### `POST /narwhal/lifecycle/readmit`

Validates one or more engines and releases their lifecycle hold only after all required checks pass.

Example:

```json
{
  "engines": ["e0"]
}
```

Readmission checks:

1. health
2. process-bound attestation
3. configured model
4. direct generation
5. role-compatible KV transfer
6. final health

For a planned restart, the engine's process start must be newer than the process start captured during drain.

Breaker recovery may reuse the existing process identity because a transient failure may eject a process that did not restart.

HTTP `409` leaves candidates that fail validation blocked.

### Whole-wave restart policy

With:

```text
recovery.engine_restart_policy: whole_wave
```

every lifecycle action covers wave members that have both:

- a recorded drain identity
- a newer process

Operators must issue an explicit wave drain before restarting the engines.

See [Operate Narwhal](../operate/03-Restart-Engines.md#7-restart-one-engine) for the external-supervisor restart sequence and whole-wave requirements.
