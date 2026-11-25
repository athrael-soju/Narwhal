---
description: List, join and remove the engines a Narwhal router serves through the /narwhal/engines routes.
---

# Engine membership

The router serves the engines in its membership snapshot. Generation `0` holds the fleet file's `engines`. Each join or removal replaces the snapshot with the next generation. [Joining and removing engines](../operate/08-Joining-and-Removing-Engines.md) gives the operator procedure.

## Membership document

Every route on this page returns the `narwhal.membership` schema version `1` document, except the HTTP `422` for a `deadline_s` that is not a number, which carries FastAPI's validation body with `detail`.

| Field            | Meaning                                                                                                   |
| ---------------- | --------------------------------------------------------------------------------------------------------- |
| `schema`         | `narwhal.membership`                                                                                      |
| `schema_version` | Membership schema version, `1`                                                                            |
| `generation`     | Snapshot generation                                                                                       |
| `engines`        | Member engine entries in snapshot order, with `${VARIABLE}` references resolved                           |
| `leaving`        | Sorted IDs of engines draining before removal                                                             |
| `error`          | Refused request's message, empty otherwise                                                                |
| `join`           | Join result, or `null` when the router did not run a join                                                 |

Each `engines` entry holds `iid`, `url` and `role`, plus `pin`, `attestation_url` and `shared_device` when the engine sets them.

`join` fields:

| Field      | Meaning                                                                 |
| ---------- | ----------------------------------------------------------------------- |
| `iid`      | Engine ID from the request                                              |
| `joined`   | `true` when the engine entered the fleet                                |
| `checks`   | Checks the engine passed                                                |
| `failures` | Failed checks, empty when the engine joined or a precondition refused it |

## `GET /narwhal/engines`

Returns HTTP `200` with the router's snapshot:

```json
{
  "schema": "narwhal.membership",
  "schema_version": 1,
  "generation": 1,
  "engines": [
    {"iid": "e0", "url": "http://node0:8000", "role": "prefill", "attestation_url": "http://node0:8010/v1/attestation"},
    {"iid": "e1", "url": "http://node1:8000", "role": "decode", "attestation_url": "http://node1:8010/v1/attestation"},
    {"iid": "e4", "url": "http://node4:8000", "role": "decode", "attestation_url": "http://node4:8010/v1/attestation"}
  ],
  "leaving": [],
  "error": "",
  "join": null
}
```

A standby router returns its own snapshot. When it takes over, it adopts the active router's saved snapshot as [Membership in the handoff](07-Handoff-and-Lifecycle.md#membership-in-the-handoff) describes.

## `POST /narwhal/engines`

Validates one engine and adds it to the fleet. The body is one engine entry as the fleet file holds it:

| Field             | Type    | Default    | Meaning                                                                                         |
| ----------------- | ------- | ---------- | ----------------------------------------------------------------------------------------------- |
| `iid`             | String  | required   | Engine ID, unique in the membership                                                             |
| `url`             | String  | required   | Engine HTTP base URL                                                                            |
| `role`            | String  | `decode`   | Role the engine joins in: `prefill` or `decode`                                                 |
| `pin`             | Boolean | `false`    | Keep the engine in its role                                                                     |
| `attestation_url` | String  | `""`       | Attestation sidecar URL, required to join                                                       |
| `shared_device`   | Object  | `null`     | Shared-GPU allocation. An engine that sets it cannot join                                       |

```json
{
  "iid": "e4",
  "url": "http://node4:8000",
  "attestation_url": "http://node4:8010/v1/attestation",
  "role": "decode"
}
```

The router answers HTTP `422`, with the reason in `error` and `join` `null`, when the body:

- is not JSON, or not a JSON object
- has an unknown key, lacks `iid` or `url`, holds a value of the wrong type, or sets `role` to a value other than `prefill` or `decode`
- has an `iid` that does not match `[A-Za-z0-9._-]+`
- has a `url`, or a nonempty `attestation_url`, that is not an `http` or `https` URL with a host
- contains `${` anywhere

