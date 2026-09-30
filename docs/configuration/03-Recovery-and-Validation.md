# Recovery, authentication, and profile validation

## 8. Engine health and recovery

### 8.1 Breaker and drift settings

| Field                                 | Default | Meaning                                                                                                                                                         |
| ------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `recovery.eject_after`                | `3`     | Consecutive failures of one class on one engine before breaker action. At least 1.                                                                              |
| `recovery.readmit_every`              | `10`    | Monitor intervals between probes of ejected engines. At least 1.                                                                                                |
| `recovery.liveness_every`             | `10`    | Monitor intervals between health probes and, for contracted fleets, identity and attestation checks. `0` disables idle sweeps and is invalid with `whole_wave`. |
| `recovery.liveness_misses`            | `2`     | Consecutive failed liveness probes before ejection. At least 1.                                                                                                 |
| `recovery.failure_quarantine_s`       | `0.0`   | Time a failed engine stays excluded from placement. `0`, the minimum, disables quarantine.                                                                      |
| `recovery.health.window_s`            | `30.0`  | Residual-scoring window length. At least 1 second.                                                                                                              |
| `recovery.health.drift_band`          | `2.0`   | Multiple of an engine's trailing healthy residual used as the drift threshold. Greater than 1.0.                                                                |
| `recovery.health.relative_band`       | `1.5`   | Multiple of the median peer score that bounds the fleet-wide surge veto. `0`, the minimum, disables the veto.                                                   |
| `recovery.health.min_samples`         | `3`     | Samples required before scoring a window. At least 1 and at most `floor(recovery.health.window_s / controller.monitor_interval_s)`.                             |
| `recovery.health.probation_windows`   | `3`     | Consecutive drifting windows before probation. At least 1.                                                                                                      |
| `recovery.health.evict_windows`       | `5`     | Consecutive drifting windows before requesting ejection. At least `probation_windows`.                                                                          |
| `recovery.health.recovery_windows`    | `3`     | Consecutive healthy windows required to clear probation. At least 1.                                                                                            |
| `recovery.health.probation_penalty_s` | `1.5`   | Placement penalty, in seconds, for an engine on probation. At least 0.                                                                                          |

Probation penalty by placement cost:

| Placement cost | Probation penalty                                              |
| -------------- | -------------------------------------------------------------- |
| Prefill        | Added in seconds                                               |
| Decode         | Converted to tokens at the time per output token (TPOT) target |

Compare the penalty with the time to first token (TTFT) target and the measured healthy placement cost.

