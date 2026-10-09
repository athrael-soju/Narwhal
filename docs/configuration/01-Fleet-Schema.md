---
description: Top-level keys, a minimal fleet definition and the engine compatibility contract for Narwhal fleet files.
---

# Fleet schema and engine contract

## 1. Configuration model

### 1.1 Top-level keys

A fleet file accepts the top-level keys `schema`, `schema_version`, `model`, `hardware`, `engines`, `engine_contract`, `slo`, `controller`, `serving`, `engine`, `recovery` and `profiles`, and annotation keys that start with `_`. Any other top-level key is an error.

Use JSON-native types:

| Value               | JSON type         |
| ------------------- | ----------------- |
| Boolean             | `true` or `false` |
| Count               | Integer           |
| Duration or ratio   | Finite number     |
| Name, path, or mode | String            |

A type mistake produces an error like this:

```text
fleet.json: controller.monitor_interval_s must be a number; serving.max_connections must be an integer; engine.tokenize must be a boolean
```

### 1.2 Paths

The deployment workflow resolves relative fleet and profile paths from the checkout root. `narwhal-serve` resolves relative `profiles.path` and `recovery.state_path` from its working directory at startup.

### 1.3 Environment loading

Set these variables in the process environment, or in the workstation `.env` that the [deployment workflow](../deploy/01-Discover.md#loading-the-private-environment) loads.

| Variable                            | Use                                                                                                | Default          |
| ----------------------------------- | -------------------------------------------------------------------------------------------------- | ---------------- |
| `NARWHAL_FLEET`                     | Fleet file for `make observe`, `create_app()`, and CLI commands through `--fleet "$NARWHAL_FLEET"` |                  |
| `NARWHAL_ENGINE_KEY`                | Example Bearer credential variable, named in `engine.engine_api_key_env`                           |                  |
| `NARWHAL_ROUTER_URL`                | Router origin that `make observe` scrapes                                                          |                  |
| `NARWHAL_GRAFANA_BIND_ADDRESS`      | Grafana listener host, on port 3000                                                                | `127.0.0.1`      |
| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | Prometheus listener host and port                                                                  | `127.0.0.1:9090` |
| `NARWHAL_CONTROL_CONSOLE_URL`       | Fleet control console address that `make observe` writes into the **Fleet control** dashboard, as the workstation browser reaches it | `http://127.0.0.1:18020/console` |

An engine `url` or `attestation_url` expands from the environment when the entire JSON string is a single `${NAME}` reference:

```json
{
  "url": "${NARWHAL_NODE_1_URL}"
}
```

With `NAME` set, the field takes the value of `NAME`. With `NAME` unset or blank, loading fails with an error that names the URL field and the variable.

A partial reference, default syntax such as `${NAME:-x}`, and a resolved value that contains `${` are errors. `$NAME`, and `${...}` in any other field, stay literal text.

Save resolved URLs from `FleetConfig.save()` to a Git-ignored fleet path.

---

## 2. Minimal fleet definition

A fleet needs six fields:

| Field            | Value             | Notes                                                                                       |
| ---------------- | ----------------- | ------------------------------------------------------------------------------------------- |
| `schema`         | `"narwhal.fleet"` | Identifies the fleet interface.                                                             |
| `schema_version` | `2`               | The configuration version.                                                                  |
| `model`          | string            | The exact model name the router exposes and every engine serves.                            |
| `engines`        | nonempty array    | The engine instances.                                                                       |
| `slo.ttft_s`     | positive seconds  | Time-to-first-token (TTFT) target for admission, placement, load projection, and control.   |
| `slo.tpot_s`     | positive seconds  | Time-per-output-token (TPOT) target for admission, placement, load projection, and control. |

Set both targets from measurements on the deployed engine shape.

Each engine entry takes these fields:

| Field             | Default    | Notes                                                                                              |
| ----------------- | ---------- | -------------------------------------------------------------------------------------------------- |
| `iid`             | required   | Scheduler identity, unique within `engines`.                                                       |
| `url`             | required   | The vLLM HTTP base URL.                                                                            |
| `attestation_url` | `""`       | Full attestation sidecar URL, required by `narwhal-check` when the fleet has an `engine_contract`. |
| `role`            | `"decode"` | The starting role: `"prefill"` or `"decode"`.                                                      |
| `pin`             | `false`    | When true, the engine serves only its configured role through role-controller moves and resume.    |
| `shared_device`   | `null`     | GPU allocation for an engine that shares one GPU with other engines.                               |

```json
{
  "iid": "n0",
  "url": "http://node0:8000",
  "attestation_url": "http://node0:8010/v1/attestation",
  "role": "prefill",
  "pin": false
}
```

### 2.1 Shared-device allocation

A `shared_device` group places two to eight engines on one GPU.

| Field                                  | Requirement                                                                         |
| -------------------------------------- | ----------------------------------------------------------------------------------- |
| `shared_device.group`                  | Nonempty group name, identical for every engine on the GPU.                         |
| `shared_device.gpu_uuid`               | Nonempty GPU UUID, identical across the group.                                      |
| `shared_device.device_allowance`       | Group fraction of device memory, above 0 and at most 1, identical across the group. |
| `shared_device.gpu_memory_utilization` | This engine's fraction of device memory, above 0 and at most 1.                     |

The group's `gpu_memory_utilization` values sum to at most its `device_allowance`.

---

## 3. Engine shape and compatibility contract

Every production fleet needs a complete `engine_contract`.

A complete `engine_contract` sets every [contract field](#32-contract-fields) to a nonempty string, positive integer or boolean. `image_digest` is optional.

With an incomplete `engine_contract`, a lifecycle drain fails with `lifecycle drain requires a complete engine_contract`. Readmission fails with `readmission requires a complete engine_contract`.

### 3.1 Hardware block

The optional `hardware` block needs all three fields when present.

| Field                              | Notes                                                                                                                        |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `hardware.accelerator`             | Nonempty accelerator product name from [engine-host inspection](../deploy/03-Validate-Engines.md#inspecting-every-engine-host). |
| `hardware.accelerators_per_engine` | Accelerators per replica, an integer of 1 or more.                                                                           |
| `hardware.tensor_parallel`         | The TP size your launcher passes to vLLM, an integer between 1 and `hardware.accelerators_per_engine`.                       |

### 3.2 Contract fields

| Field                      | Default           | Requirement                                                                                    |
| -------------------------- | ----------------- | ---------------------------------------------------------------------------------------------- |
| `vllm_version`             | required          | The exact string each engine returns from `/version`.                                          |
| `image_digest`             | `""`              | The immutable `sha256:<64 hex>` container digest from engine-side attestation.                 |
| `nixl_version`             | `""`              | The NIXL package version installed in the image.                                               |
| `nixl_connector_version`   | `0`               | A positive `NIXL_CONNECTOR_VERSION`, read from the deployed connector's metadata.              |
| `model_architecture`       | `""`              | The model implementation name.                                                                 |
| `model_dtype`              | `""`              | The dtype the model runs in.                                                                   |
| `kv_heads`                 | `0`               | Positive value of `ModelConfig.get_total_num_kv_heads()` for the whole model.                  |
| `head_size`                | `0`               | Positive value of `ModelConfig.get_head_size()`.                                               |
| `hidden_layers`            | `0`               | Positive value of `ModelConfig.get_total_num_hidden_layers()` for the whole model.             |
| `attention_backend`        | `""`              | The expected backend from the recorded launch.                                                 |
| `kv_cache_dtype`           | `""`              | The KV-cache dtype.                                                                            |
| `cross_layers_blocks`      | `null`            | Cache-block grouping, the `KVCacheLayout.is_block_outermost` value from the pinned layout API. |
| `hybrid_kv_cache_manager`  | `null`            | Whether vLLM's hybrid KV-cache manager is part of the layout.                                  |
| `connector`                | `"NixlConnector"` | Nonempty engine-side connector name.                                                           |
| `kv_role`                  | `""`              | The engine-side KV role, such as `kv_both`.                                                    |
| `transfer_mode`            | `""`              | `pull` for `NixlPullConnector`, `push` for `NixlPushConnector`.                                |
| `speculative_config`       | `""`              | The `--speculative-config` value from the recorded launch, or `disabled`.                      |
| `enforce_handshake_compat` | `true`            | `true`, as the effective value from the pinned NIXL worker extra-config lookup.                |

Capture every contract value from the deployed engine in [Gate E: Capturing the attestation inputs](../deploy/05-Attest.md#capturing-the-attestation-inputs).

Values to check against the live engine:

- `nixl_connector_version`
- the resolved `head_size` dimensions
- `cross_layers_blocks`
- the resolved `transfer_mode` class and mode
- `enforce_handshake_compat`, both the configured value and the installed default

### 3.3 Attestation

The generator in [Gate E: Attesting the live engines](../deploy/05-Attest.md) writes `runs/engine-launch-*/engine-attestation.json`.

Fill in the fleet contract:

1. Start the sidecars.
2. Run `finalize-fleet` in the router shell:

    ```bash
    .venv/bin/python -m narwhal.deployment.attestation_contract.cli finalize-fleet --fleet runs/deployment/fleet.json
    ```

When the sidecar contracts are complete and identical, `finalize-fleet` writes the `engine_contract` into the `--fleet` file. When they are incomplete or differ, `finalize-fleet` exits with status 1.

Each [attestation document](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-attestation.example.json) declares this identity:

```json
{
  "schema": "narwhal.attestation",
  "schema_version": 1
}
```

`narwhal-attest` needs at least one nonempty source entry for every populated contract field.

For each contracted engine:

1. Run one sidecar beside the engine.
2. Set the engine's `attestation_url` to the sidecar's address.

Sidecar identity values, read at startup:

- the engine's `/version`
- the `process_start_time_seconds` metric from the engine's `/metrics`

When `/version` at startup differs from `vllm_version` in the attestation document, the sidecar exits with an error. When either identity value is unreadable or differs from its startup value, every sidecar route returns HTTP 503.

Each sidecar response carries:

- the contract
- its sources
- the two identity values, under `engine`
- an `attestation_digest` over the rest of the response

An attestation document with launch evidence adds two fields to each sidecar response:

- `launch`, the launch evidence
- `launch_digest`, a digest over the contract and the launch evidence

An engine restarted from an identical launch keeps its `launch_digest`. Each change to the engine's launch record, including `runtime.environment`, gives the engine a new `launch_digest`.

Saved profiles and first-token calibrations bind to the engine's `launch_digest` when the response carries launch evidence, otherwise to its `attestation_digest`.

Treat an HTTP 503 as a changed engine process:

1. Check the engine's HTTP endpoints.
2. Restart the sidecar through its process manager.

An attested-field or process-start-time mismatch fails `narwhal-check` and skips its produce and consume KV-transfer probes.
