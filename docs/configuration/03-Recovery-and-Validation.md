---
description: Narwhal fleet settings for engine recovery, warm standby, engine authentication and profile validation.
---

# Recovery, authentication, and profile validation

## 8. Engine health and recovery

### 8.1 Breaker and drift settings

| Field                                 | Default | Meaning                                                                                             | Valid values                                                                |
| ------------------------------------- | :-----: | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `recovery.eject_after`                | `3`     | Consecutive failures of one class on one engine before breaker action                               | At least 1                                                                  |
| `recovery.readmit_every`              | `10`    | Monitor intervals between probes of ejected engines                                                 | At least 1                                                                  |
| `recovery.liveness_every`             | `10`    | Monitor intervals between health probes and, for contracted fleets, identity and attestation checks | `0` disables idle sweeps                                                    |
| `recovery.liveness_misses`            | `2`     | Consecutive failed liveness probes before ejection                                                  | At least 1                                                                  |
| `recovery.failure_quarantine_s`       | `0.0`   | Time a failed engine stays excluded from placement                                                  | At least 0, where `0` disables quarantine                                   |
| `recovery.health.window_s`            | `30.0`  | Residual-scoring window length                                                                      | At least 1 second                                                           |
| `recovery.health.drift_band`          | `2.0`   | Multiple of an engine's trailing healthy residual used as the drift threshold                       | Greater than 1.0                                                            |
| `recovery.health.relative_band`       | `1.5`   | Multiple of the median peer score that bounds the fleet-wide surge veto                             | At least 0, where `0` disables the veto                                     |
| `recovery.health.min_samples`         | `3`     | Samples required before scoring a window                                                            | From 1 to `floor(recovery.health.window_s / controller.monitor_interval_s)` |
| `recovery.health.probation_windows`   | `3`     | Consecutive drifting windows before probation                                                       | At least 1                                                                  |
| `recovery.health.evict_windows`       | `5`     | Consecutive drifting windows before requesting ejection                                             | At least `probation_windows`                                                |
| `recovery.health.recovery_windows`    | `3`     | Consecutive healthy windows required to clear probation                                             | At least 1                                                                  |
| `recovery.health.probation_penalty_s` | `1.5`   | Placement penalty in seconds for an engine on probation                                             | At least 0                                                                  |

Probation penalty by placement cost:

| Placement cost | Probation penalty                                              |
| -------------- | -------------------------------------------------------------- |
| Prefill        | Added in seconds                                               |
| Decode         | Converted to tokens at the time per output token (TPOT) target |

Compare the penalty with the time to first token (TTFT) target and the measured healthy placement cost.

