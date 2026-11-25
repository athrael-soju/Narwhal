---
description: Narwhal fleet settings for engine recovery, warm standby, engine authentication and profile validation.
---

# Recovery, authentication, and profile validation

## Engine health and recovery

### Breaker and drift settings

These fields set breaker thresholds, liveness probing, failure quarantine and drift scoring.

| Field                                 | Default | Meaning                                                                                             | Valid values                                                                |
| ------------------------------------- | :-----: | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `recovery.eject_after`                |   `3`   | Consecutive failures of one class on one engine before breaker action                               | At least 1                                                                  |
| `recovery.readmit_every`              |  `10`   | Monitor intervals between probes of ejected engines                                                 | At least 1                                                                  |
| `recovery.liveness_every`             |  `10`   | Monitor intervals between health probes and, for contracted fleets, identity and attestation checks | `0` disables idle sweeps                                                    |
| `recovery.liveness_misses`            |   `2`   | Consecutive failed liveness probes before ejection                                                  | At least 1                                                                  |
| `recovery.failure_quarantine_s`       |  `0.0`  | Time a failed engine stays excluded from placement while other live engines place its roles         | At least 0, where `0` disables quarantine                                   |
| `recovery.health.window_s`            | `30.0`  | Residual-scoring window length                                                                      | At least 1 second                                                           |
| `recovery.health.drift_band`          |  `2.0`  | Multiple of an engine's trailing healthy residual used as the drift threshold                       | Greater than 1.0                                                            |
| `recovery.health.relative_band`       |  `1.5`  | Multiple of the median peer score that bounds the fleet-wide surge veto                             | At least 0, where `0` disables the veto                                     |
| `recovery.health.min_samples`         |   `3`   | Samples required before scoring a window                                                            | From 1 to `floor(recovery.health.window_s / controller.monitor_interval_s)` |
| `recovery.health.probation_windows`   |   `3`   | Consecutive drifting windows before probation                                                       | At least 1                                                                  |
| `recovery.health.evict_windows`       |   `5`   | Consecutive drifting windows before requesting ejection                                             | At least `probation_windows`                                                |
| `recovery.health.recovery_windows`    |   `3`   | Consecutive healthy windows required to clear probation                                             | At least 1                                                                  |
| `recovery.health.probation_penalty_s` |  `1.5`  | Placement penalty in seconds for an engine on probation                                             | At least 0                                                                  |

The probation penalty adds to an engine's placement cost, so it moves a placement only when it is larger than the cost gap between that engine and its healthy peers. For a prefill placement the cost is seconds: the engine's resident prefill work plus the request's prefill time plus the penalty. For a decode placement Narwhal divides the penalty by the TPOT target and adds the result in tokens, so the default 1.5 s penalty at a 0.125 s TPOT target adds 12 tokens. Narwhal compares the first cost component (decode tokens for prefill, prefill tokens for decode) before the second, so the penalty breaks ties only among engines equal on the first component. Set the penalty from the cost gaps you see between healthy engines on your own traffic.

