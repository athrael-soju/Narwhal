---
description: Generate and serve attestation documents for the live vLLM, SGLang or TensorRT-LLM engines of a Narwhal fleet.
---

# Attest live engines

## Checking the router inventory

On the router, confirm that `runs/deployment/fleet.json` lists:

- Each running engine with URL and attestation URL references that resolve in `.env.router`.
- The model.
- The initial roles.
- The SLO values.
- The profile path.

## Capturing the attestation

In each engine's role shell, capture the engine's attestation inputs and generate its document:

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli capture --run "$ENGINE_RUN"
```

For a vLLM engine that uses another engine's [fabric budget](04-Qualify-Fabric.md#building-the-source-budget), also pass `--representative-layout` with the resolved layout you retained for that representative:

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli capture --run "$ENGINE_RUN" \
  --representative-layout '<layout name from the representative cache-layout.json>'
```

`capture` reads the engine's `ENGINE_RUN` directory: `launch.json`, `checked.json` and the check log, and `container.id` and `container-start.json`, or the start records of a native engine. A vLLM engine also needs the `cache-layout.json` from [Capturing the live cache layout](03-Validate-Engines.md#capturing-the-live-cache-layout).

Each run of `capture` writes its captures to a new attempt directory, `ENGINE_RUN/attestation/<UTC time>-<id>/`. It runs these captures in order and stops at the first that fails:

| Capture | Engines | Writes or checks |
| --- | --- | --- |
| `startup-log` | All | `startup.log`: for a container, `docker logs --since` the container start in `container-start.json`; for a native engine, a copy of the `startup.log` it keeps writing |
| `model-dimensions` | All | `model-dimensions.live.json` on vLLM, `model-dimensions.json` on SGLang and TensorRT-LLM |
| `http-identity` | vLLM | `version.json` and `metrics.txt` from the live engine |
| `connector-version` | vLLM | `nixl-connector-version.json` |
| `cache-registration` | vLLM | `cache-registration.json` |
| `transfer-mode` | vLLM | `transfer-mode.json` |
| `startup-connector` | vLLM | The attempt's `startup.log` names the connector class in `transfer-mode.json` |
| `handshake-policy` | vLLM | `handshake-policy.json` |
| `server-info` | SGLang, TensorRT-LLM | `server-info.json` |
| `document` | All | `engine-attestation.json` |

When every capture passes, `capture` publishes the attempt: it writes `ENGINE_RUN/published-attestation.json` with the attempt's name and the SHA-256 of its `engine-attestation.json`, and prints `Generated private engine attestation in <attempt>/engine-attestation.json`. `serve` and `verify` read the published attempt. Once an attempt is published, `capture` refuses to run again for that launch directory. When two captures run at once, the first to finish publishes, and the other exits with `Attempt <name> already published ...` and records the attempt that won in its `capture-failure.json`.

A failure exits with status 1 and names the capture, its output and the cause, such as `Capture transfer-mode (transfer-mode.json) failed: ...`. The attempt directory stays as evidence, with the cause in `capture-failure.json`. Fix the cause and run `capture` again: the next attempt starts from the first capture.

Every capture checks that the container in `container.id` still runs the checked image and that its `State.StartedAt` matches `container-start.json`. When the container has started again since `narwhal-engine` started it, the engine's earlier evidence, such as `cache-layout.json`, describes another process, so prepare, check and start the engine in a fresh launch directory. On vLLM, `http-identity` also requires the process start in `/metrics` to be within 2 seconds of the container start, or to equal the native start record.

`--representative-layout` applies only to vLLM, and `capture` checks it before the first capture: the layout must match the one layout in the engine's `cache-layout.json`, which `cache-registration` then registers. For an SGLang or TensorRT-LLM engine, `capture` refuses it.

### Model dimensions

For a vLLM engine, the capture reads the dimensions from the live engine into `model-dimensions.live.json`, and requires the live container's plan and launcher hashes to match `launch.json`.

For an SGLang or TensorRT-LLM engine, the capture reads the dimensions and the first listed architecture from the checkpoint's `config.json`, or from its `text_config`, into `model-dimensions.json`. `config.json` must match `model_config_sha256` in `launch.json`.

### vLLM connector and cache evidence

`contract.connector_version` comes from the installed connector's `NIXL_CONNECTOR_VERSION`. `transfer_version` records the version of `nixl` in `runtime.expected_packages`, or of `nixl-rocm` when `nixl` is not listed.

