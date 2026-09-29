# State handoff and engine lifecycle

## State handoff

### `GET /narwhal/handoff`

`GET /narwhal/handoff` returns the current state handoff (`narwhal.handoff`, schema version `1`). A high-availability (HA) standby router polls it before taking over.

Serve the route on the trusted control network.

### Handoff fields

| Field               | Meaning                                                                                              |
| ------------------- | ---------------------------------------------------------------------------------------------------- |
| `schema`            | `narwhal.handoff`                                                                                    |
| `schema_version`    | Handoff schema version; this release writes `1`                                                      |
| `at`                | Unix wall-clock timestamp                                                                            |
| `run`               | Request-journal run ID of the writing process                                                        |
| `model`             | Configured served model                                                                              |
| `epoch`             | Lease epoch; zero with HA fencing off                                                                |
| `holder`            | Unique lease-holder token; empty with HA fencing off                                                 |
| `engines`           | Sorted configured engine IDs                                                                         |
| `roles`             | Engine ID mapped to `prefill` or `decode`                                                            |
| `ejected`           | Engines excluded by the breaker, including suspects held for an inference probe                      |
| `inference_sources` | Suspect engine ID mapped to the producer IDs needed to verify it; an empty producer ID requests a local probe |
| `counters`          | `served`, `failed`, `unserved`, `refused`, `rejected`, `cancelled` totals                            |
| `lifecycle`         | Durable drain, validation, wave state, restart policy, and accepted process starts                   |
| `demand_risk`       | Newest consolidation-risk event, or `null`                                                           |

`demand_risk` fields:

| Field    | Meaning                        |
| -------- | ------------------------------ |
| `kind`   | Kind of the newest risk event  |
| `age_s`  | Seconds since that event       |
| `events` | Risk-event counts by kind      |

A receiving router reanchors `age_s` to its own clock and starts gathering new arrival evidence.

The request-journal header carries package and Git provenance.

### Restored and process-local state

Resume and takeover restore the `counters` totals from the state handoff. The new process starts fresh on:

- resident tracking
- flip history
- role-change counters
- controller-decision counters
- latency histograms
- floor history
- monitoring-failure counters

Narwhal writes the state handoff to `recovery.state_path`, and `narwhal-serve --resume` loads it after a process restart.

A warm standby applies the latest polled, lease-validated state handoff when it takes over.

---

## Lifecycle API

### `GET /narwhal/lifecycle`

`GET /narwhal/lifecycle` returns the `narwhal.lifecycle` schema version `1` document. The drain and readmit action routes return that same document.

| Field                   | Meaning                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| `router.controls_fleet` | `true` when this router holds the active lease; `false` for a standby or fenced router           |
| `router.ready`          | `true` when the router admits new client requests                                                |
| `wave.id`               | Active whole-wave ID, or empty                                                                   |
| `wave.active`           | `true` while a whole-wave hold is in effect and withdraws router-wide readiness                  |
| `wave.ready_to_stop`    | `true` when every wave member has drained and is safe for the external supervisor to stop        |
| `engines`               | One lifecycle record per engine, keyed by engine ID                                              |
| `events`                | Retained lifecycle events, up to the 200 most recent                                             |
| `engine_restart_policy` | Configured restart policy: `individual` or `whole_wave`                                          |
| `process_starts`        | Engine ID mapped to the last accepted process-start timestamp                                    |
| `error`                 | Rejected action's message on non-2xx responses; an empty string on success                       |

Engine record fields:

| Field               | Meaning                                                                                                     |
| ------------------- | ----------------------------------------------------------------------------------------------------------- |
| `state`             | `active`, `draining`, `drained`, `deadline_exceeded`, `blocked`, or `validating`                           |
| `draining`          | `true` while a lifecycle action holds the engine out of placement                                           |
| `accepts_new`       | `true` when the engine is eligible for placement                                                            |
| `ready_to_stop`     | `true` when the engine has drained with its process identity recorded and, for a wave member, the whole wave has drained |
| `resident`          | Resident `prefill` and `decode` request counts                                                              |
| `deadline_at`       | Unix time when the drain deadline expires; `null` until the engine has a lifecycle record                   |
| `restart_required`  | `true` when lifecycle readmission requires a process start newer than the drain record                      |
| `wave_id`           | Whole-wave ID, or empty                                                                                     |
| `old_process_start` | Process-start timestamp recorded at drain                                                                   |
| `new_process_start` | Process-start timestamp accepted at lifecycle readmission                                                   |
| `checks`            | Validation checks the engine passed                                                                         |
| `error`             | Current failure, such as an identity-capture failure, an exceeded drain deadline, or failed validation      |

