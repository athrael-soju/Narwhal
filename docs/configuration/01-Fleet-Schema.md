# Fleet schema and engine contract

## 1. Configuration model

### 1.1 Accepted top-level objects

A fleet document can contain these top-level keys: `schema`, `schema_version`, `model`, `hardware`, `engines`, `engine_contract`, `slo`, `controller`, `serving`, `engine`, `recovery`, and `profiles`. Keys that start with an underscore are treated as annotations and are also accepted. When cross-field checks fail, the loader reports all of the failures together rather than stopping at the first one.

Values must use JSON-native types: `true` or `false` for booleans, JSON integers for counts, and finite JSON numbers for durations and ratios. Type errors name the public field path:

```text
controller.monitor_interval_s must be a number; serving.max_connections must be an integer; engine.tokenize must be a boolean
```

### 1.2 Paths

The deployment workflow resolves relative fleet and profile paths from the checkout root. `narwhal-serve` works differently and resolves relative `profiles.path` and `recovery.state_path` values from its own working directory. The request journal's default location and the `--journal` flag are covered in [§19](06-Fabric-and-Operations.md#19-request-journal).

### 1.3 Environment loading

Narwhal reads its variables from the process environment. The [deployment workflow](../deploy/01-Discover.md#load-the-private-environment) loads the workstation's `.env` and generates a separate environment for each remote role. The variables you will see most often are:

| Variable                            | Use                                                                                                                            |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `NARWHAL_FLEET`                     | Fleet JSON used by `make observe` and by Python callers of `create_app()`. CLI commands still need `--fleet "$NARWHAL_FLEET"`. |
| `NARWHAL_ENGINE_KEY`                | Engine Bearer credential. When it's set, discovery points `engine.engine_api_key_env` at this name.                            |
| `NARWHAL_ROUTER_URL`                | Router that `make observe` scrapes.                                                                                            |
| `NARWHAL_GRAFANA_BIND_ADDRESS`      | Overrides the Grafana bind address, host only. Grafana keeps port 3000.                                                        |
| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | Overrides the Prometheus listen address, as `host:port`.                                                                       |

In the example observability setup, Grafana listens on `127.0.0.1:3000` and Prometheus on `127.0.0.1:9090`.

The fleet JSON holds model names, hardware shape, SLOs, profiles, and engine endpoints. Credentials stay out of it: engine launch credentials are handled by the engine launcher, and public client authentication happens at ingress.

An engine's `url` or `attestation_url` can refer to an environment variable, provided the reference makes up the whole JSON value:

```json
{
  "url": "${NARWHAL_NODE_1_URL}"
}
```

The referenced variable must be set to a nonempty value. If it isn't, the loader reports the URL's field path and the variable name. Partial references such as `http://${HOST}:8000`, default syntax inside `${...}`, and values that resolve to another reference are all rejected. Shell-style `$NAME` strings are left as literal text, and so is every JSON field other than the two endpoint fields.

`FleetConfig.save()` writes URLs in their resolved form, so save its output to a fleet path that Git ignores.

---

## 2. Minimal fleet definition

A serving fleet needs at least these fields:

| Field            | Required value    | Meaning                                                                  |
| ---------------- | ----------------- | ------------------------------------------------------------------------ |
| `schema`         | `"narwhal.fleet"` | Fleet interface identity.                                                |
| `schema_version` | `1`               | Fleet document version.                                                  |
| `model`          | string            | Model name the router exposes. Every engine must serve this name.        |
| `engines`        | nonempty array    | Engine instances, each with a unique `iid`.                              |
| `slo.ttft_s`     | positive seconds  | TTFT target used for admission, placement, load projection, and control. |
| `slo.tpot_s`     | positive seconds  | TPOT target used for admission, placement, load projection, and control. |

Each engine entry accepts:

| Field             | Default    | Meaning                                                                                                |
| ----------------- | ---------- | ------------------------------------------------------------------------------------------------------ |
| `iid`             | required   | Scheduler identity, unique within `engines`.                                                           |
| `url`             | required   | vLLM HTTP base URL. Trailing `/` characters are removed.                                               |
| `attestation_url` | `""`       | Full URL of the attestation sidecar. `narwhal-check` requires it when `engine_contract` is configured. |
| `role`            | `"decode"` | Initial role, either `"prefill"` or `"decode"`.                                                        |
| `pin`             | `false`    | Stops the configured role from changing.                                                               |
| `shared_device`   | `null`     | Allocation for an engine that shares a GPU with other engines. Described below the example.            |

For example:

```json
{
  "iid": "n0",
  "url": "http://node0:8000",
  "attestation_url": "http://node0:8010/v1/attestation",
  "role": "prefill",
  "pin": false
}
```

A pinned engine keeps its configured role through controller moves, placement changes, and resume. That makes pinning one way to hold prefill capacity in reserve for warm-standby takeover.

`shared_device` describes an engine that shares one GPU with other engines, as covered in [running several engines on one GPU](../cli/Engine.md#running-several-engines-on-one-gpu). When present, it needs all four fields. `group` and `gpu_uuid` are nonempty strings. `device_allowance` and `gpu_memory_utilization` are fractions of the GPU's total memory, each above 0 and at most 1. A group holds two to eight engines with the same `gpu_uuid` and `device_allowance`, and their `gpu_memory_utilization` values can't add up to more than that allowance. `narwhal-profile --colocated` loads the other engines in the group while it profiles each one.

Set the TTFT and TPOT targets from measurements taken on the engine shape you actually deploy. Narwhal also derives its Prometheus histogram buckets from them.

---

## 3. Engine shape and compatibility contract

Production fleets should define a complete `engine_contract`. Preflight, lifecycle readmission, restart handling, and live NIXL checks all use it to tell a restarted process apart from a different engine build before any peer transfer takes place. Lifecycle drain and readmission need a complete contract, with every field except `image_digest` set. If one is empty, drain can't record the engine's process identity and readmission fails, so the engine stays held out.

### 3.1 Hardware block

Set `hardware.accelerator` to the product name reported by the [engine-host inspection](../deploy/03-Validate-Engines.md#inspect-every-engine-host). `hardware.accelerators_per_engine` is the number of accelerators allocated to each replica, and `hardware.tensor_parallel` is the vLLM TP size used by the external launcher. Both must be positive, and TP cannot exceed the allocation.

These values describe the shape you intend to run. Attestation, profiling, transfer tests, and deployment load are how you confirm that the engines really run it.

### 3.2 Contract fields

| Field                      | Default           | Requirement                                                                                                                                                                                                                                    |
| -------------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vllm_version`             | required          | Exact value returned by every engine's `/version` endpoint.                                                                                                                                                                                    |
| `image_digest`             | `""`              | Immutable `sha256:<64 hex>` container digest from engine-side attestation.                                                                                                                                                                     |
| `nixl_version`             | `""`              | NIXL package version in the image.                                                                                                                                                                                                             |
| `nixl_connector_version`   | `0`               | Positive `NIXL_CONNECTOR_VERSION` from the deployed connector metadata. Capture it with the other [attestation inputs](../deploy/05-Attest.md#capture-attestation-inputs).                                                                     |
| `model_architecture`       | `""`              | Model implementation name that determines KV layout.                                                                                                                                                                                           |
| `model_dtype`              | `""`              | Model execution dtype.                                                                                                                                                                                                                         |
| `kv_heads`                 | `0`               | Positive model-wide result of `ModelConfig.get_total_num_kv_heads()`.                                                                                                                                                                          |
| `head_size`                | `0`               | Positive `ModelConfig.get_head_size()`, as used in the NIXL compatibility hash. Capture the resolved dimensions with the other [attestation inputs](../deploy/05-Attest.md#capture-attestation-inputs).                                        |
| `hidden_layers`            | `0`               | Positive model-wide result of `ModelConfig.get_total_num_hidden_layers()`.                                                                                                                                                                     |
| `attention_backend`        | `""`              | Attention backend expected from the recorded launch.                                                                                                                                                                                           |
| `kv_cache_dtype`           | `""`              | KV-cache dtype.                                                                                                                                                                                                                                |
| `cross_layers_blocks`      | `null`            | Resolved physical cache-block grouping (`KVCacheLayout.is_block_outermost`) from the pinned layout API. Capture it with the other [attestation inputs](../deploy/05-Attest.md#capture-attestation-inputs).                                     |
| `hybrid_kv_cache_manager`  | `null`            | Whether vLLM's hybrid KV-cache manager takes part in the layout.                                                                                                                                                                               |
| `connector`                | `"NixlConnector"` | Engine-side connector name. Must be a nonempty string.                                                                                                                                                                                         |
| `kv_role`                  | `""`              | Engine-side KV-role semantics, such as `kv_both`.                                                                                                                                                                                              |
| `transfer_mode`            | `""`              | `pull` for `NixlPullConnector`, `push` for `NixlPushConnector`. Keep the resolved class and mode with the other [attestation inputs](../deploy/05-Attest.md#capture-attestation-inputs).                                                       |
| `speculative_config`       | `""`              | Stable name of the speculative-decoding configuration, or `disabled`.                                                                                                                                                                          |
| `enforce_handshake_compat` | `true`            | Effective value from the pinned NIXL worker's extra-config lookup. Narwhal requires `true`. Record both the configured value and the installed default with the other [attestation inputs](../deploy/05-Attest.md#capture-attestation-inputs). |

### 3.3 Attestation

The attestation generator described in [Deploy](../deploy/05-Attest.md) builds `runs/engine-launch-*/engine-attestation.json` from the checked serving plan, a runtime inspection, a live cache capture, and the startup log. Finalizing the router fleet reads the live sidecars, requires every contract to be complete and to match, and fills in `engine_contract` in `runs/deployment/fleet.json`.

Attestation documents declare their own schema, as the [attestation example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-attestation.example.json) shows:

```json
{
  "schema": "narwhal.attestation",
  "schema_version": 1
}
```

Run one `narwhal-attest` sidecar next to each contracted engine and set that engine's `attestation_url`. Before it queries the engine, the sidecar checks that every populated contract field has at least one nonempty source entry. At startup it reads the engine's `/version` and `process_start_time_seconds`, adds both to `contract` and `sources`, and hashes the complete response into `attestation_digest`.

If either of those values changes later, both sidecar routes return HTTP 503. When an engine process is replaced, check its HTTP endpoints and then restart the sidecar through its process manager so that the new sidecar binds to the new process.

`narwhal-check` compares the attested fields and the process start time before it opens the NIXL handshake or runs any produce/consume probes. A mismatch ends preflight at that point, before any live KV transfer.
