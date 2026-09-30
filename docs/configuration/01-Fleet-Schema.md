# Fleet schema and engine contract

## 1. Configuration model

### 1.1 Top-level keys

The loader accepts these top-level keys: `schema`, `schema_version`, `model`, `hardware`, `engines`, `engine_contract`, `slo`, `controller`, `serving`, `engine`, `recovery`, and `profiles`. It also accepts annotation keys, which start with an underscore. Any other key is an error. One run reports every cross-field validation failure together.

Use JSON-native types:

| Value             | JSON type         |
| ----------------- | ----------------- |
| Boolean           | `true` or `false` |
| Count             | Integer           |
| Duration or ratio | Finite number     |

A type mistake produces an error like this:

```text
engine.tokenize must be a boolean; serving.max_connections must be an integer; controller.monitor_interval_s must be a number
```

### 1.2 Paths

| Caller              | Relative paths                            | Resolved from                |
| ------------------- | ----------------------------------------- | ---------------------------- |
| Deployment workflow | Fleet and profile paths                   | Checkout root                |
| `narwhal-serve`     | `profiles.path` and `recovery.state_path` | Working directory at startup |

The journal defaults to `journal.jsonl` next to `profiles.path`. Pass `--journal` to set another path.

### 1.3 Environment loading

Narwhal reads its variables from the process environment. The [deployment workflow](../deploy/01-Discover.md#load-the-private-environment) loads your workstation `.env` and generates role-specific environments for remote commands.

| Variable                            | Use                                                                                                                         |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `NARWHAL_FLEET`                     | Fleet file for `make observe` and for Python callers of `create_app()`. CLI commands take `--fleet "$NARWHAL_FLEET"`.       |
| `NARWHAL_ENGINE_KEY`                | An example variable for the engine Bearer credential. Set `engine.engine_api_key_env` to the variable name you use.         |
| `NARWHAL_ROUTER_URL`                | The router target that `make observe` scrapes.                                                                              |
| `NARWHAL_GRAFANA_BIND_ADDRESS`      | Overrides the Grafana listener.                                                                                             |
| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | Overrides the Prometheus listener.                                                                                          |

The example monitoring stack binds these addresses:

| Service    | Address          |
| ---------- | ---------------- |
| Grafana    | `127.0.0.1:3000` |
| Prometheus | `127.0.0.1:9090` |

An engine `url` or `attestation_url` expands from the environment when the entire JSON string is a single `${NAME}` reference:

```json
{
  "url": "${NARWHAL_NODE_1_URL}"
}
```

Other cases:

| Case                                     | Result                                          |
| ---------------------------------------- | ----------------------------------------------- |
| Unset or blank variable                  | Error that names the URL field and the variable |
| Partial reference, or default syntax     | Error                                           |
| Resolved value that contains a reference | Error                                           |
| `$NAME`, or `${...}` in any other field  | Literal text                                    |

`FleetConfig.save()` writes resolved URLs. Save its output to an ignored fleet path.

---

## 2. Minimal fleet definition

A fleet needs six fields:

| Field            | Value             | Notes                                                                                         |
| ---------------- | ----------------- | --------------------------------------------------------------------------------------------- |
| `schema`         | `"narwhal.fleet"` | Identifies the fleet interface.                                                               |
| `schema_version` | `1`               | The configuration version.                                                                    |
| `model`          | string            | The model name the router exposes. Every engine must serve this exact name.                   |
| `engines`        | nonempty array    | The engine instances. Each `iid` must be unique.                                              |
| `slo.ttft_s`     | positive seconds  | Time-to-first-token (TTFT) target. Drives admission, placement, load projection, and control. |
| `slo.tpot_s`     | positive seconds  | Time-per-output-token (TPOT) target. Used in the same four places.                            |

Narwhal derives its Prometheus histogram buckets from the TTFT and TPOT targets. Set both targets from measurements on the deployed engine shape.

Each engine entry takes these fields:

| Field             | Default    | Notes                                                                                                       |
| ----------------- | ---------- | ----------------------------------------------------------------------------------------------------------- |
| `iid`             | required   | The scheduler identity. Unique within `engines`.                                                            |
| `url`             | required   | The vLLM HTTP base URL. The loader strips trailing `/` characters.                                          |
| `attestation_url` | `""`       | The full URL of the attestation sidecar. Required by `narwhal-check` if the fleet has an `engine_contract`. |
| `role`            | `"decode"` | The starting role: `"prefill"` or `"decode"`.                                                               |
| `pin`             | `false`    | When true, the engine keeps its configured role.                                                            |

```json
{
  "iid": "n0",
  "url": "http://node0:8000",
  "attestation_url": "http://node0:8010/v1/attestation",
  "role": "prefill",
  "pin": false
}
```

A pinned engine keeps its role through controller moves, placement changes, and resume. One use is reserving prefill capacity for a warm-standby takeover.

---

## 3. Engine shape and compatibility contract

Every production engine needs a complete `engine_contract`. Preflight, lifecycle readmission, restart handling, and the live NIXL checks read it. Drain and readmission need every field. An engine with an empty required value stays held out with `missing-contract`.

### 3.1 Hardware block

The `hardware` block is optional. When present, it needs all three fields.

| Field                              | Notes                                                                                                                                                        |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `hardware.accelerator`             | The accelerator product name you saw during [engine-host inspection](../deploy/03-Validate-Engines.md#inspect-every-engine-host). Must be a nonempty string. |
| `hardware.accelerators_per_engine` | Accelerators per replica. An integer, 1 or more.                                                                                                             |
| `hardware.tensor_parallel`         | The TP size your launcher passes to vLLM. An integer between 1 and `hardware.accelerators_per_engine`.                                                       |

Host inspection reports the vendor and product name. Confirm how the engine runs through attestation, profiles, transfer tests, and real load.

### 3.2 Contract fields

| Field                      | Default           | Requirement                                                                                                                |
| -------------------------- | ----------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `vllm_version`             | required          | The exact string each engine returns from `/version`.                                                                      |
| `image_digest`             | `""`              | The immutable container digest (`sha256:<64 hex>`), taken from engine-side attestation.                                    |
| `nixl_version`             | `""`              | The NIXL package version installed in the image.                                                                           |
| `nixl_connector_version`   | `0`               | A positive `NIXL_CONNECTOR_VERSION`, read from the deployed connector's metadata.                                          |
| `model_architecture`       | `""`              | The model implementation name. It affects KV layout.                                                                       |
| `model_dtype`              | `""`              | The dtype the model runs in.                                                                                               |
| `kv_heads`                 | `0`               | Positive. The value of `ModelConfig.get_total_num_kv_heads()` for the whole model.                                         |
| `head_size`                | `0`               | Positive. The value of `ModelConfig.get_head_size()`, which the NIXL compatibility hash uses.                              |
| `hidden_layers`            | `0`               | Positive. The value of `ModelConfig.get_total_num_hidden_layers()` for the whole model.                                    |
| `attention_backend`        | `""`              | The expected backend from the recorded launch.                                                                             |
| `kv_cache_dtype`           | `""`              | The KV-cache dtype.                                                                                                        |
| `cross_layers_blocks`      | `null`            | How physical cache blocks are grouped. This is `KVCacheLayout.is_block_outermost`, resolved through the pinned layout API. |
| `hybrid_kv_cache_manager`  | `null`            | Whether vLLM's hybrid KV-cache manager is part of the layout.                                                              |
| `connector`                | `"NixlConnector"` | The engine-side connector name. Nonempty.                                                                                  |
| `kv_role`                  | `""`              | The engine-side KV role, such as `kv_both`.                                                                                |
| `transfer_mode`            | `""`              | `pull` for `NixlPullConnector`, `push` for `NixlPushConnector`.                                                            |
| `speculative_config`       | `""`              | A stable name for your speculative-decoding setup, or `disabled`.                                                          |
| `enforce_handshake_compat` | `true`            | The effective value from the pinned NIXL worker extra-config lookup. Narwhal requires `true`.                              |

Capture every contract value from the deployed engine in [Gate E: capture attestation inputs](../deploy/05-Attest.md#capture-the-attestation-inputs). Take extra care with these five:

- `nixl_connector_version`
- the resolved `head_size` dimensions
- `cross_layers_blocks`
- the resolved `transfer_mode` class and mode
- `enforce_handshake_compat`, both the configured value and the installed default

### 3.3 Attestation

The generator in [Gate E: attest the live engine processes](../deploy/05-Attest.md) writes `runs/engine-launch-*/engine-attestation.json`. Its inputs are the checked serving plan, runtime inspection, a live cache capture, and the startup log.

Fill in the fleet contract:

1. Start the sidecars.
2. Run `tools/deployment/attestation_contract.py finalize-fleet` in the router shell.

The command fills in `engine_contract` in `runs/deployment/fleet.json` from the attestations. It exits with status 1 when any contract is incomplete or the contracts differ.

Each attestation document declares this identity ([example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-attestation.example.json)):

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

The sidecar has two identity values, both read at startup: the engine's `/version` and the `process_start_time_seconds` metric from `/metrics`.

| Condition                                                                     | Sidecar result               |
| ----------------------------------------------------------------------------- | ---------------------------- |
| `/version` at startup differs from `vllm_version` in the attestation document | Exits with an error          |
| Either identity value is unreadable or differs from its startup value         | Every route returns HTTP 503 |

Each sidecar response carries:

- the contract
- its sources
- the two identity values, under `engine`
- an `attestation_digest` over the whole response

Treat an HTTP 503 as a changed engine process:

1. Check the engine's HTTP endpoints.
2. Restart the sidecar through its process manager.

`narwhal-check` verifies the attested fields and the process start time. On a mismatch, preflight halts before the NIXL handshake, the produce/consume probes, and the live KV transfer.
