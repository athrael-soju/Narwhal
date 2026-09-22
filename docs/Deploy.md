# Deploy a fleet

From your management workstation, derive the deployment configuration from your private `.env` and remote host inspection, then open shells on the router and GPU engine hosts. Follow the numbered steps through a real model completion, measure the workload through the private SSH route, then inspect the running fleet through Prometheus and Grafana.

## Hosts and inputs

| Host | Work performed here | Inputs |
| --- | --- | --- |
| Management workstation and initial load client | Create private configuration, open remote shells, send trial traffic through an SSH tunnel, retain the deployment record. | A private `.env` containing management destinations and credentials, the approved source revision, model/image selection, paths, fabric interface and service endpoints. Step 1 derives the other configuration files. |
| Router and observability host | Install Narwhal, create the fleet config, profile and check engines, run the router, Prometheus and Grafana. | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO targets; Docker Engine with the Compose plugin for monitoring. |
| Engine hosts | Inspect GPUs and artifacts, configure the fabric, launch vLLM and attestation sidecars. | Accelerator and TP shape, engine image, model checkpoint, launch configuration, fabric addresses and ports. |

The management workstation needs Git, Bash, Python 3.11 or newer and the supplied access tooling. Install Python 3.11 or newer with `venv`, Git, Make and curl on the remote hosts that run Narwhal commands. Engine hosts also need the declared accelerator driver, container runtime and transfer devices. The inventory assigns these roles to machines; a management workstation's local hardware describes that machine alone.

Narwhal serves one model and compatible KV layout across engines running vLLM with NIXL and effective `kv_both` behaviour. Every eligible producer must transfer KV to every eligible consumer, and one active controller owns the fleet.

