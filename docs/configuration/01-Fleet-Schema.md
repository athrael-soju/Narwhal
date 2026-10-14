---
description: Top-level keys, a minimal fleet definition and the engine compatibility contract for Narwhal fleet files.
---

# Fleet schema and engine contract

## Configuration model

### Top-level keys

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

### Paths

A command that loads the fleet, such as `narwhal-serve`, resolves a relative `profiles.path` or `recovery.state_path` from its working directory.

### Environment loading

Set these variables in the process environment, or in the workstation `.env` that the [deployment workflow](../deploy/01-Discover.md#loading-the-private-environment) loads.

| Variable                            | Use                                                                                                                             | Default                          |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| `NARWHAL_FLEET`                     | Fleet file for `create_app()` and CLI commands through `--fleet "$NARWHAL_FLEET"`                                               |                                  |
| `NARWHAL_ENGINE_KEY`                | Example Bearer credential variable, named in `engine.engine_api_key_env`                                                        |                                  |
| `NARWHAL_ROUTER_URL`                | Router origin that `make observe` scrapes                                                                                       |                                  |
| `NARWHAL_GRAFANA_BIND_ADDRESS`      | Grafana listener host, on port 3000                                                                                             | `127.0.0.1`                      |
| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | Prometheus listener host and port                                                                                               | `127.0.0.1:9090`                 |
| `NARWHAL_CONTROL_CONSOLE_URL`       | Fleet control console address, opened by the dashboard's **Fleet control** link                                                 | `http://127.0.0.1:18020/console` |
| `NARWHAL_CONTROL_METRICS_URL`       | Fleet control service address for Prometheus to scrape. Needs `NARWHAL_CONTROL_TOKEN`. When unset, Prometheus skips the service |                                  |
| `NARWHAL_CONTROL_TOKEN`             | Fleet control bearer token                                                                                                      |                                  |

An engine `url` or `attestation_url` expands from the environment when the entire JSON string is a single `${NAME}` reference:

```json
{
  "url": "${NARWHAL_NODE_1_URL}"
}
```

With `NAME` set, the field takes the value of `NAME`. With `NAME` unset or blank, loading fails with an error that names the URL field and the variable.

A partial reference, default syntax such as `${NAME:-x}`, and a resolved value that contains `${` are errors. `$NAME`, and `${...}` in any other field, stay literal text.

`FleetConfig.save()` writes the resolved URLs. Save to a Git-ignored fleet path.


## Minimal fleet definition

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
| `url`             | required   | The engine HTTP base URL.                                                                          |
| `attestation_url` | `""`       | Full attestation sidecar URL, required by `narwhal-check` when the fleet has an `engine_contract`. |
| `role`            | `"decode"` | The starting role: `"prefill"` or `"decode"`.                                                      |
| `pin`             | `false`    | When true, the engine serves only its configured role through role-controller moves and resume. Set it on an engine that must stay in one role, or on every engine for [static disaggregation](../concepts/01-Request-and-Topology.md#static-disaggregation). |
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

### Shared-device allocation

A `shared_device` group places two to eight engines on one GPU.

| Field                                  | Requirement                                                                         |
| -------------------------------------- | ----------------------------------------------------------------------------------- |
| `shared_device.group`                  | Nonempty group name, identical for every engine on the GPU.                         |
| `shared_device.gpu_uuid`               | Nonempty GPU UUID, identical across the group.                                      |
| `shared_device.device_allowance`       | Group fraction of device memory, above 0 and at most 1, identical across the group. |
| `shared_device.gpu_memory_utilization` | This engine's fraction of device memory, above 0 and at most 1.                     |

The group's `gpu_memory_utilization` values sum to at most its `device_allowance`.


## Engine shape and compatibility contract

Every production fleet needs a complete `engine_contract`.

An `engine_contract` needs `engine_version` and `connector`. It is complete when every other [contract field](#contract-fields) that the fleet's `engine.backend` attests holds a nonempty string, a positive integer or a boolean. `image_digest` is optional. On vLLM, `enforce_handshake_compat` defaults to `true`, the only value it accepts. On SGLang, leave `connector_version`, `cross_layers_blocks`, `hybrid_kv_cache_manager`, `kv_role`, `transfer_mode` and `enforce_handshake_compat` unset. Loading fails when one of them is `true`, a nonempty string or a positive integer.

With an incomplete `engine_contract`, a lifecycle drain fails with `lifecycle drain requires a complete engine_contract`, and readmission fails with `readmission requires a complete engine_contract`. A fleet with `recovery.engine_restart_policy` set to `whole_wave` fails to load.

### Hardware block

The optional `hardware` block needs all three fields when present.

| Field                              | Notes                                                                                                                           |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `hardware.accelerator`             | Nonempty accelerator product name from [engine-host inspection](../deploy/03-Validate-Engines.md#inspecting-every-engine-host). |
| `hardware.accelerators_per_engine` | Accelerators per replica, an integer of 1 or more.                                                                              |
| `hardware.tensor_parallel`         | The TP size your launcher passes to the engine, an integer between 1 and `hardware.accelerators_per_engine`.                    |

### Contract fields

The attestation generator derives each field from the deployed engine. Fields that name only vLLM sources stay unset on SGLang.

| Field                      | Default           | Requirement                                                                                                                                    |
| -------------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `engine_version`           | required          | The exact version each engine reports: `/version` on vLLM, `/server_info` on SGLang.                                                           |
| `image_digest`             | `""`              | The engine's local container image ID, `sha256:<64 hex>`. Empty for a native engine.                                                           |
| `transfer_version`         | `""`              | The pinned transfer package version: `nixl` or `nixl-rocm` on vLLM, the Mooncake package on SGLang.                                            |
| `connector_version`        | `0`               | vLLM only. A positive `NIXL_CONNECTOR_VERSION`, read from the deployed connector's module.                                                     |
| `model_architecture`       | `""`              | The model implementation name: vLLM's resolved architecture, or the first `architectures` entry in the model's `config.json` on SGLang.        |
| `model_dtype`              | `""`              | The dtype the model runs in: the `--dtype` launch argument on vLLM, `dtype` from `/server_info` on SGLang.                                     |
| `kv_heads`                 | `0`               | Positive KV head count for the whole model: `ModelConfig.get_total_num_kv_heads()` on vLLM, `num_key_value_heads` in `config.json` on SGLang.  |
| `head_size`                | `0`               | Positive head size: `ModelConfig.get_head_size()` on vLLM, `head_dim` in `config.json` on SGLang.                                              |
| `hidden_layers`            | `0`               | Positive layer count for the whole model: `ModelConfig.get_total_num_hidden_layers()` on vLLM, `num_hidden_layers` in `config.json` on SGLang. |
| `attention_backend`        | `""`              | The attention backend: from the startup log on vLLM, from `/server_info` on SGLang.                                                            |
| `kv_cache_dtype`           | `""`              | The KV-cache dtype: the `--kv-cache-dtype` launch argument on vLLM, from `/server_info` on SGLang.                                             |
| `cross_layers_blocks`      | `null`            | vLLM only. Cache-block grouping, the `KVCacheLayout.is_block_outermost` value from the pinned layout API.                                      |
| `hybrid_kv_cache_manager`  | `null`            | vLLM only. Whether vLLM's hybrid KV-cache manager is part of the layout.                                                                       |
| `connector`                | `"NixlConnector"` | Nonempty engine-side connector name. On SGLang, the default and the attested value are `engine.connector`.                                     |
| `kv_role`                  | `""`              | vLLM only. The engine-side KV role, such as `kv_both`.                                                                                         |
| `transfer_mode`            | `""`              | vLLM only. `pull` for `NixlConnector` and `NixlPullConnector`, `push` for `NixlPushConnector`.                                                 |
| `speculative_config`       | `""`              | The `--speculative-config` launch value on vLLM, or SGLang's `speculative_algorithm`, or `disabled`.                                           |
| `enforce_handshake_compat` | `true`            | vLLM only. `true`, as the effective value from the pinned NIXL worker extra-config lookup. `null` on SGLang.                                   |

On SGLang, `head_size` falls back to `hidden_size` divided by `num_attention_heads`, and `kv_heads` falls back to `num_attention_heads`. Both read the model's `text_config` when it has one.

Capture every contract value from the deployed engine in [Capturing the attestation inputs](../deploy/05-Attest.md#capturing-the-attestation-inputs).

Check the resolved `head_size` dimensions against the live engine. On vLLM, also check these values:

- `connector_version`
- `cross_layers_blocks`
- the resolved `transfer_mode` class and mode
- `enforce_handshake_compat`, both the configured value and the installed default

### Attestation

The [attestation generator](../deploy/05-Attest.md) writes `engine-attestation.json` into the engine's launch directory.

To fill in the fleet contract:

1. Start the sidecars.
2. Run `finalize-fleet` in the router shell:

    ```bash
    .venv/bin/python -m narwhal.deployment.attestation_contract.cli finalize-fleet --fleet runs/deployment/fleet.json
    ```

`finalize-fleet` checks each sidecar's response against its live engine. When the contracts are complete and identical, it writes the `engine_contract` into the `--fleet` file and saves the previous file as `runs/fleet.before-attestation-<id>.json`. When the file already holds that contract, it leaves the file unchanged. When a contract is incomplete, the contracts differ, or the file holds another contract, it exits with status 1.

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

At startup, the sidecar reads two identity values:

- the engine version, from `/version` on vLLM or `/server_info` on SGLang
- the engine process start time, from the `process_start_time_seconds` metric on vLLM, or on SGLang from the host start time of the process that `--engine-container` or `--engine-pid` names

An SGLang sidecar also serves the process start time at `/v1/process`.

When the engine version at startup differs from `engine_version` in the attestation document, the sidecar exits with an error. When either identity value is unreadable or differs from its startup value, every sidecar route except `/v1/process` returns HTTP 503.

Each sidecar response carries:

- the contract
- its sources
- the two identity values, under `engine`
- an `attestation_digest` over the rest of the response

An attestation document with launch evidence adds two fields to each sidecar response:

- `launch`, the launch evidence
- `launch_digest`, a digest over the contract and the launch evidence

An engine restarted from an identical launch keeps its `launch_digest`. Each change to the engine's launch record, including `runtime.environment`, gives the engine a new `launch_digest`. So does a change to the code of Narwhal's engine launcher, or on vLLM of its cache capture hook. Edits to their comments, docstrings or formatting keep the `launch_digest`.

Saved profiles and first-token calibrations bind to the engine's `launch_digest` when the response carries launch evidence, otherwise to its `attestation_digest`.

Treat an HTTP 503 as a changed engine process:

1. Check the engine's HTTP endpoints.
2. Restart the sidecar through its process manager.

An attested-field or process-start-time mismatch fails `narwhal-check` and skips its produce and consume KV-transfer probes.