The router refuses a valid entry before it contacts any engine when:

- `recovery.engine_restart_policy` is `whole_wave`
- the fleet's `engine_contract` is incomplete
- `iid` names a member
- another member serves `url`
- `shared_device` is set
- `attestation_url` is empty
- `pin` is `false` while `engine.connector` keeps the backend's engine roles fixed, the case in which fleet validation requires `pin` on every engine
- an engine's lifecycle `state` is not `active`

Otherwise the router runs these checks with the engine out of placement:

1. It reads the new engine's attestation and checks it against `engine_contract`.
2. It reads `profiles.path` again. The store must hold a profile for exactly the members and the new engine, and each profile must match the process generation of its live engine.
3. When `engine.first_token_calibration_path` is set, it reads that artifact again. The artifact must cover every directed pair of the members and the new engine, and match the process generation of each live engine.
4. It runs the [readmission checks](07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) on the new engine in its requested role, with `process identity` in place of `new process identity`. The `model identity` check requires the new engine to attest the model file digests of every member that is not ejected or draining.

When every check passes, the router loads the store and the calibration it read, adds the engine to the next generation with no resident work, and writes the state handoff. A failed handoff write logs a warning, and the next monitoring pass writes the handoff again.

|  HTTP | Meaning                                                                                                   |
| :---: | --------------------------------------------------------------------------------------------------------- |
| `200` | The engine joined, and `generation` increased by one                                                      |
| `409` | A precondition refused the join, named in `error`, with `join.failures` empty                             |
| `409` | A check failed, with `join.failures` listing the failures and `join.checks` the checks that passed        |
| `422` | The body is not a valid engine entry, named in `error`, with `join` `null`                                |
| `503` | `router.controls_fleet` is `false`                                                                        |
| `503` | The router lost fleet control during the checks                                                           |

## `DELETE /narwhal/engines/{iid}`

Drains the engine and removes it from the fleet. The query parameter `deadline_s` sets the drain deadline in seconds. It defaults to `300` and must be positive and finite.

The drain holds the engine out of placement and records its process identity, as [`POST /narwhal/lifecycle/drain`](07-Handoff-and-Lifecycle.md#post-narwhallifecycledrain) does. On each monitoring pass, the router removes a drained engine that has no resident requests and from which no decode leg, placed or waiting for a seat, still reads KV. At the deadline, the engine's lifecycle `state` becomes `deadline_exceeded`, and the removal still waits for its resident requests to finish. The router does not stop the engine process.

The router refuses the removal when:

- `recovery.engine_restart_policy` is `whole_wave`
- the fleet's `engine_contract` is incomplete
- the engine sets `shared_device`
- a lifecycle action holds any engine, except when it holds only this engine, outside a wave, in `drained`, `blocked` or `deadline_exceeded`
- no other engine is eligible for placement
- fewer engines than `controller.min_prefill` plus `controller.min_decode` would remain

|  HTTP | Meaning                                                                                       |
| :---: | --------------------------------------------------------------------------------------------- |
| `200` | The engine left at once, and `generation` increased by one                                    |
| `202` | The engine is draining, and `leaving` lists it                                                |
| `404` | `iid` names no member                                                                         |
| `409` | A rule above refused the removal, or `deadline_s` is zero, negative, or infinite              |
| `422` | `deadline_s` is not a number                                                                  |
| `503` | `router.controls_fleet` is `false`, or the router lost fleet control during the request       |
| `503` | Process-identity capture failed, with the engine held out of placement and `error` naming the cause |

After a process-identity capture failure, the engine stays out of placement, and [readmission](07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) refuses it because it has no recorded process identity. Repeat the request once the engine and its attestation sidecar answer. Readmission ends a leave.

While the engine drains, a repeated request answers HTTP `202` and keeps the drain's deadline, and [`GET /narwhal/lifecycle`](07-Handoff-and-Lifecycle.md#get-narwhallifecycle) reports its record with `leaving: true`.