---

## Draining engines

### `POST /narwhal/lifecycle/drain`

`POST /narwhal/lifecycle/drain` drains one engine or every configured engine as a whole wave. Each target leaves placement first, then Narwhal records its process identity.

A whole-wave drain also withdraws router `/ready`.

| Field        | Type                | Default | Meaning                                     |
| ------------ | ------------------- | ------- | ------------------------------------------- |
| `engines`    | Array of engine IDs | `[]`    | Engines to drain                            |
| `wave`       | Boolean             | `false` | Drain every configured engine as one wave   |
| `deadline_s` | Number of seconds   | `300`   | Drain deadline; positive and finite         |

| Condition                                        | Rule                                                                  |
| ------------------------------------------------ | --------------------------------------------------------------------- |
| `wave` is `false`                                | Name one engine; another engine must stay eligible for placement      |
| `wave: true`                                     | Name every configured engine, or send an empty `engines` list         |
| `recovery.engine_restart_policy: whole_wave`     | Every drain must be a whole-wave drain                                |

Example:

```json
{
  "engines": ["e0"],
  "deadline_s": 300
}
```

Drain failures:

|  HTTP | Meaning                                                                                  |
| ----: | ---------------------------------------------------------------------------------------- |
| `409` | Unsafe lifecycle request shape, or another lifecycle action is already active            |
| `503` | `router.controls_fleet` is `false`, or the router became fenced during the drain          |
| `503` | Process-identity capture failed; the target stays out of placement. Repeat the drain request |

---

## Readmitting engines

### `POST /narwhal/lifecycle/readmit`

Lifecycle readmission requires a complete `engine_contract`.

Request body:

```json
{
  "engines": ["e0"]
}
```

| Field     | Type                | Default | Meaning                                  |
| --------- | ------------------- | ------- | ---------------------------------------- |
| `engines` | Array of engine IDs | `[]`    | Engines to readmit                       |
| `wave`    | Boolean             | `false` | Readmit the active whole wave as one set |

| Condition            | Rule                                                              |
| -------------------- | ----------------------------------------------------------------- |
| `wave` is `false`    | Name one engine                                                   |
| Active whole wave    | Set `wave: true` and name the wave's complete engine set          |
| `wave: true`         | An empty `engines` list names every configured engine             |

Before releasing the hold, these checks run and appear in the engine record's `checks`:

1. `health`: the engine health endpoint answers HTTP 200.
2. `attestation <fingerprint>`: process-bound attestation matches the engine contract.
3. `profile generation`: loaded profile generations match the verified live process generation.
4. `model`: the engine serves the configured model.
5. `new process identity`, or `process identity` when `restart_required` is `false`: the engine process identity. A planned restart needs a process start newer than the drain record.
6. `generation`: a direct completion probe returns a completion.
7. `fabric produce to <peer>` and `fabric consume from <peer>`: role-compatible KV transfer.
8. `final health`: the engine health endpoint answers after KV transfer validation.

Checks 1 to 4 also run on the candidate's role-permitted peers. `profile generation` covers every loaded profile variant.

The candidate stays excluded when profiles or generation evidence are missing or a digest mismatches, and the error names the engine to reprofile.

An engine ejected transiently by the breaker can be readmitted without a restart.

|  HTTP | Meaning                                                  |
| ----: | -------------------------------------------------------- |
| `409` | Validation failed; the candidate stays blocked           |
| `503` | The router became fenced during validation               |

To load updated profiles:

1. Follow [Activate replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles) to restart the router with its hold preserved.
2. Repeat lifecycle readmission.

### Whole-wave restart policy

With `recovery.engine_restart_policy: whole_wave`, drain and lifecycle readmission cover the whole wave of every configured engine. The drain records each member's process start.

When `wave.ready_to_stop` turns `true`, restart the wave through its supervisor. Readmission returns it to service once every replacement passes validation with a newer process start.

See [Restart an engine wave](../operate/03-Restart-Engines.md#8-restart-an-engine-wave) for the supervisor sequence.
