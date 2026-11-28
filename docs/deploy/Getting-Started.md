---
description: Take a new Narwhal fleet from bare hosts to serving with narwhal deploy, on one SSH host, several SSH hosts, or a Kubernetes cluster of one node or several.
---

# Getting started

This page takes a fleet from hosts with nothing of Narwhal's on them to a serving fleet you can watch on the dashboard. Pick a layout:

| Layout | Engines | Routers |
| --- | --- | --- |
| One SSH host | Two or more engines on one host, each with its own GPUs | One router on the same host, with no failover |
| Several SSH hosts | Engines on two or more hosts | A primary and a standby router on two hosts, behind HAProxy, with a shared lease. Or one router, with no failover |
| One Kubernetes node | Two or more engine pods on one node | A router pair, both on that node |
| Several Kubernetes nodes | Engine pods on two or more nodes | A router pair on two different nodes |

The SSH layouts share every step below; the inputs file is where they differ. The Kubernetes layouts are at the [end of the page](#deploying-on-kubernetes).

## Preparing the workstation

The workstation runs `narwhal deploy` and keeps the run directories. It needs Python 3.11 or later, OpenSSH 8.5 or later and `uv`:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'narwhal-inference==<version>'
```

Download the same release's wheel for the hosts and compile its dependency lock, as [Building the deployment package](02-Install.md#building-the-deployment-package) describes. Keep both SHA-256 values.

Write the known-hosts file before anything connects. One way is to scan each host, then compare every fingerprint with one from a source you trust, such as the provider console:

```bash
mkdir -p config
ssh-keyscan -t ed25519 <host-a> >> config/ssh.known_hosts
ssh-keygen -lf config/ssh.known_hosts
```

For a host whose SSH port is not 22, scan it with `ssh-keyscan -t ed25519 -p <port> <host-a>`, which writes the entry in the `[host]:port` form that OpenSSH looks up, and write its destination as `ssh://deploy@<host-a>:<port>` or as an alias with a `Port` line in your SSH config.

`narwhal deploy` checks every host key against this file and never adds one. Add the jump host's key too when you reach the hosts through one, which `NARWHAL_SSH_JUMP` names. The jump connection authenticates with a key or the SSH agent. Load a key that has a passphrase into the agent first, since key authentication never prompts.

## Preparing SSH hosts

The site prepares the hosts; `narwhal deploy` installs no packages and never runs `sudo`. A missing piece fails `plan` or the stage that needs it, naming the host and what is missing.

On every host, for the SSH user that `narwhal deploy` logs in as:

- SSH access with a key or a password, through the optional jump host.
- Python 3.11 or later with `venv`, and access to a Python package index for the locked dependencies.
- coreutils, including `timeout`, and a writable home directory.
- systemd 240 or later with a running user manager, and lingering, so the user's units keep running after the SSH session ends. A non-interactive SSH session needs `XDG_RUNTIME_DIR`, which `pam_systemd` sets. Enable lingering as root:

    ```bash
    loginctl enable-linger deploy
    ```

- On each host that runs engines, HAProxy or the monitoring stack, Docker Engine, usable by the SSH user, such as through membership of the `docker` group.

On each engine host:

- The accelerator driver and a container runtime that exposes the GPUs, with `nvidia-smi` or `rocminfo`.
- `ip` and `ss` from `iproute2`, `setsid` from `util-linux`, and `iperf3` for `ucx_tcp` or `perftest` for `ucx_rdma`.
- The engine image, loaded under the same image ID on every engine host, and the checkpoint staged at `NARWHAL_MODEL_DIR`, as [Staging and identifying the checkpoint](01-Discover.md#staging-and-identifying-the-checkpoint) describes.
- The directory `NARWHAL_RUN_DIR`.
- A firewall that lets the routers reach each engine and attestation port, lets the engines reach each other's KV side-channel ports and UCX port range, and lets the hosts reach each other's `NARWHAL_FABRIC_TEST_PORT` on the fabric addresses.

On the primary router's host, Docker Compose v2, the `docker-compose-plugin` package, for the monitoring stack. `plan` checks for it.

For a router pair, on both router hosts:

- The lease directory, the directory of `NARWHAL_LEASE_PATH`, mounted from shared storage at the same path, writable by the SSH user, and mounted again at boot. On NFS, mount it with close-to-open consistency (no `nocto`), `local_lock=none` (no `nolock`) and `actimeo=1` or `noac`, as in [Mount options for the lease](07-Serve-and-Measure.md#mount-options-for-the-lease):

    ```text
    nfs-server:/export/narwhal  /srv/shared  nfs4  rw,hard,actimeo=1  0  0
    ```

- Clocks within one second of each other, kept by NTP or chrony. `lease-check` measures the skew.
- A firewall that lets each router and HAProxy reach both routers' ports, and clients reach the load balancer's.

A router pair also needs Docker on the load balancer's host, which is the primary router's host unless `NARWHAL_LOAD_BALANCER_SSH` names another.

## Writing the inputs file

`narwhal deploy` reads its inputs from the environment. Write them to `.env` at mode 0600; [Deployment inputs](../configuration/04-Deployment-Inputs.md) lists every variable. For one host with two engines, each on its own GPU:

```bash
# The release the hosts install, and the host keys
NARWHAL_WHEEL=dist/narwhal_inference-<version>-py3-none-any.whl
NARWHAL_WHEEL_SHA256=<wheel SHA-256>
NARWHAL_REQUIREMENTS=requirements.lock
NARWHAL_REQUIREMENTS_SHA256=<lock SHA-256>
NARWHAL_SSH_KNOWN_HOSTS=config/ssh.known_hosts

# Two engines on one host, with their own GPUs and ports
NARWHAL_NODE_1_SSH=deploy@<host-a>
NARWHAL_NODE_2_SSH=deploy@<host-a>
NARWHAL_NODE_1_GPU_IDS=0
NARWHAL_NODE_2_GPU_IDS=1
NARWHAL_ENGINE_PORT=8001
NARWHAL_ATTEST_PORT=9001
NARWHAL_KV_SIDE_CHANNEL_PORT=5601
NARWHAL_UCX_TCP_PORT_RANGE=20000-20099
NARWHAL_NODE_2_ENGINE_PORT=8002
NARWHAL_NODE_2_ATTEST_PORT=9002
NARWHAL_NODE_2_KV_SIDE_CHANNEL_PORT=5602
NARWHAL_NODE_2_UCX_TCP_PORT_RANGE=20100-20199

# The engine image, the model and the host paths
NARWHAL_ENGINE_IMAGE=sha256:<image ID>
NARWHAL_ENGINE_MODEL_NAME=<served model name>
NARWHAL_MODEL_DIR=/srv/models/<model>
NARWHAL_MODEL_TREE_SHA256=<model_tree_sha256>
NARWHAL_RUN_DIR=/srv/narwhal/runs
NARWHAL_FABRIC_INTERFACE=<interface>

# The KV each fabric edge must carry, and the port that measures it
NARWHAL_FABRIC_PROMPT_TOKENS=1024
NARWHAL_FABRIC_HANDOFFS_PER_S=1
NARWHAL_FABRIC_BURST=1
NARWHAL_FABRIC_TRANSFER_BUDGET_S=1
NARWHAL_FABRIC_HEADROOM=1.25
NARWHAL_FABRIC_TEST_PORT=5201

# SLO targets, profiling and first-token calibration
NARWHAL_TTFT_S=<seconds>
NARWHAL_TPOT_S=<seconds>
NARWHAL_ROUTER_OVERHEAD_S=0.05
NARWHAL_FIRST_TOKEN_MARGIN_S=0.1
NARWHAL_PROFILE_PREFILL_LENS=256,700,1300,2300,4096,4300,8300,12300
NARWHAL_PROFILE_DECODE_INPUT_LENS=512,4096,8192
NARWHAL_PROFILE_WARMUP_REQUESTS=2
NARWHAL_CALIBRATION_INPUT_TOKENS=256,8192,16382
NARWHAL_CALIBRATION_SAMPLES=100
NARWHAL_CALIBRATION_OBSERVATION_TIMEOUT_S=20

# The router, its control token and the monitoring stack
NARWHAL_ROUTER_URL=http://<host address>:8000
NARWHAL_ROUTER_TOKEN=<64 hex digits>
NARWHAL_FLEET_NAME=first-fleet
```

Generate the router's [control token](../HTTP-API.md#control-token) once per fleet, and keep it only in `.env`:

```bash
python -c 'import secrets; print(secrets.token_hex(32))'
```

The profiling and calibration lengths above suit engines whose live context is 16,384 tokens; [Profile engines and run preflight](06-Profile-and-Preflight.md) explains how to choose them, and [Setting production SLOs](../measure/02-Targets-and-Freeze.md#setting-production-slos) the SLO targets. If `profile` fails with a prefill fit error, change the lengths as [Profiling idle engines](06-Profile-and-Preflight.md#profiling-idle-engines) describes. Engines on one host share its fabric interface, so they need distinct engine, attestation and side-channel ports and port ranges. A fleet on one host has no fabric edges to measure, and preflight checks the transfers between its engines. [Needs verification: task 13 follows this layout on one live host]

The router overhead and first-token margin above are starting values. `router-start` measures the overhead and fails when it exceeds `NARWHAL_ROUTER_OVERHEAD_S`. Preflight's [`slo` gate](../cli/Check.md#the-slo-gate) fails when the calibrated first-token deadline plus the margin, the slowest prefill of the longest calibrated input and the overhead add up to more than `NARWHAL_TTFT_S`.

For several hosts, give each engine its own destination and add the standby router, the load balancer and the lease. Engines on different hosts can keep one set of ports:

```bash
NARWHAL_NODE_1_SSH=deploy@<host-a>
NARWHAL_NODE_2_SSH=deploy@<host-b>
NARWHAL_GPU_IDS=0
NARWHAL_STANDBY_ROUTER_SSH=deploy@<host-b>
NARWHAL_ROUTER_URL=http://<host-a address>:8000
NARWHAL_STANDBY_ROUTER_URL=http://<host-b address>:8000
NARWHAL_LOAD_BALANCER_URL=http://<host-a address>:8080
NARWHAL_LEASE_PATH=/srv/shared/narwhal/router.lease
NARWHAL_HAPROXY_IMAGE=haproxy@sha256:<digest>
```

Without GPU IDs, each engine takes every GPU on its host, with tensor parallelism set to that count. `NARWHAL_GPU_IDS=0` gives each engine the host's first GPU. The primary router runs on the host of `NARWHAL_ROUTER_SSH`, which defaults to the lowest-numbered engine's host, and HAProxy on the same host unless `NARWHAL_LOAD_BALANCER_SSH` names another. Leave `NARWHAL_STANDBY_ROUTER_SSH` and the pair's variables unset for one router on several hosts.

For KV over RDMA, also name each engine's HCA port and devices, and the GID index and RoCE mode. For example, with engine 1 on port 1 of `mlx5_0` and engine 2 on port 1 of `mlx5_1`, on an InfiniBand link:

```bash
NARWHAL_TRANSFER_TRANSPORT=ucx_rdma
NARWHAL_NODE_1_TRANSFER_NET_DEVICES=mlx5_0:1
NARWHAL_NODE_1_TRANSFER_DEVICES='["/dev/infiniband/uverbs0","/dev/infiniband/rdma_cm"]'
NARWHAL_NODE_2_TRANSFER_NET_DEVICES=mlx5_1:1
NARWHAL_NODE_2_TRANSFER_DEVICES='["/dev/infiniband/uverbs1","/dev/infiniband/rdma_cm"]'
NARWHAL_RDMA_GID_INDEX=0
NARWHAL_RDMA_ROCE_MODE=infiniband
```

[Fabric qualification inputs](../configuration/04-Deployment-Inputs.md#fabric-qualification-inputs) shows how to read each value from the host. Single-quote JSON values such as `NARWHAL_TRANSFER_DEVICES` and `NARWHAL_ENGINE_ARGS`: the shell reads `.env` when you load it, and strips unquoted double quotes, which leaves `[/dev/infiniband/uverbs0,/dev/infiniband/rdma_cm]`. [Needs verification: whether the engines need `/dev/infiniband/rdma_cm`]

## Checking the inputs with plan

Load the inputs and run `plan` with a new run directory:

```bash
set +x; set -a; . ./.env; set +a
narwhal deploy plan --run runs/deploy/first-deploy
```

`plan` names every missing or malformed input and exits 2 before it touches a host. It then logs in to each host and checks its systemd user manager, lingering and `systemd-run`, and Docker Compose on the primary router's host. It prints each stage and why `run` would run it, and exits 1 naming each host that failed a check. On a fleet with one router it also prints `Note: NARWHAL_STANDBY_ROUTER_SSH is unset, so the fleet runs one router with no standby, lease or load balancer: it has no router failover`.

## Running the deployment

```bash
narwhal deploy run --run runs/deploy/first-deploy
```

`run` prints each stage instance as it starts and passes, from `access` to `monitoring`, through the seven gates of [Deploying a fleet](../Deploy.md#deployment-sequence). The first run takes as long as hashing the checkpoint, starting the engines, measuring the fabric, profiling and calibrating, which can be hours. Keep other traffic off the engines until it ends.

When a stage fails, `run` prints its cause, the link to the gate section that covers it and its evidence directory:

```text
narwhal: deploy run: engine-check[engine-1] failed: node-1: check the image and plan exited 1; inspect ...
Recovery: https://athrael-soju.github.io/Narwhal/deploy/03-Validate-Engines/#preparing-checking-and-starting-each-engine
Evidence: runs/deploy/first-deploy/stages/engine-check/engine-1/attempt-1
```

Fix the cause and run the same command again. It resumes at the failed stage and skips every stage whose result still holds. To also run the capacity trial, add `--trial` with a trial file, as [Running the initial capacity trial](07-Serve-and-Measure.md#running-the-initial-capacity-trial) describes. The trial needs `NARWHAL_PREFIX_CACHING=false`.

## Checking the fleet with status and the dashboard

```bash
narwhal deploy status --run runs/deploy/first-deploy
```

`status` prints each stage's state, attempt and evidence directory, then checks every engine container, sidecar unit, router unit, HAProxy container, monitoring container and fabric route the run recorded. It exits 1 while the last run stopped at a failed stage, and 3 when a recorded process or route is gone or changed. Run `run` again in either case.

Clients send requests to `NARWHAL_LOAD_BALANCER_URL`, or to `NARWHAL_ROUTER_URL` on a fleet with one router. The router and HAProxy listen on the addresses in those URLs, and Prometheus and Grafana on the primary router's host at `127.0.0.1:9090` and `127.0.0.1:3000` by default, so the workstation needs one tunnel for each address, as [Tunnelling router, Prometheus, and Grafana to the workstation](07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) shows. Then open `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator`. The dashboard's [Deployment row](../observability/05-Dashboard.md#deployment) shows each stage of the last run, the fabric margins, the attestation generations and the supervisor restarts. Set `NARWHAL_ALERTMANAGER_URL` to deliver its alerts, as [Monitor](../operate/02-Monitor.md) describes.

Grafana and Prometheus keep their data across `down`, and the **Fleet** selector opens on the first fleet they hold. If the dashboard shows an earlier fleet, with its engines at N/A and its router down, pick yours in **Fleet**, or open `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator?var-fleet=<NARWHAL_FLEET_NAME>`.

## Stopping the fleet with down

```bash
narwhal deploy down --run runs/deploy/first-deploy
```

`down` stops the monitoring containers, HAProxy, the routers, the sidecars and the engine containers that the run started, each only while its recorded identity still matches. It keeps the monitoring volumes, the deployment directories on the hosts and the run directory, so a later `run` starts the fleet again from the stages that no longer hold.

After a host reboot, a new release, or to find a process's logs, see [After a host reboot](../Deploy.md#after-a-host-reboot), [Deploying a new release](../Deploy.md#deploying-a-new-release) and [Where each process logs](../Deploy.md#where-each-process-logs).

## Deploying on Kubernetes

On Kubernetes, a `NarwhalFleet` custom resource holds the deployment inputs, and a controller in the cluster runs the same stages for it as gate Jobs, with the same rerun rules and evidence. The CRD chart ships now. The controller, and with it the steps after the cluster's preparation, are being finished. [Needs verification: task 26]

### Preparing the cluster

The cluster admin provides:

- Kubernetes 1.30 or later, which the CRD chart requires, and `helm` and `kubectl` on the workstation.
- A GPU device plugin and an RDMA device plugin on every engine node, which hand each engine pod its GPUs and one HCA under the resource names the inputs give.
- A ReadWriteMany storage class for the state volume, and a volume that serves the staged checkpoint.
- The engine image, and a Narwhal image built from the repository's `Dockerfile`, in a registry the nodes pull from, each pinned by digest. Narwhal publishes no image.
- The model volume allow-lists in the Narwhal chart's values. `modelVolumeSources` allows only `persistentVolumeClaim` by default; a `hostPath`, `nfs` or `csi` volume also needs `modelHostPathPrefixes`, `modelNfsServers` or `modelCsiDrivers`, as the [CRD chart](https://github.com/athrael-soju/Narwhal/blob/main/deploy/helm/narwhal-crds/README.md) describes.

Install the CRD chart first, then the Narwhal chart:

```bash
helm install narwhal-crds deploy/helm/narwhal-crds
helm install narwhal deploy/helm/narwhal --namespace <namespace> --values config/helm/<fleet>.yaml
```

The second install reaches a serving fleet only with the controller. [Needs verification: task 26] Keep the values file in the ignored `config/helm/` directory.

### One node and several nodes

On one node, both engine pods and both router pods run there. On several nodes, each engine pod runs on the node the inputs name, and the routers never share a node. The values file is the only difference. [Needs verification: task 26]

1. Write the values file with the fleet's inputs. [Needs verification: task 26]
2. Install or upgrade the Narwhal chart with it, as above. [Needs verification: task 26]
3. Follow each stage with `kubectl get narwhalfleet --namespace <namespace>`. [Needs verification: task 26]
4. Reach Grafana and Prometheus with `kubectl port-forward`. [Needs verification: task 26]
5. Stop the fleet by deleting its `NarwhalFleet`, or with `helm uninstall`. [Needs verification: task 26]

[Deploying on Kubernetes](Kubernetes.md) covers what each stage does on a cluster today, through `narwhal deploy --target kubernetes`.
