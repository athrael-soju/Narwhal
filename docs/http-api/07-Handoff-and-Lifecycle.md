# State handoff and engine lifecycle

## State handoff

### `GET /narwhal/handoff`

Returns the `narwhal.handoff` schema version `1` document.

The high-availability (HA) standby router polls this route before takeover.

### Handoff fields

| Field               | Meaning                                                                               |
| ------------------- | ------------------------------------------------------------------------------------- |
| `schema`            | `narwhal.handoff`                                                                     |
| `schema_version`    | Handoff schema version, `1`                                                           |
| `at`                | Unix wall-clock timestamp                                                             |
| `run`               | Request-journal run ID of the writing process                                         |
| `model`             | Configured served model                                                               |
| `epoch`             | Lease epoch, zero with HA fencing off                                                 |
| `holder`            | Unique lease-holder token, empty with HA fencing off                                  |
| `engines`           | Sorted configured engine IDs                                                          |
| `roles`             | Engine ID mapped to `prefill` or `decode`                                             |
| `ejected`           | Breaker-excluded engines and inference-probe suspects                                 |
| `inference_sources` | Suspect engine ID mapped to its inference-probe producer IDs, `""` for a local probe  |
| `counters`          | `served`, `failed`, `unserved`, `refused`, `rejected`, `cancelled` totals             |
| `lifecycle`         | Drain records, lifecycle events, wave ID, restart policy, and accepted process starts |
| `demand_risk`       | Newest consolidation-risk event, or `null`                                            |

`demand_risk` fields:

| Field    | Meaning                       |
| -------- | ----------------------------- |
| `kind`   | Kind of the newest risk event |
| `age_s`  | Seconds since that event      |
| `events` | Risk-event counts by kind     |

### Restored and process-local state

| State                          | New router process                                       |
| ------------------------------ | -------------------------------------------------------- |
| `roles`                        | Restored for unpinned engines                            |
| `ejected`, `inference_sources` | Restored, with a readmission probe due at once           |
| `counters`, `lifecycle`        | Restored                                                 |
| `demand_risk`                  | Restored, with its age measured on the new process clock |

The new process resets:

- resident tracking
- flip history
- role-change counters
- controller-decision counters
- latency histograms
- floor history
- monitoring-failure counters

State handoff sources:

| Recovery                                         | State handoff source                                 |
| ------------------------------------------------ | ---------------------------------------------------- |
| `narwhal-serve --resume` after a process restart | `recovery.state_path`, written by the running router |
| Warm standby takeover                            | Latest polled, lease-validated state handoff         |

## Lifecycle API

### `GET /narwhal/lifecycle`

`GET /narwhal/lifecycle`, `POST /narwhal/lifecycle/drain`, and `POST /narwhal/lifecycle/readmit` return the `narwhal.lifecycle` schema version `1` document.

| Field                   | Meaning                                                                                   |
| ----------------------- | ----------------------------------------------------------------------------------------- |
| `router.controls_fleet` | `true` when this router holds the active lease                                            |
| `router.ready`          | `true` when the router admits new client requests                                         |
| `wave.id`               | Active whole-wave ID, or empty                                                            |
| `wave.active`           | `true` while a whole-wave hold withdraws router-wide readiness                            |
| `wave.ready_to_stop`    | `true` when every wave member has drained and is safe for the external supervisor to stop |
| `engines`               | One lifecycle record per engine, keyed by engine ID                                       |
| `events`                | Retained lifecycle events, up to the 200 most recent                                      |
| `engine_restart_policy` | Configured restart policy: `individual` or `whole_wave`                                   |
| `process_starts`        | Engine ID mapped to the last accepted process-start timestamp                             |
| `error`                 | Rejected action's message, empty on HTTP `200`                                            |

Engine record fields:

| Field               | Meaning                                                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `state`             | `active`, `draining`, `drained`, `deadline_exceeded`, `blocked`, or `validating`                                         |
| `draining`          | `true` while a lifecycle action holds the engine out of placement                                                        |
| `accepts_new`       | `true` when the engine is eligible for placement                                                                         |
| `ready_to_stop`     | `true` when the engine has drained with its process identity recorded and, for a wave member, the whole wave has drained |
| `resident`          | Resident `prefill` and `decode` request counts                                                                           |
| `deadline_at`       | Unix time when the drain deadline expires, `null` until the engine has a lifecycle record                                |
| `restart_required`  | `true` when lifecycle readmission requires a process start newer than the drain record                                   |
| `wave_id`           | Whole-wave ID, or empty                                                                                                  |
| `old_process_start` | Process-start timestamp recorded at drain                                                                                |
| `new_process_start` | Process-start timestamp accepted at lifecycle readmission                                                                |
| `checks`            | Validation checks the engine passed                                                                                      |
| `error`             | Current failure: identity capture, exceeded drain deadline, or failed validation                                         |

