# Deploy a fleet

From your management workstation, use the supplied inventory and private access to open shells on the router and GPU engine hosts. Follow the numbered steps through a real model completion, then validate the intended ingress and workload before admitting client traffic.

## Hosts and inputs

| Host | Work performed here | Supplied inputs |
| --- | --- | --- |
| Management workstation | Read the guides and inventory, open remote shells, retain the deployment record. | Checkout-local `.env`, private fleet JSON and verified `config/ssh.known_hosts`. |
| Router host | Install Narwhal, create the fleet config, profile and check engines, run the router. | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO targets. |
| Engine hosts | Inspect GPUs and artifacts, configure the fabric, launch vLLM and attestation sidecars. | Accelerator and TP shape, engine image, model checkpoint, launch configuration, fabric addresses and ports. |
| Load-client and observability hosts | Generate deployment traffic, scrape metrics and inspect the dashboard. | Ingress route and credentials, workload, scrape targets and dashboard access. |

The management workstation needs Git, Bash, Python 3.11 or newer and the supplied access tooling. Install Python 3.11 or newer with `venv`, Git, Make and curl on the remote hosts that run Narwhal commands. Engine hosts also need the declared accelerator driver, container runtime and transfer devices. The inventory assigns these roles to machines; a management workstation's local hardware describes that machine alone.

Narwhal serves one model and compatible KV layout across engines running vLLM with NIXL and effective `kv_both` behaviour. Every eligible producer must transfer KV to every eligible consumer, and one active controller owns the fleet.

