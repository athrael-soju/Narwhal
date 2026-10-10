---
description: Narwhal fleet settings for engine recovery, warm standby, engine authentication and profile validation.
---

# Recovery, authentication, and profile validation

## 8. Engine health and recovery

### 8.1 Breaker and drift settings

These fields set breaker thresholds, liveness probing, failure quarantine and drift scoring.

| Field                                 | Default | Meaning                                                                                             | Valid values                                                                |
| ------------------------------------- | :-----: | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `recovery.eject_after`                | `3`     | Consecutive failures of one class on one engine before breaker action                               | At least 1                                                                  |
| `recovery.readmit_every`              | `10`    | Monitor intervals between probes of ejected engines                                                 | At least 1                                                                  |
| `recovery.liveness_every`             | `10`    | Monitor intervals between health probes and, for contracted fleets, identity and attestation checks | `0` disables idle sweeps                                                    |
| `recovery.liveness_misses`            | `2`     | Consecutive failed liveness probes before ejection                                                  | At least 1                                                                  |
| `recovery.failure_quarantine_s`       | `0.0`   | Time a failed engine stays excluded from placement while other live engines place its roles         | At least 0, where `0` disables quarantine                                   |
| `recovery.health.window_s`            | `30.0`  | Residual-scoring window length                                                                      | At least 1 second                                                           |
| `recovery.health.drift_band`          | `2.0`   | Multiple of an engine's trailing healthy residual used as the drift threshold                       | Greater than 1.0                                                            |
| `recovery.health.relative_band`       | `1.5`   | Multiple of the median peer score that bounds the fleet-wide surge veto                             | At least 0, where `0` disables the veto                                     |
| `recovery.health.min_samples`         | `3`     | Samples required before scoring a window                                                            | From 1 to `floor(recovery.health.window_s / controller.monitor_interval_s)` |
| `recovery.health.probation_windows`   | `3`     | Consecutive drifting windows before probation                                                       | At least 1                                                                  |
| `recovery.health.evict_windows`       | `5`     | Consecutive drifting windows before requesting ejection                                             | At least `probation_windows`                                                |
| `recovery.health.recovery_windows`    | `3`     | Consecutive healthy windows required to clear probation                                             | At least 1                                                                  |
| `recovery.health.probation_penalty_s` | `1.5`   | Placement penalty in seconds for an engine on probation                                             | At least 0                                                                  |

The probation penalty adds to a prefill placement cost in seconds. For a decode placement cost, Narwhal converts it to tokens at the time per output token (TPOT) target.

Compare the penalty with the time to first token (TTFT) target and the measured healthy placement cost.

