# Fleet schema and engine contract

## 1. Configuration model

### 1.1 Accepted top-level objects

The native fleet document accepts these top-level keys:

- `schema`
- `schema_version`
- `model`
- `hardware`
- `engines`
- `engine_contract`
- `slo`
- `controller`
- `serving`
- `engine`
- `recovery`
- `profiles`

Unknown keys are rejected, except underscore-prefixed annotation fields. Validation accumulates cross-field failures and reports them together.

Use JSON-native types:

- booleans: `true` or `false`
- counts: JSON integers
- durations and ratios: finite JSON numbers

Type failures are reported with public field paths, for example:

```text
engine.tokenize must be a boolean; serving.max_connections must be an integer; controller.monitor_interval_s must be a number
```

### 1.2 Paths

Relative fleet and profile paths resolve from the checkout root when used by the deployment workflow.

At serving time, relative values for:

- `profiles.path`
- `recovery.state_path`

resolve from the serving process working directory.

`narwhal-serve --journal` is CLI-only. When omitted, the journal is placed beside `profiles.path` as `journal.jsonl`.

### 1.3 Environment loading

Narwhal reads the process environment directly, without loading `.env`.

The deployment workflow in [Deploy a fleet](../deploy/01-Discover.md#load-the-private-environment) defines how workstation variables are loaded and how role-specific environments are generated for remote commands.

Common variables are:

| Variable                            | Use                                                                                                                                    |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `NARWHAL_FLEET`                     | Selects the fleet JSON for `make observe` and Python callers of `create_app()`. CLI commands still require `--fleet "$NARWHAL_FLEET"`. |
| `NARWHAL_ENGINE_KEY`                | Example Bearer credential variable for engine authentication. Point `engine.engine_api_key_env` at this name.                          |
| `NARWHAL_ROUTER_URL`                | Router target scraped by `make observe`.                                                                                               |
| `NARWHAL_GRAFANA_BIND_ADDRESS`      | Grafana listener override.                                                                                                             |
| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | Prometheus listener override.                                                                                                          |

The example observability setup binds Grafana to `127.0.0.1:3000` and Prometheus to `127.0.0.1:9090`.

Fleet JSON owns model names, hardware shape, SLOs, profiles, and engine endpoints. Engine launch credentials belong to the engine launcher. Public client authentication belongs at ingress.

Engine `url` and `attestation_url` values support environment substitution only when the complete JSON value is a single reference:

```json
{
  "url": "${NARWHAL_NODE_1_URL}"
}
```

A missing or empty referenced variable fails configuration loading and reports both the field path and variable name.

Only complete URL values can be substituted. Shell expressions, default syntax, partial interpolation, and recursive expansion are unsupported. All other JSON fields remain literal.

`FleetConfig.save()` writes resolved URLs. Save that output only to an ignored fleet path.

---

## 2. Minimal fleet definition

A valid serving fleet needs the following fields.

| Field            | Required value    | Meaning                                                                  |
| ---------------- | ----------------- | ------------------------------------------------------------------------ |
| `schema`         | `"narwhal.fleet"` | Fleet interface identity.                                                |
| `schema_version` | `1`               | Fleet document version.                                                  |
| `model`          | string            | Model name exposed by the router. Every engine must serve this name.     |
| `engines`        | nonempty array    | Engine instances. `iid` values must be unique.                           |
| `slo.ttft_s`     | positive seconds  | TTFT target used for admission, placement, load projection, and control. |
| `slo.tpot_s`     | positive seconds  | TPOT target used for admission, placement, load projection, and control. |

Each engine entry accepts:

| Field             | Default    | Meaning                                                                                         |
| ----------------- | ---------- | ----------------------------------------------------------------------------------------------- |
| `iid`             | required   | Scheduler identity, unique within `engines`.                                                    |
| `url`             | required   | vLLM HTTP base URL. Trailing `/` characters are removed.                                        |
| `attestation_url` | `""`       | Full attestation-sidecar URL. Required by `narwhal-check` when `engine_contract` is configured. |
| `role`            | `"decode"` | Initial role. Accepted values are `"prefill"` and `"decode"`.                                   |
| `pin`             | `false`    | Prevents the configured role from changing.                                                     |

Example:

```json
{
  "iid": "n0",
  "url": "http://node0:8000",
  "attestation_url": "http://node0:8010/v1/attestation",
  "role": "prefill",
  "pin": false
}
```

An engine defaults to `decode`. A pinned engine retains its configured role through controller movement, placement changes, and resume. Pinning can reserve prefill capacity for warm-standby takeover.

Set TTFT and TPOT targets from measurements taken on the deployed engine shape. Narwhal derives Prometheus histogram buckets from those values.

---

## 3. Engine shape and compatibility contract

Production fleets serving client traffic should define a complete `engine_contract`.

Narwhal uses the contract during preflight, lifecycle readmission, restart handling, and live NIXL checks. Before it exercises NIXL against a peer, the router must be able to distinguish a restarted process from a different engine build.

Lifecycle drain and readmission require every contract field. If a required contract value is empty, the engine remains held out with `missing-contract`.

### 3.1 Hardware block

`hardware` records the accelerator identity and tensor-parallel shape.

Engine-host inspection in [Deploy](../deploy/03-Validate-Engines.md#inspect-every-engine-host) discovers accelerator vendor and product. Set `hardware.accelerator` to that observed product name.

Both:

- `hardware.accelerators_per_engine`
- `hardware.tensor_parallel`

must be positive. Tensor parallelism cannot exceed the accelerator count assigned to a replica.

The external launcher controls vLLM TP size. Keep `hardware.tensor_parallel` equal to the launch value and `hardware.accelerators_per_engine` equal to the replica's allocated accelerator count.

Verify the running shape through attestation, profiles, transfer tests, and deployment load.

### 3.2 Contract fields

| Field                      | Default           | Requirement                                                                                                                                                                                                   |
| -------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vllm_version`             | required          | Exact value returned by every engine `/version` endpoint.                                                                                                                                                     |
| `image_digest`             | `""`              | Immutable `sha256:<64 hex>` container digest from engine-side attestation.                                                                                                                                    |
| `nixl_version`             | `""`              | NIXL package version in the image.                                                                                                                                                                            |
| `nixl_connector_version`   | `0`               | Positive `NIXL_CONNECTOR_VERSION` from deployed connector metadata. Capture it in [Gate E](../deploy/05-Attest.md#capture-attestation-inputs).                                                     |
| `model_architecture`       | `""`              | Model implementation name relevant to KV layout.                                                                                                                                                              |
| `model_dtype`              | `""`              | Model execution dtype.                                                                                                                                                                                        |
| `kv_heads`                 | `0`               | Positive model-wide `ModelConfig.get_total_num_kv_heads()` result.                                                                                                                                            |
| `head_size`                | `0`               | Positive `ModelConfig.get_head_size()` used by the NIXL compatibility hash. Capture the resolved dimensions in [Deploy](../deploy/05-Attest.md#capture-attestation-inputs).                    |
| `hidden_layers`            | `0`               | Positive model-wide `ModelConfig.get_total_num_hidden_layers()` result.                                                                                                                                       |
| `attention_backend`        | `""`              | Attention backend expected from the recorded launch.                                                                                                                                                          |
| `kv_cache_dtype`           | `""`              | KV-cache dtype.                                                                                                                                                                                               |
| `cross_layers_blocks`      | `null`            | Resolved physical cache-block grouping, `KVCacheLayout.is_block_outermost`, from the pinned layout API. Capture it in [Deploy](../deploy/05-Attest.md#capture-attestation-inputs).                                    |
| `hybrid_kv_cache_manager`  | `null`            | Whether vLLM's hybrid KV-cache manager participates in layout.                                                                                                                                                |
| `connector`                | `"NixlConnector"` | Engine-side connector name. Cannot be empty.                                                                                                                                                                  |
| `kv_role`                  | `""`              | Engine-side KV-role semantics, for example `kv_both`.                                                                                                                                                         |
| `transfer_mode`            | `""`              | `pull` for `NixlPullConnector`, `push` for `NixlPushConnector`. Retain resolved class and mode in [Deploy](../deploy/05-Attest.md#capture-attestation-inputs).                                                             |
| `speculative_config`       | `""`              | Stable speculative-decoding configuration name, or `disabled`.                                                                                                                                                |
| `enforce_handshake_compat` | `true`            | Effective value from the pinned NIXL worker extra-config lookup. Narwhal requires `true`. Capture configured and installed-default values in [Deploy](../deploy/05-Attest.md#capture-attestation-inputs). |

### 3.3 Attestation

The attestation generator described in [Deploy](../deploy/05-Attest.md) writes:

```text
runs/engine-launch-*/engine-attestation.json
```

It is assembled from the checked serving plan, runtime inspection, live cache capture, and startup log.

Router finalisation reads the live sidecars, requires complete matching contracts, and fills `engine_contract` in:

```text
runs/deployment/fleet.json
```

The development/schema example is:

[config/engine-attestation.example.json](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-attestation.example.json)

The document declares:

```json
{
  "schema": "narwhal.attestation",
  "schema_version": 1
}
```

`narwhal-attest` requires at least one nonempty source entry for each populated contract field before querying the engine.

Run one sidecar beside every contracted engine and configure the engine's `attestation_url`.

At sidecar startup, Narwhal reads:

- `/version`
- `process_start_time_seconds`

Those process-identity values are added to both `contract` and `sources`, then the complete response is hashed into `attestation_digest`.

If either identity value changes later, both sidecar routes return HTTP 503.

After an engine process changes:

1. verify the engine HTTP endpoints,
2. restart the sidecar through its configured process manager.

The new sidecar instance then binds to the new process identity.

`narwhal-check` verifies attested fields and process start time before opening the NIXL handshake or running produce/consume probes. A mismatch stops preflight before live KV transfer.
