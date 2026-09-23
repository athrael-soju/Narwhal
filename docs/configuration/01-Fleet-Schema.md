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

The loader accepts the listed keys and underscore-prefixed annotation fields, and reports cross-field validation failures together.

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

`narwhal-serve` resolves relative `profiles.path` and `recovery.state_path` values from its working directory.

The `--journal` flag selects a journal path. By default, `narwhal-serve` writes `journal.jsonl` beside `profiles.path`.

### 1.3 Environment loading

Narwhal reads variables from the process environment. The [deployment workflow](../deploy/01-Discover.md#load-the-private-environment) loads workstation `.env` and generates role-specific environments for remote commands.

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

The loader requires a populated referenced variable and reports the URL field path and variable name when that check fails.

The loader rejects partial and default-syntax `${...}` references and resolved values containing another reference. It leaves shell-style strings such as `$NAME` and non-endpoint JSON fields literal.

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

Define a complete `engine_contract` for production traffic. Preflight, lifecycle readmission, restart handling, and live NIXL checks use it to distinguish a restarted process from a different engine build before peer transfer.

Lifecycle drain and readmission require every contract field. If a required contract value is empty, the engine remains held out with `missing-contract`.

### 3.1 Hardware block

The [engine-host inspection](../deploy/03-Validate-Engines.md#inspect-every-engine-host) identifies the accelerator vendor and product; set `hardware.accelerator` to the observed product name. Set `hardware.accelerators_per_engine` to the replica's allocated accelerator count and `hardware.tensor_parallel` to the external launcher's vLLM TP size. Both counts must be positive, with TP at or below the allocation.

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
| `connector`                | `"NixlConnector"` | Engine-side connector name; requires a nonempty string.                                                                                                                                                      |
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

The [attestation example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-attestation.example.json) declares:

```json
{
  "schema": "narwhal.attestation",
  "schema_version": 1
}
```

`narwhal-attest` requires at least one nonempty source entry for each populated contract field before querying the engine.

Run one sidecar beside every contracted engine and configure the engine's `attestation_url`.

At startup, `narwhal-attest` reads `/version` and `process_start_time_seconds`, adds both values to `contract` and `sources`, then hashes the complete response into `attestation_digest`.

If either identity value changes later, both sidecar routes return HTTP 503.

After an engine process changes, verify its HTTP endpoints and restart the sidecar through its configured process manager so the new instance binds to the new process identity.

`narwhal-check` verifies attested fields and process start time before opening the NIXL handshake or running produce/consume probes. A mismatch stops preflight before live KV transfer.
