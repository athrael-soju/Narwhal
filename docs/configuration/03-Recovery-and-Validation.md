# Recovery, authentication, and profile validation

## 8. Engine health and recovery

### 8.1 Breaker and drift settings

| Field                                 | Default | Meaning                                                                                                                                                         |
| ------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `recovery.eject_after`                | `3`     | Consecutive failed legs before breaker action. At least 1.                                                                                                      |
| `recovery.readmit_every`              | `10`    | Monitor intervals between probes of ejected engines. At least 1.                                                                                                |
| `recovery.liveness_every`             | `10`    | Monitor intervals between health probes and, for contracted fleets, identity and attestation checks. `0` disables idle sweeps and is invalid with `whole_wave`. |
| `recovery.liveness_misses`            | `2`     | Consecutive failed liveness probes before ejection. At least 1.                                                                                                 |
| `recovery.health.window_s`            | `30.0`  | Residual-scoring window length. At least 1 second.                                                                                                              |
| `recovery.health.drift_band`          | `2.0`   | Multiple of an engine's trailing healthy residual used as the drift threshold. Greater than 1.0.                                                                |
| `recovery.health.relative_band`       | `1.5`   | Peer-relative multiple that can override the fleet-surge veto. `0` disables the veto. Nonnegative.                                                              |
| `recovery.health.min_samples`         | `3`     | Samples required before scoring a window. At least 1 and bounded by the monitor/window relation above.                                                          |
| `recovery.health.probation_windows`   | `3`     | Consecutive drifting windows before probation. At least 1.                                                                                                      |
| `recovery.health.evict_windows`       | `5`     | Consecutive drifting windows before requesting ejection. At least `probation_windows`.                                                                          |
| `recovery.health.recovery_windows`    | `3`     | Consecutive healthy windows required to clear probation. At least 1.                                                                                            |
| `recovery.health.probation_penalty_s` | `1.5`   | Placement penalty during probation. Nonnegative.                                                                                                                |

Connection failures count immediately toward `recovery.eject_after`.

Transport timeouts trigger a health probe first.

First-token timeouts and mid-stream stalls require functional verification of the affected inference path.

If verification is inconclusive, Narwhal keeps the hold and retries on the readmission cadence.

Router handoff keeps both the hold and the failed transfer paths.

### 8.2 Decode drift evidence

The drift tracker compares:

- fresh decode residuals
- stalled inter-token gaps

against each engine's recent healthy baseline.

Placement estimates remaining after decode completion are excluded from health evidence.

Local prefill work invalidates the decode-only profile for the affected interval. Narwhal pauses decode correction and drift scoring for gaps that cross a prefill boundary, including prefills that both start and finish between monitor passes.

When pure decode observations return, Narwhal starts a new scoring window while retaining:

- the previous healthy baseline
- probation state

Client latency, controller pressure, request deadlines, and liveness checks continue to observe mixed work while decode drift scoring is paused.

`/narwhal/state` exposes:

- `health.prefill_paused`
- `health.prefill_pauses`

The peer-relative test suppresses ejection during fleet-wide slowdowns.

A window with at least one observation and fewer than `recovery.health.min_samples` closes as `undersampled`. Narwhal discards its residuals and carries the baseline and probation state into the next window.

`/narwhal/state` and `narwhal_health_windows_*_total` report scored and undersampled windows.

`last_scored_s_ago` reports time since the most recent health verdict.

Confirmed ejection clears the affected engine's drift history.

### 8.3 Engine restart policy

`recovery.engine_restart_policy` accepts:

- `individual` (default)
- `whole_wave`

`whole_wave` requires:

- a complete `engine_contract`
- `recovery.liveness_every > 0`

Under `whole_wave`, an ejection or identity failure holds the fleet until an operator completes the [engine-wave procedure](../operate/03-Restart-Engines.md#8-restart-an-engine-wave).

`recovery.failure_quarantine_s` keeps failed engines out of placement while health state converges.

---

## 9. Resume, shutdown, and warm-standby state

| Field                        | Default             | Meaning                                                                  |
| ---------------------------- | ------------------- | ------------------------------------------------------------------------ |
| `recovery.state_path`        | `"runs/state.json"` | Atomic handoff file for roles, ejections, lifecycle state, and counters. |
| `recovery.resume`            | `false`             | Applies a compatible handoff file at startup.                            |
| `serving.graceful_timeout_s` | `30.0`              | Uvicorn drain interval after `SIGTERM`. Nonnegative.                     |

With a saved handoff, the router resumes from `recovery.state_path`.

Without a handoff, startup uses the split declared in fleet configuration.

An uncontracted development fleet also falls back to the configured split if the saved engine set differs from the current one.

Contracted resume and automatic takeover require:

- handoff schema version 1
- accepted process identities for every available engine

An unknown handoff schema or version aborts startup.

When a schema-valid handoff fails the contracted fleet's resume checks, Narwhal holds the fleet for a managed wave.

Successful resume restores:

- roles
- ejections
- lifecycle holds
- complete-backend-outage state

Per-engine dwell timestamps restart from process startup.

The prefill-to-decode cooldown starts when the replacement scheduler is created.

Warm-standby takeover remains a CLI concern because router IDs and shared lease paths vary by host.

[Operate Narwhal](../operate/01-Start-Routers.md#4-start-a-router-pair) defines readiness, fencing, recovery, and partition behaviour.

---

## 10. Engine authentication and protocol adapters

Ingress terminates public client credentials.

For engine requests, Narwhal attaches:

- the configured engine Bearer credential
- a router-generated `x-request-id` unique to every attempt and phase

Configure engine authentication as:

```json
{
  "engine": {
    "engine_api_key_env": "NARWHAL_ENGINE_KEY"
  }
}
```

| Field                       | Default | Meaning                                                                                                                                                                                                                                                                                     |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `engine.engine_api_key_env` | `""`    | Environment-variable name resolved when an engine client is created. Serving, profiling, preflight, cache-reset, and lifecycle requests carry its Bearer credential. Attestation uses the sidecar URL on the trusted control network. A named but unset variable fails client construction. |

When this field names a credential during deployment export, the engine role also receives it as `NARWHAL_ENGINE_API_KEY`.

The engine launcher writes `VLLM_API_KEY` into mode-0600 `container.env` and supplies that file to Docker.

`/narwhal/state` reports either:

- `boundary`
- `engine-credential`

under `admission.engine_auth`.

Keep the same authentication mode between workload measurement and production serving.

Protocol selection is configured as:

| Field              | Default  | Accepted value in this release |
| ------------------ | -------- | ------------------------------ |
| `engine.connector` | `"nixl"` | `nixl`                         |
| `engine.dialect`   | `"vllm"` | `vllm`                         |

A connector or dialect is registered only after its preflight gates pass against the target engine build.

Unknown names fail configuration validation.

---

## 11. Profile validation

The `profiles` object selects the profile store and decode-fit acceptance limits used by `narwhal-check`.

| Field                          | Default | Meaning                                                                                                                   |
| ------------------------------ | ------- | ------------------------------------------------------------------------------------------------------------------------- |
| `profiles.max_decode_fit_mape` | `0.05`  | Maximum accepted in-sample decode-fit error. Positive, finite, and at most `controller.reactive.movement_margin`.            |
| `profiles.max_decode_cv_mape`  | `0.13`  | Maximum accepted leave-one-out cross-validation error. Positive and finite.                                               |

The default decode-fit limit equals the default movement margin.

Profiles must cover the context and concurrency range used by the deployment.

`narwhal-check` applies both limits to every configured engine. A failure reports:

- engine identity
- measured error
- configured limit
