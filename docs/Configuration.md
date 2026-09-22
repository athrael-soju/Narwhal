# Configuration

Narwhal loads one JSON fleet document for one model and one engine fleet before starting the router. Serving another model requires a separate router and fleet, with model routing handled at ingress.

Print the annotated example configuration with:

```bash
.venv/bin/narwhal-check --print-example-config
```

`narwhal-serve`, `narwhal-profile`, and `narwhal-check` read the native fleet document supplied through `--fleet`. Python code calling `create_app()` directly can use `NARWHAL_FLEET`.

Valid top-level keys are `model`, `hardware`, `engines`, `engine_contract`, `slo`, `controller`, `serving`, `engine`, `recovery`, and `profiles`. Objects reject unknown keys except underscore-prefixed annotations. Validation collects cross-field errors and reports them in one pass.

Use JSON `true` and `false` for booleans, JSON integers for counts, and finite JSON numbers for durations and ratios. Type errors use public field paths, for example:

`engine.tokenize must be a boolean; serving.max_connections must be an integer; controller.monitor_interval_s must be a number`

Fleet documents declare:

```json
"schema": "narwhal.fleet",
"schema_version": 1
```

Version 1 leaves client identity and content capture at ingress. Narwhal owns one global admission budget and request timing over token counts and durations. Schema validation runs before fleet fields are read. The [contract-version reference](Telemetry-and-Artifacts.md#contract-versions) defines the full interface set.

## Environment variables

Narwhal reads the process environment directly. It does not load `.env`. [Deploy a fleet](Deploy.md#load-the-private-environment) defines the workstation input and export procedure; deployment discovery creates role-specific environments for remote commands. Relative fleet and profile paths resolve from the checkout root.

- `NARWHAL_FLEET` selects the fleet JSON used by `make observe` and Python callers of `create_app()`. CLI commands still require `--fleet "$NARWHAL_FLEET"`.
- `NARWHAL_ENGINE_KEY` is the example shared Bearer token name for engine authentication. Set `engine.engine_api_key_env` to `"NARWHAL_ENGINE_KEY"` in the fleet JSON, then define the variable in `.env`. [Engine authentication](#engine-authentication) lists the requests that carry it.
- `NARWHAL_ROUTER_URL` selects the router target scraped by `make observe`. Configure the router listener separately with `narwhal-serve --host` and `--port`.
- `NARWHAL_GRAFANA_BIND_ADDRESS` and `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` configure observability listeners. The example binds Grafana to `127.0.0.1` on port 3000 and Prometheus to `127.0.0.1:9090`.

Fleet JSON owns model names, hardware, profiles, and SLOs. Engine launch credentials belong to the engine launcher. Public client authentication belongs to ingress. Engine `url` and `attestation_url` values are resolved when the entire value is an environment-variable reference.

### Generate deployment configuration

[Deployment discovery](Deploy.md#discover-the-deployed-hosts) derives the host inventory, SSH trust store, fleet, launch records, and source index from the supplied `.env` plus remote inspection. [Launch policy](Deploy.md#launch-policy) defines defaults and environment overrides.

Keep `config/hosts.local.json`, `config/ssh.known_hosts`, `config/engine-launch.local.json`, `config/engine-launch.sources.json`, `config/fleet.json` and `config/deployment.env` together as private inputs for the inspected fleet. Discovery observations and command logs remain under `runs/discovery/<run>/`. Reuse the saved inputs after loading `.env` and `config/deployment.env`; [engine inspection](Deploy.md#3-inspect-each-engine-host) checks their current host, image and model assumptions before launch.

### Host environment files

`NARWHAL_DEPLOYMENT_REVISION` specifies the full commit SHA from the management checkout.

`tools/deployment/deploy_hosts.py prepare` packages that revision as `source.bundle` and verifies the bundle by cloning it locally into a fresh checkout. `install` copies the bundle to each selected host. Each remote checkout clones from the bundle and verifies its revision against the role file before installation.

Store `source.bundle` beside the generated environment files under ignored `runs/deployment-env/`.

From the management checkout:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out <directory>
```

`prepare` exports each role from the loaded workstation environment and generated fleet JSON. `tools/deployment/prepare_host_env.py` selects the exported fields, preserves shell literals, and writes mode-0600 role files. [Host installation](Deploy.md#2-install-narwhal) contains the preparation, installation, and role-shell sequence.

| Remote file                                              | Exported values                                                                                                                                                          | Workstation source                                                                                                                                                                                                          |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runs/deployment/.env.router`                           | Revision, `NARWHAL_FLEET=runs/deployment/fleet.json`, referenced engine and attestation URLs, configured engine API credential, optional router and observability settings. | `NARWHAL_DEPLOYMENT_REVISION`, variables referenced by fleet endpoint fields and `engine.engine_api_key_env`, `NARWHAL_ROUTER_URL`, `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`.                    |
| `runs/deployment/.env.engine-<n>`                       | Revision, launch and artifact fields, selected node URLs, fabric peer addresses, configured engine API credential.                                                       | Shared engine fields below, optional `NARWHAL_NODE_<n>_<field>` overrides, derived node service URLs and fabric addresses, and the configured engine API credential. |
| `config/engine-launch.engine-<n>.json`                   | Selected GPU allocation, TP size, device mappings, resolved UCX selection, generated launch arguments.                                                                   | The engine role in workstation `NARWHAL_LAUNCH_CONFIG`.                                                                                                                                                                     |
| `runs/deployment-tools/launch_engine.py` on engine hosts | Standalone launcher snapshot, with its path and SHA-256 recorded in the engine role environment.                                                                         | `tools/deployment/launch_engine.py` from the management checkout at preparation time.                                                                                                                                                  |
| `runs/deployment-tools/fabric_budget.py` on engine hosts | Standalone calculator snapshot, with path and SHA-256 recorded in `.env.engine-<n>`.                                                                                     | `tools/deployment/fabric_budget.py` from the management checkout at preparation time.                                                                                                                                                  |
| `runs/deployment-tools/cache_capture_hook.py` on engine hosts | Serving cache capture snapshot, with path and SHA-256 recorded in `.env.engine-<n>`. | `tools/deployment/cache_capture_hook.py` from the management checkout at preparation time. |
| `runs/deployment/fleet.json` on the router                  | Generated fleet document copied before deployment edits.                                                                                                                 | File selected by workstation `NARWHAL_FLEET`.                                                                                                                                                                               |

Engine export requires:

- `NARWHAL_ENGINE_IMAGE`
- `NARWHAL_ENGINE_MODEL_NAME`
- `NARWHAL_MODEL_DIR`
- `NARWHAL_RUN_DIR`
- `NARWHAL_MODEL_CONFIG_SHA256`
- `NARWHAL_FABRIC_INTERFACE`
- `NARWHAL_ENGINE_PORT`
- `NARWHAL_ATTEST_PORT`
- `NARWHAL_NIXL_SIDE_CHANNEL_PORT`
- `NARWHAL_UCX_TCP_PORT_RANGE`

The exporter sets:

```text
NARWHAL_ENGINE_LAUNCH_CONFIG=config/engine-launch.engine-<n>.json
```

Per-node overrides insert `NODE_<n>_` after `NARWHAL_`. For example:

```text
NARWHAL_NODE_2_ENGINE_PORT
```

becomes:

```text
NARWHAL_ENGINE_PORT
```

inside `.env.engine-2`.

An empty override for a required field is reported as an error against that field. Fleet endpoint references and API-key references select variables by name. Variable names containing `SSH`, and access variables referenced by the host inventory, are rejected. Other values are selected from the explicit role fields.

Keep management destinations, passwords, SSH identities, and host keys in the workstation's private access files.

Prepared workstation files live under ignored `runs/deployment-env/`. Remote role environments and the effective fleet live under ignored `runs/deployment/`.

Load a role environment with:

```bash
deploy_hosts.py shell --run <directory> --role <role>
```

Shell tracing is disabled while the role file is loaded. A machine serving both router and engine roles keeps both files in one checkout; each role shell loads only its own file.

### Engine launch records

`NARWHAL_LAUNCH_CONFIG` selects the private generated `config/engine-launch.local.json` on the management workstation.

Discovery creates one record for every assigned `engine-<n>` role using:

- detected GPUs,
- device paths,
- image package versions,
- environment-selected launch policy.

Each record contains `sources` identifying the inspection and policy inputs retained for that record. `config/engine-launch.sources.json` indexes those references.

The [example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-launch.example.json) defines the schema.

To change a GPU allocation or runtime setting, change its `.env` policy input and rerun discovery into a fresh output set.

| Field                                            | Deployment use                                                                                                                             |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `accelerator`, `gpu_ids`, `tensor_parallel_size` | Product identity, selected GPU indices or UUIDs, and TP size for one replica. Compare them with host inspection and fleet hardware fields. |
| `gpu_visibility_env`                             | Chooses `ROCR_VISIBLE_DEVICES` or `CUDA_VISIBLE_DEVICES`. Preparation joins `gpu_ids` into the exported value.                             |
| `accelerator_devices`                            | Host device paths mapped into the container. ROCm deployments require `/dev/kfd` and DRI mappings for the allocated GPUs.                  |
| `network_mode`                                   | Uses `host` for the recorded network and port allocation.                                                                                  |
| `transfer.transport`                             | Declares `ucx_tcp` or `ucx_rdma` for inspection and transport checks.                                                                      |
| `transfer.net_devices`                           | Ethernet interface names for TCP or HCA:port names for RDMA. `${NARWHAL_FABRIC_INTERFACE}` resolves from the selected engine environment.  |
| `transfer.devices`                               | Transport device paths mapped into the container. RDMA requires its character devices. TCP uses an empty list.                             |
| `sources`                                        | Names the allocation, device, and transfer definitions behind the record.                                                                  |

`prepare` validates every assigned engine record before creating the output directory. It derives GPU visibility and `UCX_NET_DEVICES` values under `environment`, and writes `--tensor-parallel-size` under `vllm_args`.

`install` copies the selected record into the engine checkout's `config/` directory. The role environment points to that file.

The delivered launcher combines these recorded arguments and device mappings with the runtime fields described below, then records the complete container command.

Launch records and supporting extracts remain in ignored mode-0600 `config/engine-launch.*.json` files. The public example describes the schema; real allocations and runtime evidence stay private.

When preparation finds a bad `.env` input or missing remote prerequisite, correct the named input, regenerate the affected configuration, and prepare a new run so the manifest records the corrected state.

### Runtime launch records

Every generated engine record contains a `runtime` object consumed by `launch_engine.py`.

Discovery reads:

- package pins from the selected image,
- supported runtime environment fields from that image,
- model dtype from the model config,
- policy from [environment launch policy](Deploy.md#launch-policy).

Preparation transfers the record and a launcher snapshot to the engine host.

The [example record](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-launch.example.json) shows the schema.

| Runtime field                                 | Operator input                                                                                                                                                                                                                    |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `expected_packages`                           | Exact installed distribution versions for `vllm` and `nixl` or `nixl-rocm`; add any other image packages whose identity should be checked.                                                                                        |
| `model_dtype`, `kv_cache_dtype`, `block_size` | `bfloat16` or `float16`, `auto`, and the requested runtime block size. Cache planning records adjusted token-block size and padded page bytes for fabric sizing.                                                                  |
| `environment`                                 | Image-local ROCm/CUDA, UCX, NIXL, and library-path settings. The launcher supplies GPU visibility, advertised addresses and ports, transport selection, and engine authentication from the role environment.                      |
| `extra_args`                                  | Model-specific vLLM arguments covering context and batching limits, memory utilisation, reasoning parser, attention backend, remote model code, language-only loading, eager execution, async scheduling, or hybrid-cache policy. |

The launcher takes the model mount, served name, bind family, and HTTP port from the role environment. It applies the recorded TP size and configures `NixlConnector` with `kv_both`, UCX, and failure propagation.

Discovery adds `--trust-remote-code` when the checkpoint model or tokenizer metadata contains an `auto_map`. It sets `VLLM_SSM_CONV_STATE_LAYOUT=DS` when model metadata identifies convolutional SSM transfer state. These derived settings apply when `NARWHAL_ENGINE_ARGS` or `NARWHAL_ENGINE_ENV` supplies other values.

Extra arguments are validated against supported model options so they cannot override launcher-managed settings.

Before model startup, the image check:

1. verifies `--trust-remote-code` against the mounted checkpoint,
2. validates image identity,
3. checks exact distribution package versions,
4. validates connector configuration and import,
5. constructs the checkpoint tokenizer,
6. checks the pinned image's convolutional state layout for SSM models.

It records `vllm.version.__version__` as `vllm_api_version` in `checked.json`, tied to the launch-plan hash and image ID. The HTTP probe then compares `/version` with that captured value.

Use an image whose NIXL connector implements the fleet's required `kv_both` behaviour. Runtime transfer probes exercise both producer and consumer operations.

When `engine.engine_api_key_env` names a credential, the engine exporter also exposes it as `NARWHAL_ENGINE_API_KEY`. The launcher writes `VLLM_API_KEY` into mode-0600 `container.env` and gives that file to Docker.

Keep launch directories, environment files, and runtime captures under ignored `runs/`. Record the application revision, launcher digest, and container ID with the deployment.

### Fabric workload budget

During `prepare`, Narwhal snapshots `tools/deployment/fabric_budget.py` from the management checkout into the files prepared for each engine host. Its SHA-256 is recorded in both the manifest and role environment.

`install` verifies the transferred helper before placing it under ignored `runs/deployment-tools/`. The approved application bundle retains the selected application revision.

Record that revision together with `NARWHAL_FABRIC_BUDGET_SHA256`. Creating a new preparation directory captures a changed helper without modifying older prepared runs.

For each representative engine role and matching cache configuration:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate
```

Use `--runtime-layout` with the `cache-layout.json` captured from the running cache representative by `launch_engine.py capture-cache`.

The calculator verifies model and launch-record hashes, sums padded cache-page bounds across TP ranks, then derives the link rate required for the configured:

- prompt length,
- peak remote-handoff rate,
- burst,
- transfer-time budget.

[Transfer fabric preparation](Deploy.md#4-qualify-the-transfer-fabric) groups roles by discovered image, model, accelerator, TP, and runtime inputs. It retains a serving representative per group, derives the initial trial budget from that process's cache pages, and compares each directed edge with its source budget. Each running engine later verifies its resolved cache layout against its representative.

The representative stores a mode-0600 `runs/fabric-*/budget.json` containing:

- input and runtime-layout hashes,
- prompt length,
- padded-page payload bound,
- sizing assumptions,
- required decimal Gbit/s.

Each matching source role records the budget rate and hash in its private comparison file.

The layout retains per-rank, per-layer page bytes, token block size, state and boundary allowances, image identity, package versions, application revision, and launch-plan hash.

The serving capture records cache pages after model loading and memory profiling while the representative continues through HTTP startup. The model load and its cache geometry serve the later attestation and workload trial.

`--uniform-cache` selects an analytical attention/MLA estimate. `--bytes-per-token` provides a measured uniform-cache override. Both uniform modes require `--element-bytes` and `--block-tokens`.

The deployment path uses the runtime page record for both hybrid and uniform models.

TCP comparisons use aggregate bitrate reported by the iperf3 receiver. RDMA comparisons use average Gbit/s from the retained perftest report.

`fabric_budget.py link` records each directed pair's roles, addresses, interfaces, routes, transport, utility version and test parameters. `record-edge` binds the sample and source budget to that fingerprint. `reuse-edge` compares the retained bandwidth sample with a corrected budget when the current fingerprint matches and writes a new private comparison.

Each budget covers one directed host edge at the recorded workload. Running-engine KV probes and concurrent-capacity tests provide later deployment acceptance evidence.

### Host inventory and SSH access

`NARWHAL_HOSTS` selects the generated physical-host inventory. Its default is `config/hosts.local.json`.

Discovery groups identical `NARWHAL_NODE_<n>_SSH` and `NARWHAL_ROUTER_SSH` values from `.env` into one host entry.

Each `hosts` record contains:

- unique `id`,
- `ssh_env`, naming the environment variable holding its management destination,
- optional `password_env`,
- assigned `roles`.

The `router` role appears once. Each engine role is named `engine-<n>` and corresponds to the numbered deployment variables. Colocated roles belong to the same host entry.

The [example inventory](https://github.com/athrael-soju/Narwhal/blob/main/config/hosts.example.json) assigns `router` and `engine-1` to one machine and `engine-2` to another.

Keep destinations and credentials in workstation `.env`; the inventory stores variable names only.

SSH destinations may be aliases or `user@management-host`.

If `password_env` is present, password authentication is enabled and the named variable must be nonempty. Without `password_env`, OpenSSH key or agent authentication is used.

All roles on one physical host share that host's access record.

`tools/deployment/deploy_hosts.py plan` validates:

- unique host IDs,
- unique role ownership,
- required access variables,
- distinct destination entries.

Deployment is grouped by host ID. One physical machine gets one inventory entry containing all of its roles. Discovery performs the same grouping when SSH destinations match.

`check-access` establishes one verified connection per host. `shell --role <role>` resolves the owning host through the inventory.

A fresh management checkout begins from `.env`; discovery writes private generated files with mode 0600.

`NARWHAL_SSH_KNOWN_HOSTS` selects the checkout-local `config/ssh.known_hosts`. Discovery uses OpenSSH `accept-new`: the first connection records a key, while a changed key is rejected. Existing verified stores retain their entries.

Deployment commands then use strict host-key checking against this file.

For password authentication, the selected secret reaches `sshpass` through a private file descriptor. The workstation therefore needs OpenSSH and, when password access is used, `sshpass`.

For key or SSH-agent authentication, put address, username, identity, and optional `Port` or `ProxyJump` configuration in the workstation's private SSH config. Point the inventory `ssh_env` at that alias.

Before replacing a key for a new or changed server, verify it through the supplied private access source, then update the checkout-local known-hosts store.

Treat remote `hostname` output as an observed label. Server identity comes from the SSH host key.

`prepare --out <directory>` writes:

- one verified source bundle,
- role files grouped by host ID,
- a private manifest containing revision, host assignments, destination fingerprints, and file hashes.

Use a new output directory for each deployment.

`install --run <directory>` checks the manifest against current host mappings and local files, then transfers and installs once per selected host.

`--role engine-1` selects the host that owns `engine-1`, including any colocated roles. `--host <id>` selects one physical host directly.

Repeating an installation verifies existing content and reuses it when the completion marker matches the approved revision.

Each remote run lives under a unique:

```text
~/Narwhal-deploy/<id>/
```

The directory contains the bundle, role files, `checkout/`, installation lock, and completion marker.

A source or configuration mismatch stops that host before the helper advances to the next one. Existing remote content is preserved.

Local private logs under the run's `logs/` directory capture executed scripts, exit status, and output for each host.

A role shell opened with `--run` enters the deployed checkout, loads the matching role environment, and activates its virtual environment.

### Node URLs from the environment

An engine URL points to the running vLLM HTTP API, for example:

```text
http://10.0.0.11:8000
```

It may use an IP address directly. The router must be able to reach the selected address and port.

The attestation sidecar has its own URL, for example:

```text
http://10.0.0.11:8010/v1/attestation
```

Use the ports actually configured by the engine deployment.

Engine `url` and `attestation_url` accept environment references only when the entire JSON value is `${VARIABLE}`.

For the generated deployment, supply `NARWHAL_FABRIC_INTERFACE`, `NARWHAL_ENGINE_PORT` and `NARWHAL_ATTEST_PORT` in `.env`. Discovery reads the selected interface on each engine host and writes its unique global address as `NARWHAL_NODE_<n>_IP` in `config/deployment.env`. It builds the engine and attestation URLs from that address, with IPv6 host brackets where required. Set `NARWHAL_NODE_<n>_IP` when the interface has multiple global addresses, or set either full URL when its service uses a different reachable address. A port change also needs the matching per-node port override.

The generated fleet record refers to those derived values:

```json
{
  "iid": "n1",
  "url": "${NARWHAL_NODE_1_URL}",
  "attestation_url": "${NARWHAL_NODE_1_ATTESTATION_URL}",
  "role": "prefill"
}
```

Discovery adds one record per engine. `.env.example` shows the two-engine minimum and the shared fabric interface.

Both `config/fleet.json` and working `config/fleet.*.json` files are ignored by Git. The repository keeps the example and stub configurations tracked.

After loading `.env`, invoke profiling, preflight, and serving with:

```bash
--fleet "$NARWHAL_FLEET"
```

A missing or empty referenced variable causes configuration loading to fail with the field path and variable name.

References can substitute complete URL values only. Shell expressions, defaults, and recursive expansion are unsupported. All other JSON fields remain literal. Engine credentials continue to use `engine.engine_api_key_env`.

Observability applies the same URL resolution when creating scrape targets.

`FleetConfig.save()` writes resolved URLs. Save its output only to an ignored fleet file.

## Required fields

| Field            | Value             | Purpose                                                                |
| ---------------- | ----------------- | ---------------------------------------------------------------------- |
| `schema`         | `"narwhal.fleet"` | Fleet interface identity.                                              |
| `schema_version` | `1`               | Fleet document version.                                                |
| `model`          | string            | Model name exposed by the router. Every engine must serve this name.   |
| `engines`        | array             | Engine records. At least one is required; `iid` values must be unique. |
| `slo.ttft_s`     | positive seconds  | TTFT target used for placement, admission, pool load, and control.     |
| `slo.tpot_s`     | positive seconds  | TPOT target used for placement, pool load, and control.                |

Each engine record accepts:

| Field             | Default    | Purpose and validation                                                                                           |
| ----------------- | ---------- | ---------------------------------------------------------------------------------------------------------------- |
| `iid`             | required   | Scheduler identity. Unique within `engines`.                                                                     |
| `url`             | required   | HTTP base URL. Trailing `/` characters are removed.                                                              |
| `attestation_url` | `""`       | Full URL for the engine's attestation sidecar. Required by `narwhal-check` when `engine_contract` is configured. |
| `role`            | `"decode"` | Initial role. Valid values are `"prefill"` and `"decode"`.                                                       |
| `pin`             | `false`    | Prevents the configured role from changing.                                                                      |

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

An engine starts in `decode` unless its record chooses another role. A pinned engine keeps its configured role across placement changes, controller moves, and resume. This can reserve a prefill engine during warm-standby takeover.

Set SLO values from measurements taken on the deployed engine shape. Narwhal derives Prometheus histogram buckets from them.

## Engine contract

`recovery.engine_restart_policy` accepts:

- `individual`, the default,
- `whole_wave`.

`whole_wave` requires a complete `engine_contract` and `recovery.liveness_every > 0`.

Under `whole_wave`, an ejection or identity failure holds the fleet until an operator completes the [wave procedure](Operate.md#restart-an-engine-wave).

`engine_contract` describes the expected engine generation. Preflight and lifecycle readmission compare running engines against it. Production fleets serving client traffic should use a complete contract.

Lifecycle drain and readmission require every contract field. Before NIXL is exercised against a live peer, the router must be able to distinguish a restarted process from a different engine build.

If any required contract field is empty, the requested engine remains held out with `missing-contract`.

The `hardware` block records accelerator identity and tensor-parallel shape.

[Engine-host inspection](Deploy.md#3-inspect-each-engine-host) discovers the accelerator vendor and product on each remote host. Set `accelerator` to the observed product name.

`accelerators_per_engine` and `tensor_parallel` must both be positive. Tensor parallelism cannot exceed the number of accelerators assigned to the replica.

The external launcher controls vLLM's TP size. Keep `hardware.tensor_parallel` equal to that value and `hardware.accelerators_per_engine` equal to the replica's allocated accelerator count.

Verify the running shape through process-bound attestation, profiles, transfer tests, and deployment load.

| Field                      | Default           | Purpose and validation                                                                                                                                                                                        |
| -------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vllm_version`             | required          | Exact value returned by every engine's `/version` endpoint.                                                                                                                                                   |
| `image_digest`             | `""`              | Immutable `sha256:<64 hex>` container digest reported by engine-side attestation.                                                                                                                             |
| `nixl_version`             | `""`              | NIXL package version in the image.                                                                                                                                                                            |
| `nixl_connector_version`   | `0`               | Positive `NIXL_CONNECTOR_VERSION` integer from the deployed connector metadata; [capture it in step 6](Deploy.md#capture-the-nixl-connector-protocol-version).                                                   |
| `model_architecture`       | `""`              | Model implementation name relevant to KV layout.                                                                                                                                                              |
| `model_dtype`              | `""`              | Model execution dtype.                                                                                                                                                                                        |
| `kv_heads`                 | `0`               | Positive model-wide value from `ModelConfig.get_total_num_kv_heads()`.                                                                                                                                        |
| `head_size`                | `0`               | Positive `ModelConfig.get_head_size()` result used by the NIXL compatibility hash; [capture resolved model dimensions](Deploy.md#capture-model-dimensions-used-by-compatibility-hashing).                                     |
| `hidden_layers`            | `0`               | Positive model-wide value from `ModelConfig.get_total_num_hidden_layers()`.                                                                                                                                   |
| `attention_backend`        | `""`              | Attention backend expected from the recorded launch.                                                                                                                                                          |
| `kv_cache_dtype`           | `""`              | KV-cache dtype.                                                                                                                                                                                               |
| `cross_layers_blocks`      | `null`            | Resolved physical cache-block grouping, `KVCacheLayout.is_block_outermost`, from the pinned layout API. [Capture the layout and boolean](Deploy.md#capture-physical-cache-grouping).                             |
| `hybrid_kv_cache_manager`  | `null`            | Whether vLLM's hybrid KV-cache manager participates in the layout.                                                                                                                                            |
| `connector`                | `"NixlConnector"` | Engine-side connector name. Cannot be empty.                                                                                                                                                                  |
| `kv_role`                  | `""`              | Engine-side KV role semantics, for example `kv_both`.                                                                                                                                                         |
| `transfer_mode`            | `""`              | `pull` for `NixlPullConnector`; `push` for `NixlPushConnector`. [Retain the resolved class and mode](Deploy.md#capture-transfer-direction).                                                           |
| `speculative_config`       | `""`              | Stable name for the speculative-decoding configuration, or `disabled`.                                                                                                                                        |
| `enforce_handshake_compat` | `true`            | Effective value from the pinned NIXL worker's extra-config lookup. [Capture both the configured value and installed default](Deploy.md#capture-handshake-compatibility-enforcement). Narwhal requires `true`. |

### Attestation document

The [attestation generator](Deploy.md#6-attest-each-engine-process) writes `runs/engine-launch-*/engine-attestation.json` on each engine host from its checked serving plan, runtime inspections, live cache capture and startup log. The router finalisation command reads the live sidecars, requires matching complete contracts and fills `engine_contract` in `runs/deployment/fleet.json`.

[`config/engine-attestation.example.json`](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-attestation.example.json) describes the document shape for development and schema review.

The attestation file declares:

```json
"schema": "narwhal.attestation",
"schema_version": 1
```

`narwhal-attest` requires at least one nonempty source entry for every populated contract field before it queries the engine.

Run one sidecar beside every contracted engine and configure its `attestation_url`.

At startup, the sidecar reads:

- `/version`,
- `process_start_time_seconds`.

It adds those process-identity values to `contract` and `sources`, then hashes the response into `attestation_digest`.

If either identity value later changes, both sidecar routes return HTTP 503.

After an engine process changes, verify its HTTP endpoints and restart the sidecar through its configured process manager. The restarted sidecar binds itself to the new engine process.

`narwhal-check` compares the attested fields and process start before opening the NIXL handshake or produce/consume probes. A mismatch stops preflight before live KV transfer.

## Role control

| Field                                       | Default | Purpose and validation                                                                                                                                                                                                                                         |
| ------------------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `controller.advisory`                       | `false` | Records proposed role splits and reasons without changing current roles.                                                                                                                                                                                       |
| `controller.monitor_interval_s`             | `1.0`   | Delay between monitor passes. Positive. `recovery.health.min_samples` cannot exceed `floor(recovery.health.window_s / controller.monitor_interval_s)`. Each pass contributes no more than one residual per engine, so delayed passes can undersample a window. |
| `controller.monitor_failure_limit`          | `5`     | Consecutive passes with any monitoring-stage failure before the router enters degraded state and stops new admission. At least 1. Failures count even when role control itself succeeds.                                                                       |
| `controller.min_prefill`                    | `1`     | Minimum live prefill-engine count preserved by controller moves. At least 1.                                                                                                                                                                                   |
| `controller.min_decode`                     | `1`     | Minimum live decode-engine count preserved by controller moves. At least 1.                                                                                                                                                                                    |
| `controller.thresholds.expand`              | `1.0`   | SLO-relative pool load that initiates reactive expansion. Positive.                                                                                                                                                                                            |
| `controller.thresholds.shrink`              | `0.5`   | Maximum projected source load for ordinary consolidation. Mixed-pressure D-to-P moves may exceed it. Nonnegative and lower than `expand`.                                                                                                                      |
| `controller.thresholds.cooldown_s`          | `10.0`  | Minimum time between prefill-to-decode moves. Nonnegative.                                                                                                                                                                                                     |
| `controller.thresholds.sustained_intervals` | `3`     | Minimum confirmation count for nonurgent, incomplete-demand, and mixed-pressure proposals. At least 1.                                                                                                                                                         |
| `controller.thresholds.dwell_s`             | `0.0`   | Minimum residence time after an engine changes role. Nonnegative.                                                                                                                                                                                              |
| `controller.thresholds.panic_ratio`         | `0.0`   | Decode-load multiple allowed to bypass cooldown while prefill remains below `shrink`. `0` disables it; enabled values must be at least 1.                                                                                                                      |
| `controller.thresholds.flip_resident_guard` | `0`     | Maximum resident decode streams permitted on an engine selected for D-to-P movement. The move waits until residency falls to this value. `0` disables the guard. Nonnegative.                                                                                  |
| `controller.flip_history`                   | `1000`  | Maximum retained role-change records exposed by `/narwhal/state`. At least 1.                                                                                                                                                                                  |

Prefill load is predicted work divided by the TTFT target.

Decode load uses the observed token interval above the corrected idle floor, divided by the remaining TPOT budget. When the idle floor itself reaches the TPOT target, Narwhal uses the raw interval-to-target ratio.

A load of `1.0` means the phase target has been reached.

`controller.min_prefill + controller.min_decode` must fit the configured fleet. A single-engine topology with both floors set to 1 is accepted and serves aggregate inference.

Pins, drains, quarantine, and health ejections can leave too few movable engines to satisfy a configured floor. The controller reports the breach and keeps the safest reachable split.

All role changes preserve `controller.min_decode`.

When live decode capacity falls below its floor, monitoring skips the normal cooldown and restores one eligible engine per pass. Per-engine dwell still applies to decode-floor recovery.

Prefill-floor recovery ignores cooldown and dwell. Pins, engine availability, both floors, and advisory mode still apply.

Every applied floor recovery records a dwell timestamp.

When unavailable capacity returns, the resulting split re-enters the ordinary adjacent-split decision path. Recovery records use the same `controller.flip_history` limit as other role changes.

New requests observe a role change immediately. Existing requests stay on their current engines until completion or cancellation, retaining their reservations. The controller includes this resident work in later movement decisions.

Engine restarts and operator drains mark engines unavailable.

Before converting decode capacity into prefill capacity, the controller verifies that the proposed fleet split and all resident decode batches stay within the measured profile domain and available KV capacity.

Run `controller.advisory: true` against recorded traffic before applying role changes in production. `/narwhal/state` and Prometheus report the proposed prefill/decode counts, caller, reason, and result.

The controller evaluates the current split and each adjacent split.

Ordinary consolidation requires:

- projected source pressure at or below `controller.thresholds.shrink`,
- improvement in the worst projected SLO ratio of at least `controller.reactive.movement_margin`.

When measured prefill pressure reaches `controller.thresholds.expand` and every engine has a profile, the mixed-pressure rule may move one decode engine to prefill even when projected decode pressure remains above `shrink`.

That candidate split must improve the worst projected SLO ratio by at least the movement margin. Improvement must remain strictly positive when the configured margin is zero.

A proposal needs:

```text
max(
  controller.reactive.confirmations,
  controller.thresholds.sustained_intervals
)
```

matching confirmations. This also applies when demand evidence is complete.

Any change in proposed split, demand completeness, or eligibility rule restarts confirmation. A failed eligibility check clears the candidate.

P-to-D moves require prefill pressure at or below `shrink` and obey the decode cooldown.

During overload, movement margin, confirmation requirements, consolidation evidence, and dwell bound repeated role changes. Sustained overload still requires either lower offered demand or more serving capacity to meet the configured SLOs.

D-to-P consolidation requires a closed arrival-evidence window.

The window closes when either:

- `controller.reactive.evidence_span_s` has elapsed and at least `controller.reactive.evidence_min_arrivals` samples exist,
- `controller.reactive.evidence_max_span_s` has elapsed under sparse traffic.

Decode demand must also be stable. Consolidation pauses when the short-horizon estimate exceeds the long-horizon estimate by more than `controller.reactive.demand_rise_tolerance`.

Candidate pricing uses the larger demand estimate and includes resident plus pending decode work.

A first-token timeout or prefill-to-decode recovery move resets the consolidation evidence window.

Moves toward decode, including emergency floor restoration, may proceed while evidence is still accumulating.

Predictive admission refusals contribute to offered demand and attainment misses without resetting the evidence window.

State and metrics expose both demand estimates, the evidence-window state, and the gate currently preventing a move.

### Reactive fields

Reactive control is configured under `controller.reactive`.

Repeat preflight and deployment load measurement after changing the engine build or serving policy.

| Field                                               | Default | Purpose and validation                                                                                                                                                                                |
| --------------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `controller.reactive.window_s`                      | `120.0` | Demand-estimation window. Positive.                                                                                                                                                                   |
| `controller.reactive.confirmations`                 | `2`     | Required consecutive identical adjacent proposals for nonurgent, incomplete-demand, and mixed-pressure moves. At least 1.                                                                             |
| `controller.reactive.utilization`                   | `0.8`   | Fraction of each engine treated as usable capacity. Greater than 0 and at most 1.                                                                                                                     |
| `controller.reactive.min_arrivals`                  | `10`    | Minimum arrival count required for a decision. At least 1.                                                                                                                                            |
| `controller.reactive.demand_floor`                  | `0.5`   | Minimum accepted demand signal. Positive.                                                                                                                                                             |
| `controller.reactive.movement_margin`               | `0.05`  | Required reduction in the worst projected SLO ratio before movement. Range `[0, 1)`.                                                                                                                  |
| `controller.reactive.step_s`                        | `5.0`   | Minimum interval between scheduled adjacent-split evaluations. A projected prefill TTFT breach may trigger one guarded D-to-P evaluation between scheduled passes. Positive.                          |
| `controller.reactive.evidence_span_s`               | `60.0`  | Minimum recent-arrival span required for D-to-P consolidation and short horizon for the rising-demand test. Positive and no greater than `evidence_max_span_s`.                                       |
| `controller.reactive.evidence_max_span_s`           | `120.0` | Maximum evidence duration under sparse traffic. The window closes at this age even below `evidence_min_arrivals`. Positive, at least `evidence_span_s`, and no greater than `window_s`.               |
| `controller.reactive.evidence_min_arrivals`         | `10`    | Minimum samples within the evidence span before D-to-P consolidation. At least 1.                                                                                                                     |
| `controller.reactive.demand_rise_tolerance`         | `0.25`  | Maximum accepted short-horizon rise over long-horizon decode demand. Consolidation is blocked when short demand exceeds long demand by more than `1 + demand_rise_tolerance`. Finite and nonnegative. |
| `controller.reactive.decode_correction_min`         | `0.5`   | Lower bound on the live/profile decode correction applied to capacity. Positive.                                                                                                                      |
| `controller.reactive.decode_correction_max`         | `2.0`   | Upper bound on the live/profile decode correction. At least the minimum.                                                                                                                              |
| `controller.reactive.decode_correction_alpha`       | `0.2`   | Fraction of each qualifying observation window applied to the correction. Range `(0, 1]`.                                                                                                             |
| `controller.reactive.decode_correction_min_samples` | `8`     | Required decode gaps before an observation window updates the correction. At least 1.                                                                                                                 |

Demand pricing uses the mean profile across configured engines.

Decode profiles treat active requests and resident KV tokens as separate inputs. Recent token intervals apply a bounded correction to the profile estimate.

All configured engines share one hardware and TP shape, so these measurements describe the full fleet.

## Bounded serving

Without bounded-serving configuration, Narwhal sends admitted work directly to phase dispatch and allows one prefill/decode attempt per request.

Nested `serving` fields can add FIFO admission waiting and retry. Each phase is dispatched only when its eligible role pool has capacity, and all waiting or retrying remains inside the original request deadline.

| Field                         | Default    | Meaning                                                                                                                                                                                                                |
| ----------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serving.queue_capacity`      | `0`        | Maximum requests waiting for admission. `0` rejects immediately at saturation.                                                                                                                                         |
| `serving.queue_timeout_s`     | `0.0`      | Maximum admission wait, also capped by the original request deadline. Must be positive when queueing is enabled.                                                                                                       |
| `serving.prefill_concurrency` | `0`        | Resident prefill requests allowed per engine. Queueing requires a positive measured value.                                                                                                                             |
| `serving.decode_concurrency`  | `0`        | Resident decode requests allowed per engine. Queueing requires a positive measured value.                                                                                                                              |
| `serving.handoff_timeout_s`   | `0.0`      | Maximum KV-handoff age measured from the start of the prefill HTTP request. Queueing or retries require a positive value below the verified backend lease. At `0`, the request deadline is the only handoff-age limit. |
| `serving.max_attempts`        | `1`        | Maximum complete prefill/decode attempts per original request. Range 1 to 3.                                                                                                                                           |
| `serving.retry_base_s`        | `0.1`      | Initial exponential-backoff ceiling. Full jitter samples from zero to that ceiling.                                                                                                                                    |
| `serving.retry_cap_s`         | `1.0`      | Maximum backoff ceiling. At least the base value.                                                                                                                                                                      |
| `serving.retry_budget`        | `10`       | Initial and maximum pool of retry credits. Every retry spends one credit.                                                                                                                                              |
| `serving.retry_replenish`     | `0.1`      | Credits added after each successful original request. Range zero to one.                                                                                                                                               |
| `serving.max_request_bytes`   | `4194304`  | Maximum HTTP request-body size, enforced while reading.                                                                                                                                                                |
| `serving.max_response_bytes`  | `16777216` | Maximum retained bytes for each non-streaming response attempt and streaming pre-output metadata buffer.                                                                                                               |

While reading request bodies, waiting for admission, or writing responses, the router retains at most:

```text
serving.max_connections + serving.queue_capacity
```

completion requests.

Hitting that ceiling returns HTTP 429 before body parsing. The rejected request is recorded as an unsized offer.

All active requests share one global limit.

Requests waiting for admission or phase dispatch still contribute to demand. Retries retain the deadline and reservation of the original arrival.

Before any visible response output, Narwhal can retry:

- transient transport failures,
- HTTP 408,
- HTTP 429,
- HTTP 500,
- HTTP 502,
- HTTP 503,
- HTTP 504.

Every retry begins with fresh prefill ownership. Recovery from an expired handoff does the same.

A permanent error, local HTTP pool starvation, cancellation, or failure after visible output terminates the request.

For non-streaming output, any partial body from a failed attempt is discarded before retry.

The original request deadline includes tokenisation, queue wait, retry backoff, engine work, and client writes.

Derive queue capacity, phase concurrency, and deadlines from measured workload latency and capacity. Request and response byte limits cap router-retained data.

Set `serving.handoff_timeout_s` below the producer's KV lease. Verify separately that abandoned handoffs are released by the backend when that lease expires.

Prefill failures and non-streaming decode failures return HTTP errors.

Streaming responses commit HTTP 200 before decode begins. A later decode failure therefore appears as a terminal stream error event, including failures before the first generated token.

When the request deadline expires, Narwhal emits `code: expired` and closes the stream.

Client backpressure closes the connection immediately.

Clients should treat either of these cases as a failed response:

- an error event,
- stream termination before the success terminator.

Any retry by the client must fit within its remaining deadline.

Model-aware placement, resident role changes, cancellation, timeouts, engine exclusion, and lifecycle recovery continue to operate with bounded serving enabled.

`recovery.failure_quarantine_s` keeps failed engines out of placement while health checks converge.

Queueing, phase-concurrency limits, handoff expiry, retries, and byte limits all affect measured deployment capacity. Repeat workload measurements after changing them.

## Placement and admission

| Field                        | Default        | Purpose and validation                                                                                                                                                                              |
| ---------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serving.admission`          | `"predictive"` | `predictive` prices every prefill path against the TTFT budget and constrains aggregate placement to the single-phase region covered by engine measurements. `open` disables both admission checks. |
| `serving.admission_margin`   | `0.0`          | Fraction added to the TTFT admission budget to reduce boundary churn. Nonnegative.                                                                                                                  |
| `serving.max_connections`    | `512`          | Global admitted-request limit and HTTP data-pool size. At least 1.                                                                                                                                  |
| `engine.control_connections` | `0`            | HTTP connections reserved for health and recovery. `0` derives two per engine, with a minimum of four. Nonnegative.                                                                                 |

Predictive admission returns HTTP 429 when the least expensive prefill path exceeds the TTFT budget.

Refusals caused by engine backlog include `Retry-After` with the projected wait.

A prompt that cannot fit the TTFT budget even without backlog gets an error envelope directing the caller to shorten the prompt or increase the TTFT target.

Measure sustained healthy inflight load before raising `serving.max_connections`.

If admitted work exceeds what engines can drain before KV handoffs expire, decode requests can fail.

The loader rejects an admission override above `serving.max_connections` during startup because such a setting would permit admission beyond the dispatch pool.

### Placement

Placement first filters engines by:

- role,
- availability,
- exclusions,
- projected SLO compliance.

Among eligible engines, Narwhal selects the lowest-cost candidate. Equal-cost candidates are resolved deterministically by instance ID.

If every candidate violates the SLO projection, Narwhal records an unserved placement and chooses the least-cost fallback.

Engine-side prefix caching is independent of router placement.

A live role change affects scheduling for new requests immediately.

Resident requests remain assigned to their existing engine until they complete or are cancelled.

Lifecycle drain, quarantine, ejection, and restart holds remain active during role changes.

## Engine requests and timeouts

| Field                           | Default                | Purpose and validation                                                                                                     |
| ------------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `profiles.path`                 | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`.                                                         |
| `serving.request_timeout_s`     | `600.0`                | End-to-end completion deadline from HTTP ingress through response delivery. Positive.                                      |
| `serving.prefill_timeout_s`     | `120.0`                | Prefill-leg deadline. Positive.                                                                                            |
| `recovery.failure_quarantine_s` | `0.0`                  | Time a failed engine remains excluded from placement. `0` disables quarantine. Nonnegative.                                |
| `engine.decode_read_timeout_s`  | `60.0`                 | Maximum silent interval between decode chunks. `0` disables this gap limit. Nonnegative.                                   |
| `engine.first_token_timeout_s`  | `2.5`                  | Deadline to the first decode token and for each functional-verification leg. Positive.                                     |
| `engine.tokenize`               | `true`                 | Requests exact input length from the dialect's tokenisation endpoint.                                                      |
| `engine.tokenize_timeout_s`     | `2.0`                  | Exact-token-count deadline. Positive.                                                                                      |
| `engine.chars_per_token`        | `3.8`                  | Character-to-token ratio used when the tokenisation endpoint is unavailable. Positive.                                     |
| `engine.pool_timeout_s`         | `5.0`                  | Maximum wait for an engine HTTP connection on either pool. Probe exhaustion leaves the engine verdict unchanged. Positive. |
| `engine.connect_timeout_s`      | `10.0`                 | TCP connect deadline for engine requests. Positive.                                                                        |
| `engine.health_timeout_s`       | `5.0`                  | Deadline for preflight, breaker, and readmission health probes. Positive.                                                  |

Set `engine.first_token_timeout_s` above the measured crossed-handoff p99 over the fleet's supported context range.

`serving.request_timeout_s` bounds the full request, including the decode stream.

`engine.first_token_timeout_s` covers the period from opening the decode HTTP stream until its first generated token.

After the first token arrives, `engine.decode_read_timeout_s` limits the gap between transport chunks. Partial SSE lines and metadata chunks reset that timer.

With `engine.decode_read_timeout_s: 0`, the overall request deadline is the only stream bound.

Breaker verification uses `engine.first_token_timeout_s` separately for each complete prefill and decode verification leg.

Retries begin a new complete prefill/decode attempt before visible output. [Bounded serving](#bounded-serving) specifies eligible failures and resource limits.

`engine.chars_per_token` feeds the quadratic prefill estimate when tokenisation falls back to text length. Profile this ratio for every dialect that can use character-based estimation.

## Engine health

| Field                                 | Default | Purpose and validation                                                                                                                                                                                                        |
| ------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `recovery.eject_after`                | `3`     | Consecutive failed legs before breaker action. At least 1.                                                                                                                                                                    |
| `recovery.readmit_every`              | `10`    | Monitor intervals between probes of ejected engines. At least 1.                                                                                                                                                              |
| `recovery.liveness_every`             | `10`    | Monitor intervals between health probes and, for contracted fleets, process-identity and attestation checks. `0` disables idle sweeps and is invalid with `whole_wave`.                                                       |
| `recovery.liveness_misses`            | `2`     | Consecutive failed liveness probes before ejection. At least 1.                                                                                                                                                               |
| `recovery.health.window_s`            | `30.0`  | Residual-scoring window length. At least 1 second. The first observation starts the window; slow or skipped monitor passes can leave it undersampled.                                                                         |
| `recovery.health.drift_band`          | `2.0`   | Multiple of the engine's trailing healthy residual used as the drift threshold. Greater than 1.0.                                                                                                                             |
| `recovery.health.relative_band`       | `1.5`   | Peer-relative multiple that can override the fleet-surge veto. `0` disables the veto. Nonnegative.                                                                                                                            |
| `recovery.health.min_samples`         | `3`     | Observations required before scoring a window. At least 1 and no greater than `floor(recovery.health.window_s / controller.monitor_interval_s)`. An undersampled window increments `health.undersampled` in `/narwhal/state`. |
| `recovery.health.probation_windows`   | `3`     | Consecutive drifting windows before probation. At least 1.                                                                                                                                                                    |
| `recovery.health.evict_windows`       | `5`     | Consecutive drifting windows before ejection is requested. At least 1 and no lower than `probation_windows`.                                                                                                                  |
| `recovery.health.recovery_windows`    | `3`     | Consecutive healthy windows required to clear probation. At least 1.                                                                                                                                                          |
| `recovery.health.probation_penalty_s` | `1.5`   | Placement penalty applied during probation. Nonnegative.                                                                                                                                                                      |

Connection failures count immediately toward `recovery.eject_after`.

Transport timeouts first cause a health probe.

First-token timeouts and mid-stream stalls require functional verification of the affected inference path.

If verification is inconclusive, Narwhal retains the hold and retries on the readmission cadence.

Router handoff keeps the hold and its failed transfer paths.

The drift tracker compares fresh decode residuals and stalled inter-token gaps against each engine's recent healthy baseline.

Placement estimates left over after decode completion are excluded from health evidence.

Local prefill work makes the decode-only profile invalid for that interval.

The tracker therefore pauses decode correction and drift scoring for gaps that cross a prefill boundary. This includes prefills that both start and finish between monitor passes.

When pure decode observations return, Narwhal opens a new scoring window while retaining the previous healthy baseline and probation state.

Client latency, controller pressure, request deadlines, and liveness checks continue to observe mixed work while decode health scoring is paused.

`health.prefill_paused` and `health.prefill_pauses` in `/narwhal/state` expose this condition.

The peer-relative test suppresses ejection during fleet-wide slowdowns.

If a nonempty health window closes without enough samples, it is marked `undersampled`. Its residuals are discarded, while the current baseline and probation state carry into the next window.

`/narwhal/state` and `narwhal_health_windows_*_total` report scored and undersampled windows.

`last_scored_s_ago` records elapsed time since the most recent verdict.

Confirmed ejection clears the affected engine's drift history.

## Resume and shutdown

| Field                        | Default             | Purpose and validation                                                       |
| ---------------------------- | ------------------- | ---------------------------------------------------------------------------- |
| `recovery.state_path`        | `"runs/state.json"` | Atomic handoff file storing roles, ejections, lifecycle state, and counters. |
| `recovery.resume`            | `false`             | Applies a compatible handoff file at process startup.                        |
| `serving.graceful_timeout_s` | `30.0`              | Uvicorn drain interval after `SIGTERM`. Nonnegative.                         |

With a saved handoff, the router resumes from `recovery.state_path`.

Without one, startup uses the split declared in the fleet configuration.

An uncontracted development fleet also falls back to the configured split when the saved engine set differs from the current one.

Contracted resume and automatic takeover require handoff schema version 1 and accepted process identities for every available engine.

An unknown handoff schema or version aborts startup.

If a handoff passes schema validation but cannot be applied to a contracted fleet, the fleet remains held for a managed wave.

Successful resume restores:

- roles,
- ejections,
- lifecycle holds,
- complete-backend-outage state.

Per-engine dwell timestamps restart from process startup.

The P-to-D cooldown begins when the replacement scheduler is created.

Warm-standby takeover remains a CLI concern because router IDs and shared lease paths vary by host.

[Operate Narwhal](Operate.md#start-the-production-routers) defines readiness, fencing, recovery, and partition behaviour.

## Engine authentication

```json
{
  "engine": {
    "engine_api_key_env": "NARWHAL_ENGINE_KEY"
  }
}
```

Ingress terminates public client credentials.

For engine requests, Narwhal attaches the configured engine credential plus a router-generated `x-request-id` that is unique for every attempt and phase.

| Field                       | Default | Purpose and validation                                                                                                                                                                                                                                                                                                  |
| --------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `engine.engine_api_key_env` | `""`    | Environment-variable name resolved when an engine client is created. Engine clients attach its Bearer credential to serving, profiling, preflight, cache reset, and lifecycle requests. Attestation uses the sidecar URL on the trusted control network. A named but unset variable causes client construction to fail. |

`/narwhal/state` reports either `boundary` or `engine-credential` under `admission.engine_auth`.

Keep that authentication mode unchanged between workload measurement and production serving.

## Request journal

Narwhal writes request timings to `journal.jsonl` beside `profiles.path`.

`narwhal-serve --journal` can select another location.

The [journal reference](Telemetry-and-Artifacts.md#request-journal) defines the record format.

## Protocol adapters

| Field              | Default  | Accepted values                                               |
| ------------------ | -------- | ------------------------------------------------------------- |
| `engine.connector` | `"nixl"` | Registered KV transport. This release provides `nixl`.        |
| `engine.dialect`   | `"vllm"` | Registered engine HTTP dialect. This release provides `vllm`. |

A connector or dialect is registered only after its preflight gates pass against the target engine build.

Unknown names fail configuration validation.

## Profile validation

The `profiles` section selects the profile store and the decode-fit limits enforced by `narwhal-check`.

| Field                          | Default | Purpose and validation                                                                                                                                                              |
| ------------------------------ | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profiles.max_decode_fit_mape` | `0.05`  | Maximum accepted in-sample decode fit error. Positive, finite, and no greater than `controller.reactive.movement_margin`. The default fit limit equals the default movement margin. |
| `profiles.max_decode_cv_mape`  | `0.13`  | Maximum accepted leave-one-out cross-validation error. Positive and finite. Profiles must cover the context and concurrency range used by the deployment.                           |

`narwhal-check` applies both limits to every configured engine. Failures report the engine, measured error, and configured limit.

## Paths and CLI precedence

Relative `profiles.path` and `recovery.state_path` values resolve from the serving process's working directory.

`narwhal-serve --journal` exists only on the CLI and defaults to `journal.jsonl` beside `profiles.path`.

The serving CLI applies these overrides after loading the fleet document:

| CLI option           | Config value                 | Precedence                                           |
| -------------------- | ---------------------------- | ---------------------------------------------------- |
| `--max-concurrent`   | `serving.max_connections`    | Replaces router admission capacity.                  |
| `--graceful-timeout` | `serving.graceful_timeout_s` | Replaces Uvicorn shutdown drain time.                |
| `--resume`           | `recovery.resume`            | Forces resume on. A configured `true` stays enabled. |

`--host`, `--port`, `--log-level`, `--journal`, and warm-standby options have no fleet-config equivalents.

The [CLI reference](CLI-Reference.md) defines their defaults and validation.

## Config provenance

Keep the exact fleet configuration beside every scored run.

Fleet documents contain engine URLs and can expose site addresses. Replace those values before publishing an artifact.

The repository's annotated example uses placeholder addresses. Live configurations belong under `runs/` or a gitignored `config/fleet.*.json` path.
