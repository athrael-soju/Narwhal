# Recovery, authentication, and profile validation

## 8. Engine health and recovery

### 8.1 Breaker and drift settings

| Field                                 | Default | Meaning                                                                                                                                                         |
| ------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `recovery.eject_after`                | `3`     | Consecutive failed legs before the breaker acts. At least 1.                                                                                                    |
| `recovery.readmit_every`              | `10`    | Monitor intervals between probes of ejected engines. At least 1.                                                                                                |
| `recovery.liveness_every`             | `10`    | Monitor intervals between health probes and, for contracted fleets, identity and attestation checks. `0` disables idle sweeps and is invalid with `whole_wave`. |
| `recovery.liveness_misses`            | `2`     | Consecutive failed liveness probes before ejection. At least 1.                                                                                                 |
| `recovery.health.window_s`            | `30.0`  | Residual-scoring window length. At least 1 second.                                                                                                       |
| `recovery.health.drift_band`          | `2.0`   | Drift threshold as a multiple of the engine's trailing healthy residual. Greater than 1.0.                                                                |
| `recovery.health.relative_band`       | `1.5`   | Peer-relative multiple that can override the fleet-surge veto. `0` disables the veto. Nonnegative.                                                              |
| `recovery.health.min_samples`         | `3`     | Samples required to score a window. At least 1, and no more than the monitor/window bound in [§7](02-Serving-and-Role-Control.md#7-role-control).         |
| `recovery.health.probation_windows`   | `3`     | Consecutive drifting windows before probation. At least 1.                                                                                                      |
| `recovery.health.evict_windows`       | `5`     | Consecutive drifting windows before ejection is requested. At least `probation_windows`.                                                                        |
| `recovery.health.recovery_windows`    | `3`     | Consecutive healthy windows required to clear probation. At least 1.                                                                                              |
| `recovery.health.probation_penalty_s` | `1.5`   | Placement penalty while on probation. Nonnegative.                                                                                                              |

The probation penalty is added to prefill placement cost in seconds. Before changing it, compare it with the fleet's TTFT target and with the placement cost you measure on healthy engines.

A connection failure counts toward `recovery.eject_after` at once. A transport timeout first triggers a health probe. First-token timeouts and mid-stream stalls require a functional check of the inference path that failed. If that check is inconclusive, the engine stays out of placement and Narwhal tries again on the readmission cadence. Router handoff carries this placement hold across and records the producer IDs of failed KV-transfer paths in [`inference_sources`](../http-api/07-Handoff-and-Lifecycle.md#handoff-fields).

### 8.2 Decode drift evidence

The drift tracker compares each engine's fresh decode residuals and stalled inter-token gaps with that engine's recent healthy baseline. Placement estimates still outstanding when decode completes are not counted as health evidence. The peer-relative test suppresses ejection when the whole fleet slows down at once.

Prefill work on an engine invalidates its decode-only profile for as long as that work runs. Decode correction and drift scoring are therefore paused for any gap that crosses a prefill boundary, including a prefill that starts and finishes between two monitor passes. When decode-only observations resume, scoring opens a new window and keeps the previous healthy baseline and probation state. Client latency, controller pressure, request deadlines, and liveness checks keep observing the mixed work the whole time. The pauses appear in `/narwhal/state` as `health.prefill_paused` and `health.prefill_pauses`.

A window with at least one observation but fewer than `recovery.health.min_samples` closes as `undersampled`. Its residuals are discarded, and the baseline and probation state carry forward into the next window. Scored and undersampled windows are counted in `/narwhal/state` and in `narwhal_health_windows_*_total`, and `last_scored_s_ago` reports the time since the most recent health verdict. A confirmed ejection clears the engine's drift history.

### 8.3 Engine restart policy

`recovery.engine_restart_policy` is `individual` (default) or `whole_wave`. Choosing `whole_wave` requires a complete `engine_contract` and a `recovery.liveness_every` greater than zero. Under that policy, any ejection or identity failure holds the whole fleet until an operator completes the [engine-wave procedure](../operate/03-Restart-Engines.md#8-restart-an-engine-wave).

Independently of the policy, `recovery.failure_quarantine_s` (see [§6](02-Serving-and-Role-Control.md#6-request-deadlines-and-engine-http-behavior)) keeps failed engines out of placement while their health state settles.

---

## 9. Resume, shutdown, and warm-standby state

| Field                        | Default             | Meaning                                                                      |
| ---------------------------- | ------------------- | ---------------------------------------------------------------------------- |
| `recovery.state_path`        | `"runs/state.json"` | Atomic handoff file holding roles, ejections, lifecycle state, and counters. |
| `recovery.resume`            | `false`             | Load a compatible handoff file at startup.                                |
| `serving.graceful_timeout_s` | `30.0`              | Uvicorn drain interval after `SIGTERM`. Nonnegative whole number of seconds.           |

On first start, the router uses the split declared in the fleet configuration. On a restart with resume enabled, it loads the handoff from `recovery.state_path` if the file is compatible. An unknown handoff schema or version aborts startup.

An uncontracted development fleet also returns to the configured split when the saved engine set differs from the current one. For contracted fleets, resume and automatic takeover need handoff schema version 1 and an accepted process identity for every available engine, and a schema-valid handoff that fails those checks puts the fleet on hold for a managed wave.

A successful resume restores roles, ejections, lifecycle holds, and complete-backend-outage state. Per-engine dwell timestamps restart from process startup, and the prefill-to-decode cooldown starts when the replacement scheduler is created.

Warm-standby takeover is handled from the command line, because router IDs and shared lease paths vary from host to host. [Operate Narwhal](../operate/01-Start-Routers.md#4-start-a-router-pair) covers readiness, fencing, recovery, and partition behavior.

---

## 10. Engine authentication and protocol adapters

Client credentials are not forwarded to engines. Requests to engines carry the engine Bearer credential and a new `x-request-id` for each attempt and phase. Set the name of the environment variable that holds the credential:

```json
{
  "engine": {
    "engine_api_key_env": "NARWHAL_ENGINE_KEY"
  }
}
```

| Field                       | Default | Meaning                                                                                                                                                                                                                                                                                                                   |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `engine.engine_api_key_env` | `""`    | Name of the environment variable read when an engine client is created. Serving, profiling, preflight, cache-reset, and lifecycle requests carry its Bearer credential. Attestation requests go to the sidecar URL on the trusted control network. If the variable is named but unset, client construction fails. |

When this field names a credential, deployment export also passes it to the engine role as `NARWHAL_ENGINE_API_KEY`. The engine launcher then writes `VLLM_API_KEY` into a mode-0600 `container.env` and hands that file to Docker. `/narwhal/state` shows the active mode under `admission.engine_auth` as either `boundary` or `engine-credential`. Use the same mode for workload measurement as for production serving.

Protocol selection is configured with two fields:

| Field              | Default  | Accepted value in this release |
| ------------------ | -------- | ------------------------------ |
| `engine.connector` | `"nixl"` | `nixl`                         |
| `engine.dialect`   | `"vllm"` | `vllm`                         |

Unknown names fail configuration validation. A connector or dialect is registered only if its preflight checks pass against the target engine build.

---

## 11. Profile validation

The `profiles` object selects the profile store and sets the decode-fit limits that `narwhal-check` enforces.

| Field                          | Default | Meaning                                                                                                                                     |
| ------------------------------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `profiles.max_decode_fit_mape` | `0.05`  | Largest accepted in-sample decode-fit error. Must be positive, finite, and no greater than `controller.reactive.movement_margin` (default 0.05). |
| `profiles.max_decode_cv_mape`  | `0.13`  | Largest accepted leave-one-out cross-validation error. Positive and finite.                                                                 |

`narwhal-check` applies both limits to every configured engine. When a limit fails, it reports the engine, the measured error, and the configured limit. Profiles need to cover the full range of context lengths and concurrency the deployment will see.
