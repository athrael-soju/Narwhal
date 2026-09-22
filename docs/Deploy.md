# Deploy a fleet

From your management workstation, use the supplied inventory and private access to open shells on the router and GPU engine hosts. Follow the numbered steps through a real model completion, then validate the intended ingress and workload before admitting client traffic.

## Hosts and inputs

| Host | Work performed here | Supplied inputs |
| --- | --- | --- |
| Management workstation | Read the guides and inventory, open remote shells, retain the deployment record. | Checkout-local `.env`, `config/hosts.local.json`, private fleet JSON, `config/engine-launch.local.json` and verified `config/ssh.known_hosts`. |
| Router host | Install Narwhal, create the fleet config, profile and check engines, run the router. | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO targets. |
| Engine hosts | Inspect GPUs and artifacts, configure the fabric, launch vLLM and attestation sidecars. | Accelerator and TP shape, engine image, model checkpoint, launch configuration, fabric addresses and ports. |
| Load-client and observability hosts | Generate deployment traffic, scrape metrics and inspect the dashboard. | Ingress route and credentials, workload, scrape targets and dashboard access. |

The management workstation needs Git, Bash, Python 3.11 or newer and the supplied access tooling. Install Python 3.11 or newer with `venv`, Git, Make and curl on the remote hosts that run Narwhal commands. Engine hosts also need the declared accelerator driver, container runtime and transfer devices. The inventory assigns these roles to machines; a management workstation's local hardware describes that machine alone.

Narwhal serves one model and compatible KV layout across engines running vLLM with NIXL and effective `kv_both` behaviour. Every eligible producer must transfer KV to every eligible consumer, and one active controller owns the fleet.

