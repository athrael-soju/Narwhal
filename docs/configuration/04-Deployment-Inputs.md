---
description: The deployment environment, trust inputs, generated artifacts, host inventory and SSH trust for a Narwhal fleet.
---

# Deployment inputs and SSH trust

## Deployment inputs and generated artifacts

`narwhal deploy` reads its inputs from the environment of the workstation, which a private `.env` file sets, as [Loading the private environment](../deploy/01-Discover.md#loading-the-private-environment) shows. This page lists every input. `plan` and `run` name each one that is missing or malformed before they reach a host.

The trust inputs pin what the deployment accepts. The commands that read them refuse to run without them:

| Variable | Read by | Value |
| --- | --- | --- |
| `NARWHAL_SSH_KNOWN_HOSTS` | Every `narwhal deploy` subcommand that reaches a host | Known-hosts file with the verified key of every SSH destination, as in [Known-hosts handling](#known-hosts-handling) |
| `NARWHAL_MODEL_TREE_SHA256` | `narwhal deploy discover`, and `narwhal-engine prepare` through each engine's role environment | The checkpoint's `model_tree_sha256`, computed as in [Staging and identifying the checkpoint](../deploy/01-Discover.md#staging-and-identifying-the-checkpoint). `NARWHAL_NODE_<n>_MODEL_TREE_SHA256` sets it for one engine |
| `NARWHAL_WHEEL` | `narwhal deploy package` | Path of the release wheel, `narwhal_inference-<major>.<minor>.<patch>-py3-none-any.whl`, that every host installs |
| `NARWHAL_WHEEL_SHA256` | `narwhal deploy package`, then `narwhal deploy install` on each host | SHA-256 of that wheel, 64 lowercase hex digits |
| `NARWHAL_REQUIREMENTS` | `narwhal deploy package`, then `pip` on each host | Hash-checking lock of the wheel's dependencies, as in [Dependency lock](#dependency-lock) |
| `NARWHAL_REQUIREMENTS_SHA256` | `narwhal deploy package`, then `narwhal deploy install` on each host | SHA-256 of that lock |
| `NARWHAL_ROUTER_TOKEN` | `narwhal deploy plan` and `run`, then each router through its role environment | The router's [control token](../HTTP-API.md#control-token), at least 32 random bytes: 64 hex digits or 43 base64url characters. Generate it with `python -c 'import secrets; print(secrets.token_hex(32))'` |

The control token is a credential. It reaches only the router role files, `.env.router` and `.env.standby-router` at mode 0600, which the routers and the readmission in `router-start` read. The run record holds only its HMAC under the run's salt, as part of the `discovery` input, so changing the token reruns `discover` and every stage after it, as changing `NARWHAL_ENGINE_KEY` does. A Kubernetes fleet takes the token from a Secret instead, and refuses an environment that holds it.

`NARWHAL_SSH_JUMP` is optional. It names the one jump host, an alias or `user@host`, through which every connection reaches its destination, as in [Jump host](#jump-host).

### Hosts and roles

| Variable | Value |
| --- | --- |
| `NARWHAL_NODE_<n>_SSH` | SSH destination of engine `<n>`'s host: an alias, `user@host`, or `ssh://user@host:port` for a port other than 22. Set one for each engine; engines with the same destination share a host |
| `NARWHAL_ROUTER_SSH` | Optional destination of the primary router's host. It defaults to the lowest-numbered engine's destination |
| `NARWHAL_STANDBY_ROUTER_SSH`, `NARWHAL_LOAD_BALANCER_SSH` | The hosts of the [router pair](#router-pair)'s standby and load balancer |
| `<destination variable>_PASSWORD` | Optional SSH password of that destination, such as `NARWHAL_NODE_1_SSH_PASSWORD`. Without one, the connection uses a key or the SSH agent |

[Host inventory and SSH trust](#host-inventory-and-ssh-trust) describes the inventory that discovery builds from them.

### Engine and discovery inputs

Discovery reads these, and each engine's role environment carries the engine values. `NARWHAL_NODE_<n>_<field>` sets a field for engine `<n>` alone, such as `NARWHAL_NODE_2_ENGINE_PORT`.

| Variable | Value |
| --- | --- |
| `NARWHAL_ENGINE_IMAGE` | The engine image, pinned by image ID or registry digest |
| `NARWHAL_ENGINE_MODEL_NAME` | The served model name |
| `NARWHAL_MODEL_DIR` | The checkpoint directory on each engine host, which the engine mounts read-only at `/model` |
| `NARWHAL_MODEL_PATH`, `NARWHAL_MODEL_SHA256` | For a [GGUF model](../deploy/01-Discover.md#staging-a-gguf-model-on-vllm), the `.gguf` file in `NARWHAL_MODEL_DIR`, and an optional SHA-256 that discovery checks it against |
| `NARWHAL_GGUF_PLUGIN_PYTHON_SHA256`, `NARWHAL_GGUF_PLUGIN_EXTENSION_SHA256` | For a GGUF model, the plugin digests the engine image build prints |
| `NARWHAL_RUN_DIR` | An existing directory on each engine host |
| `NARWHAL_FABRIC_INTERFACE` | The interface that carries KV, whose global address gives the engine and attestation URLs |
| `NARWHAL_ENGINE_PORT`, `NARWHAL_ATTEST_PORT` | The engine's HTTP port and its attestation sidecar's port |
| `NARWHAL_KV_SIDE_CHANNEL_PORT` | The vLLM NIXL side channel, SGLang bootstrap server or TensorRT-LLM transfer port |
| `NARWHAL_UCX_TCP_PORT_RANGE` | `LOW-HIGH`, the ports UCX TCP listeners take |
| `NARWHAL_NODE_<n>_IP`, `NARWHAL_NODE_<n>_URL`, `NARWHAL_NODE_<n>_ATTESTATION_URL` | Optional overrides of the fabric address and the service URLs, as [Loading the private environment](../deploy/01-Discover.md#loading-the-private-environment) lists |
| `NARWHAL_ENGINE_BACKEND`, `NARWHAL_ENGINE_CONNECTOR`, `NARWHAL_DECODE_CUDA_GRAPH_MEMORY_GB`, `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE`, `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE`, `NARWHAL_CONTEXT_LENGTH`, `NARWHAL_MAX_SEQUENCES`, `NARWHAL_PREFIX_CACHING`, `NARWHAL_KV_EVENTS`, `NARWHAL_TOKENIZER`, `NARWHAL_LANGUAGE_MODEL_ONLY`, `NARWHAL_ENGINE_ARGS`, `NARWHAL_ENGINE_ENV`, `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES`, `NARWHAL_ENGINE_KEY` | The optional launch policy, as [Confirming the launch policy](../deploy/01-Discover.md#confirming-the-launch-policy) lists |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S` | The fleet's initial latency targets, which `fleet-limits` later sets as the SLO |

Discovery writes each engine's `NARWHAL_NODE_<n>_IP`, `NARWHAL_NODE_<n>_URL` and `NARWHAL_NODE_<n>_ATTESTATION_URL`, its observed image ID as `NARWHAL_NODE_<n>_ENGINE_IMAGE`, and its model digests as `NARWHAL_NODE_<n>_MODEL_CONFIG_SHA256` and `NARWHAL_NODE_<n>_MODEL_SHA256` to `deployment.env`, which `package` reads.

Discovery writes one private set of files per fleet at mode 0600:

| File                         | Selected by             | Default path                                           |
| ---------------------------- | ----------------------- | ------------------------------------------------------ |
| Host inventory               | `NARWHAL_HOSTS`         | `config/hosts.local.json`                              |
| Launch records               | `NARWHAL_LAUNCH_CONFIG` | `config/engine-launch.local.json`                      |
| Launch-record sources        |                         | `engine-launch.sources.json` beside the launch records |
| Fleet                        | `NARWHAL_FLEET`         | `config/fleet.json`                                    |
| Derived deployment variables |                         | `deployment.env` beside the fleet                      |

| Directory                    | Host                 | Contents                                                                                     |
| ---------------------------- | -------------------- | -------------------------------------------------------------------------------------------- |
| `runs/discovery/<run>/`      | Workstation          | Per-engine checkpoint file hashes, the checked tree digest, observations, and command logs   |
| `runs/deployment-env/<run>/` | Workstation          | The packaged wheel, role files by host ID, manifest, and command logs                        |
| `~/Narwhal-deploy/<id>/`     | Each host            | The deployment directory, with `package/`, `.venv` and the role files at their working paths |
| `runs/deployment/`           | Deployment directory | Role environments, the effective fleet, and `profiling-limits.json`                          |

Load `.env` and `config/deployment.env` before reusing saved discovery inputs.

### Narwhal wheel and deployment package

Build the package for [host installation](../deploy/02-Install.md):

```bash
narwhal deploy package --out <directory>
```

| Command                  | Output                                                                                                         |
| ------------------------ | -------------------------------------------------------------------------------------------------------------- |
| `narwhal deploy package` | A copy of the wheel checked against `NARWHAL_WHEEL_SHA256`, and role files under `<directory>/<host-id>/`      |
| `narwhal deploy install` | The package, and a virtual environment with the wheel installed, in each selected host's deployment directory |

Each role environment sets `NARWHAL_DEPLOYMENT_REVISION` to `NARWHAL_WHEEL_SHA256`, and launch plans record it as `revision`.

### Dependency lock

Every host installs the same dependency files: `pip` runs in hash-checking mode with `--require-hashes --only-binary :all:`, from the lock that `NARWHAL_REQUIREMENTS` names, and installs the wheel itself with `--no-deps --no-index`. `pip check` then confirms that the lock satisfies every dependency of the wheel. The hosts still download the locked files from their Python package index.

Compile the lock from the wheel with `uv`, for every platform and Python version that the wheel supports, and record its SHA-256 with the wheel's:

```bash
echo "narwhal-inference @ file://$(realpath "$NARWHAL_WHEEL")" > requirements.in
uv pip compile requirements.in --universal --python-version 3.11 --generate-hashes \
  --no-emit-package narwhal-inference -o requirements.lock
sha256sum requirements.lock
```

Keep `requirements.lock` with the wheel, and set `NARWHAL_REQUIREMENTS` to its path. Compile it again only to take new dependency releases, and package a new run when you do.

`package` refuses a lock with a line that does not pin one exact version with its `--hash=sha256` values, a lock with pip options such as `--index-url`, and a lock that names `narwhal-inference`.

### Generated host-side files

Paths are relative to the deployment directory.

| Remote file                                   | Contents                                                                                                                                                                       | Source                                                                                                                                                                                                   |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runs/deployment/.env.router`                 | Revision, `NARWHAL_FLEET=runs/deployment/fleet.json`, referenced engine and attestation URLs, configured engine credential, and optional router and monitoring stack settings. | `NARWHAL_WHEEL_SHA256`, variables referenced by fleet endpoint fields and `engine.engine_api_key_env`, `NARWHAL_ROUTER_URL`, `NARWHAL_TARGETS_URL`, `NARWHAL_FLEET_NAME`, `NARWHAL_ALERTMANAGER_URL`, `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| `runs/deployment/.env.engine-<n>`             | Revision, launch and artifact fields, helper snapshot paths and SHA-256 values, selected node URLs, fabric peer addresses, and configured engine credential.                  | `NARWHAL_WHEEL_SHA256`, shared engine fields, optional `NARWHAL_NODE_<n>_<field>` overrides, derived service URLs and fabric addresses, the packaged helper snapshots, and configured engine credential. |
| `config/engine-launch.engine-<n>.json`        | GPU allocation, TP size, device mappings, resolved UCX selection, and generated launch arguments.                                                                              | The engine role in workstation `NARWHAL_LAUNCH_CONFIG`.                                                                                                                                                  |
| `runs/deployment-tools/cache_capture_hook.py` | Copy of the cache-capture hook for a vLLM or TensorRT-LLM fleet, with its path and SHA-256 in `.env.engine-<n>`.                                                               | `narwhal/backends/<backend>/cache_capture_hook.py` in the wheel that `NARWHAL_WHEEL` names, for `vllm` or `trtllm`.                                                                                    |
| `runs/deployment-tools/transfer_measure.py`   | Standalone KV handoff measurement tool for an SGLang fleet, with its path and SHA-256 in `.env.engine-<n>`.                                                                    | `narwhal/backends/sglang/transfer_measure.py` in the wheel that `NARWHAL_WHEEL` names.                                                                                                                |
| `runs/deployment/fleet.json`                  | Effective generated fleet copied before deployment edits.                                                                                                                      | Workstation file selected by `NARWHAL_FLEET`.                                                                                                                                                            |
| `runs/deployment/profiling-limits.json`       | Each engine's `runtime.max_sequences` by engine ID, or the backend default when unset (`256` on SGLang, `64` on TensorRT-LLM), on the router host.                                                               | Each engine's generated launch record.                                                                                                                                                                   |

Engine export requires:

```text
NARWHAL_ENGINE_IMAGE
NARWHAL_ENGINE_MODEL_NAME
NARWHAL_MODEL_DIR
NARWHAL_RUN_DIR
NARWHAL_MODEL_CONFIG_SHA256
NARWHAL_MODEL_TREE_SHA256
NARWHAL_FABRIC_INTERFACE
NARWHAL_ENGINE_PORT
NARWHAL_ATTEST_PORT
NARWHAL_UCX_TCP_PORT_RANGE
NARWHAL_KV_SIDE_CHANNEL_PORT
```

With `engine.engine_api_key_env` set, engine export requires the variable it names.

For a [GGUF model](../deploy/01-Discover.md#staging-a-gguf-model-on-vllm), engine export also carries `NARWHAL_MODEL_PATH` and `NARWHAL_MODEL_SHA256`, and fails when only one of them is set.

Each `.env.engine-<n>` sets `NARWHAL_ENGINE_LAUNCH_CONFIG=config/engine-launch.engine-<n>.json`.

Per-node overrides insert `NODE_<n>_` after `NARWHAL_`. For example, `NARWHAL_NODE_2_ENGINE_PORT` overrides `NARWHAL_ENGINE_PORT` in `.env.engine-2`.

Export fails when:

- a required value is unset or empty
- a per-node override is set to an empty value
- a variable name contains `SSH`
- a variable name matches an access variable from the host inventory

### Kubernetes launch inputs

[`narwhal-engine prepare --backend kubernetes`](../cli/Engine.md#kubernetes-plans) reads these variables as well as the engine inputs above, except `NARWHAL_NODE_<n>_IP`. It refuses a missing or malformed value.

| Variable | Value |
| --- | --- |
| `NARWHAL_KUBERNETES_GPU_RESOURCE` | The GPU device plugin's extended resource name, `<domain>/<name>`, such as `gpu.example.com/gpu` |
| `NARWHAL_KUBERNETES_RDMA_RESOURCE` | The RDMA device plugin's extended resource name, which hands each engine pod one HCA |
| `NARWHAL_KUBERNETES_MODEL_VOLUME` | A JSON volume source that serves the staged checkpoint, with one of `persistentVolumeClaim`, `hostPath`, `nfs` or `csi`, such as `{"persistentVolumeClaim": {"claimName": "models", "readOnly": true}}` |
| `NARWHAL_KUBERNETES_STATE_CLAIM` | The name of the state PersistentVolumeClaim, where the engine pod writes its records |
| `NARWHAL_KUBERNETES_API_KEY_SECRET` | `<secret name>/<key>` of the Secret that holds `NARWHAL_ENGINE_API_KEY`. Required when `NARWHAL_ENGINE_API_KEY` is set, and refused when it is not |
| `NARWHAL_KUBERNETES_SERVICE_ACCOUNT` | The service account the engine pod's attestation sidecar runs as. `kubernetes.json` holds it with a Role and RoleBinding that grant `get` on pods in its namespace |
| `NARWHAL_KUBERNETES_SIDECAR_IMAGE` | The Narwhal image the attestation sidecar runs, as a registry digest, `<repository>@sha256:<hex>` |
| `NARWHAL_KUBERNETES_RUNTIME_CLASS` | Optional. The RuntimeClass the engine pod runs under, such as `nvidia`, for nodes whose default container runtime exposes no GPUs |

A resource name in the `kubernetes.io` or `k8s.io` domain is refused, because Kubernetes reserves those domains. `NARWHAL_ENGINE_IMAGE` must be a registry digest, and `NARWHAL_NODE_<n>_URL` must name the engine pod. `NARWHAL_NODE_<n>_ATTESTATION_URL` must name the pod's sidecar route, `http://<pod>:<NARWHAL_ATTEST_PORT>/v1/attestation`, which the plan pins.

### Kubernetes target inputs

`narwhal deploy --target kubernetes` reads the Kubernetes launch inputs above, the engine and fabric inputs, the [deployment run inputs](#deployment-run-inputs) and these, as [Deploying on Kubernetes](../deploy/Kubernetes.md#inputs) describes:

| Variable | Value |
| --- | --- |
| `NARWHAL_KUBERNETES_NAMESPACE` | The namespace the fleet runs in. One fleet per namespace |
| `NARWHAL_KUBERNETES_VALUES` | A JSON file of chart values for `images`, `state`, `routers`, `haproxy`, `credentials` and `monitoring`. The run sets `engines`, `placement`, `gates`, `routers.enabled`, `routers.fleet` and `state.claimName` itself, and refuses a file that sets the first three. `images.narwhal` and `images.fabric` must be registry digests |
| `NARWHAL_NODE_<n>_KUBERNETES_NODE` | The node engine `n` runs on. Each one names an engine role; at least two are required |
| `NARWHAL_KUBERNETES_RELEASE` | Optional. The Helm release name, `narwhal` by default |
| `NARWHAL_KUBERNETES_CONTEXT` | Optional. The kubeconfig context; the current one by default |
| `NARWHAL_KUBERNETES_CHART` | Optional. The chart directory; `deploy/helm/narwhal` of the checkout by default |
| `NARWHAL_KUBECTL`, `NARWHAL_HELM` | Optional. The `kubectl` and `helm` executables |

### Fabric qualification inputs

[`narwhal deploy fabric`](../cli/Deploy.md#fabric-qualification) reads these variables from the workstation environment. It refuses to start, before it reaches any host, when one it needs is unset or invalid.

| Variable | Value |
| --- | --- |
| `NARWHAL_FABRIC_PROMPT_TOKENS` | Prompt tokens per KV handoff, a positive integer. `narwhal deploy transfer-measure` reads it too, for the SGLang handoffs it measures |
| `NARWHAL_FABRIC_HANDOFFS_PER_S` | Peak remote KV handoffs per second that each directed host edge carries for each source cache group |
| `NARWHAL_FABRIC_BURST` | Handoffs in a burst, a positive integer |
| `NARWHAL_FABRIC_TRANSFER_BUDGET_S` | Seconds in which one burst must transfer |
| `NARWHAL_FABRIC_HEADROOM` | Multiplier of at least 1 on the required rate |
| `NARWHAL_FABRIC_TEST_PORT` | TCP port that `iperf3` or `ib_write_bw` listens on at the destination of each sample. The firewall between the fabric addresses must already allow it |
| `NARWHAL_RDMA_GID_INDEX` | For `ucx_rdma` engines, the GID index of the HCA port in `transfer.net_devices` |
| `NARWHAL_RDMA_ROCE_MODE` | For `ucx_rdma` engines, `v2` or `v1` for RoCE, or `infiniband` for a native InfiniBand port |

`NARWHAL_NODE_<n>_RDMA_GID_INDEX` and `NARWHAL_NODE_<n>_RDMA_ROCE_MODE` set the value for engine `<n>`, for example when engines on one host use different HCAs. The HCA and port come from the engine's `transfer.net_devices`, which `NARWHAL_TRANSFER_NET_DEVICES` or `NARWHAL_NODE_<n>_TRANSFER_NET_DEVICES` sets before discovery. Each `ucx_rdma` engine names exactly one `device:port`.

Read each value from sysfs on the engine's host, for the HCA and port you choose:

```bash
cat /sys/class/infiniband/<hca>/ports/<port>/state       # 4: ACTIVE
cat /sys/class/infiniband/<hca>/ports/<port>/link_layer  # InfiniBand or Ethernet
ls /sys/class/infiniband/<hca>/device/infiniband_verbs   # uverbs<k>
```

Pick an `ACTIVE` port. Its verbs device is `/dev/infiniband/uverbs<k>`, which goes in `NARWHAL_NODE_<n>_TRANSFER_DEVICES` with `/dev/infiniband/rdma_cm`. On an `InfiniBand` link layer, set the RoCE mode to `infiniband` and the GID index to `0`. On `Ethernet`, set the RoCE mode to `v2` and the GID index to the `<i>` whose `ports/<port>/gid_attrs/types/<i>` reads `RoCE v2` and whose `ports/<port>/gid_attrs/ndevs/<i>` names the interface in `NARWHAL_FABRIC_INTERFACE`. [Getting started](../deploy/Getting-Started.md#writing-the-inputs-file) shows the inputs for two engines.

[Building the source budget](../deploy/04-Qualify-Fabric.md#building-the-source-budget) shows how the workload sets the required rate.

### Deployment run inputs

[`narwhal deploy plan` and `run`](../cli/Deploy.md#running-the-stage-graph) read these variables besides the discovery, package and fabric inputs above. Both name every one that is unset or invalid, before they reach a host.

| Variable | Read by | Value |
| --- | --- | --- |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S` | `fleet-limits` | The fleet's `slo.ttft_s` and `slo.tpot_s`, positive seconds |
| `NARWHAL_ROUTER_OVERHEAD_S` | `fleet-limits` | The fleet's [`slo.router_overhead_s`](01-Fleet-Schema.md#minimal-fleet-definition), seconds of at least 0 |
| `NARWHAL_FIRST_TOKEN_MARGIN_S` | `fleet-limits` | Positive seconds that `engine.first_token_timeout_s` adds to the calibrated candidate deadline |
| `NARWHAL_PROFILE_PREFILL_LENS`, `NARWHAL_PROFILE_DECODE_INPUT_LENS` | `profile` | `narwhal-profile --prefill-lens` and `--decode-input-lens`, comma-separated positive integers |
| `NARWHAL_PROFILE_DECODE_CONCURRENCY` | `profile` | Optional `--decode-concurrency`, comma-separated positive integers |
| `NARWHAL_PROFILE_WARMUP_REQUESTS` | `profile` | `--warmup-requests`, an integer of at least 0 |
| `NARWHAL_CALIBRATION_INPUT_TOKENS` | `calibrate` | `narwhal-check --input-tokens`, comma-separated positive integers |
| `NARWHAL_CALIBRATION_SAMPLES` | `calibrate` | `--samples`, an integer of at least 100, the fewest that preflight accepts as calibration evidence |
| `NARWHAL_CALIBRATION_OBSERVATION_TIMEOUT_S` | `calibrate` | `--observation-timeout-s`, positive seconds |
| `NARWHAL_ROUTER_URL` | `router-start` | `http://HOST:PORT` that the primary router binds, as in [Router pair](#router-pair) |
| `NARWHAL_STANDBY_ROUTER_URL`, `NARWHAL_LOAD_BALANCER_URL`, `NARWHAL_LEASE_PATH`, `NARWHAL_HAPROXY_IMAGE` | `lease-check`, `router-start`, `load-balancer`, `monitoring` | The [router pair](#router-pair) |
| `NARWHAL_FLEET_NAME` | `monitoring` | Required. The `fleet` label of every series, 1 to 63 lowercase letters, digits, `.`, `_` or `-`, starting with a letter or digit |
| `NARWHAL_ALERTMANAGER_URL` | `monitoring` | Optional Alertmanager that Prometheus sends alerts to, `http(s)://HOST:PORT` with an optional path. Without one, alerts show only on the Prometheus alerts page |
| `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | `monitoring`, `trial` | Optional listen addresses of the [monitoring stack](../observability/02-Access.md). The trial queries Prometheus at its listen address |
| `NARWHAL_PREFIX_CACHING`, `NARWHAL_NODE_<n>_PREFIX_CACHING` | Discovery, and `plan` and `run` with `--trial` | `false` for every engine when `run` has `--trial`, which [the capacity trial](../deploy/07-Serve-and-Measure.md#running-the-initial-capacity-trial) needs |

Each group is one input of the [stage table](../Deploy.md#rerunning-after-a-change), and a change reruns the stages of its row. The `--trial` file is the `trial` input. Every other `NARWHAL_` variable belongs to discovery, apart from the SSH, inventory and stage limit variables and the `NARWHAL_TRIAL_` variables that the [measurement pages](../Measure.md) set. A secret enters the run record only as part of a digest.

### Router pair

With `NARWHAL_STANDBY_ROUTER_SSH` set, `run` deploys a primary router, a standby router and an HAProxy load balancer, as in [Serve and measure](../deploy/07-Serve-and-Measure.md), and needs every variable below.

Without it, the fleet runs one router at `NARWHAL_ROUTER_URL` with no standby, no lease and no load balancer, so it has no router failover; `plan` and `run` say so. The `lease-check` and `load-balancer` stages do not run, clients use `NARWHAL_ROUTER_URL`, and the other variables below are not read. A fleet on one SSH host sets every `NARWHAL_NODE_<n>_SSH` to that host and leaves `NARWHAL_STANDBY_ROUTER_SSH` unset.

| Variable | Value |
| --- | --- |
| `NARWHAL_STANDBY_ROUTER_SSH` | SSH destination of the standby router's host, another host than the primary router's. Unset for a fleet with one router |
| `NARWHAL_LOAD_BALANCER_SSH` | Optional SSH destination of the load balancer's host. Without it, HAProxy runs on the primary router's host |
| `NARWHAL_ROUTER_URL` | `http://HOST:PORT` the primary router binds. The standby and the load balancer reach it there, so HOST is not a loopback address |
| `NARWHAL_STANDBY_ROUTER_URL` | `http://HOST:PORT` the standby router binds, reachable from the primary and the load balancer |
| `NARWHAL_LOAD_BALANCER_URL` | `http://HOST:PORT` that HAProxy binds on its host, and the origin clients use |
| `NARWHAL_LEASE_PATH` | Absolute path of the pair's lease file, in a directory below the root that both router hosts mount at that path with the [mount options the lease needs](../deploy/07-Serve-and-Measure.md#mount-options-for-the-lease), as the [`lease-check` stage](../deploy/07-Serve-and-Measure.md#checking-the-shared-lease-path) checks. Each path segment holds only letters, digits, `.`, `_` and `-`, and starts with a letter, digit or `_` |
| `NARWHAL_HAPROXY_IMAGE` | HAProxy image pinned by image ID or registry digest, such as `haproxy@sha256:<digest>` |

Each router binds its own port, so a router and HAProxy on one host need different ports.

### Stage time limits

| Variable | Value |
| --- | --- |
| `NARWHAL_STAGE_<STAGE>_TIMEOUT_SECONDS` | Optional time limit of one stage, in seconds, with `-` written as `_`, such as `NARWHAL_STAGE_FABRIC_EDGE_TIMEOUT_SECONDS`. [Host keys and time limits](../cli/Deploy.md#host-keys-and-time-limits) lists each stage's default |
| `NARWHAL_STAGE_TIMEOUT_SECONDS` | Optional time limit of every stage |
| `NARWHAL_STAGE_CLEANUP_GRACE_SECONDS`, `NARWHAL_STAGE_KILL_GRACE_SECONDS` | Optional cleanup budgets of the commands a stage runs on the workstation, as [Stage deadlines and recovery](../Dev-Runtime.md#stage-deadlines-and-recovery) describes |

None of these is a run input, so changing one reruns no stage.

### Variables that are not inputs

Narwhal sets these itself, in the role environments, the units and containers it starts, or the processes it runs. Leave them out of `.env`.

| Variables | Set by and for |
| --- | --- |
| `NARWHAL_HOSTS`, `NARWHAL_LAUNCH_CONFIG`, `NARWHAL_FLEET` | `run` points them at each stage's generated files. The subcommands run alone read them, as in [Deployment inputs and generated artifacts](#deployment-inputs-and-generated-artifacts) |
| `NARWHAL_DEPLOYMENT_REVISION`, `NARWHAL_ENGINE_LAUNCH_CONFIG`, `NARWHAL_ENGINE_API_KEY`, `NARWHAL_CACHE_CAPTURE_HOOK`, `NARWHAL_CACHE_CAPTURE_HOOK_SHA256`, `NARWHAL_TRANSFER_MEASURE_TOOL`, `NARWHAL_TRANSFER_MEASURE_SHA256` | `package`, in each role environment, as [Generated host-side files](#generated-host-side-files) lists |
| `NARWHAL_DEPLOY` | Every SSH connection, set to `1` on the host |
| `NARWHAL_SUPERVISOR_UNIT` | Each systemd unit, naming the unit whose restart count its process exports |
| `NARWHAL_SGLANG_API_KEY`, `NARWHAL_SGLANG_BOOTSTRAP_HOST`, `NARWHAL_SGLANG_BOOTSTRAP_PORT`, `NARWHAL_TRTLLM_API_KEY`, `NARWHAL_TRTLLM_TRANSFER_HOST`, `NARWHAL_TRTLLM_TRANSFER_PORT`, `NARWHAL_CACHE_PLAN`, `NARWHAL_CACHE_OUTPUT`, `NARWHAL_CAPTURE_CACHE` | The launcher, in the engine container, as [Engine endpoints and launch](05-Engine-Launch.md) describes |
| `NARWHAL_POD_IP`, `NARWHAL_POD_NAME`, `NARWHAL_POD_UID`, `NARWHAL_NODE_NAME`, `NARWHAL_ROLE` | The engine pod's specification, from the pod's own fields |
| `NARWHAL_GATE_KEEP` | A Kubernetes gate Job, for the directory that keeps its returned files |
| `NARWHAL_KUBERNETES_FLEET_UID`, `NARWHAL_KUBERNETES_HAPROXY_IMAGE`, `NARWHAL_KUBERNETES_FABRIC_IMAGE`, `NARWHAL_KUBERNETES_ROUTERS`, `NARWHAL_KUBERNETES_HAPROXY` | The in-cluster controller, from a `NarwhalFleet` resource. [Needs verification: task 26] |
| `NARWHAL_PROMETHEUS_URL`, `NARWHAL_GRAFANA_URL`, `NARWHAL_RENDERER_ADDRESS`, `NARWHAL_RENDERER_TOKEN`, `NARWHAL_PROMETHEUS_CONFIG_DIGEST` | The monitoring stack's start, for Docker Compose |

Other commands read their own variables: `NARWHAL_TARGETS_URL` and `NARWHAL_SIDECAR_URLS` the [monitoring stack](../observability/01-Start-and-Verify.md) started by hand, `NARWHAL_TOOL_METRICS_URL` [`narwhal-check`](../cli/Check.md) and [`narwhal-profile`](../cli/Profile.md), `NARWHAL_MODEL_REVISION` [`narwhal-engine`](../cli/Engine.md), `NARWHAL_DOCKER_RECONCILE_SECONDS` the [Docker command reconciliation](../dev/Recovery-and-Qualification.md#docker-command-reconciliation) of `narwhal dev`, the `NARWHAL_CONTROL_` variables the [fleet control service](../operate/fleet-control/09-Configuration-Reference.md), and the `NARWHAL_TRIAL_` variables the [measurement pages](../Measure.md).

### Profiling limits

Profile with the generated limits from the router shell:

```bash
.venv/bin/narwhal-profile --fleet runs/deployment/fleet.json --limits runs/deployment/profiling-limits.json
```

### Role shells

Open a shell for a role on its host. With `--run`, the shell starts in the deployment directory with the role environment loaded and the deployment's virtual environment active, as [Opening installed role shells](../deploy/02-Install.md#opening-installed-role-shells) shows:

```bash
narwhal deploy shell --run <directory> --role <role>
```

A host with both roles holds both role files in one deployment directory.


## Host inventory and SSH trust

`narwhal deploy access`, `package`, `install`, `shell` and `tunnel` read the physical-host inventory from `--hosts PATH`, then from `NARWHAL_HOSTS`, then from `config/hosts.local.json`, as in `narwhal deploy access --hosts <path>`.

Roles with identical `NARWHAL_NODE_<n>_SSH`, `NARWHAL_ROUTER_SSH`, `NARWHAL_STANDBY_ROUTER_SSH` or `NARWHAL_LOAD_BALANCER_SSH` destinations share one physical-host record.

Each `hosts` entry contains:

| Field          | Value                                                                                          |
| -------------- | ---------------------------------------------------------------------------------------------- |
| `id`           | Unique lowercase host ID                                                                       |
| `ssh_env`      | Environment variable that holds the management destination, an alias or `user@management-host` |
| `password_env` | Optional environment variable that holds the SSH password                                      |
| `roles`        | Assigned roles                                                                                 |

Role names:

- `router`, the primary router, assigned once
- `standby-router`, the standby router of a pair, on another host than `router`
- `load-balancer`, the HAProxy host of a pair
- `engine-<n>`, where `<n>` matches the numbered deployment variables

The [example inventory](https://github.com/athrael-soju/Narwhal/blob/main/config/hosts.example.json) assigns `router` and `engine-1` to one machine and `engine-2` to another.

The workstation `.env` holds destination values and credentials, and the inventory holds the variable names.

With `password_env` omitted, SSH connections use OpenSSH key or agent authentication, which never prompts. With `password_env` set, they use password authentication from the populated named variable.

### Inventory validation

Every command that reads the inventory checks it first: unique host IDs, one host per role, a `router` role, at least one engine role, distinct destination entries, and set access variables. Check the inventory and every login:

```bash
narwhal deploy access
```

`access` prints each host ID with its roles once its login succeeds. `shell --role <role>` and `tunnel --role <role>` resolve the role's host from the inventory.

### Known-hosts handling

`NARWHAL_SSH_KNOWN_HOSTS` selects the SSH known-hosts file, such as `config/ssh.known_hosts`, which the publication check keeps out of the repository. Its path may not contain whitespace or `%`. Discovery and every later command require it. They check each host's key against this file alone, with OpenSSH `StrictHostKeyChecking=yes`, before they log in, and never write to it. A host whose key is missing from the file or differs from it stops the command at that host. The discovery manifest records the file's SHA-256.

Options on the command line take precedence over `~/.ssh/config` and `/etc/ssh/ssh_config`, so every connection also sets `KnownHostsCommand=none`, `VerifyHostKeyDNS=no`, `ControlMaster=no` and `ControlPath=none`, which stop it from consulting other keys or reusing a connection whose host key it did not check. It sets `ProxyJump=none` and `ProxyCommand=none`, so a jump host in your SSH config is ignored, and `PermitLocalCommand=no`, `RemoteCommand=none`, `ForwardAgent=no` and `ForwardX11=no`. It sets `SetEnv=NARWHAL_DEPLOY=1`, which displaces any `SetEnv` in your SSH config, because OpenSSH keeps the first value it reads. OpenSSH adds a config file's `SendEnv` to the command line's instead, so `ssh` runs with only `PATH`, `HOME`, `TERM`, `SSH_AUTH_SOCK` and `XDG_RUNTIME_DIR` from your environment, and `SendEnv` has nothing else to forward. `XDG_RUNTIME_DIR` stays so that an `IdentityAgent` written as `${XDG_RUNTIME_DIR}/…` still finds its socket. Your SSH config still supplies each alias's `HostName`, `User`, `Port` and `IdentityFile`.

Write the file before discovery, with one entry for each SSH destination. Take each key from a source you trust, such as the provider console, and compare fingerprints with `ssh-keygen -lf <file>`. For a destination alias, write the entry for its `HostName`, in `[host]:port` form when the port is not 22.

The workstation needs OpenSSH 8.5 or later. For password authentication, OpenSSH asks a private askpass helper, which reads the password from a FIFO that `narwhal deploy` holds open. The password stays in the pipe until OpenSSH reads it, and is never written to a file.

For key or agent authentication:

1. Put the address, username, identity, and optional `Port` configuration under an alias in the workstation's private SSH config.
2. Point `ssh_env` at that alias.

### Jump host

Set `NARWHAL_SSH_JUMP` to reach every host through one jump host. Each connection then runs `ssh -W` to the jump host with the same options, so the jump host's key must also be in `NARWHAL_SSH_KNOWN_HOSTS`. The jump connection authenticates with a key or the SSH agent, never a password.

To replace the key of a changed server:

1. Verify the replacement through the private access source.
2. Replace its entry in `NARWHAL_SSH_KNOWN_HOSTS`.

### Packages and deployment directories

`narwhal deploy package --out <directory>` writes the wheel, role files grouped by host ID and a private manifest, as [Building the deployment package](../deploy/02-Install.md#building-the-deployment-package) lists. Use a new directory for each deployment.

`narwhal deploy install --run <directory>` installs the package once on each selected host.

| `install` option | Selected hosts                                      |
| ---------------- | --------------------------------------------------- |
| `--role <role>`  | The host that runs `<role>` and its colocated roles |
| `--host <id>`    | One physical host by inventory ID                   |
| Default          | Every inventory host                                |

Each deployment directory at `~/Narwhal-deploy/<run-id>/` holds:

- `package/`, the transferred wheel and role files
- `.venv`, with the wheel installed
- the role files at their working paths under `runs/deployment/`, `runs/deployment-tools/` and `config/`
- the installation lock `.install-lock`, while `install` runs, with an `owner` file that names the installer's process, host and start time
- the `installed` completion marker, which holds the wheel's SHA-256

`<run-id>` is a random 32-hex ID that `package` records in the manifest.

A repeated `install` on a host reuses files that match their manifest SHA-256. With an `installed` marker for the packaged wheel, it reuses the virtual environment. A changed file, or a marker for another wheel, stops the install at that host.

The package's private `logs/` directory holds one log per host with executed scripts, exit status, and host output.