## Draining engines

### `POST /narwhal/lifecycle/drain`

Drains one engine, or every configured engine as a whole wave.

| Field        | Type                | Default | Meaning                                   |
| ------------ | ------------------- | ------- | ----------------------------------------- |
| `engines`    | Array of engine IDs | `[]`    | Engines to drain                          |
| `wave`       | Boolean             | `false` | Drain every configured engine as one wave |
| `deadline_s` | Number of seconds   | `300`   | Drain deadline, positive and finite       |

| Condition                                    | Rule                                                                      |
| -------------------------------------------- | ------------------------------------------------------------------------- |
| `wave` is `false`                            | Name one engine while at least one other engine is eligible for placement |
| `wave: true`                                 | Name every configured engine, or send an empty `engines` list             |
| `recovery.engine_restart_policy: whole_wave` | Every drain must be a whole-wave drain                                    |

```json
{
  "engines": ["e0"],
  "deadline_s": 300
}
```

|  HTTP | Meaning                                                                  |
| :---: | ------------------------------------------------------------------------ |
| `409` | Named engine set violates a drain rule                                   |
| `409` | Unknown engine ID, or a `deadline_s` that is zero, negative, or infinite |
| `409` | Another lifecycle action is active                                       |
| `503` | `router.controls_fleet` is `false`                                       |
| `503` | The router became fenced during the drain                                |
| `503` | Process-identity capture failed, with the target held out of placement   |

Repeat the drain request after a process-identity capture failure.

## Readmitting engines

### `POST /narwhal/lifecycle/readmit`

Lifecycle readmission requires a complete `engine_contract`.

```json
{
  "engines": ["e0"]
}
```

| Field     | Type                | Default | Meaning                                  |
| --------- | ------------------- | ------- | ---------------------------------------- |
| `engines` | Array of engine IDs | `[]`    | Engines to readmit                       |
| `wave`    | Boolean             | `false` | Readmit the active whole wave as one set |

| Condition         | Rule                                                  |
| ----------------- | ----------------------------------------------------- |
| `wave` is `false` | Name one engine                                       |
| Active whole wave | Send `wave: true` with the wave's complete engine set |
| `wave: true`      | An empty `engines` list names every configured engine |

Readmission checks:

1. `health`: the engine health endpoint answers HTTP 200.
2. `attestation <fingerprint>`: process-bound attestation matches the engine contract.
3. `profile generation`: every loaded profile variant's generation matches the verified live process generation.
4. `model`: the engine serves the configured model.
5. `new process identity`, or `process identity` when `restart_required` is `false`: the engine process identity.
6. `generation`: a direct completion probe returns a completion.
7. `fabric produce to <peer>` and `fabric consume from <peer>`: role-compatible KV transfer.
8. `final health`: the engine health endpoint answers after KV transfer validation.

Checks 1 to 4 cover the candidate and its role-permitted peers.

| Condition                                                           | Result                                                         |
| ------------------------------------------------------------------- | -------------------------------------------------------------- |
| Missing profiles, missing generation evidence, or a digest mismatch | Readmission fails with an error naming the engine to reprofile |
| Engine the breaker ejected transiently                              | Readmission accepts its current process                        |

|  HTTP | Meaning                                                                      |
| :---: | ---------------------------------------------------------------------------- |
| `409` | Named engine set violates a readmission rule                                 |
| `409` | A named engine is `active`, `draining`, `deadline_exceeded`, or `validating` |
| `409` | A named engine with `restart_required: true` has a null `old_process_start`  |
| `409` | Validation failed, candidate `blocked`                                       |
| `503` | `router.controls_fleet` is `false`                                           |
| `503` | The router became fenced during validation                                   |

To load updated profiles:

1. Restart the router with its hold preserved through [Activate replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles).
2. Repeat lifecycle readmission.

### Whole-wave restart policy

Under `recovery.engine_restart_policy: whole_wave`:

1. Drain the wave.
2. When `wave.ready_to_stop` is `true`, restart the wave through its [supervisor sequence](../operate/03-Restart-Engines.md#8-restart-an-engine-wave).
3. Readmit the wave.
