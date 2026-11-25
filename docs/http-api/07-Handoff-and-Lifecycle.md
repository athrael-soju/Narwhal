---
description: Hand off router state and drain or readmit engines through the Narwhal lifecycle API.
---

# State handoff and engine lifecycle

## State handoff

### `GET /narwhal/handoff`

Returns the `narwhal.handoff` schema version `1` document.

The high-availability (HA) standby router polls this route before takeover.

### Handoff fields

The handoff document carries these fields:

| Field               | Meaning                                                                               |
| ------------------- | ------------------------------------------------------------------------------------- |
| `schema`            | `narwhal.handoff`                                                                     |
| `schema_version`    | Handoff schema version, `1`                                                           |
| `at`                | Unix wall-clock timestamp                                                             |
| `run`               | Request-journal run ID of the writing process                                         |
| `model`             | Configured served model                                                               |
| `epoch`             | Lease epoch, zero with HA fencing off                                                 |
| `holder`            | Unique lease-holder token, empty with HA fencing off                                  |
| `engines`           | Sorted engine IDs of the membership snapshot                                          |
| `membership`        | Snapshot `generation` and `engines`, each engine entry as the fleet file holds it      |
| `roles`             | Engine ID mapped to `prefill` or `decode`                                             |
| `ejected`           | Breaker-excluded engines                                                              |
| `inference_sources` | Suspect engine ID mapped to its inference-probe producer IDs, `""` for a local probe  |
| `counters`          | `offered`, `unsized_offered`, `served`, `slo_met`, `failed`, `expired`, `invalid_requests`, `unserved`, `refused`, `rejected`, `cancelled` totals |
| `outcome_reasons`   | `failed`, `refused`, `rejected`, and `expired` counts by reason                       |
| `lifecycle`         | Drain records, lifecycle events, wave ID, restart policy, and accepted process starts |
| `demand_risk`       | Newest consolidation-risk event, or `null`                                            |

`demand_risk` fields:

| Field    | Meaning                       |
| -------- | ----------------------------- |
| `kind`   | Kind of the newest risk event |
| `age_s`  | Seconds since that event      |
| `events` | Risk-event counts by kind     |

### Restored and process-local state

A new router process applies each handoff field as follows:

| State                                                                                    | New router process                                       |
| ---------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `membership`                                                                             | Replaces the engine set and generation                   |
| `roles`                                                                                  | Restored for unpinned engines                            |
| `ejected`                                                                                | Restored, with a readmission probe due at once           |
| `inference_sources` suspect whose placed roles stay placeable through other live engines | Ejected, with a readmission probe due at once            |
| Other `inference_sources` suspect outside `ejected`                                      | Kept live, with an inference probe due at once           |
| `counters`, `outcome_reasons`, `lifecycle`                                               | Restored                                                 |
| `demand_risk`                                                                            | Restored, with its age measured on the new process clock |

The new process resets:

- resident tracking
- flip history
- role-change counters
- controller-decision counters
- latency histograms
- floor history
- monitoring-failure counters
- timed quarantines
- ejection, hold, probe and readmission counts

After a process restart, `narwhal-serve --resume` reads the state handoff from `recovery.state_path`, written by the running router. A warm standby takeover uses the latest polled, lease-validated state handoff.

### Membership in the handoff

A resumed restart and a standby takeover adopt a saved `membership` whose `generation` is `1` or higher in place of the fleet file's engines. An engine that the router's own fleet file names keeps its fleet-file entry, and the router logs a warning when the saved entry differs. An engine outside the fleet file takes its saved entry. The router refuses the handoff when:

- `membership` names different engines from `engines`
- its `generation` is below the router's own
- it adds, removes or changes an engine that sets `shared_device`
- the store at `profiles.path` lacks a profile for a saved engine

Profiles for engines outside the saved membership are allowed. After a resumed restart adopts the membership, the router checks profiles and first-token calibration against the adopted engines. A standby takeover that adopts the membership checks first-token calibration against the adopted engines that are not ejected or draining. A rejected check blocks the failover, and the standby's `/ready` names the problems in `reason`.

A handoff without `membership`, or with `membership` at generation `0`, applies only when its `engines` match the fleet file, and the engines keep their fleet-file entries. The router ignores keys it does not know in `membership` and in its engine entries.

