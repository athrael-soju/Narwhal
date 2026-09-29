# Fleet schema and engine contract

## 1. Configuration model

### 1.1 Top-level keys

The loader accepts these top-level keys: `schema`, `schema_version`, `model`, `hardware`, `engines`, `engine_contract`, `slo`, `controller`, `serving`, `engine`, `recovery`, and `profiles`. It also allows any key that starts with an underscore, so you can leave annotations in the file. Any other key is an error. Cross-field validation failures are collected and reported together, so one run shows you everything to fix.

Use JSON-native types: `true` or `false` for booleans, integers for counts, and finite numbers for durations and ratios. A type mistake produces an error like this:

```text
engine.tokenize must be a boolean; serving.max_connections must be an integer; controller.monitor_interval_s must be a number
```

### 1.2 Paths

Relative fleet and profile paths resolve from the checkout root in the deployment workflow. `narwhal-serve` resolves relative `profiles.path` and `recovery.state_path` from the directory it's started in, so launching it from somewhere else changes which files it finds.

The journal defaults to `journal.jsonl` next to `profiles.path`. Pass `--journal` to put it somewhere else.

### 1.3 Environment loading

Narwhal reads its variables from the process environment. The [deployment workflow](../deploy/01-Discover.md#load-the-private-environment) loads your workstation `.env` and generates role-specific environments for remote commands.

| Variable                            | Use                                                                                                                         |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `NARWHAL_FLEET`                     | Fleet file for `make observe` and for Python callers of `create_app()`. CLI commands still need `--fleet "$NARWHAL_FLEET"`. |
| `NARWHAL_ENGINE_KEY`                | An example Bearer credential for engine authentication. Point `engine.engine_api_key_env` at whatever name you use.         |
| `NARWHAL_ROUTER_URL`                | The router target that `make observe` scrapes.                                                                              |
| `NARWHAL_GRAFANA_BIND_ADDRESS`      | Overrides the Grafana listener.                                                                                             |
| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | Overrides the Prometheus listener.                                                                                          |

The example monitoring stack binds Grafana to `127.0.0.1:3000` and Prometheus to `127.0.0.1:9090`.

**URL expansion.** An engine `url` or `attestation_url` can pull its value from the environment, but only when the entire JSON string is a single `${NAME}` reference:

```json
{
  "url": "${NARWHAL_NODE_1_URL}"
}
```

Anything looser fails or is left alone:

- An unset or blank variable is an error that names the URL field and the variable.
- A partial reference, or one using default syntax, is an error.
- A resolved value that itself contains a reference is an error.
- `$NAME`, or `${...}` in any other field, is treated as literal text.

`FleetConfig.save()` writes resolved URLs, so save its output to an ignored fleet path.

---

## 2. Minimal fleet definition

A fleet needs six things:

| Field            | Value             | Notes                                                                                         |
| ---------------- | ----------------- | --------------------------------------------------------------------------------------------- |
| `schema`         | `"narwhal.fleet"` | Identifies the fleet interface.                                                               |
| `schema_version` | `1`               | The configuration version.                                                                    |
| `model`          | string            | The model name the router exposes. Every engine must serve this exact name.                   |
| `engines`        | nonempty array    | The engine instances. Each `iid` must be unique.                                              |
| `slo.ttft_s`     | positive seconds  | Time-to-first-token (TTFT) target. Drives admission, placement, load projection, and control. |
| `slo.tpot_s`     | positive seconds  | Time-per-output-token (TPOT) target. Used in the same four places.                            |

Set the TTFT and TPOT targets from measurements on the deployed engine shape, since Narwhal derives its Prometheus histogram buckets from them.

Each engine entry takes these fields:

| Field             | Default    | Notes                                                                                                       |
| ----------------- | ---------- | ----------------------------------------------------------------------------------------------------------- |
| `iid`             | required   | The scheduler identity. Unique within `engines`.                                                            |
| `url`             | required   | The vLLM HTTP base URL. The loader strips trailing `/` characters.                                          |
| `attestation_url` | `""`       | The full URL of the attestation sidecar. Required by `narwhal-check` if the fleet has an `engine_contract`. |
| `role`            | `"decode"` | The starting role: `"prefill"` or `"decode"`.                                                               |
| `pin`             | `false`    | When true, the configured role never changes.                                                               |

```json
{
  "iid": "n0",
  "url": "http://node0:8000",
  "attestation_url": "http://node0:8010/v1/attestation",
  "role": "prefill",
  "pin": false
}
```

A pinned engine keeps its role through controller moves, placement changes, and resume. One use is holding prefill capacity back so a warm standby can take over.

---

## 3. Engine shape and compatibility contract

Every production engine needs a complete `engine_contract`. Preflight, lifecycle readmission, restart handling, and the live NIXL checks all read from it. Drain and readmission are the strictest: they need every field, and an engine with any required value empty stays held out with `missing-contract`.

### 3.1 Hardware block

You can leave out the `hardware` block. If you include it, fill in all three fields.

| Field                              | Notes                                                                                                                                                        |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `hardware.accelerator`             | The accelerator product name you saw during [engine-host inspection](../deploy/03-Validate-Engines.md#inspect-every-engine-host). Must be a nonempty string. |
| `hardware.accelerators_per_engine` | Accelerators per replica. An integer, 1 or more.                                                                                                             |
| `hardware.tensor_parallel`         | The TP size your launcher passes to vLLM. An integer between 1 and `hardware.accelerators_per_engine`.                                                       |

Host inspection only gives you the vendor and product name. It can't tell you how the engine is actually running, so confirm that through attestation, profiles, transfer tests, and real load.

### 3.2 Contract fields

| Field                      | Default           | Requirement                                                                                                                |
| -------------------------- | ----------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `vllm_version`             | required          | The exact string each engine returns from `/version`.                                                                      |
| `image_digest`             | `""`              | The immutable container digest (`sha256:<64 hex>`), taken from engine-side attestation.                                    |
| `nixl_version`             | `""`              | The NIXL package version installed in the image.                                                                           |
| `nixl_connector_version`   | `0`               | A positive `NIXL_CONNECTOR_VERSION`, read from the deployed connector's metadata.                                          |
| `model_architecture`       | `""`              | The model implementation name. It matters because it affects KV layout.                                                    |
| `model_dtype`              | `""`              | The dtype the model runs in.                                                                                               |
| `kv_heads`                 | `0`               | Positive. The value of `ModelConfig.get_total_num_kv_heads()` for the whole model.                                         |
| `head_size`                | `0`               | Positive. The value of `ModelConfig.get_head_size()`, which the NIXL compatibility hash uses.                              |
| `hidden_layers`            | `0`               | Positive. The value of `ModelConfig.get_total_num_hidden_layers()` for the whole model.                                    |
| `attention_backend`        | `""`              | The backend you expect based on the recorded launch.                                                                       |
| `kv_cache_dtype`           | `""`              | The KV-cache dtype.                                                                                                        |
| `cross_layers_blocks`      | `null`            | How physical cache blocks are grouped. This is `KVCacheLayout.is_block_outermost`, resolved through the pinned layout API. |
| `hybrid_kv_cache_manager`  | `null`            | Whether vLLM's hybrid KV-cache manager is part of the layout.                                                              |
| `connector`                | `"NixlConnector"` | The engine-side connector name. Nonempty.                                                                                  |
| `kv_role`                  | `""`              | The engine-side KV role, such as `kv_both`.                                                                                |
| `transfer_mode`            | `""`              | `pull` for `NixlPullConnector`, `push` for `NixlPushConnector`.                                                            |
| `speculative_config`       | `""`              | A stable name for your speculative-decoding setup, or `disabled`.                                                          |
| `enforce_handshake_compat` | `true`            | The effective value from the pinned NIXL worker extra-config lookup. Narwhal requires `true`.                              |

Don't fill these in from memory. Five of them are especially easy to get wrong: `nixl_connector_version`, the resolved `head_size` dimensions, `cross_layers_blocks`, the resolved `transfer_mode` class and mode, and `enforce_handshake_compat` (both the configured value and the installed default). Capture them in [Gate E: capture attestation inputs](../deploy/05-Attest.md#capture-the-attestation-inputs).

### 3.3 Attestation

The generator in [Gate E: attest the live engine processes](../deploy/05-Attest.md) writes `runs/engine-launch-*/engine-attestation.json`. It builds the file from the checked serving plan, runtime inspection, a live cache capture, and the startup log.

Once the sidecars are up, run `tools/deployment/attestation_contract.py finalize-fleet` in the router shell. It reads the attestations and fills in `engine_contract` in `runs/deployment/fleet.json`. If any contract is incomplete or the contracts don't match, it refuses to finish.

Each attestation document declares its identity like this (see the [example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-attestation.example.json)):

```json
{
  "schema": "narwhal.attestation",
  "schema_version": 1
}
```

`narwhal-attest` needs at least one nonempty source entry for every populated contract field. Run one sidecar beside each contracted engine and set that engine's `attestation_url` to the sidecar's address.

At startup the sidecar reads `/version` and the `process_start_time_seconds` metric from `/metrics`. If the version differs from `vllm_version` in the attestation document, it exits with an error. Each response after that carries the contract, its sources, the two identity values under `engine`, and an `attestation_digest` over the whole response.

If either identity value can't be read, or changes from its startup value, every route returns HTTP 503. Treat that as a changed engine process: check the engine's HTTP endpoints, then restart the sidecar through its process manager.

`narwhal-check` verifies the attested fields and the process start time. On a mismatch, preflight halts before the NIXL handshake, the produce/consume probes, and the live KV transfer.