Create a private deployment record before the first command. At each gate, record the host, full source revision, starting state, commands actually run, exit status and artifact locations. Report the first blocked gate before recovery; after reporting, clean only the processes and files created by that test. The [gate reference](#deployment-gates-and-recovery) maps each step to its inputs and recovery.

## 1. Open the management shells

On the management workstation, use the supplied private `.env`, host inventory, fleet JSON and verified SSH host-key file in the Narwhal checkout. `NARWHAL_HOSTS` selects `config/hosts.local.json`, `NARWHAL_FLEET` selects the engine fleet document, `NARWHAL_LAUNCH_CONFIG` selects the supplied per-engine allocation and device records, and `NARWHAL_SSH_KNOWN_HOSTS` selects `config/ssh.known_hosts`. The host inventory defines each physical machine once, names its access variables and assigns its router and engine roles. [Host inventory and SSH access](Configuration.md#host-inventory-and-ssh-access) defines the format.

A fresh clone receives those private files separately from the public source. Preserve the supplied values and load `.env` from the management checkout with shell tracing disabled:

```bash
set +x
set -a
. ./.env
set +a
python3 tools/deploy_hosts.py plan
python3 tools/deploy_hosts.py check-access
```

`plan` lists host IDs and assigned roles. `check-access` verifies the pinned SSH key and login once per host, recording `hostname` and the executed command in private logs under `runs/access-<id>/`. A successful verified login completes the access gate for every role on that host. Hostnames serve as observed labels and can repeat across machines. Management destinations open shells; engine HTTP, attestation and fabric addresses retain their service roles.

Open a shell by role when inspecting a host:

```bash
python3 tools/deploy_hosts.py shell --role engine-1
```

Use `--role router` for the router shell and the corresponding numbered engine role for other hosts. Both roles resolve to the same authentication entry when they share a machine. For a missing access variable, inspect the named field in the supplied `.env`. For an unknown or changed host key, verify the destination and fingerprint through the supplied private access source before updating the checkout-local host-key entry. For authentication or connection failures, check the username, credential, management route, SSH port and firewall. Record the first blocked host and gate before recovery; share sanitised extracts from the private command log.

## 2. Install Narwhal on the remote hosts

### Prepare the source and role environments on the workstation

With `.env` loaded, prepare a new deployment directory. The supplied `NARWHAL_DEPLOYMENT_REVISION` selects the full approved commit in the management checkout. The helper packages that commit into a Git bundle and verifies the exact SHA with a fresh local clone before writing the completed manifest.

```bash
python3 tools/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
```

The helper exports `.env.router` and one `.env.engine-<n>` for every assigned role, using the supplied fleet document and shared engine values with per-node overrides. It copies the fleet document for the router and selects one `engine-launch.engine-<n>.json` per engine from `NARWHAL_LAUNCH_CONFIG`, validating its GPU allocation, TP size and transport device declarations. Preparation also snapshots the management checkout's `tools/fabric_budget.py` and `tools/launch_engine.py` and delivers both to each engine host under `runs/deployment-tools/`. The role environment exports that path as `NARWHAL_FABRIC_BUDGET_TOOL` and its digest as `NARWHAL_FABRIC_BUDGET_SHA256`. The engine launcher uses the corresponding `NARWHAL_ENGINE_LAUNCHER` path and `NARWHAL_ENGINE_LAUNCHER_SHA256` digest. The manifest records hashes for the source bundle, helper snapshots and all role files. Management credentials stay in the workstation environment. [Host environment files](Configuration.md#host-environment-files) lists the exported fields.

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

The supplied private record declares the replica allocation, container device mappings and UCX selection; its `sources` entries identify the allocation and device definitions used to prepare it. [Engine launch records](Configuration.md#engine-launch-records) defines these fields. Retain this output in the private deployment record.

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

Use these observations to replace `<accelerator-model>` in the router's working `config/fleet.local.json` under `hardware.accelerator`. Confirm that every participating engine has the declared accelerator model. Set `hardware.accelerators_per_engine` to the length of the record's `gpu_ids` and `hardware.tensor_parallel` to its `tensor_parallel_size`; verify that the selected indices or UUIDs identify available GPUs on this host. A host's total GPU count describes available hardware; the selected replica allocation determines its TP shape. Record the observations with the deployment and retain the corrected fleet config for subsequent runs.

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

The supplied per-engine record's `runtime` object names pinned package versions, library environment, model dtype, cache dtype, block size and model-specific arguments. Step 2 delivers `launch_engine.py` beside the fabric calculator and exports `NARWHAL_ENGINE_LAUNCHER` and `NARWHAL_ENGINE_LAUNCHER_SHA256` in each engine-role environment. The launcher builds a Docker command invoking `python3 -m vllm.entrypoints.openai.api_server` inside `NARWHAL_ENGINE_IMAGE`, mounts `NARWHAL_MODEL_DIR` read-only at `/model`, exposes the declared devices, and applies the selected TP allocation.

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

The check inspects the local immutable image identity, starts a temporary container to compare package versions and resolves the configured connector through the image's `KVConnectorFactory`, then records the plan hash in `checked.json`. The factory imports the class registered by that vLLM build; `image-check.log` retains its module and class name alongside the package versions and command output. The temporary container exits after these imports. Resolve package and library failures against the supplied image and runtime record. A launcher correction requires a fresh step 2 preparation to deliver the updated helper and its digest, followed by a fresh launch plan; retain the earlier image-check log and fabric samples with their original manifest.

Inspect the planned listeners with `ss -ltnp`, then start the checked plan:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" start --run "$ENGINE_RUN"
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
docker logs --follow "$ENGINE_CONTAINER"
```

`start` creates a uniquely named container, records its ID before starting it and retains Docker output in `launch.log`. It verifies the saved environment and checked plan before creating the container. Follow the engine log through model loading and HTTP startup; Ctrl-C ends the log follower while the engine container continues running. An existing `container.id` directs recovery to that recorded container. Inspect `docker inspect "$ENGINE_CONTAINER"` and `docker logs "$ENGINE_CONTAINER"` for a startup exit, then correct the implicated device, model, memory, library or transport input and prepare a fresh launch plan.

### Verify the engine HTTP API

After HTTP startup, run these probes in the same engine-role shell. They use the supplied engine endpoint and configured engine credential, and save responses under the launch directory:

```bash
python3 - <<'PY_ENGINE'
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
run = Path(os.environ["ENGINE_RUN"])
plan = json.loads((run / "launch.json").read_text())
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
assert version["version"] == plan["expected_packages"]["vllm"]
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

For a failed probe, retain its command, HTTP status and engine log with the first blocked gate. Preserve completed response files; use fresh filenames when repeating a probe after repair. After the first engine passes, apply the same prepare, check, start and probe sequence in each remaining engine-role shell with its own `ENGINE_RUN`. Record each container ID and launch directory for step 6. When cleaning a test deployment, capture its logs before using `docker stop` and `docker rm` on the container IDs created by that test.

## 6. Attest each engine process

On each engine host, create the attestation document under ignored `runs/`, preserving any document from an earlier deployment.

```bash
mkdir -p runs
(set -o noclobber; cat config/engine-attestation.example.json > runs/engine-attestation.production.json)
```

Populate `contract` from that host's deployed image, packages, model and launch configuration, and match those values to the router fleet config's `engine_contract`. Confirm the engine's `/health`, `/version` and `process_start_time_seconds` metric identify the running process, then launch the sidecar in that engine-host shell, replacing the placeholders with its engine HTTP URL, control-network bind address and attestation port from the private inventory.

```bash
.venv/bin/narwhal-attest \
  --document runs/engine-attestation.production.json \
  --engine-base <engine-http-url> \
  --host <node-serving-address> \
  --port <attestation-port>
```

Point `attestation_url` at its `/v1/attestation` route, expose `/health` and `/v1/attestation` through the trusted control network, capture both responses, and restart the sidecar with the engine process. Validate the input and digested response against the [attestation document contract](Configuration.md#attestation-document).

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

At startup, Narwhal compares the profile rows with the configured engine IDs and halts with the differing IDs when the sets diverge.

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

Use one concrete router URL for these probes, the Prometheus target and the deployment load client, confirming that the process listens on its selected address family because an IPv4 `127.0.0.1` client reaches a different listener from an IPv6-only `::` socket.

Before load, retain `/health` with process liveness and cached instance counts, `/ready` with admission state, `/narwhal/state` with every configured engine and the current split, and `/metrics`; then record one successful completion that increments `served`.

Keep the default anonymous listener on a trusted network until ingress supplies TLS, public authentication, WAF policy, request limits and model routing.

## 10. Validate ingress and capacity

Use the deployment's designated load-client and observability hosts from the private inventory for workload and monitoring commands; keep Narwhal preflight on the router host. Close the deployment in this order:

1. Create the deployment identifier and assemble the [deployment evidence set](Measure.md#2-validate-the-deployment-under-load).
2. Run the default preflight mesh against those processes and retain its output.
3. Configure Prometheus with the same router URL used by the deployment load client, then verify the router and every engine target.
4. Configure the [intended ingress](Operate.md#configure-ingress), then run the [deployment workload](Measure.md#2-validate-the-deployment-under-load) through its intended ingress and attach its artifacts to the deployment identifier.
5. Drain resident work, reconcile every offer to its client and router terminal classes, query the dashboard's engine, request, token, role and pool-load series through Grafana's provisioned data source, then run the post-load KV ring.

[Measure a fleet](Measure.md) defines timing boundaries, rate selection and artifact contents; [Set up observability](Observability.md) defines scrape and dashboard checks.

Run the post-load ring from the router host after resident work drains:

```bash
.venv/bin/narwhal-check --fleet config/fleet.local.json --ring
```

[Operate Narwhal](Operate.md) covers supervision, failover, maintenance and upgrades after acceptance.

## Deployment gates and recovery

Share sanitised extracts from the private deployment record, using stable host aliases and replacing private addresses, paths and credential values.

| Step and documentation | Required knowledge | Likely failure and recovery | Private value location |
| --- | --- | --- | --- |
| [Management access](#1-open-the-management-shells) | Physical host IDs, assigned roles, access variable names and verified server keys. | Missing access input: fill the named environment field. Host-key rejection: verify the destination and fingerprint before updating its entry. Login failure: inspect the host's private log and check its credential and route. | Workstation `.env`, `config/hosts.local.json`, `config/ssh.known_hosts` and `runs/access-<id>/`. |
| [Host installation](#2-install-narwhal-on-the-remote-hosts) | Approved commit in the management checkout, shared launch fields, per-engine allocation records, per-node overrides and host prerequisites. | Preparation failure: correct the named field or source revision. Transfer or checkout mismatch: inspect the prepared hashes and existing artifacts. Setup failure: repair the dependency error on that host and repeat the same run. | Workstation `runs/deployment-env/<run>/`; remote `~/Narwhal-deploy/<id>/`, role files, router fleet config and installation marker. |
| [Engine preparation](#3-inspect-each-engine-host) | Remote PCI vendor, observed GPU model and count, declared replica allocation and TP, image identity, model hash, paths and ports. | Device, artifact or listener mismatch: inspect the failing resource, restore the declared artifact or resolve resource ownership before launch. | Engine `.env.engine-<n>` and `config/engine-launch.engine-<n>.json`; workstation `NARWHAL_LAUNCH_CONFIG`. |
| [Fabric](#4-prepare-the-transfer-fabric) | Peer addresses, TCP or RDMA selection, checked runtime, available GPUs for cache sizing, prompt length, handoff rate, burst and transfer-time budget. | Sizing failure: inspect the recorded probe and repair its model, device, runtime or cache-spec input. Route or connection failure: check the source address, listener, firewall and selected device/GID. Rate below budget: inspect link counters, MTU, CPU and concurrent traffic, then retain a fresh sample after repair. | Engine role environment and launch record; model config; helper path and digest; host-local `runs/fabric-*/cache-probe/` plan, page specs, container ID and logs; budget, directed samples and private edge matrix. |
| [Fleet config and engine launch](#5-configure-the-fleet-and-launch-engines) | Complete host/fabric checks, immutable image, pinned packages, model flags, library environment, cache shape and selected TP/devices. | Image check failure: correct package or library input. Startup or HTTP failure: inspect the recorded container and logs, then prepare a fresh corrected plan. | Router fleet config; engine role environment and runtime record; delivered launcher; private `runs/engine-launch-*/` plan, environment, image check, container ID and HTTP captures. |
| [Attestation](#6-attest-each-engine-process) | Running engine identity, contract and sidecar bind address. | Identity endpoint failure or contract mismatch: verify the engine process and document, then restart its sidecar against that process. | Engine `runs/engine-attestation.production.json` and private inventory. |
| [Profiling](#7-profile-the-idle-engines) | Idle engine reservation, cache policy, workload lengths and concurrency. | Probe failure or fit rejection: inspect the named engine, measured range and sample file; repair the cause and retain a new sweep under a fresh profile path. | Router fleet config and profile/sample files under `runs/`. |
| [Preflight](#8-check-the-engine-and-kv-contract) | Current engine set, profiles and SLO targets. | Failed gate: use its engine, leg and budget to select the corresponding [fleet troubleshooting](Troubleshoot.md) check. | Router environment, fleet config and private preflight output. |
| [Router verification](#9-start-the-router-and-send-a-request) | Listener address, served model, engine count and opening split. | Bind error or failed readiness/completion: check listener ownership, URL address family and the engine or controller error in the router log. | Router environment, ignored fleet config and endpoint captures. |
| [Capacity acceptance](#10-validate-ingress-and-capacity) | Client workload, ingress route, SLO target and scrape targets. | SLO, accounting or scrape failure: reconcile client and router records, repair the identified bottleneck or target, then rerun the affected acceptance checks. | Private load-client and observability configuration, deployment evidence store. |
