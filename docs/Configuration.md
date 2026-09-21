# Configuration

Narwhal loads one JSON document for one model and one engine fleet before it opens the router. Serve another model through a separate router and fleet, then route between them at ingress. Print the annotated example with this command.

```bash
.venv/bin/narwhal-check --print-example-config
```

`narwhal-serve`, `narwhal-profile` and `narwhal-check` load the native fleet document passed to `--fleet`. Code that calls `create_app()` directly can set `NARWHAL_FLEET`.

The loader accepts `model`, `hardware`, `engines`, `engine_contract`, `slo`, `controller`, `serving`, `engine`, `recovery`, and `profiles` at the top level. Every object rejects unknown keys while accepting underscore-prefixed annotations. Validation reports every cross-field error found in one pass.

Booleans use JSON `true` or `false`, counts use JSON integers, and durations and ratios use finite JSON numbers. The parser reports each mistyped field by its public path, for example `engine.tokenize must be a boolean; serving.max_connections must be an integer; controller.monitor_interval_s must be a number`.

Fleet configs declare `"schema": "narwhal.fleet"` and `"schema_version": 1`. Version 1 assigns client identity and content capture to ingress while Narwhal retains one global admission budget and request timings over token counts and durations. The loader checks the schema before reading fleet fields, and the [API and data reference](API-and-Data-Reference.md#contract-versions) lists the complete interface set.

## Environment variables

Use an existing supplied `.env` or injected environment for deployment values. The annotated [.env.example](https://github.com/athrael-soju/Narwhal/blob/main/.env.example) describes the deployment fleet path and optional engine credentials; create a template copy only when preparing a new configuration file:

```bash
test -f .env || install -m 600 .env.example .env
```

Inspect the supplied values locally, fill any required fields for that host, then export them in each terminal that runs Narwhal or `make observe`. Keep shell tracing disabled and retain credentials in private configuration:

```bash
set +x
set -a
. ./.env
set +a
```

Narwhal does not load `.env` automatically. Run these commands from the checkout root before invoking Narwhal; relative fleet and profile paths resolve from the working directory. `.env` is ignored by Git. Quote literal credentials with single quotes, as shown in the example.

- `NARWHAL_FLEET` selects the JSON file for `make observe` and Python callers of `create_app()`. The CLI commands still require `--fleet "$NARWHAL_FLEET"`.
- `NARWHAL_ENGINE_KEY` is the example name for the shared engine Bearer token. To enable it, set `engine.engine_api_key_env` to `"NARWHAL_ENGINE_KEY"` in the fleet JSON and uncomment and fill the corresponding value in `.env`. [Engine authentication](#engine-authentication) describes which requests carry it. CPU stubs need no key.
- `NARWHAL_ROUTER_URL` selects the router scrape target for `make observe`. Set the router's listening address and port separately through `narwhal-serve --host` and `--port`.
- `NARWHAL_GRAFANA_BIND_ADDRESS` and `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` select the observability listeners. The example uses `127.0.0.1` for Grafana (port 3000) and `127.0.0.1:9090` for Prometheus.

Model names, hardware, profiles and SLOs belong in the fleet JSON. Engine launch credentials and public client authentication belong to the deployment's engine launcher and ingress.

The commented deployment inputs in `.env.example`, such as `NARWHAL_ENGINE_IMAGE`, `NARWHAL_MODEL_DIR`, and `NARWHAL_FABRIC_INTERFACE`, belong to the [engine-host preparation](Deploy.md#3-inspect-each-engine-host) shell or site automation. Narwhal reads the fleet JSON; its `engines` array defines inventory size. The loader resolves engine `url` and `attestation_url` when their entire values are environment references. Model path and image variables remain inputs to the engine launcher.

### Host environment files

`NARWHAL_DEPLOYMENT_REVISION` selects the full commit SHA in the management checkout. `tools/deploy_hosts.py prepare` packages that commit into `source.bundle` and verifies it with a fresh local clone; `install` transfers it to each selected host. Remote checkouts clone that bundle and compare their revision with the role file before installation. Store the bundle with the generated environment files under ignored `runs/deployment-env/`.

From the management checkout, `python3 tools/deploy_hosts.py prepare --out <directory>` exports each role from the loaded workstation environment and supplied fleet JSON. It uses `tools/prepare_host_env.py` to select fields, preserve shell literals and create mode-0600 role files. [Host installation](Deploy.md#2-install-narwhal-on-the-remote-hosts) gives the preparation, installation and role-shell commands.

| Remote file | Exported values | Workstation source |
| --- | --- | --- |
| `.env.router` | Revision, `NARWHAL_FLEET=config/fleet.local.json`, referenced engine and attestation URLs, configured engine API credential, optional router and observability settings. | `NARWHAL_DEPLOYMENT_REVISION`, variables referenced by fleet endpoint fields and `engine.engine_api_key_env`, `NARWHAL_ROUTER_URL`, `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| `.env.engine-<n>` | Revision, launch and artifact fields, selected node URLs, fabric peer addresses and configured engine API credential. | Shared engine fields below, optional `NARWHAL_NODE_<n>_<field>` overrides, `NARWHAL_NODE_<n>_URL`, `NARWHAL_NODE_<n>_ATTESTATION_URL`, all supplied `NARWHAL_NODE_<n>_IP` values and the configured engine API credential. |
| `config/fleet.local.json` on the router | Supplied fleet document, copied before deployment edits. | The file selected by workstation `NARWHAL_FLEET`. |

The engine exporter requires `NARWHAL_ENGINE_IMAGE`, `NARWHAL_ENGINE_MODEL_NAME`, `NARWHAL_MODEL_DIR`, `NARWHAL_RUN_DIR`, `NARWHAL_MODEL_CONFIG_SHA256`, `NARWHAL_FABRIC_INTERFACE`, `NARWHAL_ENGINE_PORT`, `NARWHAL_ATTEST_PORT`, `NARWHAL_NIXL_SIDE_CHANNEL_PORT` and `NARWHAL_UCX_TCP_PORT_RANGE`. It also exports supplied `NARWHAL_ATTEST_DOCUMENT_SOURCE` and `NARWHAL_ATTEST_DOCUMENT_SHA256` values. These paths identify artifacts on the engine host; artifact provisioning belongs to the engine preparation and launch steps.

For a per-node override, insert `NODE_<n>_` after `NARWHAL_`: `NARWHAL_NODE_2_ENGINE_PORT` becomes `NARWHAL_ENGINE_PORT` in `.env.engine-2`. An empty override for a required field reports that field for correction. Fleet endpoint and API-key references select their named environment variables; names containing `SSH` and access variables referenced by the host inventory are rejected. The exporter selects other values through the listed role fields. Store management login destinations, passwords, identity configuration and host keys in the workstation's private access files.

Generated files live under ignored `runs/deployment-env/` on the workstation. Git ignores `.env.router`, `.env.engine-<n>` and `config/fleet.local.json` on remote hosts. `deploy_hosts.py shell --run <directory> --role <role>` loads the appropriate role file with shell tracing disabled. A host serving both roles keeps both files in one checkout; each shell loads its own role file.

### Host inventory and SSH access

`NARWHAL_HOSTS` selects the private physical-host inventory, defaulting to `config/hosts.local.json`. Each `hosts` entry supplies a unique `id`, an `ssh_env` variable naming its management destination, an optional `password_env` variable and its `roles`. The `router` role appears once; each engine uses an `engine-<n>` role matching its numbered deployment variables. Assign colocated roles to the same host entry. The [example inventory](https://github.com/athrael-soju/Narwhal/blob/main/config/hosts.example.json) places `router` and `engine-1` on one host and `engine-2` on another.

Store destinations and credentials in the workstation's `.env`; the inventory holds their variable names. Destinations accept an SSH alias or `user@management-host`. Supplying `password_env` selects password authentication and requires that variable to contain a value. Omitting it selects OpenSSH key or agent authentication. Every role assigned to a host reuses that host's access entry.

`tools/deploy_hosts.py plan` validates unique host IDs, role ownership, required access variables and distinct destination entries. The helper groups deployment work by host ID. Two aliases for the same physical machine belong in one host entry with the combined roles; the supplied inventory determines machine identity. `check-access` opens one verified connection per host. `shell --role <role>` resolves the role through that inventory.

Store verified management host keys in checkout-local `config/ssh.known_hosts` and select it with `NARWHAL_SSH_KNOWN_HOSTS`. Supply that file, `.env`, the host inventory and fleet JSON with a fresh management checkout, keeping private file permissions at mode 0600. The helper uses strict host-key checking against this store and passes a selected password to `sshpass` through a private file descriptor. Install OpenSSH and, for password access, `sshpass` on the management workstation.

For key or SSH-agent authentication, configure the workstation's private SSH configuration with the host's address, username, identity and optional `Port` or `ProxyJump`. Point the inventory's `ssh_env` variable to that alias. Verify a new or changed server key through the supplied private access source before updating the checkout-local host-key store. Record remote `hostname` output as an observed label; SSH keys establish server identity.

`prepare --out <directory>` creates one verified source bundle, role files grouped under host IDs, and a private manifest containing the revision, host assignments, destination fingerprints and file hashes. Choose a fresh output directory for a new deployment. `install --run <directory>` verifies that manifest against the current host mapping and local files, then transfers and installs once per selected host. `--role engine-1` selects the host owning that role, including its colocated roles; `--host <id>` selects a named machine. Repeating the command verifies existing content and reuses an installation whose completion marker matches the approved revision.

The helper creates each run under a unique remote `~/Narwhal-deploy/<id>/` directory with its bundle, role files, `checkout/`, installation lock and completion marker. Source and configuration mismatches stop the affected host; the helper preserves existing content and stops before the next host. Private per-host logs under the local run's `logs/` directory retain executed scripts, exit statuses and output. A role shell opened with `--run` enters that checkout, loads the role environment and activates its virtual environment.

### Node URLs from the environment

An engine URL identifies the running vLLM HTTP API, for example `http://10.0.0.11:8000`; it can use an IP address without a DNS name. Choose an address and port reachable from the router. The attestation URL identifies the separately running sidecar, for example `http://10.0.0.11:8010/v1/attestation`. Use the actual listening ports from your engine deployment.

Engine `url` and `attestation_url` fields accept a whole-value `${VARIABLE}` reference. For example, set these values in `.env`:

```bash
NARWHAL_FLEET=config/fleet.json
NARWHAL_NODE_1_URL='http://node1:8000'
NARWHAL_NODE_1_ATTESTATION_URL='http://node1:8010/v1/attestation'
```

Use those references in the corresponding engine entry in `config/fleet.json`:

```json
{
  "iid": "n1",
  "url": "${NARWHAL_NODE_1_URL}",
  "attestation_url": "${NARWHAL_NODE_1_ATTESTATION_URL}",
  "role": "prefill"
}
```

Add one entry per running engine to the fleet's `engines` array. `.env.example` shows one URL pair; define a pair for each engine whose URLs you reference from the environment. Both `config/fleet.json` and working `config/fleet.*.json` files are ignored by Git; the shipped example and stub configs remain tracked.

After loading `.env`, pass `--fleet "$NARWHAL_FLEET"` to profiling, preflight and serving commands. Missing or blank referenced variables fail configuration loading with the field and variable name. References support complete URL values only, with no shell expressions, defaults or recursive expansion. Other JSON fields retain their literal values; engine credentials continue to use `engine.engine_api_key_env`.

Observability resolves the same engine URL references when generating scrape targets. Saving a loaded fleet through `FleetConfig.save()` writes resolved URLs, so keep that output in an ignored fleet file.

## Required fields

| Field | Value | Purpose |
| --- | --- | --- |
| `schema` | `"narwhal.fleet"` | Fleet interface identity. |
| `schema_version` | `1` | Fleet document version. |
| `model` | string | Model name exposed by the router. Every engine must serve this name. |
| `engines` | array | Engine records. At least one record is required and `iid` values must be unique. |
| `slo.ttft_s` | positive seconds | TTFT target used by placement, admission, pool load, and control. |
| `slo.tpot_s` | positive seconds | TPOT target used by placement, pool load, and control. |

Each engine record accepts these fields.

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `iid` | required | Scheduler identity. Values must be unique within `engines`. |
| `url` | required | HTTP base URL. The loader removes trailing `/` characters. |
| `attestation_url` | `""` | Full URL of this engine's attestation sidecar. `narwhal-check` requires it when `engine_contract` is present. |
| `role` | `"decode"` | Opening role. Accepted values are `"prefill"` and `"decode"`. |
| `pin` | `false` | Pins the engine's configured role. |

An engine record has this shape.

```json
{
  "iid": "n0",
  "url": "http://node0:8000",
  "attestation_url": "http://node0:8010/v1/attestation",
  "role": "prefill",
  "pin": false
}
```

An engine opens as `decode` unless its record says otherwise. Pinning fixes that role across placement, controller moves, and resume, which can reserve a prefill seat through warm-standby takeover.

Set the SLOs from measurements on the deployed engine shape. The router derives its Prometheus histogram buckets from these values.

## Engine contract

`recovery.engine_restart_policy` selects `individual` (default) or `whole_wave`. `whole_wave` requires a complete `engine_contract` and `recovery.liveness_every > 0`; an ejection or identity failure then holds the fleet until an operator completes the [wave procedure](Operate.md#restart-an-engine-wave).

`engine_contract` declares one expected engine generation for the fleet. Preflight and lifecycle readmission compare the running engines with it. Use a complete contract for fleets serving client traffic.

Lifecycle drain and readmission require every field in this contract. The router distinguishes a restarted process from a different engine build before exercising NIXL against a live peer; an empty contract field keeps the requested engine held out and reports `missing-contract`.

The `hardware` block records the accelerator identity and tensor-parallel shape. [Engine-host inspection](Deploy.md#3-inspect-each-engine-host) discovers the vendor and product on each remote machine. Set `accelerator` to that observed product name, and use positive values for the declared replica allocation in `accelerators_per_engine` and `tensor_parallel`. Tensor parallelism must fit within the accelerator count.

The external launcher selects vLLM's TP size. Match `hardware.tensor_parallel` to that setting and `hardware.accelerators_per_engine` to the accelerators allocated to one replica, then verify the running shape through process-bound attestation, profiles, transfer checks and deployment load.

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `vllm_version` | required | Exact value returned by every engine's `/version` route. |
| `image_digest` | `""` | Immutable `sha256:<64 hex>` container digest reported by the engine-side attestation document. |
| `nixl_version` | `""` | NIXL package version carried by the image. |
| `nixl_connector_version` | `0` | vLLM NIXL wire-protocol version. Zero means undeclared. |
| `model_architecture` | `""` | Model implementation name relevant to KV layout. |
| `model_dtype` | `""` | Model execution dtype. |
| `kv_heads` | `0` | Number of KV heads. Zero means undeclared. |
| `head_size` | `0` | KV head size. Zero means undeclared. |
| `hidden_layers` | `0` | Hidden-layer count. Zero means undeclared. |
| `attention_backend` | `""` | Runtime attention backend expected from the launch. |
| `kv_cache_dtype` | `""` | KV cache dtype. |
| `cross_layers_blocks` | `null` | Whether NIXL registers cross-layer KV blocks. |
| `hybrid_kv_cache_manager` | `null` | Whether vLLM's hybrid KV cache manager participates in the layout. |
| `connector` | `"NixlConnector"` | Engine-side connector name. Must be nonempty. |
| `kv_role` | `""` | Engine-side role semantics, such as `kv_both`. |
| `transfer_mode` | `""` | Pull or push transfer mode. |
| `speculative_config` | `""` | Stable name for the speculation configuration, or `disabled`. |
| `enforce_handshake_compat` | `true` | Declares that vLLM's NIXL compatibility hash is enabled. `false` is rejected. |

### Attestation document

Copy [`config/engine-attestation.example.json`](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-attestation.example.json), replace `contract` with the complete values from the fleet config, and map each field under `sources` to its container inspection, package record, model config, launch config or startup log. The document declares `schema: "narwhal.attestation"` and `schema_version: 1`; `narwhal-attest` requires one nonempty source entry for every populated contract field before querying the engine.

Run one sidecar beside each contracted engine and set its `attestation_url`. At startup, the sidecar reads `/version` and `process_start_time_seconds`, adds that engine identity to `contract` and `sources`, and hashes the response into `attestation_digest`. Both sidecar routes return HTTP 503 after either identity value changes, so the supervisor restarts the sidecar with the new engine process.

`narwhal-check` compares every attested field and the process start before opening the NIXL handshake and produce/consume probes; a mismatch stops the sequence before live KV transfer.

## Role control

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `controller.advisory` | `false` | Records proposed controller splits and reasons while preserving current roles. |
| `controller.monitor_interval_s` | `1.0` | Delay between monitor passes. Must be positive. Validation limits `recovery.health.min_samples` to `floor(recovery.health.window_s / controller.monitor_interval_s)`. Each pass contributes at most one residual per engine; slow passes can leave a window undersampled. |
| `controller.monitor_failure_limit` | `5` | Consecutive monitoring passes with any stage failure before the router enters the degraded state and stops admitting new requests. Must be at least 1. The streak counts failures in any monitor stage, even when role control succeeds. |
| `controller.min_prefill` | `1` | Minimum live prefill engines preserved by role changes. Must be at least 1. |
| `controller.min_decode` | `1` | Minimum live decode engines preserved by role changes. Must be at least 1. |
| `controller.thresholds.expand` | `1.0` | SLO-relative pool load that triggers reactive growth. Must be positive. |
| `controller.thresholds.shrink` | `0.5` | Maximum projected source load for ordinary consolidation. The mixed-pressure D-to-P rule below can exceed it. Must be nonnegative and below `expand`. |
| `controller.thresholds.cooldown_s` | `10.0` | Minimum time between prefill-to-decode moves. Must be nonnegative. |
| `controller.thresholds.sustained_intervals` | `3` | Lower bound on confirmations for nonurgent, incomplete-demand and mixed-pressure proposals. Must be at least 1. |
| `controller.thresholds.dwell_s` | `0.0` | Time a moved engine must remain in its new role. Must be nonnegative. |
| `controller.thresholds.panic_ratio` | `0.0` | Decode-load multiple that may bypass cooldown while prefill stays below `shrink`. `0` disables it. Enabled values must be at least 1. |
| `controller.thresholds.flip_resident_guard` | `0` | Maximum resident decode streams allowed on the decode engine chosen for a decode-to-prefill move. The move waits until residency drains to this count. `0` disables the guard. Must be nonnegative. |
| `controller.flip_history` | `1000` | Maximum retained role-change records exposed through `/narwhal/state`. Must be at least 1. |

Prefill load divides predicted work by the TTFT target. Decode load measures the observed token interval above the corrected idle floor as a fraction of the remaining TPOT budget. When that floor already reaches the TPOT target, decode load uses the raw interval-to-target ratio. A value of `1.0` reaches the phase target.

`controller.min_prefill + controller.min_decode` must fit the configured fleet. The loader also accepts a single engine with both floors set to 1; that topology serves aggregate inference. Pins, drains, quarantine, and health ejections can leave fewer movable engines than either floor needs. The controller reports the breach and holds the safest reachable split.

Role changes preserve `controller.min_decode`. If live decode capacity falls below that floor, the monitor bypasses the ordinary cooldown and restores one eligible engine per pass. Decode-floor recovery still observes per-engine dwell.

Prefill-floor recovery bypasses cooldown and dwell. It still respects pins, engine availability, both role floors, and advisory mode. Every applied recovery records its dwell timestamp. After unavailable capacity returns, the resulting split goes through the normal adjacent-split decision. Recovery records obey the same `controller.flip_history` bound as ordinary moves.

A role change takes effect for new requests immediately. Existing requests stay on their serving engine and keep their reservations until they finish or are cancelled. The controller includes that remaining work when considering further role changes.

Engine relaunches and operator drains make an engine unavailable. Before moving decode capacity to prefill, the controller checks that the projected fleet split and every resident decode batch fit within the measured profile domain and available KV capacity.

Use `controller.advisory: true` with recorded traffic before changing a production split. `/narwhal/state` and Prometheus expose the proposed prefill and decode counts, caller, reason, and result.

The controller compares the current split with each adjacent split. Ordinary consolidation requires projected source pressure at or below `controller.thresholds.shrink` and an improvement in the worst projected SLO ratio of at least `controller.reactive.movement_margin`.

When observed prefill pressure reaches `controller.thresholds.expand` and every engine has a profile, the mixed-pressure rule can move a decode engine to prefill even while projected decode pressure remains above `controller.thresholds.shrink`. The proposed split must reduce the worst projected SLO ratio by at least the movement margin, with a strict improvement even when that margin is zero.

Confirmation requires the greater of `controller.reactive.confirmations` and `controller.thresholds.sustained_intervals`, including when demand is complete. Changing the proposed split, demand completeness or eligibility rule restarts confirmation; a candidate that fails the checks clears it.

P-to-D moves require prefill pressure at or below `shrink` and observe the decode cooldown. During overload, the movement margin, confirmations, consolidation evidence and dwell limit repeated role changes. Meeting the SLOs under sustained overload still requires lower offered traffic or more capacity.

Decode-to-prefill consolidation requires a closed arrival-evidence window. The window closes after `controller.reactive.evidence_span_s` with at least `controller.reactive.evidence_min_arrivals` samples, or at `controller.reactive.evidence_max_span_s` under sparse traffic.

Decode demand must also be stable: consolidation waits while the short-horizon estimate exceeds the long-horizon estimate by more than `controller.reactive.demand_rise_tolerance`. Candidate pricing uses the larger estimate and accounts for resident and pending decode work.

A first-token timeout or a prefill-to-decode recovery move restarts the consolidation evidence window. Moves toward decode and emergency floor restoration can proceed while evidence accumulates.

Predictive refusals count as offered demand and attainment misses while leaving the window in place. State and metrics report the demand estimates, evidence window and any gate blocking a move.

### Reactive fields

Configure reactive control through `controller.reactive`. Repeat preflight and deployment load measurement after changing the engine build or serving policy.

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `controller.reactive.window_s` | `120.0` | Demand estimation window. Must be positive. |
| `controller.reactive.confirmations` | `2` | Consecutive identical adjacent proposals for nonurgent, incomplete-demand and mixed-pressure moves. Must be at least 1. |
| `controller.reactive.utilization` | `0.8` | Fraction of each engine treated as available capacity. Must be greater than 0 and at most 1. |
| `controller.reactive.min_arrivals` | `10` | Minimum arrivals required for a decision. Must be at least 1. |
| `controller.reactive.demand_floor` | `0.5` | Minimum demand signal accepted by the reactive controller. Must be positive. |
| `controller.reactive.movement_margin` | `0.05` | Minimum improvement in the worst projected SLO ratio before moving an engine. Valid range is `[0, 1)`. |
| `controller.reactive.step_s` | `5.0` | Minimum interval between scheduled adjacent role-change evaluations. A projected prefill TTFT breach can wake one guarded D-to-P evaluation between scheduled passes. Must be positive. |
| `controller.reactive.evidence_span_s` | `60.0` | Minimum elapsed span of recent arrival evidence required before D-to-P consolidation. Also sets the short horizon of the rising-demand check. Must be positive and at most `controller.reactive.evidence_max_span_s`. |
| `controller.reactive.evidence_max_span_s` | `120.0` | Bounded evidence duration for sparse traffic. The evidence window closes at this span even with fewer than `controller.reactive.evidence_min_arrivals` samples. Must be positive, at least `controller.reactive.evidence_span_s`, and at most `controller.reactive.window_s`. |
| `controller.reactive.evidence_min_arrivals` | `10` | Minimum arrival sample count inside the evidence span before D-to-P consolidation. Must be at least 1. |
| `controller.reactive.demand_rise_tolerance` | `0.25` | Tolerated rise of the short-horizon decode demand estimate over the long-horizon one. Consolidation is refused while the short estimate exceeds the long estimate by more than a factor of `1 + demand_rise_tolerance`. Must be finite and at least 0. |
| `controller.reactive.decode_correction_min` | `0.5` | Lowest live/profile decode ratio applied to capacity estimates. Must be positive. |
| `controller.reactive.decode_correction_max` | `2.0` | Highest live/profile decode ratio applied to capacity estimates. Must be at least the minimum. |
| `controller.reactive.decode_correction_alpha` | `0.2` | Share of each qualifying observation window applied to the correction. Valid range is `(0, 1]`. |
| `controller.reactive.decode_correction_min_samples` | `8` | Decode gaps required before an observation window updates the correction. Must be at least 1. |

The controller prices demand with the mean profile of the configured engines. Decode profiles use active requests and resident KV tokens as separate inputs. Recent token intervals apply a bounded correction to the profiled estimate. Every configured engine uses one hardware and TP shape so those measurements describe the complete fleet.

## Bounded serving

By default Narwhal admits work straight into phase dispatch and makes one prefill/decode attempt per request. Nested `serving` fields add FIFO waiting and retries within the original deadline, dispatching each phase when its eligible role pool has capacity.

| Field | Default | Meaning |
| --- | --- | --- |
| `serving.queue_capacity` | `0` | Maximum waiting admission requests. Zero rejects saturation immediately. |
| `serving.queue_timeout_s` | `0.0` | Maximum admission wait, also bounded by the original request deadline. Positive when queueing is enabled. |
| `serving.prefill_concurrency` | `0` | Resident prefill requests per engine. A positive measured limit is required with queueing. |
| `serving.decode_concurrency` | `0` | Resident decode requests per engine. A positive measured limit is required with queueing. |
| `serving.handoff_timeout_s` | `0.0` | Conservative KV handoff age measured from the start of the prefill HTTP call. Queueing or retries require a positive limit below the verified backend lease. At zero the original request deadline remains the sole handoff-age bound. |
| `serving.max_attempts` | `1` | Total complete prefill/decode attempts per original request, from 1 to 3. |
| `serving.retry_base_s` | `0.1` | Initial exponential backoff ceiling; full jitter samples between zero and that ceiling. |
| `serving.retry_cap_s` | `1.0` | Maximum backoff ceiling. Must be at least the base. |
| `serving.retry_budget` | `10` | Initial and maximum shared retry credits. Each retry costs one credit. |
| `serving.retry_replenish` | `0.1` | Credits earned per successful original request, from zero to one. |
| `serving.max_request_bytes` | `4194304` | HTTP request-body byte ceiling, enforced while reading. |
| `serving.max_response_bytes` | `16777216` | Retained bytes per non-streaming response attempt and per streaming pre-output metadata buffer. |

The router retains at most `serving.max_connections + serving.queue_capacity` completion requests while reading bodies, waiting for admission or writing responses. Reaching that limit returns 429 before body parsing and records the request as an unsized offer.

Active requests share one global limit. Requests waiting for admission or phase dispatch contribute to demand, and admission counts retries against the original arrival's deadline and reservation.

Before visible output begins, Narwhal can retry transient transport failures and HTTP 408, 429, 500, 502, 503 and 504 responses. Each retry starts with fresh prefill ownership, as does recovery from an expired handoff.

Permanent errors, local HTTP pool starvation, cancellation and failures after visible output end the request. For non-streaming responses, the router discards partial output from a failed attempt before retrying.

Every attempt shares the original request deadline, including time spent on tokenization, backoff and client writes.

Choose queue capacity, phase concurrency and deadlines from measured workload capacity and latency. The byte limits bound how much request and response data the router retains.

Set `serving.handoff_timeout_s` below the producer's KV lease, then verify that the backend releases abandoned handoffs when their leases expire.

Prefill failures and non-streaming decode failures return HTTP errors. Streaming responses commit HTTP 200 before decode starts, so a later failure appears as a terminal error event even when decode fails before producing its first token.

When the original deadline expires, the router sends `code: expired` and closes the stream; client backpressure closes the connection immediately. Clients should treat an error event or a stream ending ahead of its success terminator as a failed response and retry against the remaining deadline.

Model-aware placement, resident role changes, cancellation, timeouts, engine exclusion and lifecycle recovery remain active. `recovery.failure_quarantine_s` holds failed engines out of placement while health checks catch up.

Queues, phase concurrency, handoff expiry, retries and body limits change the deployment envelope. Repeat the workload measurement after changing any of these settings.

## Placement and admission

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `serving.admission` | `"predictive"` | `predictive` prices every prefill path against the TTFT budget and keeps aggregate placement inside the single-phase domain measured by the engine curves; details follow. `open` bypasses both checks. |
| `serving.admission_margin` | `0.0` | Fraction added to the TTFT admission budget to reduce boundary churn. Must be nonnegative. |
| `serving.max_connections` | `512` | Global admitted-request limit and HTTP data-pool size. Must be at least 1. |
| `engine.control_connections` | `0` | Reserved HTTP connections for health and recovery probes. `0` derives two per engine with a floor of four. Must be nonnegative. |

Predictive admission returns `429` when the cheapest prefill path exceeds the TTFT budget. Engine-backlog refusals carry `Retry-After` with the projected wait. Prompt-only refusals carry the error envelope with the corrective action: shorten the prompt or raise the TTFT target.

Measure sustained healthy inflight work before raising `serving.max_connections`. If admitted work exceeds what engines can drain before KV handoffs expire, decode can fail. The loader refuses an admission override above `serving.max_connections` before startup because admission would outrun the dispatch pool.

### Placement

Narwhal filters engines by role, availability, exclusions, and projected SLO compliance. It selects the lowest-cost eligible engine, with a deterministic instance-ID tie-break. When every candidate exceeds the SLO, it records an unserved placement and selects the least-cost fallback. Engine prefix caching operates independently of router placement.

A live role change immediately updates the scheduler role used for new requests. Requests already resident on the engine keep their serving assignment until they finish or are cancelled. Lifecycle drains, quarantine, ejection, and restart holds remain enforced.

## Engine requests and timeouts

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `profiles.path` | `"runs/profiles.json"` | Profile store loaded by the router and written by `narwhal-profile`. |
| `serving.request_timeout_s` | `600.0` | Original completion deadline from HTTP ingress through response delivery. Must be positive. |
| `serving.prefill_timeout_s` | `120.0` | Deadline for the prefill leg. Must be positive. |
| `recovery.failure_quarantine_s` | `0.0` | Time an engine failure holds the engine out of placement. `0` disables quarantine. Must be nonnegative. |
| `engine.decode_read_timeout_s` | `60.0` | Maximum silent gap between decode chunks. `0` disables the gap bound. Must be nonnegative. |
| `engine.first_token_timeout_s` | `2.5` | Deadline for the decode leg's first token and each functional verification leg. Must be positive. |
| `engine.tokenize` | `true` | Requests exact input length through the dialect's tokenize route. |
| `engine.tokenize_timeout_s` | `2.0` | Deadline for exact token counting. Must be positive. |
| `engine.chars_per_token` | `3.8` | The router applies this character ratio when the dialect's tokenize route is unreachable. Must be positive. |
| `engine.pool_timeout_s` | `5.0` | Maximum wait for an engine HTTP connection slot on either pool. A probe that exhausts this wait leaves the engine verdict unchanged. Must be positive. |
| `engine.connect_timeout_s` | `10.0` | Engine TCP connection deadline. Must be positive. |
| `engine.health_timeout_s` | `5.0` | Deadline for preflight, breaker, and readmission health probes. Must be positive. |

Set `engine.first_token_timeout_s` above the crossed-handoff p99 measured for the fleet's context range.

The original `serving.request_timeout_s` deadline bounds the complete decode stream. `engine.first_token_timeout_s` limits the time from opening the decode HTTP stream to the first generated token. After that token, `engine.decode_read_timeout_s` bounds gaps between transport chunks, including partial SSE lines and metadata. A value of `0` uses the request deadline as the stream bound. Breaker verification applies `engine.first_token_timeout_s` to each complete prefill and decode probe.

Retries start a complete prefill/decode attempt before visible output. [Bounded serving](#bounded-serving) defines retry eligibility and resource limits.

An error in `engine.chars_per_token` affects the quadratic prefill estimate. Profile the tokenizer ratio for dialects that estimate token counts from text length.

## Engine health

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `recovery.eject_after` | `3` | Consecutive failed legs required before breaker action. Must be at least 1. |
| `recovery.readmit_every` | `10` | Monitor intervals between probes of ejected engines. Must be at least 1. |
| `recovery.liveness_every` | `10` | Monitor intervals between health probes and, for contracted fleets, process identity and attestation checks. `0` disables idle sweeps and is forbidden with `whole_wave` restart policy. |
| `recovery.liveness_misses` | `2` | Consecutive missed liveness probes required for ejection. Must be at least 1. |
| `recovery.health.window_s` | `30.0` | Duration of each residual-scoring window. Must be at least 1 second. The first observation opens the window; slow passes or skipped observations can leave it undersampled. |
| `recovery.health.drift_band` | `2.0` | Multiple of the engine's trailing healthy residual that marks drift. Must exceed 1.0. |
| `recovery.health.relative_band` | `1.5` | Peer-relative multiple that overrides the fleet-surge veto. `0` disables the veto. Must be nonnegative. |
| `recovery.health.min_samples` | `3` | Observations required to score a window. Must be at least 1 and at most `floor(recovery.health.window_s / controller.monitor_interval_s)`, the configured cadence bound. An undersampled window increments `health.undersampled` in `/narwhal/state`. |
| `recovery.health.probation_windows` | `3` | Consecutive drifting windows required for probation. Must be at least 1. |
| `recovery.health.evict_windows` | `5` | Consecutive drifting windows required to request ejection. Must be at least 1 and at least `recovery.health.probation_windows`, since ejection counts up from probation. |
| `recovery.health.recovery_windows` | `3` | Consecutive healthy windows required to clear probation. Must be at least 1. |
| `recovery.health.probation_penalty_s` | `1.5` | Placement penalty applied during probation. Must be nonnegative. |

Connection failures count directly toward `recovery.eject_after`. Transport timeouts first trigger a health probe. First-token deadlines and mid-stream silence require functional verification of the failing inference path. Inconclusive verification retains the hold and retries on the readmission cadence. Router handoff preserves the hold and its failed transfer paths.

The drift tracker compares fresh decode residuals and stalled inter-token gaps with each engine's recent healthy baseline, dropping placement estimates retained after decode work finishes from the health evidence.

Local prefill work invalidates the decode-only profile for that interval. The tracker pauses decode correction and drift scoring for gaps that cross a prefill boundary, including prefills that start and finish between monitoring passes. Once pure decode observations resume, it opens a fresh scoring window and keeps the existing healthy baseline and probation state.

Client latency, controller pressure, request timeouts and liveness checks continue to observe mixed work throughout the pause. Use `health.prefill_paused` and `health.prefill_pauses` in `/narwhal/state` to inspect these pauses.

The peer-relative check suppresses ejection when the whole fleet slows down. A nonempty window with too few observations closes as `undersampled`, discards its residuals and carries the engine's baseline and probation state into the next window.

State and `narwhal_health_windows_*_total` report scored and undersampled windows, while `last_scored_s_ago` measures the time since the last verdict. Confirmed ejection clears that engine's drift history.

## Resume and shutdown

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `recovery.state_path` | `"runs/state.json"` | Atomic handoff file containing roles, ejections, lifecycle state, and counters. |
| `recovery.resume` | `false` | Applies a matching handoff at process start. |
| `serving.graceful_timeout_s` | `30.0` | Uvicorn drain time after `SIGTERM`. Must be nonnegative. |

With a saved handoff, the router resumes from `recovery.state_path`; at first boot it opens the configured split. An uncontracted development fleet also opens that split when the saved engine set differs from the current fleet.

Contracted resume and automatic takeover require handoff schema version 1, including accepted process identities for every available engine. An unknown schema or version aborts startup. If an existing handoff passes the schema check but fails to apply to a contracted fleet, the fleet stays held for a managed wave.

On a successful resume, the router restores saved roles, ejections and lifecycle holds, including a complete backend outage. Per-engine dwell timestamps start fresh, and the P-to-D cooldown begins when the router constructs the replacement scheduler.

Automatic warm-standby takeover is CLI-configured because router IDs and the shared lease path differ by host. [Operate Narwhal](Operate.md#start-production-routers) defines readiness, fencing, recovery, and partition behaviour.

## Engine authentication

```json
{
  "engine": {
    "engine_api_key_env": "NARWHAL_ENGINE_KEY"
  }
}
```

Ingress terminates client credentials. Narwhal attaches the configured engine credential and a router-generated `x-request-id` unique to each attempt and phase.

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `engine.engine_api_key_env` | `""` | Environment variable resolved when constructing an engine client. Engine clients attach the Bearer credential to serving, profiling, preflight, cache reset and lifecycle requests. Attestation requests use the sidecar URL through the trusted control network. Named but unset variables fail client construction. |

`/narwhal/state` exposes `boundary` or `engine-credential` under `admission.engine_auth`. Keep that mode fixed across workload measurement and serving.

## Request journal

Narwhal writes request timings to `journal.jsonl` beside `profiles.path` unless `narwhal-serve --journal` selects another path. The [API and data reference](API-and-Data-Reference.md#request-journal) documents its fields.

## Protocol adapters

| Field | Default | Accepted values |
| --- | --- | --- |
| `engine.connector` | `"nixl"` | Registered KV transport. This release registers `nixl`. |
| `engine.dialect` | `"vllm"` | Registered engine HTTP dialect. This release registers `vllm`. |

A connector or dialect enters the registry after its preflight gates pass against the target engine build. Unknown names fail config validation.

## Profile validation

The `profiles` section selects the profile store and the decode fit limits accepted by `narwhal-check`.

| Field | Default | Purpose and validation |
| --- | --- | --- |
| `profiles.max_decode_fit_mape` | `0.05` | Largest accepted in-sample decode fit error. Must be positive, finite and bounded by `controller.reactive.movement_margin`. The defaults for the fit limit and movement margin are equal. |
| `profiles.max_decode_cv_mape` | `0.13` | Largest accepted leave-one-out cross-validation error. Must be positive and finite. Validate the profile over the context and concurrency range the deployment will serve. |

`narwhal-check` applies both limits to every configured engine and reports the engine, measured error and accepted limit.

## Paths and CLI precedence

Relative values in `profiles.path` and `recovery.state_path` resolve from the serving process's working directory. The `narwhal-serve --journal` option is CLI-only and defaults to `journal.jsonl` beside `profiles.path`.

The serving CLI applies these overrides after loading the fleet config.

| CLI option | Config value | Precedence |
| --- | --- | --- |
| `--max-concurrent` | `serving.max_connections` | Overrides router admission capacity. |
| `--graceful-timeout` | `serving.graceful_timeout_s` | Overrides Uvicorn's shutdown drain time. |
| `--resume` | `recovery.resume` | Forces resume on. A configured `true` remains enabled. |

`--host`, `--port`, `--log-level`, `--journal`, and the warm-standby options are CLI-only. The [CLI reference](CLI-Reference.md) lists their defaults and validation.

## Config provenance

Keep a copy of the exact config beside every scored run. The document contains engine URLs and may reveal site addresses. Replace those values before publishing an artifact.

The shipped annotated config uses placeholder addresses. Working configs with live addresses belong under `runs/` or a gitignored `config/fleet.*.json` path.