Create a private deployment record before the first command. At each gate, record the host, full source revision, starting state, commands actually run, exit status and artifact locations. Report the first blocked gate before recovery; after reporting, clean only the processes and files created by that test. The [gate reference](#deployment-gates-and-recovery) maps each step to its inputs and recovery.

## 1. Open the management shells

On the management workstation, use the supplied private `.env`, fleet JSON and verified SSH host-key file in the Narwhal checkout. The environment identifies the fleet file through `NARWHAL_FLEET` and the host-key file through `NARWHAL_SSH_KNOWN_HOSTS`, normally `config/ssh.known_hosts`. Read these local inputs to identify the router, engine hosts and SSH logins.

A fresh clone receives those private files separately from the public source. Preserve existing deployment values and keep the private files with their checkout. Load `.env` from that checkout root with shell tracing disabled:

```bash
set +x
set -a
. ./.env
set +a
```

`NARWHAL_ROUTER_SSH` identifies the router's management login; each `NARWHAL_NODE_<n>_SSH` identifies the engine host with the same node number in the fleet inventory. Password authentication uses the corresponding `NARWHAL_ROUTER_SSH_PASSWORD` or `NARWHAL_NODE_<n>_SSH_PASSWORD` value. [SSH management access](Configuration.md#ssh-management-access) defines their storage and authentication options. Fabric and HTTP addresses retain their declared service roles.

Open the first engine shell from a management terminal. The following block selects password authentication when that host's password is loaded; otherwise OpenSSH uses its configured key or SSH agent.

```bash
: "${NARWHAL_NODE_1_SSH:?load the first engine SSH destination}"
: "${NARWHAL_SSH_KNOWN_HOSTS:?set the verified management host-key store}"
if test -n "${NARWHAL_NODE_1_SSH_PASSWORD:-}"; then
  sshpass -d 3 ssh \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o StrictHostKeyChecking=yes \
    -o GlobalKnownHostsFile=/dev/null \
    -o "UserKnownHostsFile=$NARWHAL_SSH_KNOWN_HOSTS" \
    "$NARWHAL_NODE_1_SSH" 3<<<"$NARWHAL_NODE_1_SSH_PASSWORD"
else
  ssh -o StrictHostKeyChecking=yes \
    -o GlobalKnownHostsFile=/dev/null \
    -o "UserKnownHostsFile=$NARWHAL_SSH_KNOWN_HOSTS" \
    "$NARWHAL_NODE_1_SSH"
fi
```

Open the designated router shell from a second management terminal after loading the same local `.env`:

```bash
: "${NARWHAL_ROUTER_SSH:?set the router SSH destination in the private environment}"
: "${NARWHAL_SSH_KNOWN_HOSTS:?set the verified management host-key store}"
if test -n "${NARWHAL_ROUTER_SSH_PASSWORD:-}"; then
  sshpass -d 3 ssh \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o StrictHostKeyChecking=yes \
    -o GlobalKnownHostsFile=/dev/null \
    -o "UserKnownHostsFile=$NARWHAL_SSH_KNOWN_HOSTS" \
    "$NARWHAL_ROUTER_SSH" 3<<<"$NARWHAL_ROUTER_SSH_PASSWORD"
else
  ssh -o StrictHostKeyChecking=yes \
    -o GlobalKnownHostsFile=/dev/null \
    -o "UserKnownHostsFile=$NARWHAL_SSH_KNOWN_HOSTS" \
    "$NARWHAL_ROUTER_SSH"
fi
```

OpenSSH verifies the server key for the selected management destination against the supplied `config/ssh.known_hosts` through these commands. A successful login with that verification completes this gate and permits installation on the selected host. Run `hostname` in each remote shell and record it as an observed label alongside the management destination and assigned role; image-provided hostnames can repeat across machines. When the inventory assigns router and engine roles to the same management destination, both shells reach the same host. Repeat the engine connection with its corresponding `NARWHAL_NODE_<n>_SSH` value as you reach the remaining hosts.

If these sources leave a required host role, management destination or credential unresolved, report the specific missing input and the sources inspected as the first blocked gate. For an unknown or changed host key, confirm the destination and fingerprint through the supplied private access source, then update the checkout-local host-key entry after verification and retry with strict checking enabled. For an authentication rejection, check the configured username and the selected password, identity file or SSH agent. For a connection timeout or refusal, check the management address, SSH port, bastion route and firewall. Retain the exact error in the private deployment record before recovery. Reports use host aliases, variable names and sanitised errors; credentials and raw environment contents stay private.

Step 2 exports the supplied deployment values into separate router and engine files and transfers them through the verified management connection. Management credentials remain in the workstation checkout.

## 2. Install Narwhal on the remote hosts

### Prepare the role environments on the workstation

From the management checkout with `.env` loaded, export the approved full commit SHA as `NARWHAL_DEPLOYMENT_REVISION`. Run the exporter with Python 3.11 or newer on the workstation; it uses the standard library. The router file receives the revision, fleet endpoint references, configured engine API credential and router settings. Each engine file receives the revision, launch settings, its endpoint values, fabric peer addresses and configured engine API credential. SSH destinations, passwords and host-key paths stay in the workstation's `.env`.

```bash
set +x
: "${NARWHAL_DEPLOYMENT_REVISION:?load the approved full commit SHA}"
: "${NARWHAL_FLEET:?load the supplied fleet path}"
umask 077
mkdir -p runs/deployment-env
NARWHAL_ENV_DIR=$(mktemp -d "$PWD/runs/deployment-env/transfer.XXXXXX")
export NARWHAL_ENV_DIR
export NARWHAL_ENGINE_NODE=1
python3 tools/prepare_host_env.py --role router \
  --fleet "$NARWHAL_FLEET" --out "$NARWHAL_ENV_DIR/.env.router"
python3 tools/prepare_host_env.py --role engine --node "$NARWHAL_ENGINE_NODE" \
  --fleet "$NARWHAL_FLEET" --out "$NARWHAL_ENV_DIR/.env.engine-$NARWHAL_ENGINE_NODE"
install -m 600 "$NARWHAL_FLEET" "$NARWHAL_ENV_DIR/fleet.local.json"
```

The exporter creates mode-0600 files and reports variable names for missing inputs. It reads shared `NARWHAL_ENGINE_IMAGE`, `NARWHAL_MODEL_DIR` and other launch values from the loaded environment. A `NARWHAL_NODE_<n>_<field>` override, such as `NARWHAL_NODE_2_MODEL_DIR`, supplies that host's value under the usual `NARWHAL_MODEL_DIR` name. [Host environment files](Configuration.md#host-environment-files) lists the fields and locations. Record the generated directory in the private deployment record. For each remaining engine, set `NARWHAL_ENGINE_NODE` to its inventory number and repeat the engine export into the same directory.

### Package the approved revision on the workstation

Create a Git bundle from the exact commit named by `NARWHAL_DEPLOYMENT_REVISION`. The temporary bare repository gives that commit a `deployment` branch inside the bundle. The bundle carries committed source objects; the role files carry the private deployment values separately. The management checkout supplies the approved commit, so its object database must contain that revision before packaging.

```bash
(
  set -e
  umask 077
  source_repo=$(git rev-parse --show-toplevel)
  test ! -e "$NARWHAL_ENV_DIR/source.bundle"
  test "$(git rev-parse --verify "$NARWHAL_DEPLOYMENT_REVISION^{commit}")" = "$NARWHAL_DEPLOYMENT_REVISION"
  bundle_repo=$(mktemp -d "$NARWHAL_ENV_DIR/source.XXXXXX")
  trap 'rm -rf "$bundle_repo"' EXIT
  git init --bare "$bundle_repo"
  git -C "$bundle_repo" fetch --no-tags "$source_repo" \
    "$NARWHAL_DEPLOYMENT_REVISION:refs/heads/deployment"
  git -C "$bundle_repo" bundle create "$NARWHAL_ENV_DIR/source.bundle" \
    refs/heads/deployment
  git -C "$bundle_repo" bundle verify "$NARWHAL_ENV_DIR/source.bundle"
  git clone --branch deployment "$NARWHAL_ENV_DIR/source.bundle" "$bundle_repo/verify"
  test "$(git -C "$bundle_repo/verify" rev-parse HEAD)" = "$NARWHAL_DEPLOYMENT_REVISION"
)
```

Successful verification and the fresh local clone establish that the bundle supplies the approved revision. If revision lookup or bundle verification fails, report that gate on the management workstation and recover the approved commit from its supplied source before repeating preparation in a fresh transfer directory. Keep the bundle with the private deployment artifacts.

### Transfer the source bundle from the workstation

Run these commands in the management terminal that holds `NARWHAL_ENV_DIR` and the loaded private `.env`. `narwhal_send` streams each selected file through verified SSH, using a separate file descriptor for password authentication. The receiver creates mode-0600 files and rejects an existing destination.

```bash
set +x
narwhal_send() {
  local destination=$1 password=$2 input=$3 receiver=$4
  : "${destination:?load the management destination}"
  : "${NARWHAL_SSH_KNOWN_HOSTS:?load the verified host-key store}"
  local options=(
    -o StrictHostKeyChecking=yes
    -o GlobalKnownHostsFile=/dev/null
    -o "UserKnownHostsFile=$NARWHAL_SSH_KNOWN_HOSTS"
  )
  if test -n "$password"; then
    sshpass -d 3 ssh "${options[@]}" \
      -o PreferredAuthentications=password -o PubkeyAuthentication=no \
      "$destination" "$receiver" 3<<<"$password" < "$input"
  else
    ssh "${options[@]}" "$destination" "$receiver" < "$input"
  fi
}
```

Send the bundle to the router destination, then repeat for each distinct engine destination. When router and engine roles share a destination, transfer the bundle once to that host.

```bash
narwhal_send "$NARWHAL_ROUTER_SSH" "${NARWHAL_ROUTER_SSH_PASSWORD:-}" \
  "$NARWHAL_ENV_DIR/source.bundle" \
  '(umask 077; set -C; cat > narwhal-source.bundle)'
```

For each engine, set its inventory number below. Matching router and engine destinations share the router's transferred bundle; distinct destinations receive a copy:

```bash
export NARWHAL_ENGINE_NODE=1
engine_destination=NARWHAL_NODE_${NARWHAL_ENGINE_NODE}_SSH
engine_password=NARWHAL_NODE_${NARWHAL_ENGINE_NODE}_SSH_PASSWORD
if test "${!engine_destination}" != "$NARWHAL_ROUTER_SSH"; then
  narwhal_send "${!engine_destination}" "${!engine_password:-}" \
    "$NARWHAL_ENV_DIR/source.bundle" \
    '(umask 077; set -C; cat > narwhal-source.bundle)'
fi
```

### Clone once on each distinct remote host

In each verified remote shell, clone the transferred bundle into a fresh checkout under the login home directory:

```bash
cd ~
git clone --branch deployment narwhal-source.bundle Narwhal
```

When router and engine roles share a host, use this checkout for both roles. An existing `~/Narwhal` or `~/narwhal-source.bundle` owned by another deployment requires a separate path; substitute the chosen paths in the transfer and shell commands. Record the bundle and checkout paths with their host in the private deployment record. The source bundle supplies the `deployment` branch at the approved revision, and the role files supply the SHA for the final comparison before installation.

### Transfer the role files from the workstation

In the management terminal containing `narwhal_send` and the loaded environment, transfer the router configuration:

```bash
(
  set -e
  narwhal_send "$NARWHAL_ROUTER_SSH" "${NARWHAL_ROUTER_SSH_PASSWORD:-}" \
    "$NARWHAL_ENV_DIR/.env.router" \
    '(cd Narwhal && umask 077 && set -C && cat > .env.router)'
  narwhal_send "$NARWHAL_ROUTER_SSH" "${NARWHAL_ROUTER_SSH_PASSWORD:-}" \
    "$NARWHAL_ENV_DIR/fleet.local.json" \
    '(cd Narwhal && umask 077 && set -C && cat > config/fleet.local.json)'
)
```

Transfer the first engine file with the block below. For each additional engine, change `NARWHAL_ENGINE_NODE` to its inventory number and repeat this block.

```bash
export NARWHAL_ENGINE_NODE=1
engine_destination=NARWHAL_NODE_${NARWHAL_ENGINE_NODE}_SSH
engine_password=NARWHAL_NODE_${NARWHAL_ENGINE_NODE}_SSH_PASSWORD
narwhal_send "${!engine_destination}" "${!engine_password:-}" \
  "$NARWHAL_ENV_DIR/.env.engine-$NARWHAL_ENGINE_NODE" \
  "(cd Narwhal && umask 077 && set -C && cat > .env.engine-$NARWHAL_ENGINE_NODE)"
```

A transfer rejection identifies the destination or access error; record the first failure, then inspect ownership and completeness before choosing a fresh path or removing a partial file created by this transfer. Preserve configuration belonging to an existing deployment.

### Load the role files and install

In the router shell:

```bash
cd ~/Narwhal
set +x
. ./.env.router
```

In the first engine shell, load its numbered file. Use the corresponding number in each remaining engine shell:

```bash
cd ~/Narwhal
set +x
. ./.env.engine-1
```

Both files export their selected values when sourced. Run the following commands once per distinct checkout, using either role shell when a host serves both roles:

```bash
: "${NARWHAL_DEPLOYMENT_REVISION:?load the role environment}"
git switch --detach "$NARWHAL_DEPLOYMENT_REVISION"
test "$(git rev-parse HEAD)" = "$NARWHAL_DEPLOYMENT_REVISION"
make setup
source .venv/bin/activate
```

Run later commands from this checkout root with the relevant role file loaded. Activate `.venv` with `source .venv/bin/activate` in additional shells on the same host. If checkout or SHA comparison fails, compare the loaded role-file revision with `git rev-parse deployment` and confirm that the bundle and role files came from the same preparation directory. For setup failures, use the error to repair Python, `venv`, package access or permissions on that host. Retain the source bundle, role files and fleet config with the deployment's private artifacts.

## 3. Inspect each engine host

Run read-only checks in the selected engine-host shell. Use `nvidia-smi` for an NVIDIA host or `rocminfo` for an AMD ROCm host, according to the supplied inventory, and compare the visible accelerators and device count with its declared tensor-parallel shape. A driver error or device mismatch requires inspection of that engine host's driver, device exposure and allocation before launch. Inspect the host-local image, model, devices, listeners, and routes next.

Take the engine image, model and run paths, model-config hash, fabric interface and ports from this host's supplied deployment values; [.env.example](https://github.com/athrael-soju/Narwhal/blob/main/.env.example) names these inputs. The router's ignored fleet document holds one entry per engine; the private inventory holds each engine's fabric address and peers. Replace angle-bracket placeholders in this guide from that inventory before executing commands.

On each host, check the declared artifacts and local resources before creating a new engine process. For a Docker image identified by its image ID, the following checks fail on a missing or different image and model config:

```bash
test "$(docker image inspect "$NARWHAL_ENGINE_IMAGE" --format '{{.Id}}')" = "$NARWHAL_ENGINE_IMAGE"
test "$(sha256sum "$NARWHAL_MODEL_DIR/config.json" | cut -d' ' -f1)" = "$NARWHAL_MODEL_CONFIG_SHA256"
test -d "$NARWHAL_RUN_DIR"
ip address show dev "$NARWHAL_FABRIC_INTERFACE"
ss -ltnp
```

The Docker equality check applies when `NARWHAL_ENGINE_IMAGE` is an image ID; a registry digest requires the container runtime's resolved-digest inspection. The `config.json` hash identifies the model configuration; retain the checkpoint revision or weights manifest separately. Confirm that the accelerator and transfer devices named by the launch configuration exist and are available to the engine container. Inspect `ss` for owners of the planned engine HTTP, attestation, NIXL side-channel, and transport ports. Treat a listener owned by another deployment as a stop condition and resolve ownership before launch.

## 4. Prepare the transfer fabric

On each engine host, inspect the fabric and measure directed bandwidth using its supplied peer inventory before launching engines. NIXL advertises one address from each engine to every peer. Site automation must establish these conditions:

- The kernel route to each advertised peer selects the RDMA-capable interface and its advertised source address.
- Every engine can open the NIXL side channel and register memory through the selected transport.
- Directed bandwidth between eligible peers sustains the deployment's peak KV handoff rate.
- Every node uses uniform firewall, MTU, GID, device and port configuration.

Use Kubernetes, Ansible, Terraform or site tooling to provision hosts, distribute artifacts, configure persistent routes and measure directed fabric edges, recording the route and bandwidth checks with the deployment evidence.

For every peer address, run `ip -6 route get <peer-address> from <this-node-address>` for IPv6 or `ip -4 route get <peer-address> from <this-node-address>` for IPv4. The result must select the intended fabric interface and this node's advertised source address. An address or route mismatch sends the operator back to inventory or host-network configuration before vLLM starts.

## 5. Configure the fleet and launch engines

On the router host, edit the transferred `config/fleet.local.json` using the supplied inventory. Set the model, engine IDs, opening roles, engine and attestation URLs, SLOs and a fresh profile path under `runs/`, then add the complete production `engine_contract` defined by the [configuration reference](Configuration.md#engine-contract). [Node URL references](Configuration.md#node-urls-from-the-environment) resolve endpoints from `.env.router`; `engine.engine_api_key_env` selects the exported engine credential. The exported `NARWHAL_FLEET=config/fleet.local.json` selects this file for observability. Keep this ignored config with its profile and deployment load evidence in private storage, and replace site addresses before sharing an extract.

On the engine hosts, use the site's deployment system to provision the declared image, model mounts, accelerator devices, tensor-parallel size and vLLM/NIXL launch configuration with effective `kv_both` behaviour. Use the fabric configuration from step 4 and confirm each engine serves the declared model and HTTP port before starting its sidecar. An engine startup failure requires its process logs and the image, device, model or fabric check implicated by the error. Preserve existing processes and resolve listener ownership before starting replacements.

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
| [Management access](#1-open-the-management-shells) | Router and engine SSH destinations, login usernames, credentials and verified server keys. | Unresolved access input: inspect the supplied environment and referenced inventory, then identify the missing field. Host-key rejection: verify the destination and fingerprint through the private access source before updating its entry. Login failure: check the credential and route. | Checkout-local `.env`, fleet JSON and `config/ssh.known_hosts`. |
| [Host installation](#2-install-narwhal-on-the-remote-hosts) | Approved commit in the management object database, shared launch fields, per-node overrides and host prerequisites. | Revision or bundle failure: recover the approved commit from its supplied source and prepare a verified bundle. Export failure: fill the named field in the workstation environment. Transfer rejection: inspect access, destination ownership and partial files. Checkout mismatch: compare the bundle branch and role-file SHA. Setup failure: check Python, `venv` and permissions. | Workstation `.env` and `runs/deployment-env/`; remote `narwhal-source.bundle`, `.env.router`, `.env.engine-<n>` and router `config/fleet.local.json`. |
| [Engine preparation](#3-inspect-each-engine-host) | Accelerator shape, image identity, model hash, run path, interfaces and ports. | Device, artifact or listener mismatch: inspect the failing resource, restore the declared artifact or resolve resource ownership before launch. | Engine-host environment and private inventory. |
| [Fabric](#4-prepare-the-transfer-fabric) | Advertised peer addresses, interface, source address and expected KV rate. | Route, bandwidth or transfer failure: inspect the affected directed edge, transport devices, firewall, MTU and NIXL logs. | Private peer inventory, host-network configuration and fabric measurements. |
| [Fleet config and engine launch](#5-configure-the-fleet-and-launch-engines) | Engine IDs, roles, runtime contract, model, TP size and launcher inputs. | Config error or startup exit: correct the named field or inspect engine logs against the declared launch configuration. | Router `config/fleet.local.json`, router environment and private engine launcher. |
| [Attestation](#6-attest-each-engine-process) | Running engine identity, contract and sidecar bind address. | Identity endpoint failure or contract mismatch: verify the engine process and document, then restart its sidecar against that process. | Engine `runs/engine-attestation.production.json` and private inventory. |
| [Profiling](#7-profile-the-idle-engines) | Idle engine reservation, cache policy, workload lengths and concurrency. | Probe failure or fit rejection: inspect the named engine, measured range and sample file; repair the cause and retain a new sweep under a fresh profile path. | Router fleet config and profile/sample files under `runs/`. |
| [Preflight](#8-check-the-engine-and-kv-contract) | Current engine set, profiles and SLO targets. | Failed gate: use its engine, leg and budget to select the corresponding [fleet troubleshooting](Troubleshoot.md) check. | Router environment, fleet config and private preflight output. |
| [Router verification](#9-start-the-router-and-send-a-request) | Listener address, served model, engine count and opening split. | Bind error or failed readiness/completion: check listener ownership, URL address family and the engine or controller error in the router log. | Router environment, ignored fleet config and endpoint captures. |
| [Capacity acceptance](#10-validate-ingress-and-capacity) | Client workload, ingress route, SLO target and scrape targets. | SLO, accounting or scrape failure: reconcile client and router records, repair the identified bottleneck or target, then rerun the affected acceptance checks. | Private load-client and observability configuration, deployment evidence store. |
