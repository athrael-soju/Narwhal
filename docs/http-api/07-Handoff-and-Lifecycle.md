# Failover handoff and engine restarts

## Failover handoff

### `GET /narwhal/handoff`

Standby routers poll this endpoint to copy the active router's roles, counters, lifecycle state, and demand risk, so they are up to date if they have to take over. Operators can also read it. Expose it on the trusted control network only.

### Handoff fields

| Field               | Meaning                                                                                                    |
| ------------------- | ---------------------------------------------------------------------------------------------------------- |
| `schema`            | Always `narwhal.handoff`                                                                                   |
| `schema_version`    | `1` in this release                                                                                        |
| `at`                | Unix wall-clock timestamp                                                                                  |
| `run`               | Request-journal run ID of the process that wrote it                                                        |
| `model`             | Configured served model                                                                                    |
| `epoch`             | Lease epoch; zero when HA fencing is off                                                                   |
| `holder`            | Unique lease-holder token; empty when HA fencing is off                                                    |
| `engines`           | Configured engine IDs, sorted                                                                              |
| `roles`             | Each engine ID mapped to `prefill` or `decode`                                                             |
| `ejected`           | Engines the breaker is currently holding out                                                               |
| `inference_sources` | Suspect engines mapped to the producer IDs needed to verify them; an empty producer ID means probe locally |
| `counters`          | Totals for `served`, `failed`, `unserved`, `refused`, `rejected`, `cancelled`                              |
| `lifecycle`         | Durable drain, validation, and wave state, the restart policy, and accepted process starts                 |
| `demand_risk`       | The latest consolidation-risk event, or `null`                                                             |

`demand_risk` holds the event's `kind`, its age in `age_s`, and `events`, a count for each kind. The receiving router converts `age_s` to its own clock and starts gathering fresh arrival evidence.

Package and Git provenance are not in the handoff. They live in the request-journal header.

### Restored and process-local state

A handoff carries over the running totals `served`, `failed`, `unserved`, `refused`, `rejected`, and `cancelled`, along with the roles, ejections, lifecycle state, and demand risk. These start fresh in the new process: resident tracking, flip history, role-change and controller-decision counters, latency histograms, floor history, and monitoring-failure counters.

Narwhal also writes the current handoff to `recovery.state_path`, and `narwhal-serve --resume` reads it back after a restart. A warm standby polls `/narwhal/handoff`, keeps the latest snapshot that passes lease validation, and applies it when it takes control.

## Lifecycle API

### `GET /narwhal/lifecycle`

Returns a `narwhal.lifecycle` document, schema version `1`.

| Field                     | Meaning                                                                                                                        |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `router.controls_fleet`   | `true` only on the router that controls the fleet; `false` on a standby or a fenced router                                     |
| `router.ready`            | Whether the router is admitting clients                                                                                        |
| `wave.id`, `wave.active`  | The current wave ID and whether a wave is active                                                                               |
| `wave.ready_to_stop`      | Whether every engine in the wave is safe for the external supervisor to stop                                                   |
| `engines`                 | Per-engine records, keyed by engine ID (see below)                                                                             |
| `engine_restart_policy`   | The configured restart policy                                                                                                  |
| `process_starts`          | Each engine ID mapped to its last accepted process-start timestamp                                                             |
| `events`                  | Up to 200 recent lifecycle events                                                                                              |
| `error`                   | Empty on success; on a non-2xx response, the reason the action was rejected                                                    |

Each engine record has these fields:

| Field                                     | Meaning                                                                                     |
| ----------------------------------------- | ------------------------------------------------------------------------------------------- |
| `state`                                   | `active`, `draining`, `drained`, `deadline_exceeded`, `validating`, or `blocked`            |
| `draining`                                | Whether the engine is draining                                                              |
| `accepts_new`                             | Whether the scheduler will give the engine new work                                         |
| `ready_to_stop`                           | Whether the engine is safe for the external supervisor to stop                              |
| `resident`                                | Resident prefill and decode counts                                                          |
| `deadline_at`                             | Drain deadline                                                                              |
| `restart_required`                        | Whether a restart is required                                                               |
| `wave_id`                                 | ID of the wave the engine belongs to                                                        |
| `old_process_start`, `new_process_start`  | Process-start timestamps before and after the restart                                       |
| `checks`                                  | Validation checks that have run                                                             |
| `error`                                   | The engine's last error, if any                                                             |

## Drain

### `POST /narwhal/lifecycle/drain`

Starts a drain on a single engine, or on the whole fleet as a wave. Narwhal removes the targets from placement first, then records their process identity. Draining the whole fleet also withdraws the router's `/ready`.

```json
{
  "engines": ["e0"],
  "deadline_s": 300
}
```

| Field        | Default | Meaning                                                                                                     |
| ------------ | ------- | ----------------------------------------------------------------------------------------------------------- |
| `engines`    | `[]`    | Engine IDs to drain. Without `wave`, name exactly one engine.                                               |
| `wave`       | `false` | Drain the whole fleet as one wave. Name every configured engine, or leave `engines` out to select them all. |
| `deadline_s` | `300`   | Seconds resident work has to finish before the engine is marked `deadline_exceeded`.                        |

A successful drain returns HTTP `200` with the lifecycle document.

|  HTTP | Meaning                                                                                                                                                                                                     |
| ----: | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `409` | The request conflicts with policy or current state, such as an unknown engine, the wrong number of engines, another lifecycle action already in progress, or a drain that would leave no schedulable engine |
| `503` | This router doesn't control the fleet, or lost control during the drain                                                                                                                                     |
| `503` | Process identity couldn't be captured. The target stays out of placement; send the same drain request again to retry                                                                                        |

## Readmit

### `POST /narwhal/lifecycle/readmit`

Readmission requires a complete `engine_contract`. Send the ID of the engine to bring back:

```json
{
  "engines": ["e0"]
}
```

Name exactly one drained or blocked engine. While a wave is active, set `"wave": true` and name the wave's complete engine set, or leave `engines` out to select every configured engine. A successful readmission returns HTTP `200` with the lifecycle document.

|  HTTP | Meaning                                                                                                        |
| ----: | -------------------------------------------------------------------------------------------------------------- |
| `409` | The request violates the rules above, or the candidate failed validation and stays blocked                     |
| `503` | This router doesn't control the fleet, or lost control during validation                                       |

Before lifting the engine's lifecycle hold, Narwhal runs these checks in order:

1. health
2. process-bound attestation
3. loaded profile generations against the verified live generation. This covers every loaded profile variant for the candidate and for each peer its role is allowed to exchange KV with. A missing profile, missing generation evidence, or a digest mismatch fails validation, and the error names the engine that needs reprofiling.
4. configured model
5. `generation`, a direct completion probe
6. role-compatible KV transfer
7. final health

After a planned restart, the hold is released only once the engine reports a process start newer than the drain record. After a transient breaker ejection, the running process can be validated and readmitted as it is, without a restart.

To pick up updated profiles, restart the router with its lifecycle hold preserved, as described in [Activate replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles), then send the readmission request again.

### Whole-wave restart policy

With `recovery.engine_restart_policy: whole_wave`, drain and readmission always act on the entire fleet. The drain records each engine's process start. Once `wave.ready_to_stop` is true, restart the fleet through its supervisor. Readmission brings the wave back into service only after every replacement passes validation with a newer process start.

For the external-supervisor restart sequence and the whole-wave requirements, see [Restart one engine](../operate/03-Restart-Engines.md#7-restart-one-engine).
