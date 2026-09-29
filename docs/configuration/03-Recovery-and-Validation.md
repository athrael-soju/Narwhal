# Recovery, authentication, and profile validation

## 8. Engine health and recovery

### 8.1 Breaker and drift settings

| Field                                 | Default | Meaning                                                                                                                                                         |
| ------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `recovery.eject_after`                | `3`     | Consecutive failures of one class on one engine before breaker action. At least 1.                                                                              |
| `recovery.readmit_every`              | `10`    | Monitor intervals between probes of ejected engines. At least 1.                                                                                                |
| `recovery.liveness_every`             | `10`    | Monitor intervals between health probes and, for contracted fleets, identity and attestation checks. `0` disables idle sweeps and is invalid with `whole_wave`. |
| `recovery.liveness_misses`            | `2`     | Consecutive failed liveness probes before ejection. At least 1.                                                                                                 |
| `recovery.failure_quarantine_s`       | `0.0`   | Time a failed engine remains excluded from placement. `0` disables quarantine. Nonnegative.                                                                     |
| `recovery.health.window_s`            | `30.0`  | Residual-scoring window length. At least 1 second.                                                                                                              |
| `recovery.health.drift_band`          | `2.0`   | Multiple of an engine's trailing healthy residual used as the drift threshold. Greater than 1.0.                                                                |
| `recovery.health.relative_band`       | `1.5`   | Multiple of the median peer score that bounds the fleet-wide surge veto. `0` disables it. Nonnegative.                                                           |
| `recovery.health.min_samples`         | `3`     | Samples required before scoring a window. At least 1 and at most `floor(recovery.health.window_s / controller.monitor_interval_s)`.                             |
| `recovery.health.probation_windows`   | `3`     | Consecutive drifting windows before probation. At least 1.                                                                                                      |
| `recovery.health.evict_windows`       | `5`     | Consecutive drifting windows before requesting ejection. At least `probation_windows`.                                                                          |
| `recovery.health.recovery_windows`    | `3`     | Consecutive healthy windows required to clear probation. At least 1.                                                                                            |
| `recovery.health.probation_penalty_s` | `1.5`   | Placement penalty, in seconds, for an engine on probation. Nonnegative.                                                                                         |

Narwhal adds the probation penalty to prefill placement cost in seconds. For decode placement cost it converts the penalty to tokens at the time per output token (TPOT) target. Compare the penalty with the time to first token (TTFT) target and the measured healthy placement cost.

Breaker action by failure class:

| Failure class                            | Breaker action                                                   |
| ---------------------------------------- | ---------------------------------------------------------------- |
| Connection failure                       | Counts immediately toward `recovery.eject_after`.                |
| Transport timeout                        | Triggers a health probe first.                                   |
| First-token timeout or mid-stream stall  | Requires an inference probe of the engine's prefill and decode legs. |

The [failure evidence](../concepts/03-Failure-and-State.md#failure-evidence) page lists every class. When an inference probe comes back inconclusive, Narwhal keeps the engine out of placement and retries on the readmission cadence. The state handoff preserves this placement hold. It records the producer IDs of failed KV-transfer paths in [`inference_sources`](../http-api/07-Handoff-and-Lifecycle.md#handoff-fields).

### 8.2 Decode drift evidence

The drift tracker compares fresh decode residuals and stalled inter-token gaps against each engine's recent healthy baseline. It ignores placement estimates left over after decode completes. Narwhal pauses decode correction and drift scoring for gaps that cross a prefill boundary, including prefills that start and finish between monitor passes. Client latency, role-controller load, request deadlines, and liveness checks keep observing the mixed work. When pure decode observations return, Narwhal starts a new scoring window and keeps the previous healthy baseline and probation state. `/narwhal/state` shows `health.prefill_paused` and `health.prefill_pauses`.

The fleet-wide surge veto suppresses ejection during slowdowns that affect the whole fleet. The veto applies when all of these hold:

- The engine's window score crosses its drift band.
- At least three engines have scored windows.
- At least half of the engine's scored peers also exceed their own bands.
- The engine's score stays within `recovery.health.relative_band` times the median peer score.

While the veto is in effect, the tracker withholds the drift verdict and holds the probation and recovery counters.

A window with at least one observation but fewer than `recovery.health.min_samples` closes as `undersampled`. Narwhal discards its residuals and carries the baseline and probation state into the next window. `/narwhal/state` and the `narwhal_health_windows_*_total` counters report scored and undersampled windows. `last_scored_s_ago` shows the age of the most recent health verdict.

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
| `serving.graceful_timeout_s` | `30.0`              | Uvicorn drain interval after `SIGTERM`. Nonnegative whole seconds.             |

The router starts with the split declared in the fleet configuration. When resume is on, a restart loads a compatible state handoff from `recovery.state_path` instead. An uncontracted development fleet falls back to the configured split when the saved engine set differs from the current one.

Contracted resume and automatic takeover require a state handoff schema version 1 and an accepted process identity for every engine that the saved state counts as available. An unknown schema or version aborts startup. A schema-valid handoff that fails the contracted fleet's resume checks places a whole-wave hold, and every engine then needs a managed restart.

Successful resume restores roles, ejections, lifecycle holds, and complete-backend-outage state. Per-engine dwell timestamps restart from process startup. The prefill-to-decode cooldown begins when Narwhal creates the replacement scheduler. Configure warm-standby takeover with the `narwhal-serve` options. See [Start a router pair](../operate/01-Start-Routers.md#4-start-a-router-pair) for readiness, fencing, recovery, and partition behaviour.

---

## 10. Engine authentication and protocol adapters

Ingress terminates public client credentials. Each engine request carries the configured engine Bearer credential and a router-generated `x-request-id` that is unique to every attempt and phase.

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
| `engine.engine_api_key_env` | `""`    | Environment variable holding the engine Bearer credential, resolved when Narwhal creates an engine client. A named but unset variable fails client construction. |

Serving, profiling, preflight, cache-reset, and lifecycle requests all carry this credential. Attestation requests go to the attestation sidecar URL on the trusted control network. When this field names a variable during deployment export, the engine role also receives the credential as `NARWHAL_ENGINE_API_KEY`. The engine launcher writes `VLLM_API_KEY` into a mode-0600 `container.env` and supplies that file to Docker. `/narwhal/state` reports `admission.engine_auth` as `engine-credential` when `engine.engine_api_key_env` is set. Otherwise it reports `boundary`. Keep the same authentication mode between workload measurement and production serving.

Protocol fields:

| Field              | Default  | Accepted value in this release |
| ------------------ | -------- | ------------------------------ |
| `engine.connector` | `"nixl"` | `nixl`                         |
| `engine.dialect`   | `"vllm"` | `vllm`                         |

A connector or dialect is registered after its preflight gates pass against the target engine build. Configuration validation rejects names outside the registry.

---

## 11. Profile validation

`profiles` sets the profile store path and the decode-fit limits used by `narwhal-check`.

| Field                          | Default                | Meaning                                                                                                          |
| ------------------------------ | ---------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `profiles.path`                | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`.                                               |
| `profiles.max_decode_fit_mape` | `0.05`                 | Maximum accepted in-sample decode-fit error. Positive, finite, and at most `controller.reactive.movement_margin`. |
| `profiles.max_decode_cv_mape`  | `0.13`                 | Maximum accepted leave-one-out cross-validation error. Positive and finite.                                      |

The default decode-fit limit matches the default movement margin. Profiles must cover the context and concurrency range used by the deployment. `narwhal-check` applies both limits to every configured engine. A failure names the engine and shows the measured error against the limit.