At `recovery.eject_after` consecutive failures of one class, the breaker runs the action that [failure evidence](../concepts/03-Failure-and-State.md#failure-evidence) lists for that class. To choose `recovery.failure_quarantine_s`, follow [Failure quarantine settings](../operate/07-Admission-Queue-and-Retry-Settings.md#retries-and-quarantine).

An inconclusive inference probe keeps the breaker's placement hold on a [covered engine](../concepts/03-Failure-and-State.md#failure-evidence) and repeats every `recovery.readmit_every` monitor intervals.

The state handoff carries the producer IDs of failed KV-transfer paths in [`inference_sources`](../http-api/07-Handoff-and-Lifecycle.md#handoff-fields). A new router process restores each suspect by the rules in [restored and process-local state](../http-api/07-Handoff-and-Lifecycle.md#restored-and-process-local-state).

### Decode drift evidence

The drift tracker compares each engine's decode times, or the longest pause between tokens when the engine stalls, with its healthy baseline.

A gap that crosses a prefill boundary pauses decode correction and drift scoring. The first pure decode observation after the pause starts a new scoring window with the previous healthy baseline and probation state.

`/narwhal/state` reports pauses in `health.<iid>.prefill_paused` and `health.<iid>.prefill_pauses`.

The fleet-wide surge veto withholds an engine's drift verdict when all of these are true:

- The engine's window score crosses its drift band.
- At least three engines have scored windows.
- At least half of the engine's scored peers exceed the peers' bands.
- The engine's score stays within `recovery.health.relative_band` times the median peer score.

A window that closes with at least one observation and fewer than `recovery.health.min_samples` is `undersampled`.

`/narwhal/state` reports scored and undersampled windows in `health.<iid>.scored` and `health.<iid>.undersampled`, and the age of the most recent scored window in `health.<iid>.last_scored_s_ago`. Prometheus counts scored and undersampled windows in `narwhal_health_windows_scored_total` and `narwhal_health_windows_undersampled_total`.

### Engine restart policy

`recovery.engine_restart_policy` accepts:

- `individual`, the default
- `whole_wave`

`whole_wave` requires:

- a complete `engine_contract`
- `recovery.liveness_every > 0`

Under `whole_wave`, an ejection or identity failure places a whole-wave hold on the fleet until an operator completes the [engine-wave restart](../operate/03-Restart-Engines.md#restart-an-engine-wave).


## Resume, shutdown, and warm-standby state

These fields set the state handoff file, resume at startup and the shutdown drain.

| Field                        | Default             | Meaning                                                                        |
| ---------------------------- | ------------------- | ------------------------------------------------------------------------------ |
| `recovery.state_path`        | `"runs/state.json"` | Atomic state handoff file for roles, ejections, lifecycle state, and counters. |
| `recovery.resume`            | `false`             | Applies a compatible state handoff file at startup.                            |
| `serving.graceful_timeout_s` | `30.0`              | Uvicorn drain interval after `SIGTERM`, in zero or more whole seconds.         |

With resume off, the router starts with the role split declared in the fleet configuration. With resume on and a compatible state handoff at `recovery.state_path`, the router restores the split from the state handoff, and a saved [engine membership](../http-api/07-Handoff-and-Lifecycle.md#membership-in-the-handoff) at generation 1 or higher replaces the fleet file's engines. With resume on, the router starts an uncontracted development fleet with the declared split when the saved handoff's engines differ from the fleet file's and its `membership` is missing or at generation 0.

Contracted resume and automatic takeover require:

- state handoff schema version 1
- an accepted process identity for every engine that the saved state counts as available

A saved state handoff with an unknown schema or version aborts startup. A schema-valid handoff that fails the contracted fleet's resume checks places a whole-wave hold that requires a managed restart of every engine.

A successful resume restores roles, ejections, lifecycle holds and complete-backend-outage state. It clears per-engine dwell timestamps. The prefill-to-decode cooldown begins when Narwhal creates the replacement scheduler.

A standby router checks its fleet file's engines at startup. Its profile store may also hold profiles for other engines, such as one that joined on the primary, and the standby logs a warning that names them. At takeover, when the engines it serves differ from its fleet file's, the standby checks the profile store and the first-token calibration against those engines. A takeover from a handoff without a membership at generation 1 or higher serves the fleet file's engines and refuses a store that holds profiles for other engines. A failed check blocks the takeover with its reason.

Configure warm-standby takeover with the `narwhal-serve` options in [Starting a router pair](../operate/01-Start-Routers.md#starting-a-router-pair).


## Engine authentication and protocol adapters

Ingress authenticates clients and removes their credentials before a request reaches the router. The router then authenticates to engines with one Bearer credential.

`engine.engine_api_key_env`, empty by default, names the environment variable that must hold the engine Bearer credential. Set it under `engine`:

```json
{
  "engine": {
    "engine_api_key_env": "NARWHAL_ENGINE_KEY"
  }
}
```

Every engine request carries that credential and a router-generated `x-request-id`, unique to every attempt and phase. Serving, profiling, preflight, cache-reset and lifecycle requests go to the engine URL. Attestation requests go to the attestation sidecar URL on the trusted control network.

`/narwhal/state` reports the authentication mode in `admission.engine_auth`: `engine-credential` when `engine.engine_api_key_env` is set, and `boundary` when it is empty. Use the same mode for workload measurement and production serving.

When `engine.engine_api_key_env` names a variable at deployment export, the credential reaches the engine as:

| Location                                                              | vLLM                     | SGLang                   | TensorRT-LLM             |
| --------------------------------------------------------------------- | ------------------------ | ------------------------ | ------------------------ |
| Engine role environment                                               | `NARWHAL_ENGINE_API_KEY` | `NARWHAL_ENGINE_API_KEY` | `NARWHAL_ENGINE_API_KEY` |
| Mode-0600 `container.env` that the engine launcher supplies to Docker | `VLLM_API_KEY`           | `NARWHAL_SGLANG_API_KEY` | `NARWHAL_TRTLLM_API_KEY` |
| Mode-0600 `engine.env` for a native engine                            | `VLLM_API_KEY`           | `NARWHAL_SGLANG_API_KEY` | `NARWHAL_TRTLLM_API_KEY` |

The SGLang launcher passes `NARWHAL_SGLANG_API_KEY` to the engine as `--api-key`. `trtllm-serve` does not check `NARWHAL_TRTLLM_API_KEY` and serves every caller that reaches its port, so keep a TensorRT-LLM engine port on a private network.

`engine.backend` is `"vllm"`, the default, `"sglang"`, `"trtllm"` for TensorRT-LLM, or `"llamacpp"` for a [development fleet of llama.cpp engines](../dev/Llama-Cpp-Engines.md). `engine.connector` and `engine.dialect` take the backend's values, which are also their defaults:

| Backend      | `engine.backend` | `engine.connector` | `engine.dialect` |
| ------------ | ---------------- | ------------------ | ---------------- |
| vLLM         | `"vllm"`         | `"nixl"`           | `"vllm"`         |
| SGLang       | `"sglang"`       | `"mooncake"`       | `"sglang"`       |
| TensorRT-LLM | `"trtllm"`       | `"ucx"`            | `"trtllm"`       |
| llama.cpp    | `"llamacpp"`     | `"slot-file"`      | `"llamacpp"`     |

A TensorRT-LLM engine also accepts the connector `"nixl"`.


## Profile validation

These fields locate the profile store and set the decode-fit error limits for `narwhal-check`.

| Field                          | Default                | Meaning                                                                   | Values                                                  |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------- |
| `profiles.path`                | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`         |                                                         |
| `profiles.max_decode_fit_mape` | `0.05`                 | Maximum in-sample decode-fit error that `narwhal-check` accepts           | Positive, at most `controller.reactive.movement_margin` |
| `profiles.max_decode_cv_mape`  | `0.13`                 | Maximum leave-one-out cross-validation error that `narwhal-check` accepts | Positive                                                |

Profiles must cover the context and concurrency range used by the deployment.

A decode-fit failure in `narwhal-check` names the engine, the measured error, and the limit.