A router from a release without engine membership ignores `membership` and refuses a handoff whose `engines` differ from its fleet file. Do not join or remove engines while the active and standby routers run different releases.

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
| `ready_to_stop`     | `true` when the engine has drained with its process identity recorded, no decode leg still reads KV from it and, for a wave member, the whole wave has drained |
| `resident`          | Resident `prefill` and `decode` request counts                                                                           |
| `pending_handoffs`  | Decode legs on other engines that may still read KV from the engine                                                      |
| `deadline_at`       | Unix time when the drain deadline expires, `null` until the engine has a lifecycle record                                |
| `restart_required`  | `true` when lifecycle readmission requires a process start newer than the drain record                                   |
| `leaving`           | `true` while a [removal](08-Engine-Membership.md#delete-narwhalenginesiid) drains the engine                              |
| `wave_id`           | Whole-wave ID, or empty                                                                                                  |
| `old_process_start` | Process-start timestamp recorded at drain                                                                                |
| `new_process_start` | Process-start timestamp accepted at lifecycle readmission                                                                |
| `checks`            | Validation checks the engine passed                                                                                      |
| `error`             | Current failure: identity capture, exceeded drain deadline, or failed validation                                         |

## Draining engines

### `POST /narwhal/lifecycle/drain`

Drains one engine, or every member engine as a whole wave.

| Field        | Type                | Default | Meaning                                   |
| ------------ | ------------------- | ------- | ----------------------------------------- |
| `engines`    | Array of engine IDs | `[]`    | Engines to drain                          |
| `wave`       | Boolean             | `false` | Drain every member engine as one wave     |
| `deadline_s` | Number of seconds   | `300`   | Drain deadline, positive and finite       |

A drain request follows these rules:

- With `wave: false`, name one engine while at least one other engine is eligible for placement.
- With `wave: true`, name every member engine, or send an empty `engines` list.
- Under `recovery.engine_restart_policy: whole_wave`, every drain must be a whole-wave drain.

```json
{
  "engines": ["e0"],
  "deadline_s": 300
}
```

The drain route returns these error statuses:

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

`engines` lists the engine IDs to readmit and defaults to `[]`. `wave` is a Boolean that defaults to `false`.

A readmission request follows these rules:

- With `wave: false`, name one engine.
- With `wave: true`, the request readmits the active whole wave as one set, and an empty `engines` list names every member engine.
- During an active whole wave, send `wave: true` with the wave's complete engine set.

Readmission checks:

1. `health`: the engine health endpoint answers HTTP 200.
2. `attestation <fingerprint>`: process-bound attestation matches the engine contract.
3. `profile generation`: every loaded profile variant's generation matches the verified live process generation.
4. `model`: the engine serves the configured model.
5. `model identity`: the engine attests the same model files as the router last verified for every other member that is not ejected or draining. The check compares `launch.model_config_sha256`, the SHA-256 of the checkpoint's `config.json`, `launch.model_tree_sha256`, the digest of every model file the launch reads, and `launch.model_sha256` when the launch names a model file. `launch.model_revision` is a label outside the comparison, so engines that load the same files under a commit and a `sha256:` revision match. In a whole-wave readmission, the wave members attest the same values. A single-engine fleet skips this check.
6. `new process identity`, or `process identity` when `restart_required` is `false`: the engine process identity.
7. `generation`: a direct completion probe returns a completion. SGLang engines skip this probe, and the fabric handoff in check 8 tests generation.
8. `fabric produce to <peer>` and `fabric consume from <peer>`: role-compatible KV transfer.
9. `final health`: the engine health endpoint answers after KV transfer validation.

Checks 1 to 4 cover the candidate and its role-permitted peers.

`checks` lists the checks that passed, in this order. The failed checks appear in the engine's `error`, or in `join.failures` for a [join](08-Engine-Membership.md#post-narwhalengines). A `model identity` failure does not stop checks 6 and 7, so a refused engine can list `process identity` or `new process identity`, and `generation`, in `checks`. Checks 8 and 9 run only after checks 1 to 7 pass for every engine.

Missing profiles, missing generation evidence, or a digest mismatch fail readmission with an error naming the engine to reprofile. For an engine the breaker ejected transiently, readmission accepts its current process.

A failed readmission returns one of these statuses:

|  HTTP | Meaning                                                                      |
| :---: | ---------------------------------------------------------------------------- |
| `409` | Named engine set violates a readmission rule                                 |
| `409` | A named engine is `active`, `draining`, `deadline_exceeded`, or `validating` |
| `409` | A named engine with `restart_required: true` has a null `old_process_start`  |
| `409` | Validation failed, candidate `blocked`                                       |
| `503` | `router.controls_fleet` is `false`                                           |
| `503` | The router became fenced during validation                                   |

To load updated profiles:

1. Restart the router with its hold preserved through [Activating replacement profiles](../operate/03-Restart-Engines.md#activating-replacement-profiles).
2. Repeat lifecycle readmission.

### Whole-wave restart policy

Under `recovery.engine_restart_policy: whole_wave`:

1. Drain the wave.
2. When `wave.ready_to_stop` is `true`, restart the wave through its [supervisor sequence](../operate/03-Restart-Engines.md#restart-an-engine-wave).
3. Readmit the wave.