`cache-registration.json` records the engine's `kv_cache_layout`, and that layout's `is_block_outermost` value as `cross_layers_blocks`. The `cache-layout.json` file must hold one resolved layout across every TP rank.

When the group signature, resolved layout and page geometry all match the representative's, the engine inherits the representative's fabric budget. When the layout or page geometry differs, the engine needs a separate serving capture, budget and edge comparisons.

`transfer-mode.json` records the connector class that the runtime check resolved through vLLM's connector factory, read from the check attempts for this launch plan in `image-check.log`, or `runtime-check.log` for a native engine. `NixlPullConnector` gives `transfer_mode` `pull` and `NixlPushConnector` gives `push`. In the pinned API, `NixlConnector` is an alias for `NixlPullConnector`, and `kv_both` means the engine produces and consumes KV. When the check log holds no connector class or several, rerun `narwhal-engine check` for this launch plan. Any other class fails the capture: inspect the pinned connector implementation for its transfer protocol.

The serving engine logs each connector it creates:

```text
[factory.py:62] Creating v1 connector with name: NixlPullConnector and engine_id: <engine id>
```

`startup-connector` requires every such line in the startup log to name the class in `transfer-mode.json`, and fails when the log has none. Lines such as `NixlConnector setting KV cache layout to LBHNC` name the configured alias, so the check does not read them.

The effective `enforce_handshake_compat` value is:

- The plan's transfer config setting, when set.
- The pinned NIXL worker default of `True`, otherwise.

The `handshake-policy` capture passes when the effective value is Boolean `true`. When it fails:

1. Fix the launch configuration.
2. Check a new plan.
3. Restart the engine from the new plan.

### SGLang and TensorRT-LLM server evidence

For an SGLang engine, the `server-info` capture writes fields from `/server_info` and the process start time to `server-info.json`. `/server_info` supplies `engine_version`, `model_dtype`, `attention_backend`, `kv_cache_dtype` and `speculative_config`. `transfer_version` records the version of `mooncake-transfer-engine` in `runtime.expected_packages`, or of the one CUDA build listed there, such as `mooncake-transfer-engine-cuda13`.

For a TensorRT-LLM engine, the capture writes `version` from `/version`, `tokens_per_block`, `max_batch_size` and `kv_cache_hash_algo` from `/server_info`, and the process start time to `server-info.json`. The document fails when the launch's `engine.json` sets a block size other than the live `tokens_per_block`, when the live `max_batch_size` differs from `--max_batch_size`, or when `engine.json` names a transceiver backend other than the planned connector's. `model_dtype`, `kv_cache_dtype`, `connector` and `speculative_config` come from the launch. `transfer_version` records the `tensorrt_llm` version in `runtime.expected_packages`.

The document fails when an SGLang engine's live `decode_attention_backend` in `/server_info` differs from its `--decode-attention-backend` launch argument, or is set when the launch passes none.

The document fails when an SGLang engine's startup log reports a `max_running_requests` below its `--max-running-requests`. Lower `runtime.max_sequences` to the logged value, or raise the limit the log names, such as `--max-mamba-cache-size`, and then relaunch the engine.

### The document

For a GGUF model, the document's `launch` records the file's `model_sha256` from `launch.json`. The document fails when the file no longer matches it, has become a symbolic link, or has left `NARWHAL_MODEL_DIR`.

## Serving the attestation

### Starting the sidecar

Run the sidecar in the engine's role shell, as the user that started the engine. For a container engine, that user needs Docker access, as `narwhal-engine start` already requires.

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli serve --run "$ENGINE_RUN"
```

`serve` refuses to start when the published `engine-attestation.json` differs from the launch directory's current evidence or the container has started again. On vLLM, it also reads the live `/metrics` and refuses when the process start differs from the attempt's `metrics.txt`, which means the engine process started again inside its container. [`narwhal-attest`](../cli/Attest.md) exits when the engine's version differs from the document's. When `checked.json` shows prefix caching on and cache events published, the sidecar serves the [residency routes](../cli/Attest.md#residency).

### Verifying the sidecar

In a second shell for the same engine role:

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli verify --run "$ENGINE_RUN"
```

`verify` requests the sidecar's `/health` and `/v1/attestation` at `NARWHAL_NODE_<n>_ATTESTATION_URL`, reads the live engine's version and process start, and checks the attestation against them and against the contract in the published `engine-attestation.json`. On SGLang and TensorRT-LLM, which publish no process start, it reads the start from the sidecar's `/v1/process`.