Breaker action at `recovery.eject_after` consecutive failures of one [failure class](../concepts/03-Failure-and-State.md#failure-evidence):

| Failure class                                 | Breaker action                                                  |
| --------------------------------------------- | --------------------------------------------------------------- |
| `connection`                                  | Ejects the engine                                               |
| `timeout` or `overload`                       | Runs a health probe                                             |
| `stream`, `inference_status`, or `kv_handoff` | Runs an inference probe of the engine's prefill and decode legs |

An inconclusive inference probe:

- keeps the breaker's placement hold on an engine whose roles other live engines place
- repeats every `recovery.readmit_every` monitor intervals

The state handoff carries the producer IDs of failed KV-transfer paths in [`inference_sources`](../http-api/07-Handoff-and-Lifecycle.md#handoff-fields).

For each `inference_sources` suspect whose roles other live engines place, restoring the state handoff:

- ejects the suspect
- makes its readmission probe due immediately

### 8.2 Decode drift evidence

Drift tracker handling by observation:

| Observation                                         | Drift tracker                                                                      |
| --------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Fresh decode residual                               | Scored against the engine's recent healthy baseline                                |
| Stalled inter-token gap                             | Scored against the engine's recent healthy baseline                                |
| Placement estimate left over after decode completes | Excluded from scoring                                                              |
| Gap that crosses a prefill boundary                 | Pauses decode correction and drift scoring                                         |
| First pure decode observation after a pause         | Starts a new scoring window with the previous healthy baseline and probation state |

`/narwhal/state` reports pauses in `health.<iid>.prefill_paused` and `health.<iid>.prefill_pauses`.

The fleet-wide surge veto withholds an engine's drift verdict when all of these are true:

- The engine's window score crosses its drift band.
- At least three engines have scored windows.
- At least half of the engine's scored peers exceed the peers' bands.
- The engine's score stays within `recovery.health.relative_band` times the median peer score.

A window that closes with at least one observation and fewer than `recovery.health.min_samples` is `undersampled`.

| Evidence                             | `/narwhal/state` field                                | Prometheus counter                                                                    |
| ------------------------------------ | ----------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Scored and undersampled windows      | `health.<iid>.scored` and `health.<iid>.undersampled` | `narwhal_health_windows_scored_total` and `narwhal_health_windows_undersampled_total` |
| Age of the most recent scored window | `health.<iid>.last_scored_s_ago`                      |                                                                                       |

### 8.3 Engine restart policy

`recovery.engine_restart_policy` accepts:

- `individual`, the default
- `whole_wave`

`whole_wave` requires:

- a complete `engine_contract`
- `recovery.liveness_every > 0`

Under `whole_wave`, an ejection or identity failure places a whole-wave hold on the fleet until an operator completes the [engine-wave restart](../operate/03-Restart-Engines.md#8-restart-an-engine-wave).

---

## 9. Resume, shutdown, and warm-standby state

| Field                        | Default             | Meaning                                                                        |
| ---------------------------- | ------------------- | ------------------------------------------------------------------------------ |
| `recovery.state_path`        | `"runs/state.json"` | Atomic state handoff file for roles, ejections, lifecycle state, and counters. |
| `recovery.resume`            | `false`             | Applies a compatible state handoff file at startup.                            |
| `serving.graceful_timeout_s` | `30.0`              | Uvicorn drain interval after `SIGTERM`, in zero or more whole seconds.         |

Role split at startup:

| Startup condition                                                                                    | Role split                                |
| ---------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| Resume off                                                                                           | Split declared in the fleet configuration |
| Resume on, with a compatible state handoff at `recovery.state_path`                                  | Split restored from the state handoff     |
| Resume on, for an uncontracted development fleet whose saved engine set differs from the current one | Split declared in the fleet configuration |

Contracted resume and automatic takeover require:

- state handoff schema version 1
- an accepted process identity for every engine that the saved state counts as available

| Saved state handoff                                           | Startup result                                              |
| ------------------------------------------------------------- | ----------------------------------------------------------- |
| Unknown schema or version                                     | Startup aborts                                              |
| Schema-valid and failing the contracted fleet's resume checks | Whole-wave hold requiring a managed restart of every engine |

| State                                                            | On successful resume                                  |
| ---------------------------------------------------------------- | ----------------------------------------------------- |
| Roles, ejections, lifecycle holds, complete-backend-outage state | Restored                                              |
| Per-engine dwell timestamps                                      | Cleared                                               |
| Prefill-to-decode cooldown                                       | Begins when Narwhal creates the replacement scheduler |

Configure warm-standby takeover with the `narwhal-serve` options in [Start a router pair](../operate/01-Start-Routers.md#4-start-a-router-pair).

---

## 10. Engine authentication and protocol adapters

Ingress terminates public client credentials.

Each engine request carries:

- the configured engine Bearer credential
- a router-generated `x-request-id`, unique to every attempt and phase

Set the credential variable under `engine`:

```json
{
  "engine": {
    "engine_api_key_env": "NARWHAL_ENGINE_KEY"
  }
}
```

| Field                       | Default | Meaning                                                          |
| --------------------------- | ------- | ---------------------------------------------------------------- |
| `engine.engine_api_key_env` | `""`    | Environment variable that must hold the engine Bearer credential |

| Requests                                                  | Target                                                 | Credential               |
| --------------------------------------------------------- | ------------------------------------------------------ | ------------------------ |
| Serving, profiling, preflight, cache-reset, and lifecycle | Engine URL                                             | Engine Bearer credential |
| Attestation                                               | Attestation sidecar URL on the trusted control network |                          |

When `engine.engine_api_key_env` names a variable at deployment export, the credential reaches the engine as:

| Location                                                              | Variable                 |
| --------------------------------------------------------------------- | ------------------------ |
| Engine role environment                                               | `NARWHAL_ENGINE_API_KEY` |
| Mode-0600 `container.env` that the engine launcher supplies to Docker | `VLLM_API_KEY`           |
| Mode-0600 `engine.env` for a native engine                            | `VLLM_API_KEY`           |

`/narwhal/state` reports the authentication mode in `admission.engine_auth`:

| `engine.engine_api_key_env` | `admission.engine_auth` |
| --------------------------- | ----------------------- |
| Set                         | `engine-credential`     |
| Empty                       | `boundary`              |

Use the same authentication mode for workload measurement and production serving.

Protocol fields:

| Field              | Default  | Accepted value in this release |
| ------------------ | -------- | ------------------------------ |
| `engine.connector` | `"nixl"` | `nixl`                         |
| `engine.dialect`   | `"vllm"` | `vllm`                         |

---

## 11. Profile validation

| Field                          | Default                | Meaning                                                                   | Values                                                  |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------- |
| `profiles.path`                | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`         |                                                         |
| `profiles.max_decode_fit_mape` | `0.05`                 | Maximum in-sample decode-fit error that `narwhal-check` accepts           | Positive, at most `controller.reactive.movement_margin` |
| `profiles.max_decode_cv_mape`  | `0.13`                 | Maximum leave-one-out cross-validation error that `narwhal-check` accepts | Positive                                                |

Profiles must cover the context and concurrency range used by the deployment.

A decode-fit failure in `narwhal-check` names the engine, the measured error, and the limit.