At `recovery.eject_after` consecutive failures of one class, the breaker runs the action that [failure evidence](../concepts/03-Failure-and-State.md#failure-evidence) lists for that class.

[Failure quarantine settings](../operate/07-Admission-Queue-and-Retry-Settings.md#failure-quarantine) compares placement and client outcomes with `recovery.failure_quarantine_s` at `0` and above it.

An inconclusive inference probe:

- keeps the breaker's placement hold on a [covered engine](../concepts/03-Failure-and-State.md#failure-evidence)
- repeats every `recovery.readmit_every` monitor intervals

The state handoff carries the producer IDs of failed KV-transfer paths in [`inference_sources`](../http-api/07-Handoff-and-Lifecycle.md#handoff-fields).

A new router process restores each `inference_sources` suspect by the rules in [restored and process-local state](../http-api/07-Handoff-and-Lifecycle.md#restored-and-process-local-state).

### 8.2 Decode drift evidence

The drift tracker scores fresh decode residuals and stalled inter-token gaps against the engine's recent healthy baseline. It excludes a placement estimate left over after decode completes.

A gap that crosses a prefill boundary pauses decode correction and drift scoring. The first pure decode observation after the pause starts a new scoring window with the previous healthy baseline and probation state.

`/narwhal/state` reports pauses in `health.<iid>.prefill_paused` and `health.<iid>.prefill_pauses`.

The fleet-wide surge veto withholds an engine's drift verdict when all of these are true:

- The engine's window score crosses its drift band.
- At least three engines have scored windows.
- At least half of the engine's scored peers exceed the peers' bands.
- The engine's score stays within `recovery.health.relative_band` times the median peer score.

A window that closes with at least one observation and fewer than `recovery.health.min_samples` is `undersampled`.

`/narwhal/state` reports scored and undersampled windows in `health.<iid>.scored` and `health.<iid>.undersampled`, and the age of the most recent scored window in `health.<iid>.last_scored_s_ago`. Prometheus counts scored and undersampled windows in `narwhal_health_windows_scored_total` and `narwhal_health_windows_undersampled_total`.

### 8.3 Engine restart policy

`recovery.engine_restart_policy` accepts:

- `individual`, the default
- `whole_wave`

`whole_wave` requires:

- a complete `engine_contract`
- `recovery.liveness_every > 0`

Under `whole_wave`, an ejection or identity failure places a whole-wave hold on the fleet until an operator completes the [engine-wave restart](../operate/03-Restart-Engines.md#8-restarting-an-engine-wave).

---

## 9. Resume, shutdown, and warm-standby state

These fields set the state handoff file, resume at startup and the shutdown drain.

| Field                        | Default             | Meaning                                                                        |
| ---------------------------- | ------------------- | ------------------------------------------------------------------------------ |
| `recovery.state_path`        | `"runs/state.json"` | Atomic state handoff file for roles, ejections, lifecycle state, and counters. |
| `recovery.resume`            | `false`             | Applies a compatible state handoff file at startup.                            |
| `serving.graceful_timeout_s` | `30.0`              | Uvicorn drain interval after `SIGTERM`, in zero or more whole seconds.         |

With resume off, the router starts with the role split declared in the fleet configuration. With resume on and a compatible state handoff at `recovery.state_path`, the router restores the split from the state handoff. With resume on, an uncontracted development fleet whose saved engine set differs from the current one starts with the declared split.

Contracted resume and automatic takeover require:

- state handoff schema version 1
- an accepted process identity for every engine that the saved state counts as available

A saved state handoff with an unknown schema or version aborts startup. A schema-valid handoff that fails the contracted fleet's resume checks places a whole-wave hold that requires a managed restart of every engine.

A successful resume restores roles, ejections, lifecycle holds and complete-backend-outage state. It clears per-engine dwell timestamps. The prefill-to-decode cooldown begins when Narwhal creates the replacement scheduler.

Configure warm-standby takeover with the `narwhal-serve` options in [Starting a router pair](../operate/01-Start-Routers.md#4-starting-a-router-pair).

---

## 10. Engine authentication and protocol adapters

Ingress terminates public client credentials.

Each engine request carries:

- the configured engine Bearer credential
- a router-generated `x-request-id`, unique to every attempt and phase

`engine.engine_api_key_env`, empty by default, names the environment variable that must hold the engine Bearer credential. Set it under `engine`:

```json
{
  "engine": {
    "engine_api_key_env": "NARWHAL_ENGINE_KEY"
  }
}
```

Serving, profiling, preflight, cache-reset and lifecycle requests go to the engine URL with the engine Bearer credential. Attestation requests go to the attestation sidecar URL on the trusted control network.

When `engine.engine_api_key_env` names a variable at deployment export, the credential reaches the engine as:

| Location                                                              | Variable                 |
| --------------------------------------------------------------------- | ------------------------ |
| Engine role environment                                               | `NARWHAL_ENGINE_API_KEY` |
| Mode-0600 `container.env` that the engine launcher supplies to Docker | `VLLM_API_KEY`           |
| Mode-0600 `engine.env` for a native engine                            | `VLLM_API_KEY`           |

`/narwhal/state` reports the authentication mode in `admission.engine_auth`. The mode is `engine-credential` when `engine.engine_api_key_env` is set, and `boundary` when it is empty.

Use the same authentication mode for workload measurement and production serving.

`engine.backend` defaults to `"vllm"`. `engine.connector` and `engine.dialect` default to the backend's connector and dialect: `"nixl"` and `"vllm"` for vLLM, `"mooncake"` and `"sglang"` for SGLang. SGLang's `nixl` connector keeps each engine in its launch role, so a fleet that uses it sets `pin` on every engine.

---

## 11. Profile validation

These fields locate the profile store and set the decode-fit error limits for `narwhal-check`.

| Field                          | Default                | Meaning                                                                   | Values                                                  |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------- |
| `profiles.path`                | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`         |                                                         |
| `profiles.max_decode_fit_mape` | `0.05`                 | Maximum in-sample decode-fit error that `narwhal-check` accepts           | Positive, at most `controller.reactive.movement_margin` |
| `profiles.max_decode_cv_mape`  | `0.13`                 | Maximum leave-one-out cross-validation error that `narwhal-check` accepts | Positive                                                |

Profiles must cover the context and concurrency range used by the deployment.

A decode-fit failure in `narwhal-check` names the engine, the measured error, and the limit.