It writes a new `attestation-check-XXXXXX` directory in `ENGINE_RUN` with `health.json`, `health.status`, `attestation.json`, `attestation.status` and `identity.json`. On success it prints `Sidecar attestation matches the live engine and contract; captures in <directory>`. It exits with status 1 when a sidecar route returns a status other than HTTP 200, naming the route, or when the attestation fails a check, naming the failed checks.

## Attesting an engine pod

A [Kubernetes engine pod](../cli/Engine.md#kubernetes-plans) runs its attestation sidecar as its `attestation` container. You do not run `capture` or `serve`. Once `narwhal-engine check` has passed on the pod's records, copy that launch directory to the state volume at `engines/<role>/launches/<pod UID>/`, where the sidecar mounts it read-only. Until it is there, the sidecar logs `waiting for /narwhal-launch, the launch directory that checks this pod's records`.

The sidecar then waits for the engine to answer `/health`, and captures the engine container's attestation unless that container already published one. It binds the engine through the pod's shared process namespace and the Kubernetes API: the engine container's first process, its start ticks and the container ID in its cgroup path must match the pod's `containerStatuses` entry for `engine`, and the process must have started within 2 seconds of the container's `startedAt`. The attestation reads the image ID the pod status reports, not the plan's `planned_image`, and keeps the `{image_id, container_id}` binding a container engine's attestation has.

The captures differ from a container's:

- `pod-identity` runs first and writes `pod-identity.json`: the pod, its UID and node, the engine container's runtime, ID, image, image ID, start and restart count, and the engine's process ID, start ticks and start time. Every later capture requires the engine process to be the one it records.
- `startup-log` copies the `startup.log` of the engine process's directory in the engine volume.
- The vLLM inspections read the output the `engine` container recorded in `inspections.json` before the engine started, and refuse output of other arguments. The live cache layout is the `cache-layout.json` the cache capture hook wrote in that directory.

Each engine container's attempts and published attempt lie under `containers/<container ID>/` of the pod's directory of the state volume. When the engine process ends, the sidecar stops. The kubelet restarts the engine container and the sidecar, and the sidecar captures the new container's attestation before it serves.

Verify from the sidecar container, which holds the service account token:

```bash
kubectl exec <pod> -c attestation -- python3 -I -m narwhal.deployment.attestation_contract.cli \
  verify --run /narwhal-launch --pod-evidence /narwhal-state
```

`verify` reads the pod through the Kubernetes API, takes the attestation of the engine container that runs now, and refuses when that container has published none, when the pod is no longer the checked pod, or when the container ID, image ID or start differs from the identity its attestation binds. It also refuses a served attestation whose `launch.image_id` or process start differs from that identity. It writes its `attestation-check-XXXXXX` directory beside the container's attempts.

`capture`, `serve` and `verify` take `--pod-evidence` for a Kubernetes plan and refuse a Kubernetes plan without it, and they refuse `--pod-evidence` for other plans.

### Finalizing the fleet contract

Run once from the router shell after `verify` passes for every engine:

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli finalize-fleet --fleet runs/deployment/fleet.json
```

`finalize-fleet` requests every sidecar's attestation from the router over the trusted control network, and requires:

- Each sidecar's attestation matches its engine's live identity.
- Every engine carries the same complete contract.
- Every engine attests the same model files: the same `model_config_sha256`, `model_tree_sha256` and `model_sha256` in its `launch` evidence.

`finalize-fleet` writes:

- `engine_contract` in `runs/deployment/fleet.json`.
- A copy of the previous fleet file at `runs/fleet.before-attestation-<id>.json`.

If the fleet file already has an `engine_contract`, `finalize-fleet` checks it against the live contract instead. On a mismatch, `finalize-fleet` exits with status 1 and prints `Existing router contract differs from the live sidecars`.

On success it prints `Router fleet contract verified across live sidecars: <fingerprint>`.

If a sidecar fails, the error output names the engine and the failed checks. If the output reports `Engine sidecars report different contracts`, compare the `contract` objects in each engine's published `engine-attestation.json`. If it reports `Engine sidecars attest different model files`, it names the differing field and the engines that attest each value. Start those engines from the same checkpoint.

Recovery:

1. Fix the failing engine.
2. Restart its sidecar against the checked process.
3. Run `finalize-fleet` again.

Leave every engine and sidecar running through profiling, preflight, and the trial.

[Profile engines and run preflight](06-Profile-and-Preflight.md)