Breaker action by [failure class](../concepts/03-Failure-and-State.md#failure-evidence):

| Failure class                            | Breaker action                                                   |
| ---------------------------------------- | ---------------------------------------------------------------- |
| Connection failure                       | Counts immediately toward `recovery.eject_after`.                |
| Transport timeout                        | Triggers a health probe first.                                   |
| First-token timeout or mid-stream stall  | Requires an inference probe of the engine's prefill and decode legs. |

An inconclusive inference probe keeps the engine out of placement. Narwhal retries the probe every `recovery.readmit_every` monitor intervals.

The state handoff carries:

- the placement hold
- the producer IDs of failed KV-transfer paths, in [`inference_sources`](../http-api/07-Handoff-and-Lifecycle.md#handoff-fields)

### 8.2 Decode drift evidence

Drift tracker handling by observation:

| Observation                                                                                         | Drift tracker                                                                       |
| --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Fresh decode residual                                                                               | Scored against the engine's recent healthy baseline                                 |
| Stalled inter-token gap                                                                             | Scored against the engine's recent healthy baseline                                 |
| Placement estimate left over after decode completes                                                 | Excluded from scoring                                                               |
| Gap that crosses a prefill boundary, including a prefill that starts and finishes between monitor passes | Pauses decode correction and drift scoring                                     |
| First pure decode observation after a pause                                                         | Starts a new scoring window with the previous healthy baseline and probation state |

`/narwhal/state` reports pauses in `health.prefill_paused` and `health.prefill_pauses`.

The fleet-wide surge veto withholds an engine's drift verdict and holds its probation and recovery counters. The veto applies when all of these are true:

- The engine's window score crosses its drift band.
- At least three engines have scored windows.
- At least half of the engine's scored peers also exceed their own bands.
- The engine's score stays within `recovery.health.relative_band` times the median peer score.

A window that closes with at least one observation and fewer than `recovery.health.min_samples` is `undersampled`. Narwhal discards its residuals. The baseline and probation state carry into the next window.

`/narwhal/state` and the `narwhal_health_windows_*_total` counters report scored and undersampled windows. `last_scored_s_ago` is the age of the most recent health verdict.

A confirmed ejection clears the affected engine's drift history.

### 8.3 Engine restart policy

`recovery.engine_restart_policy` accepts:

- `individual` (default)
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
| `serving.graceful_timeout_s` | `30.0`              | Uvicorn drain interval after `SIGTERM`, in whole seconds. At least 0.          |

Role split at startup:

| Startup condition                                                                                          | Role split                                   |
| ---------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| Resume off                                                                                                 | Split declared in the fleet configuration    |
| Resume on, with a compatible state handoff at `recovery.state_path`                                        | Split restored from the state handoff        |
| Resume on, for an uncontracted development fleet whose saved engine set differs from the current one       | Split declared in the fleet configuration    |

Contracted resume and automatic takeover require:

- state handoff schema version 1
- an accepted process identity for every engine that the saved state counts as available

| Saved state handoff                                      | Startup result                                           |
| -------------------------------------------------------- | -------------------------------------------------------- |
| Unknown schema or version                                | Startup aborts.                                          |
| Schema-valid, and fails the contracted fleet's resume checks | Whole-wave hold. Every engine needs a managed restart. |

| State                                                              | On successful resume                                  |
| ------------------------------------------------------------------ | ----------------------------------------------------- |
| Roles, ejections, lifecycle holds, complete-backend-outage state   | Restored                                              |
| Per-engine dwell timestamps                                        | Restart from process startup                          |
| Prefill-to-decode cooldown                                         | Begins when Narwhal creates the replacement scheduler |

Configure warm-standby takeover with the `narwhal-serve` options in [Start a router pair](../operate/01-Start-Routers.md#4-start-a-router-pair).

---

## 10. Engine authentication and protocol adapters

Ingress terminates public client credentials. Each engine request carries:

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

| Field                       | Default | Meaning                                                                                                                                                              |
| --------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `engine.engine_api_key_env` | `""`    | Environment variable that holds the engine Bearer credential. Narwhal resolves it when it creates an engine client. A named variable that is unset fails client construction. |

| Requests                                              | Target                                                  | Engine credential |
| ----------------------------------------------------- | ------------------------------------------------------- | ----------------- |
| Serving, profiling, preflight, cache-reset, lifecycle | Engine URL                                              | Bearer header     |
| Attestation                                           | Attestation sidecar URL on the trusted control network  |                   |

When `engine.engine_api_key_env` names a variable at deployment export, the credential reaches the engine as:

| Location                                                             | Variable                 |
| -------------------------------------------------------------------- | ------------------------ |
| Engine role environment                                              | `NARWHAL_ENGINE_API_KEY` |
| Mode-0600 `container.env` that the engine launcher supplies to Docker | `VLLM_API_KEY`           |

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

Configuration validation rejects any other value.

---

## 11. Profile validation

`profiles` sets the profile store path and the decode-fit limits used by `narwhal-check`.

| Field                          | Default                | Meaning                                                                                                          |
| ------------------------------ | ---------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `profiles.path`                | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`.                                               |
| `profiles.max_decode_fit_mape` | `0.05`                 | Maximum accepted in-sample decode-fit error. Positive, finite, and at most `controller.reactive.movement_margin`. |
| `profiles.max_decode_cv_mape`  | `0.13`                 | Maximum accepted leave-one-out cross-validation error. Positive and finite.                                      |

Profiles must cover the context and concurrency range used by the deployment. `narwhal-check` applies both limits to every configured engine. A failure names the engine and shows the measured error against the limit.