Create a private deployment record before the first command. At each gate, record the host, full source revision, starting state, commands actually run, exit status and artifact locations. Report the first blocked gate before recovery; after reporting, clean only the processes and files created by that test. The [gate reference](#deployment-gates-and-recovery) maps each step to its inputs and recovery.

## 1. Open the management shells

### Load the supplied environment

Start in a fresh management checkout with the supplied private `.env`. [.env.example](https://github.com/athrael-soju/Narwhal/blob/main/.env.example) names its fields. The environment supplies the management destinations and credentials, source revision, engine image and model paths, fabric interface, run directory and service endpoints. Keep credentials on the workstation and load the environment with shell tracing disabled:

```bash
set +x
set -a
. ./.env
set +a
```

Set `NARWHAL_NODE_<n>_SSH` for each engine and `NARWHAL_ROUTER_SSH` for the router. Equal destination values assign those roles to one physical host and reuse one credential. A destination may be an OpenSSH alias with its username, port, key and jump route configured in SSH, or `user@host`. Password access uses the corresponding `_SSH_PASSWORD` variable; key access uses the configured identity or SSH agent.

### Derive configuration from the hosts

Run discovery from the management checkout. It needs the workstation's Python standard library, OpenSSH and `sshpass` for password access. The remote hosts need Python 3, Docker, `ip`, and the GPU driver inspection tool (`rocminfo` or `nvidia-smi`); the pinned image and model checkpoint must be present at the paths in `.env`.

```bash
python3 tools/discover_deployment.py --out runs/discovery/first-deploy
. runs/discovery/first-deploy/derived.env
python3 tools/deploy_hosts.py plan
python3 tools/deploy_hosts.py check-access
```

Discovery records each host's SSH key on the first connection through the private management route, authenticates with the supplied credential, and rejects changes to an already recorded key. This is trust on first use; an existing verified host-key file selected by `NARWHAL_SSH_KNOWN_HOSTS` also works. Subsequent deployment commands require a matching recorded key. A changed key requires checking that host through its provider console before replacing its entry.

For each engine, discovery reads the GPU product and device mappings, model configuration/hash, selected network interface, and immutable image identity. A temporary container reads package metadata from the image and exits. It derives the model dtype and image runtime environment, then writes these mode-0600 files:

| Generated file | Source and use |
| --- | --- |
| `config/hosts.local.json` | Groups the `.env` management destinations and assigns the router and numbered engine roles. |
| `config/ssh.known_hosts` | Records server public keys during authenticated management access. |
| `config/engine-launch.local.json` | Combines inspected GPU allocation, model/image metadata and network devices with the launch policy below. |
| `config/engine-launch.sources.json` | Points each engine role to its retained inspection and policy source. |
| `config/fleet.json` | Creates the model, measured hardware/TP shape, engine URL references, opening roles, initial latency targets and profile output path. |
| `runs/discovery/first-deploy/derived.env` | Selects generated configuration paths and per-engine image/hash values for subsequent preparation. |

The corresponding path variables in `.env` select different JSON or host-key destinations. Discovery retains per-engine observations, SSH logs and output hashes under its `--out` directory. Existing generated JSON files stop discovery before remote inspection; archive them with their run and select a fresh output directory when recreating configuration.

### Launch policy and environment overrides

For one engine role on a GPU host, discovery allocates every detected GPU and sets TP to that count. Colocated engine roles require disjoint `NARWHAL_NODE_<n>_GPU_IDS` lists in `.env`. The generated fleet uses a matching accelerator and TP shape across replicas, starts the first engine in the prefill pool and the others in decode, and writes fresh profiles to `runs/profiles.json` inside the new remote checkout.

The initial launch uses TCP on `NARWHAL_FABRIC_INTERFACE`, the model's dtype (bfloat16 when the config omits it), automatic KV dtype, 128-token requested blocks, an eager runtime, up to 16,384 context tokens (bounded by the model config), eight sequences and 0.9 GPU memory utilisation. The cache probe in step 4 captures the runtime's resolved layout. Image environment defaults carry into the launch record. These initial settings can be changed through `.env` before discovery:

| Environment field | Override |
| --- | --- |
| `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE` | Comma-separated GPU indices or NVIDIA UUIDs, and the replica TP size. |
| `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE` | Model dtype and requested cache block size. |
| `NARWHAL_ENGINE_ARGS` | JSON array replacing the initial serving arguments; [Runtime launch records](Configuration.md#runtime-launch-records) lists their meaning. |
| `NARWHAL_ENGINE_ENV` | JSON object overriding image runtime environment fields supported by the launcher. |
| `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES` | `ucx_rdma`, HCA:port selection, and a JSON list of RDMA device paths when selecting RDMA. |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S` | Initial candidate latency limits in seconds; defaults are 10 and 0.125 until step 7 calibrates them. |

Engine policy fields accept `NARWHAL_NODE_<n>_<field>` overrides in the same form as the existing per-host environment fields. Put any required model-specific serving flags in `NARWHAL_ENGINE_ARGS`; the pinned image check, cache sizing and completion gates validate the resulting command. Runtime discovery outputs remain reproducible from `.env` and the selected hosts/image. Example JSON files document their schemas.

### Verify access and open role shells

`plan` lists host IDs and assigned roles. `check-access` verifies the pinned SSH key and login once per host, recording `hostname` and the executed command in private logs under `runs/access-<id>/`. A successful verified login completes the access gate for every role on that host. Hostnames serve as observed labels and can repeat across machines. Management destinations open shells; engine HTTP, attestation and fabric addresses retain their service roles.

Open a shell by role when inspecting a host:

```bash
python3 tools/deploy_hosts.py shell --role engine-1
```

Use `--role router` for the router shell and the corresponding numbered engine role for other hosts. Both roles resolve to the same authentication entry when they share a machine. For a missing access variable, inspect the named field in the supplied `.env`. For an unknown or changed host key, verify the destination and fingerprint through the supplied private access source before updating the checkout-local host-key entry. For authentication or connection failures, check the username, credential, management route, SSH port and firewall. Record the first blocked host and gate before recovery; share sanitised extracts from the private command log.

## 2. Install Narwhal on the remote hosts

### Prepare the source and role environments on the workstation

With `.env` and the discovery run's `derived.env` loaded in the management shell, prepare a new deployment directory. The supplied `NARWHAL_DEPLOYMENT_REVISION` selects the full approved commit in the management checkout. The helper packages that commit into a Git bundle and verifies the exact SHA with a fresh local clone before writing the completed manifest.

```bash
python3 tools/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
```

The helper exports `.env.router` and one `.env.engine-<n>` for every assigned role, using the fleet document generated in step 1 and shared engine values with per-node overrides. It copies the fleet document for the router and selects one `engine-launch.engine-<n>.json` per engine from `NARWHAL_LAUNCH_CONFIG`, validating its GPU allocation, TP size and transport device declarations. Preparation also snapshots the management checkout's `tools/fabric_budget.py` and `tools/launch_engine.py` and delivers both to each engine host under `runs/deployment-tools/`. The role environment exports that path as `NARWHAL_FABRIC_BUDGET_TOOL` and its digest as `NARWHAL_FABRIC_BUDGET_SHA256`. The engine launcher uses the corresponding `NARWHAL_ENGINE_LAUNCHER` path and `NARWHAL_ENGINE_LAUNCHER_SHA256` digest. The manifest records hashes for the source bundle, helper snapshots and all role files. Management credentials stay in the workstation environment. [Host environment files](Configuration.md#host-environment-files) lists the exported fields.

Choose a fresh `--out` path for a new deployment; subsequent commands reuse that path through `--run`. The mode-0600 manifest records the approved revision, host assignments, input hashes and a unique remote directory under `~/Narwhal-deploy/`. Record its path in the private deployment record. A missing field or unavailable revision stops preparation on the workstation; correct the named input or recover the approved source, then prepare a fresh directory.

### Install by host

Install the host assigned to the first engine role:

```bash
python3 tools/deploy_hosts.py install \
  --run runs/deployment-env/first-deploy --role engine-1
```

The helper transfers one source bundle and that host's role files over verified SSH, clones the bundle, checks the approved revision, and installs Narwhal in the checkout's `.venv`. A host serving both router and engine roles receives both environment files and one installation. The command log records each remote script, its exit status and output under the prepared directory's `logs/` subdirectory.

After this host passes, install the remaining hosts through the same prepared run:

```bash
python3 tools/deploy_hosts.py install --run runs/deployment-env/first-deploy
```

The helper iterates host IDs and reuses matching transferred files and completed installations. Repeating a role or running the full host list after the first host succeeds preserves that host's completed work. An existing file with different content or a changed source checkout stops the command at that host before further hosts run. Keep each prepared run's files intact; their hashes and host assignments are checked before SSH operations.

### Open the installed role shells

Open the router and engine shells from management terminals with the workstation `.env` loaded:

```bash
python3 tools/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role router
```

```bash
python3 tools/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role engine-1
```

Each shell starts in that host's prepared checkout, loads its role environment and activates `.venv`. Use the corresponding engine role for other hosts. Run the following preparation and launch steps in these remote shells.

For a transfer mismatch, compare the private command log and prepared manifest with the existing remote artifact before recovery. For a revision mismatch, confirm that the bundle and role files belong to the same run. An interrupted dependency installation can resume with the same `install --run` command; the completion marker is written after the installed CLI responds. A host's `.install-lock` directory prevents concurrent installation; remove a lock left by an interrupted session only after confirming its installer has ended. Preserve existing deployments and report the first blocked gate before cleaning artifacts owned by this test.

## 3. Inspect each engine host

In each installed engine-role shell, `NARWHAL_ENGINE_LAUNCH_CONFIG` selects the transferred `config/engine-launch.engine-<n>.json`. Read its allocation and device fields before inspecting hardware:

```bash
python3 - <<'PY_LAUNCH'
import json
import os
from pathlib import Path
record = json.loads(Path(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]).read_text())
for field in ("role", "accelerator", "gpu_ids", "tensor_parallel_size",
              "accelerator_devices", "network_mode", "transfer", "environment", "vllm_args"):
    print(f"{field}: {json.dumps(record[field])}")
PY_LAUNCH
```

The generated private record declares the replica allocation, container device mappings and UCX selection; its `sources` entries identify the allocation and device definitions used to prepare it. [Engine launch records](Configuration.md#engine-launch-records) defines these fields. Retain this output in the private deployment record.

Run these read-only commands in that engine-role shell. The PCI vendor and device class select the NVIDIA or AMD inspection tool on that remote host:

```bash
(
  set -e
  vendors=
  for device in /sys/bus/pci/devices/*; do
    test -r "$device/class" && test -r "$device/vendor" || continue
    case "$(cat "$device/class")" in
      0x03*|0x12*) ;;
      *) continue ;;
    esac
    case "$(cat "$device/vendor")" in
      0x10de) vendors="$vendors nvidia" ;;
      0x1002) vendors="$vendors amd" ;;
    esac
  done
  if test -z "$vendors"; then
    printf '%s\n' 'Inspect GPU PCI device exposure on this engine host.' >&2
    exit 1
  fi
  case "$vendors" in
    *nvidia*) nvidia-smi --query-gpu=pci.bus_id,name,uuid --format=csv ;;
  esac
  case "$vendors" in
    *amd*) rocminfo ;;
  esac
)
```

Record the reported product name and visible physical GPU count per host. For ROCm, count agents whose `Device Type` is `GPU` and use their `Marketing Name`; CPU agents describe the host processor. A missing inspection command, driver error or empty GPU enumeration requires repair of that host's driver tools, permissions or device exposure before launch.

Compare these observations with the generated `hardware.accelerator` in the router's working `config/fleet.local.json`. Confirm that every participating engine has the declared accelerator model. Set `hardware.accelerators_per_engine` to the length of the record's `gpu_ids` and `hardware.tensor_parallel` to its `tensor_parallel_size`; verify that the selected indices or UUIDs identify available GPUs on this host. A host's total GPU count describes available hardware; the selected replica allocation determines its TP shape. Record the observations with the deployment and retain the fleet config with this run.

Take the engine image, model and run paths, model-config hash, fabric interface and ports from this host's supplied deployment values; [.env.example](https://github.com/athrael-soju/Narwhal/blob/main/.env.example) names these inputs. The router's ignored fleet document holds one entry per engine; the private inventory holds each engine's fabric address and peers. Replace angle-bracket placeholders in this guide from that inventory before executing commands.

On each host, check the declared artifacts and local resources before creating a new engine process. For a Docker image identified by its image ID, the following checks fail on a missing or different image and model config:

```bash
test "$(docker image inspect "$NARWHAL_ENGINE_IMAGE" --format '{{.Id}}')" = "$NARWHAL_ENGINE_IMAGE"
test "$(sha256sum "$NARWHAL_MODEL_DIR/config.json" | cut -d' ' -f1)" = "$NARWHAL_MODEL_CONFIG_SHA256"
test -d "$NARWHAL_RUN_DIR"
ip address show dev "$NARWHAL_FABRIC_INTERFACE"
ss -ltnp
```

The Docker equality check applies when `NARWHAL_ENGINE_IMAGE` is an image ID; a registry digest requires the container runtime's resolved-digest inspection. The `config.json` hash identifies the model configuration; retain the checkpoint revision or weights manifest separately. Check the declared device paths on this host:

```bash
python3 - <<'PY_DEVICES'
import json
import os
from pathlib import Path
record = json.loads(Path(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]).read_text())
paths = record["accelerator_devices"] + record["transfer"]["devices"]
missing = [path for path in paths if not Path(path).exists()]
for path in paths:
    print(f"{path}: {'missing' if path in missing else 'present'}")
if missing:
    raise SystemExit("Restore the declared device exposure before launch.")
PY_DEVICES
```

For ROCm, `/dev/kfd` and the selected DRI devices provide container GPU access; mapping the whole `/dev/dri` directory exposes the host's DRI devices. Check ownership and container-user permissions for the declared mappings using the [ROCm container instructions](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html). Inspect `ss` for owners of the planned engine HTTP, attestation, NIXL side-channel, and transport ports. Treat a listener owned by another deployment as a stop condition and resolve ownership before launch.

## 4. Prepare the transfer fabric

Run this step in the installed engine-role shells after every host passes step 3. First collect the pinned runtime's cache page sizes with a single-host sizing process, then qualify directed host links for the declared handoff workload. The sizing process loads and profiles the model on that host's assigned GPUs, captures the cache allocation specs and exits before serving or peer transfer. Step 8 exercises NIXL handoffs between running engines, and step 10 measures shared-link contention under concurrent deployment traffic.

### Capture the runtime cache layout

Step 2 supplies the launcher and budget calculator as hashed snapshots beside the approved application bundle. In each engine-role shell, verify both helpers and prepare a private sizing plan from that host's launch record:

```bash
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
export CACHE_RUN="$FABRIC_RUN/cache-probe"
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256" &&
test "$(sha256sum "$NARWHAL_FABRIC_BUDGET_TOOL" | cut -d' ' -f1)" = "$NARWHAL_FABRIC_BUDGET_SHA256" &&
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$CACHE_RUN"
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$CACHE_RUN"
python3 "$NARWHAL_ENGINE_LAUNCHER" measure-cache --run "$CACHE_RUN"
```

`measure-cache` uses the checked image, model, runtime arguments, environment, device allocation and TP size. It invokes vLLM's engine core through cache planning, collects the final per-layer specs for every TP rank, then shuts down the model workers and removes its sizing container. The command waits through model loading and memory profiling; a second shell can follow `docker logs -f "$(cat "$CACHE_RUN/cache-probe.id")"`. The completed `cache-layout.json` records actual token block sizes, padded page bytes, state/boundary allowances and the hashes of the model config, launch record and plan. `cache-probe.log` retains the runtime output, and `cache-probe.id` identifies the container owned by this attempt.

The probe uses the pinned vLLM V1 cache-planning API. A package/import failure belongs to the image check; a model-load, device, memory-profile or unsupported-cache-spec failure belongs to the sizing probe. Retain its log, inspect the recorded container, correct the named input and prepare a fresh sizing plan. Capture `docker logs` before stopping and removing a failed sizing container by its recorded ID. A missing helper, path variable or digest mismatch requires a fresh step 2 preparation from the updated management checkout. Preserve earlier manifests, sizing logs and fabric samples.

### Calculate the link budget

For initial bring-up, use one remote handoff per second, 1,024 prompt tokens per handoff, a burst of one handoff, a one-second transfer budget and 25% bandwidth headroom. Apply the total handoff rate to each candidate edge so the budget covers traffic concentrated on that edge. Calculate with the runtime layout collected above:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate \
  --model-config "$NARWHAL_MODEL_DIR/config.json" \
  --launch-config "$NARWHAL_ENGINE_LAUNCH_CONFIG" \
  --runtime-layout "$CACHE_RUN/cache-layout.json" \
  --prompt-tokens 1024 --handoffs-per-s 1 --burst 1 \
  --transfer-budget-s 1 --headroom 1.25 \
  --out "$FABRIC_RUN/budget.json"
```

For each layer on each TP rank, the helper counts `ceil(prompt_tokens / block_tokens) + extra_blocks` padded pages, then sums their bytes across the replica. Full attention and MLA use the full prompt's pages; Mamba includes a boundary state and speculative/checkpoint slots; windowed attention includes a boundary page. This bounds a handoff by the complete padded cache for that prompt, including state pages that a connector may transfer more selectively. vLLM's [cache specs](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/kv_cache_interface.py) and [cache grouping](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/core/kv_cache_utils.py) supply the page geometry. The calculation uses those runtime sizes, including adjustments to the requested block size and Mamba padding.

The rate is `8 * payload_bytes * max(handoffs_per_second, burst_handoffs / transfer_budget_seconds) * headroom / 1e9`. The command prints decimal Gbit/s and writes a mode-0600 budget with the workload, payload bound, source hashes and image identity. Record `FABRIC_RUN` and `CACHE_RUN` in the private deployment record. Each source engine's budget applies to its outgoing edges; retain the declared workload for the later transfer and capacity gates.

Retained throughput samples can be compared with a corrected budget while host assignments, routes, interfaces, transport settings and measurement conditions still match their recorded inputs. Repeat each step 4 comparison using the new budget and the original sample file, and retain the new comparison output alongside the earlier result. A runtime-layout correction changes the budget; a changed link or transport configuration requires a new throughput sample. Complete the comparisons for every directed edge before starting serving engines in step 5.

### Select and check a directed edge

Start with engine 1 as source and engine 2 as destination. Open both installed role shells through step 2's management command. In both shells, select the numbered roles and a free temporary TCP test port:

```bash
export SOURCE_NODE=1 DEST_NODE=2 TEST_PORT=5201
source_var="NARWHAL_NODE_${SOURCE_NODE}_IP"
dest_var="NARWHAL_NODE_${DEST_NODE}_IP"
export SOURCE_IP="${!source_var:?missing source fabric address}"
export DEST_IP="${!dest_var:?missing destination fabric address}"
ss -ltnp "sport = :$TEST_PORT"
```

These addresses come from the exported fabric inventory. `TEST_PORT` belongs to this temporary test; check both hosts for an existing listener and allow the port between the two fabric addresses through the host firewall. Select another free port when an existing service owns it. Preserve existing service listeners and firewall rules.

On the source, inspect `ip route get "$DEST_IP" from "$SOURCE_IP"`; on the destination, inspect `ip route get "$SOURCE_IP" from "$DEST_IP"`. Use `ip -6 route get` for IPv6 addresses and `ip -4 route get` for IPv4. Each result must select that host's `NARWHAL_FABRIC_INTERFACE` and its supplied source address. Record both route outputs before testing throughput. Correct an address, route or interface mismatch before continuing.

Read `transfer.transport` from `NARWHAL_ENGINE_LAUNCH_CONFIG` and use the corresponding test below. Keep MTU, firewall and the selected transport's device/port settings consistent across peers.

### Measure a TCP edge

For `ucx_tcp`, install `iperf3` on both engine hosts through their package manager; on Debian or Ubuntu, use `sudo apt-get install iperf3`. Record `iperf3 --version` on both hosts. The [iperf3 command reference](https://software.es.net/iperf/invoking.html) defines address binding, parallel streams and receiver reports.

In the destination shell, start one temporary foreground server bound to its fabric address:

```bash
iperf3 --server --bind "$DEST_IP" --port "$TEST_PORT"
```

In the source shell, measure aggregate throughput with one TCP stream per TP rank. The client sends from `SOURCE_IP` to `DEST_IP`, omits three warm-up seconds and measures ten seconds:

```bash
export FABRIC_STREAMS="$(python3 -c 'import json,os; print(json.load(open(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]))["tensor_parallel_size"])')"
export EDGE_SAMPLE="$FABRIC_RUN/engine-${SOURCE_NODE}-to-engine-${DEST_NODE}.json"
(set -o noclobber; iperf3 --client "$DEST_IP" --bind "$SOURCE_IP" \
  --port "$TEST_PORT" --parallel "$FABRIC_STREAMS" --omit 3 --time 10 \
  --json > "$EDGE_SAMPLE") &&
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" compare \
  --budget "$FABRIC_RUN/budget.json" --iperf "$EDGE_SAMPLE"
```

`compare` reads `end.sum_received.bits_per_second`, prints measured and required Gbit/s and exits 0 when the receiver rate meets the budget. A lower rate exits 1; an invalid sample exits 2. Stop the destination server with Ctrl-C after recording the result. For the reverse edge, swap the source and destination roles and repeat with a new sample path and the new source's budget.

### Measure an RDMA edge

For `ucx_rdma`, install `perftest` on both engine hosts, using `sudo apt-get install perftest` on Debian or Ubuntu. Record `ib_write_bw --version` and use the same version and test parameters at both ends. Select each host's HCA and port from its launch record's `transfer.net_devices`; for a selection such as `mlx5_0:1`, set `HCA=mlx5_0` and `HCA_PORT=1` on that host. For RoCE, inspect `/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/ndevs/`, `gid_attrs/types/` and `gids/` to select the index matching its fabric interface, address and RoCE mode, then set `GID_INDEX` to that index. Record the mapping with the route evidence. Native InfiniBand uses the site's active port/GID selection. [Perftest](https://github.com/linux-rdma/perftest) documents the device, GID, duration and bandwidth options.

In both shells, select the address family for the supplied fabric addresses:

```bash
rdma_addr_args=()
case "$DEST_IP" in
  *:*) rdma_addr_args=(--ipv6-addr --ipv6) ;;
esac
```

On the destination, run:

```bash
ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
  -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
  "${rdma_addr_args[@]}" --bind_source_ip "$DEST_IP"
```

On the source, run the same test against the destination's fabric address and retain its report:

```bash
export EDGE_SAMPLE="$FABRIC_RUN/engine-${SOURCE_NODE}-to-engine-${DEST_NODE}.txt"
(set -o noclobber; ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
  -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
  "${rdma_addr_args[@]}" --bind_source_ip "$SOURCE_IP" "$DEST_IP" > "$EDGE_SAMPLE")
cat "$EDGE_SAMPLE"
```

Copy the report's `BW average[Gb/sec]` value into `MEASURED_GBPS` and compare it with the source budget:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" compare \
  --budget "$FABRIC_RUN/budget.json" --gbps "$MEASURED_GBPS"
```

This measures one-way RDMA writes between host-memory buffers. Swap source and destination roles and rerun for the reverse edge. Multi-rail deployments repeat the test for each selected HCA port and retain each report; runtime KV probes verify how the connector uses those rails.

### Complete the edge matrix

After the first pair passes in both directions, repeat for every ordered pair of participating engine hosts. A fleet of `n` distinct engine hosts produces `n * (n - 1)` directed samples. Measure one edge at a time and record source role, destination role, source revision, routes, transport, utility version, command, budget, sample and exit status. Reuse a host's calculated budget across its outgoing edges. Colocated replicas share a host and exercise their local handoff in step 8.

A connection failure requires the named listener, route, firewall, HCA or GID check. A bandwidth failure requires inspection of the affected link's speed, MTU, retransmissions or RDMA counters, CPU saturation and concurrent traffic; retain the failed sample before repairing the cause and measuring into a fresh file. Keep the declared workload target with each comparison. Advance after every required edge meets that target. Retain the matrix, budgets and samples privately, stop the temporary servers owned by this test, and carry the declared cache shape and workload into engine launch and capacity acceptance.

## 5. Configure the fleet and launch engines

Complete the host inspection and directed fabric matrix before starting the first engine. On the router host, edit the transferred `config/fleet.local.json` to set model, engine IDs, opening roles, engine and attestation URLs, SLOs and a fresh profile path. [Node URL references](Configuration.md#node-urls-from-the-environment) resolve endpoints from `.env.router`. Step 6 fills the runtime `engine_contract` from the image check, running engine and attestation sources before profiling or router startup.

### Prepare the first engine command

The generated per-engine record's `runtime` object names pinned package versions, library environment, model dtype, cache dtype, block size and model-specific arguments. Step 2 delivers `launch_engine.py` beside the fabric calculator and exports `NARWHAL_ENGINE_LAUNCHER` and `NARWHAL_ENGINE_LAUNCHER_SHA256` in each engine-role environment. The launcher builds a Docker command invoking `python3 -m vllm.entrypoints.openai.api_server` inside `NARWHAL_ENGINE_IMAGE`, mounts `NARWHAL_MODEL_DIR` read-only at `/model`, exposes the declared devices, and applies the selected TP allocation.

In the installed engine-1 shell, verify the launcher and prepare a fresh private launch directory:

```bash
umask 077
mkdir -p runs
export ENGINE_RUN="runs/engine-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256" &&
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$ENGINE_RUN"
python3 -m json.tool "$ENGINE_RUN/launch.json"
```

Review `launch.json`: it records the immutable image, complete serving arguments, mounts, device mappings, endpoint, application revision and input hashes. `container.env` contains the explicit runtime and transport values and, when configured, the engine API key; keep this file private. The launcher supplies `NixlConnector`, `kv_role=kv_both`, the UCX backend and `kv_load_failure_policy=fail`. It derives the advertised side-channel address and port from the selected engine's role environment, selects TCP or RDMA through `UCX_TLS`, and disables prefix caching for the profiling procedure. [Runtime launch records](Configuration.md#runtime-launch-records) defines the fields and the [vLLM NIXL guide](https://docs.vllm.ai/en/v0.29.0/features/nixl_connector_usage/) describes the connector settings.

Match the launch record and model-config hashes with the retained step 4 runtime layout and budget. The requested block size can be adjusted by vLLM during cache planning; the runtime layout records the resulting pages. Repeat cache sizing and budget calculation after changing the image, model, dtype, cache policy, TP allocation or model arguments; recalculate the budget after changing the workload. A missing runtime record or launcher requires a fresh step 2 preparation from the updated management inputs; retain earlier prepared runs and open the new run's role shell.

### Check the image and start the engine

Run the image check in that engine shell:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$ENGINE_RUN"
```

The check inspects the local immutable image identity, starts a temporary container to compare package versions and resolves the configured connector through the image's `KVConnectorFactory`, then records the plan hash in `checked.json`. The factory imports the class registered by that vLLM build. The check also reads `vllm.version.__version__`, which supplies the engine's `/version` response, and records it as `vllm_api_version` in `checked.json`. `image-check.log` retains the connector, exact package versions, API version and command output. The temporary container exits after these imports. Resolve package and library failures against the supplied image and runtime record. A launcher correction requires a fresh step 2 preparation to deliver the updated helper and its digest, followed by a fresh launch plan; retain the earlier image-check log and fabric samples with their original manifest.

Inspect the planned listeners with `ss -ltnp`, then start the checked plan:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" start --run "$ENGINE_RUN"
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
docker logs --follow "$ENGINE_CONTAINER"
```

`start` creates a uniquely named container, records its ID before starting it and retains Docker output in `launch.log`. It verifies the saved environment and checked plan before creating the container. Follow the engine log through model loading and HTTP startup; Ctrl-C ends the log follower while the engine container continues running. An existing `container.id` directs recovery to that recorded container. Inspect `docker inspect "$ENGINE_CONTAINER"` and `docker logs "$ENGINE_CONTAINER"` for a startup exit, then correct the implicated device, model, memory, library or transport input and prepare a fresh launch plan.

### Verify the engine HTTP API

After HTTP startup, run these probes in the same engine-role shell. They use the supplied endpoint and engine credential, compare `/version` with the value captured from the checked image and save responses under the launch directory. Distribution metadata can carry a build suffix while the API exposes its own version string; the image check verifies the full distribution pin and the HTTP probe verifies the exact captured API version:

```bash
python3 - <<'PY_ENGINE'
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
run = Path(os.environ["ENGINE_RUN"])
plan_data = (run / "launch.json").read_bytes()
plan = json.loads(plan_data)
checked = json.loads((run / "checked.json").read_text())
assert checked["plan_sha256"] == hashlib.sha256(plan_data).hexdigest(), "Repeat the image check for this plan"
expected_version = checked["vllm_api_version"]
headers = {"Content-Type": "application/json"}
if os.environ.get("NARWHAL_ENGINE_API_KEY"):
    headers["Authorization"] = "Bearer " + os.environ["NARWHAL_ENGINE_API_KEY"]
def probe(path, filename, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(plan["endpoint"].rstrip("/") + path, data=data, headers=headers)
    with urlopen(request, timeout=60) as response:
        content = response.read()
    with (run / filename).open("xb") as output:
        output.write(content)
    return content
probe("/health", "health.txt")
version = json.loads(probe("/version", "version.json"))
assert version["version"] == expected_version, (
    f"/version returned {version['version']!r}; checked image expects {expected_version!r}"
)
models = json.loads(probe("/v1/models", "models.json"))
assert os.environ["NARWHAL_ENGINE_MODEL_NAME"] in {m["id"] for m in models["data"]}
metrics = probe("/metrics", "metrics.txt").decode()
assert any(line.startswith("process_start_time_seconds ") for line in metrics.splitlines())
completion = json.loads(probe("/v1/completions", "completion.json", {
    "model": os.environ["NARWHAL_ENGINE_MODEL_NAME"], "prompt": "The sea is",
    "max_tokens": 32, "temperature": 0,
}))
assert completion["choices"][0]["text"]
print("Engine health, version, model, process identity and completion passed.")
PY_ENGINE
```

For a failed probe, retain its command, HTTP status and engine log with the first blocked gate. A checked record predating `vllm_api_version` requires the updated launcher and a fresh checked launch plan. An API-version mismatch requires checking the endpoint owner against the recorded image and container ID. Preserve completed response files; use fresh filenames when repeating a probe after repair. After the first engine passes, apply the same prepare, check, start and probe sequence in each remaining engine-role shell with its own `ENGINE_RUN`. Record each container ID and launch directory for step 6. When cleaning a test deployment, capture its logs before using `docker stop` and `docker rm` on the container IDs created by that test.

## 6. Attest each engine process

On each engine host, create the attestation document under ignored `runs/`, preserving any document from an earlier deployment.

```bash
mkdir -p runs
(set -o noclobber; cat config/engine-attestation.example.json > runs/engine-attestation.production.json)
```

### Read the NIXL connector protocol version

`nixl_connector_version` is the integer `NIXL_CONNECTOR_VERSION` defined by the installed vLLM connector. vLLM includes this constant in its peer compatibility hash. The pinned NIXL package version belongs in `nixl_version`; the connector protocol integer comes from the image's [NIXL metadata module](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/metadata.py).

In the engine-role shell, use the step 5 launch directory and recorded container ID to capture the installed constant and module hash. This command starts a Python inspection process inside the running container and preserves the serving process:

```bash
umask 077
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
(set -o noclobber
  docker exec -i "$ENGINE_CONTAINER" python3 - > "$ENGINE_RUN/nixl-connector-version.json" <<'PY_NIXL_VERSION'
import contextlib
import hashlib
import importlib
import json
import sys
from pathlib import Path
with contextlib.redirect_stdout(sys.stderr):
    module = importlib.import_module("vllm.distributed.kv_transfer.kv_connector.v1.nixl.metadata")
version = module.NIXL_CONNECTOR_VERSION
if type(version) is not int or version < 1:
    raise SystemExit("Installed NIXL_CONNECTOR_VERSION must be a positive integer")
source = Path(module.__file__)
print(json.dumps({
    "nixl_connector_version": version,
    "module": module.__name__,
    "constant": "NIXL_CONNECTOR_VERSION",
    "module_file": str(source),
    "module_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
}, indent=2))
PY_NIXL_VERSION
)
```

Copy the captured integer into `contract.nixl_connector_version` in this engine's attestation document and the router's `engine_contract.nixl_connector_version`. Set `sources.nixl_connector_version` to the retained capture's path and its module/constant reference; the launch directory's checked image ID and container ID bind that record to the deployed build. Collect it for every engine and compare the integers before declaring the fleet contract. An import or missing-constant error requires checking the installed connector module against the pinned build; retain the error and identify that build's compatibility-hash source before filling the field. A stopped or removed container requires restoring its checked serving plan before process-bound attestation.

### Read the model dimensions used by NIXL

Populate `head_size`, `kv_heads` and `hidden_layers` from the pinned runtime's `ModelConfig.get_head_size()`, `get_total_num_kv_heads()` and `get_total_num_hidden_layers()`. NIXL's [compatibility hash](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/metadata.py) calls these getters. For DeepSeek-style MLA with MLA enabled, the [head-size resolver](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/transformers_utils/model_arch_config_convertor.py) uses `kv_lora_rank + qk_rope_head_dim`. The captured getter values determine the contract for the installed build and selected runtime settings.

Run a configuration inspection in the engine-role shell with the updated step 2 launcher. A fresh inspection plan uses the supplied model and launch record, verifies the image and reads its resolved configuration; the temporary container exits after collecting metadata. This command works while the serving container is running or stopped and reads configuration before model-worker creation:

```bash
umask 077
export MODEL_INSPECT_RUN="$(mktemp -d runs/model-inspection-XXXXXX)/plan"
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$MODEL_INSPECT_RUN"
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$MODEL_INSPECT_RUN"
python3 "$NARWHAL_ENGINE_LAUNCHER" model-dimensions --run "$MODEL_INSPECT_RUN"
cat "$MODEL_INSPECT_RUN/model-dimensions.json"
```

Copy the three integers from the capture's `contract` object into the engine attestation and router fleet contract. Set their `sources` entries to the capture path and corresponding getter names. Retain its `use_mla` setting, model-config hash, image, application revision and plan hash with the deployment; compare the inspection inputs with the serving plan before applying the values. The values describe the model as consumed by the compatibility hash; the runtime page capture from step 4 supplies the fabric payload bound. Repeat the inspection for each engine's image and launch inputs. A configuration import, hash or getter failure requires checking that runtime's model metadata and argument support; retain `model-dimensions.log` and correct that input before continuing. Existing captures retain their contents, so a corrected inspection uses a fresh plan.

### Capture cache block grouping

Set `cross_layers_blocks` from the resolved physical KV cache layout. With vLLM's [layout enum](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/kv_cache_layout.py), `is_block_outermost` identifies layouts that group layer pages inside each physical block: `BLHNC`, `BLNHC` and `BHLNC` yield `true`; `LBHNC`, `LBNHC` and `LHBNC` yield `false`. The [NIXL registration code](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/base_worker.py) consumes views with those physical strides. Preserve the resolved layout name with the boolean so the contract records the allocation's block grouping.

Use the retained startup log from the serving plan. vLLM's [layout resolver](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/attention/backends/utils.py) records `Using <layout> KV cache layout.` after selecting a layout supported by the runtime backends. Set `ENGINE_STARTUP_LOG` to that log's path in the engine-role shell, then inspect the enum from the checked image:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$MODEL_INSPECT_RUN" --startup-log "$ENGINE_STARTUP_LOG"
cat "$MODEL_INSPECT_RUN/cache-registration.json"
```

The inspection requires one distinct resolved layout name, imports the enum in a temporary image container, and writes `cache-registration.json` with the boolean, layout, enum source hash, input-log hash, checked plan hash and image identity. It reads metadata while the serving engine continues running or stays stopped. Copy `cross_layers_blocks` into the attestation and router contracts, and set `sources.cross_layers_blocks` to this capture's path and recorded enum property. Compare the source log's serving-plan inputs with the checked inspection plan before applying the value.

The updated step 4 sizing probe also captures `kv_cache_layout` for each TP rank. When the retained serving log provides an incomplete layout record, use that sizing capture with the same command, replacing `--startup-log "$ENGINE_STARTUP_LOG"` with `--runtime-layout "$CACHE_RUN/cache-layout.json"`. The helper checks the image, model-config hash, launch-record hash, rank coverage and agreement on the layout. If both retained sources lack a resolved layout, repeat `measure-cache` with the updated helper and a fresh sizing plan, then inspect its capture. Retain the earlier fabric samples and budgets with their original records. An unknown layout or unavailable enum requires inspection of the pinned build's layout API before setting the boolean. Preserve failed inspection logs and use a fresh inspection plan for corrected input.

### Capture the resolved transfer mode

The resolved `NixlPullConnector` class selects `transfer_mode = "pull"`; `NixlPushConnector` selects `"push"`. In the pinned vLLM API, [NixlConnector aliases NixlPullConnector](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/connector.py). `kv_both` declares that the engine can produce and consume KV; the resolved connector class determines the pull or push protocol. Keep `connector` equal to the configured launch name and record the resolved class as the transfer-mode source.

The step 5 image check records the factory-resolved class in `image-check.log`. In the engine-role shell, use the serving plan's `ENGINE_RUN` to derive the mode from that retained record and bind it to the checked image and plan:

```bash
python3 - <<'PY_TRANSFER_MODE'
import hashlib
import json
import os
from pathlib import Path
os.umask(0o077)
run = Path(os.environ["ENGINE_RUN"])
plan_data = (run / "launch.json").read_bytes()
plan = json.loads(plan_data)
checked = json.loads((run / "checked.json").read_text())
plan_hash = hashlib.sha256(plan_data).hexdigest()
if checked["plan_sha256"] != plan_hash:
    raise SystemExit("Use the image check belonging to this launch plan")
log = run / "image-check.log"
log_data = log.read_bytes()
resolved = set()
for line in log_data.decode().splitlines():
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(item, dict) and isinstance(item.get("connector"), str):
        resolved.add(item["connector"])
if len(resolved) != 1:
    raise SystemExit("Retain one factory-resolved connector class from this image check")
connector = resolved.pop()
modes = {"NixlPullConnector": "pull", "NixlPushConnector": "push"}
mode = modes.get(connector.rsplit(".", 1)[-1])
if mode is None:
    raise SystemExit("Inspect the pinned connector implementation for its transfer protocol")
record = {
    "transfer_mode": mode,
    "configured_connector": plan["connector"]["kv_connector"],
    "kv_role": plan["connector"]["kv_role"],
    "resolved_connector": connector,
    "image_id": checked["image_id"],
    "plan_sha256": plan_hash,
    "source": str(log),
    "source_sha256": hashlib.sha256(log_data).hexdigest(),
}
with (run / "transfer-mode.json").open("x") as output:
    json.dump(record, output, indent=2)
    output.write("\n")
print(f"Captured transfer_mode={mode} from {connector}")
PY_TRANSFER_MODE
```

Copy the captured string into `contract.transfer_mode` in the attestation and `engine_contract.transfer_mode` in the router fleet config. Set `sources.transfer_mode` to the capture path and resolved class name, and confirm that class agrees with the retained serving startup log. The command reads existing files and preserves the engine's process state. An incomplete or conflicting image-check record requires resolving the connector with the pinned image check before filling the field; an unfamiliar class requires its implementation's explicit protocol definition. Preserve existing captures and use a fresh capture filename when repeating this derivation after repairing an input.

### Capture handshake compatibility enforcement

The pinned NIXL worker resolves `enforce_handshake_compat` through `kv_transfer_config.get_from_extra_config("enforce_handshake_compat", True)` and assigns it to `self.enforce_compat_hash`. That boolean controls rejection of a peer compatibility-hash mismatch in the [worker handshake](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/base_worker.py). New launcher plans explicitly set the extra-config field to `true`; an existing plan that omits it uses the installed worker's default.

In the engine-role shell, use the updated launcher with the checked serving plan to capture the effective setting:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" handshake-policy --run "$ENGINE_RUN"
cat "$ENGINE_RUN/handshake-policy.json"
```

The inspection compares the serving command's `--kv-transfer-config` with the recorded connector object, reads the worker initializer from the pinned image, extracts its default and resolves the setting through `KVTransferConfig.get_from_extra_config`. It requires boolean `true` and writes `handshake-policy.json` with the effective value, whether the plan supplies the setting explicitly, the installed default, source text and hash, connector configuration, checked plan hash and image. The temporary Python container reads metadata and exits before model-worker creation, so the existing serving process and checked plan retain their state.

Copy the captured boolean into `contract.enforce_handshake_compat` and the router fleet contract. Set `sources.enforce_handshake_compat` to the capture path and `NixlBaseConnectorWorker.__init__: self.enforce_compat_hash`. A false or non-boolean setting requires correcting the launch configuration, checking a fresh plan and restarting that engine through its deployment procedure. A changed worker implementation requires inspecting its compatibility-check assignment before deriving the field. Retain `handshake-policy.log` on failure and preserve earlier captures. This capture establishes the configured policy; the later peer handshake and KV-transfer gate exercises that policy between engines.

### Populate the contract and start the sidecar

Populate `contract` from that host's deployed image, packages, model and launch configuration, and match those values to the router fleet config's `engine_contract`. Confirm the engine's `/health`, `/version` and `process_start_time_seconds` metric identify the running process, then launch the sidecar in that engine-host shell, replacing the placeholders with its engine HTTP URL, control-network bind address and attestation port from the private inventory.

```bash
.venv/bin/narwhal-attest \
  --document runs/engine-attestation.production.json \
  --engine-base <engine-http-url> \
  --host <node-serving-address> \
  --port <attestation-port>
```

Set each fleet entry's `attestation_url` to the sidecar's `/v1/attestation` route and keep both `/health` and `/v1/attestation` reachable from the router over the trusted control network. The check below validates the response against the [attestation document contract](Configuration.md#attestation-document) and live engine identity.

### Verify the running sidecar

In a second shell for the same engine role, set `ATTEST_BASE` to that engine's attestation URL with `/v1/attestation` removed and `ATTEST_DOCUMENT` to the document used by its sidecar. Reuse the checked launch directory in `ENGINE_RUN`. Capture the sidecar responses and compare the attested contract with the live engine identity:

```bash
export ATTEST_BASE="http://<node-serving-address>:<attestation-port>"
export ATTEST_DOCUMENT="runs/engine-attestation.production.json"
umask 077
export ATTEST_RUN="$(mktemp -d "$ENGINE_RUN/attestation-check-XXXXXX")"
.venv/bin/python - <<'PY_ATTEST_CHECK'
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

import httpx
from narwhal.engines.attestation import (
    AttestationDocument, fetch_engine_identity, verify_attestation,
)

out = Path(os.environ["ATTEST_RUN"])
base = os.environ["ATTEST_BASE"].rstrip("/")
responses = {}
with httpx.Client(timeout=15) as client:
    for name, route in (("health", "/health"), ("attestation", "/v1/attestation")):
        response = client.get(base + route)
        (out / f"{name}.json").write_text(response.text)
        (out / f"{name}.status").write_text(str(response.status_code) + "\n")
        responses[name] = response
for response in responses.values():
    if response.status_code != 200:
        raise SystemExit(f"Expected HTTP 200; inspect the captures in {out}")
plan = json.loads((Path(os.environ["ENGINE_RUN"]) / "launch.json").read_text())
key = os.environ.get("NARWHAL_ENGINE_API_KEY")
headers = {"authorization": "Bearer " + key} if key else {}
identity = asyncio.run(fetch_engine_identity(plan["endpoint"], headers=headers))
(out / "identity.json").write_text(json.dumps(asdict(identity), indent=2) + "\n")
document = AttestationDocument.load(os.environ["ATTEST_DOCUMENT"])
failures = verify_attestation(responses["attestation"].json(), document.contract, identity)
if failures:
    raise SystemExit("; ".join(failures))
print("Both sidecar endpoints passed; attestation matches the live engine and contract")
PY_ATTEST_CHECK
```

Repeat this check on each engine host, retain the capture paths with the deployment record, and keep those engines and sidecars running through profiling, preflight and the workload trial. An HTTP or contract failure requires inspecting the captured responses and sidecar log, correcting the named input, and restarting that sidecar against its live engine before repeating the check.

## 7. Profile the idle engines

On the router host, run the profiler with its private engine endpoints and credentials loaded while the real engines are reserved and idle. Warm the model and disable prefix caching under the [measurement conditions](Measure.md#1-calibrate-slos) before collecting the sweep.

```bash
.venv/bin/narwhal-profile \
  --fleet config/fleet.local.json \
  --decode-input-lens 512,4096,8192 \
  --decode-concurrency 1,4,16,48
```

The profiler measures one-token prefill latency across input lengths; for decode, it varies prompt length and concurrency, then fits observed token intervals against active request counts plus estimated resident KV while the complete cohort decodes.

Choose decode input lengths and concurrency values that cover expected traffic under the [measurement conditions](Measure.md#1-calibrate-slos), and retain the sample sidecar because the controller holds any role change whose projected decode point falls outside that measured range.

At startup, Narwhal compares the profile rows with the configured engine IDs and halts with the differing IDs when the sets diverge. Keep the measured engine processes and launch configuration fixed through preflight and the workload trial. After replacing an engine process or changing its runtime configuration, collect fresh profiles and repeat preflight before resuming the trial.

Set `slo.ttft_s` and `slo.tpot_s` from light-load measurements on this engine shape, keeping TPOT above the measured per-token floor.

## 8. Check the engine and KV contract

On the router host, run preflight against the same fleet config and engine processes used for profiling.

```bash
.venv/bin/narwhal-check --fleet config/fleet.local.json
```

| Gate       | Check                                                                                                                                                                                                     |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine answers within the configured health budget.                                                                                                                                                 |
| `contract` | Attestation matches the current process and declared runtime.                                                                                                                                             |
| `model`    | Every engine serves the configured model.                                                                                                                                                                 |
| `pace`     | Prefill latency stays within the slowdown limit against the fleet median when at least three probes succeed, and against each saved engine profile when available. Smaller fleets require those profiles. |
| `tokenize` | Exact input sizing works when enabled.                                                                                                                                                                    |
| `produce`  | Each tested engine exports a KV handoff.                                                                                                                                                                  |
| `consume`  | Each tested peer consumes that handoff.                                                                                                                                                                   |
| `profile`  | The measured profile set matches the configured engine set.                                                                                                                                               |
| `slo`      | The configured targets are feasible against those profiles.                                                                                                                                               |

`narwhal-check` probes every eligible producer-consumer pair by default, limits maintenance checks to the configured ring with `--ring` and repeats transfer probes with `--repeats` when investigating intermittent failure.

Start the router after every required gate passes; a failed gate identifies its engine, leg and budget.

## 9. Start the router and send a request

On the designated router host, launch Narwhal from its checkout with the private environment loaded. Bind the listener to the trusted control network according to the supplied inventory.

```bash
.venv/bin/narwhal-serve \
  --fleet config/fleet.local.json \
  --host 0.0.0.0 \
  --port 8000
```

Open another management shell on the same router host for these checks; `localhost` resolves within that remote shell. Replace `<served-model>` with the model in the fleet config. If the listener uses a different address or port, substitute that URL in every probe.

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/ready
curl -fsS http://localhost:8000/narwhal/state | python3 -m json.tool
curl -fsS http://localhost:8000/metrics
curl -fsS http://localhost:8000/v1/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<served-model>","prompt":"Return one sentence about narwhals.","max_tokens":32}'
```

Record the router listener and each client's route to it. Router-host probes and Prometheus use the router's local address; a workstation load client uses the forwarded address established in step 10. Confirm the listener's address family because an IPv4 `127.0.0.1` client reaches a different listener from an IPv6-only `::` socket.

Before load, retain `/health` with process liveness and cached instance counts, `/ready` with admission state, `/narwhal/state` with every configured engine and the current split, and `/metrics`; then record one successful completion that increments `served`.

Keep the default anonymous listener on a trusted network until ingress supplies TLS, public authentication, WAF policy, request limits and model routing.

## 10. Validate private-route capacity

For the first deployment trial, generate load on the management workstation, run Prometheus and Grafana on the existing router host, and reach the router through the verified SSH management route. The inventory's `router` role supplies the destination and authentication for all three services. Record these role assignments, the workstation hostname and source revision, and the tunnel mapping in the private deployment record. This trial measures serving performance through the private SSH path, including its network and encryption overhead.

### Start monitoring on the router host

Open the installed router-role shell as described in step 2. Follow [Set up observability](Observability.md#1-select-the-deployment) there, using `config/fleet.local.json` and `http://127.0.0.1:8000` for the listener started in step 9. Run `make observe` and retain its target and dashboard verification output. Prometheus reaches the router directly from that host and resolves engine targets from the fleet document.

### Open the private trial route from the workstation

In a management-checkout terminal with the supplied `.env` loaded as in step 1, run:

```bash
python3 tools/deploy_hosts.py tunnel --role router \
  --forward 18000:8000 --forward 19090:9090 --forward 13000:3000
```

The helper binds workstation loopback ports, verifies the supplied SSH host key, and uses the router host's existing password or SSH-agent/key configuration. Keep that terminal open. In another workstation terminal, probe the forwarded services:

```bash
export NARWHAL_TRIAL_URL=http://127.0.0.1:18000
curl -fsS "$NARWHAL_TRIAL_URL/health"
curl -fsS "$NARWHAL_TRIAL_URL/ready"
curl -fsSG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

Open `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator` for Grafana. Use `$NARWHAL_TRIAL_URL` as the workload client's base URL. Local port conflicts require a free left-hand port and matching client URL; remote connection failures require checking the service listener from the router shell. `--remote-address` selects a different remote listener address when required; run a separate tunnel command for services bound to different addresses. Preserve the tunnel log under its reported `runs/access-<id>/` path and capture terminal errors in the private deployment record. Ctrl-C closes this tunnel's forwards.

### Measure and retain the trial

Close the trial in this order:

1. Create the deployment identifier and assemble the [deployment evidence set](Measure.md#2-validate-the-deployment-under-load).
2. Attach the passing step 8 default preflight mesh for the current processes, fleet, profiles and targets. Repeat preflight when those inputs change.
3. Retain the monitoring startup output and successful router and engine scrape query from the preceding commands.
4. Run the [initial synthetic workload](Measure.md#run-the-initial-synthetic-workload) from the workstation through `$NARWHAL_TRIAL_URL`: the guide supplies the workload preparation command and 200-request runs at 0.5 and 1 request/s for 8,192 input and 128 output tokens. Retain the generated workload, request records and summaries. Record the 2-second TTFT, 33.3-ms TPOT and 95% attainment thresholds as candidate targets until measured results and the service requirement establish acceptance. Use client CPU, memory, network and scheduling records alongside throughput to identify saturation of the load generator or SSH path.
5. Drain resident work, reconcile every offer to its client and router terminal classes, query the dashboard's engine, request, token, role and pool-load series through Grafana's provisioned data source, then run the post-load KV ring.

[Measure a fleet](Measure.md) defines timing boundaries, rate selection and artifact contents; [Set up observability](Observability.md) defines scrape and dashboard checks.

Run the post-load ring from the router host after resident work drains:

```bash
.venv/bin/narwhal-check --fleet config/fleet.local.json --ring
```

The setup is complete when the router serves the measured workload, client records reconcile with its journal, Prometheus scrapes the router and engines, Grafana displays the required series, and the post-load KV ring passes. Retain the running service locations, source revision, fleet configuration, profiles, journal and monitoring URLs in the private deployment record.

## Deployment gates and recovery

Share sanitised extracts from the private deployment record, using stable host aliases and replacing private addresses, paths and credential values.

| Step and documentation | Required knowledge | Likely failure and recovery | Private value location |
| --- | --- | --- | --- |
| [Management access](#1-open-the-management-shells) | Workstation `.env`, installed remote image/model, GPU driver tools and initial launch policy. | Discovery failure: inspect its private log and correct the named environment field, host tool or image. Host-key rejection: verify the destination and fingerprint before updating its entry. Login failure: inspect the host's private log and check its credential and route. | Workstation `.env`, generated JSON and host-key files, `runs/discovery/<run>/` and `runs/access-<id>/`. |
| [Host installation](#2-install-narwhal-on-the-remote-hosts) | Approved commit in the management checkout, shared launch fields, per-engine allocation records, per-node overrides and host prerequisites. | Preparation failure: correct the named field or source revision. Transfer or checkout mismatch: inspect the prepared hashes and existing artifacts. Setup failure: repair the dependency error on that host and repeat the same run. | Workstation `runs/deployment-env/<run>/`; remote `~/Narwhal-deploy/<id>/`, role files, router fleet config and installation marker. |
| [Engine preparation](#3-inspect-each-engine-host) | Remote PCI vendor, observed GPU model and count, declared replica allocation and TP, image identity, model hash, paths and ports. | Device, artifact or listener mismatch: inspect the failing resource, restore the declared artifact or resolve resource ownership before launch. | Engine `.env.engine-<n>` and `config/engine-launch.engine-<n>.json`; workstation `NARWHAL_LAUNCH_CONFIG`. |
| [Fabric](#4-prepare-the-transfer-fabric) | Peer addresses, TCP or RDMA selection, checked runtime, available GPUs for cache sizing, prompt length, handoff rate, burst and transfer-time budget. | Sizing failure: inspect the recorded probe and repair its model, device, runtime or cache-spec input. Route or connection failure: check the source address, listener, firewall and selected device/GID. Rate below budget: inspect link counters, MTU, CPU and concurrent traffic, then retain a fresh sample after repair. | Engine role environment and launch record; model config; helper path and digest; host-local `runs/fabric-*/cache-probe/` plan, page specs, container ID and logs; budget, directed samples and private edge matrix. |
| [Fleet config and engine launch](#5-configure-the-fleet-and-launch-engines) | Complete host/fabric checks, immutable image, pinned packages, model flags, library environment, cache shape and selected TP/devices. | Image check failure: correct package or library input. Startup or HTTP failure: inspect the recorded container and logs, then prepare a fresh corrected plan. | Router fleet config; engine role environment and runtime record; delivered launcher; private `runs/engine-launch-*/` plan, environment, image check, container ID and HTTP captures. |
| [Attestation](#6-attest-each-engine-process) | Running engine identity, installed connector protocol constant, resolved model dimension getters and physical cache layout, resolved connector transfer mode and handshake policy, contract and sidecar bind address. | Connector import/constant or model-getter failure: inspect the pinned build's compatibility-hash source and resolved model configuration. Identity endpoint failure or contract mismatch: verify the engine process and document, then restart its sidecar against that process. | Launch directory's `nixl-connector-version.json`, `transfer-mode.json` and `handshake-policy.json`, model inspection's `model-dimensions.json`, `cache-registration.json` and logs, checked images and container ID; engine `runs/engine-attestation.production.json` and private inventory. |
| [Profiling](#7-profile-the-idle-engines) | Idle engine reservation, cache policy, workload lengths and concurrency. | Probe failure or fit rejection: inspect the named engine, measured range and sample file; repair the cause and retain a new sweep under a fresh profile path. | Router fleet config and profile/sample files under `runs/`. |
| [Preflight](#8-check-the-engine-and-kv-contract) | Current engine set, profiles and SLO targets. | Failed gate: use its engine, leg and budget to select the corresponding [fleet troubleshooting](Troubleshoot.md) check. | Router environment, fleet config and private preflight output. |
| [Router verification](#9-start-the-router-and-send-a-request) | Listener address, served model, engine count and opening split. | Bind error or failed readiness/completion: check listener ownership, URL address family and the engine or controller error in the router log. | Router environment, ignored fleet config and endpoint captures. |
| [Capacity acceptance](#10-validate-private-route-capacity) | Workstation Python load helper, router-host Docker Compose, existing router SSH access, generated synthetic workload, fixed launch/cache policy, candidate latency/attainment targets and the private SSH route. | Tunnel bind failure: choose a free local port. Service or scrape failure: check the router-host listener and target error. Warmup or token-accounting failure: inspect the retained status, stream error and usage counts. Client schedule failure: inspect CPU, memory, network and lag before changing the offered rate. SLO or accounting failure: reconcile client and router records and inspect serving saturation. | Workstation access environment and tunnel logs, router fleet and role environment, Compose discovery files, private `runs/load-trial-<id>/` workload, manifests, per-request records, summaries and state/network snapshots. |
