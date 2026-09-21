# Deploy a fleet

From your management workstation, use the supplied inventory and private access to open shells on the router and GPU engine hosts. Follow the numbered steps through a real model completion, then validate the intended ingress and workload before admitting client traffic.

## Hosts and inputs

| Host | Work performed here | Supplied inputs |
| --- | --- | --- |
| Management workstation | Read the guides and inventory, open remote shells, retain the deployment record. | Checkout-local `.env`, `config/hosts.local.json`, private fleet JSON and verified `config/ssh.known_hosts`. |
| Router host | Install Narwhal, create the fleet config, profile and check engines, run the router. | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO targets. |
| Engine hosts | Inspect GPUs and artifacts, configure the fabric, launch vLLM and attestation sidecars. | Accelerator and TP shape, engine image, model checkpoint, launch configuration, fabric addresses and ports. |
| Load-client and observability hosts | Generate deployment traffic, scrape metrics and inspect the dashboard. | Ingress route and credentials, workload, scrape targets and dashboard access. |

The management workstation needs Git, Bash, Python 3.11 or newer and the supplied access tooling. Install Python 3.11 or newer with `venv`, Git, Make and curl on the remote hosts that run Narwhal commands. Engine hosts also need the declared accelerator driver, container runtime and transfer devices. The inventory assigns these roles to machines; a management workstation's local hardware describes that machine alone.

Narwhal serves one model and compatible KV layout across engines running vLLM with NIXL and effective `kv_both` behaviour. Every eligible producer must transfer KV to every eligible consumer, and one active controller owns the fleet.

Create a private deployment record before the first command. At each gate, record the host, full source revision, starting state, commands actually run, exit status and artifact locations. Report the first blocked gate before recovery; after reporting, clean only the processes and files created by that test. The [gate reference](#deployment-gates-and-recovery) maps each step to its inputs and recovery.

## 1. Open the management shells

On the management workstation, use the supplied private `.env`, host inventory, fleet JSON and verified SSH host-key file in the Narwhal checkout. `NARWHAL_HOSTS` selects `config/hosts.local.json`, `NARWHAL_FLEET` selects the engine fleet document, and `NARWHAL_SSH_KNOWN_HOSTS` selects `config/ssh.known_hosts`. The host inventory defines each physical machine once, names its access variables and assigns its router and engine roles. [Host inventory and SSH access](Configuration.md#host-inventory-and-ssh-access) defines the format.

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

The helper exports `.env.router` and one `.env.engine-<n>` for every assigned role, using the supplied fleet document and shared engine values with per-node overrides. It copies the fleet document for the router and records hashes for the source bundle and role files. Management credentials stay in the workstation environment. [Host environment files](Configuration.md#host-environment-files) lists the exported fields.

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

Run these read-only commands in each installed engine-role shell. The PCI vendor and device class select the NVIDIA or AMD inspection tool on that remote host:

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

Use these observations to replace `<accelerator-model>` in the router's working `config/fleet.local.json` under `hardware.accelerator`. Confirm that every participating engine has the declared accelerator model. The launch configuration determines `hardware.accelerators_per_engine` and `hardware.tensor_parallel`: compare them with the GPU devices allocated to one engine replica. A host's total GPU count describes available hardware; the selected replica allocation determines its TP shape. Record the observations with the deployment and retain the corrected fleet config for subsequent runs.

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
| [Management access](#1-open-the-management-shells) | Physical host IDs, assigned roles, access variable names and verified server keys. | Missing access input: fill the named environment field. Host-key rejection: verify the destination and fingerprint before updating its entry. Login failure: inspect the host's private log and check its credential and route. | Workstation `.env`, `config/hosts.local.json`, `config/ssh.known_hosts` and `runs/access-<id>/`. |
| [Host installation](#2-install-narwhal-on-the-remote-hosts) | Approved commit in the management checkout, shared launch fields, per-node overrides and host prerequisites. | Preparation failure: correct the named field or source revision. Transfer or checkout mismatch: inspect the prepared hashes and existing artifacts. Setup failure: repair the dependency error on that host and repeat the same run. | Workstation `runs/deployment-env/<run>/`; remote `~/Narwhal-deploy/<id>/`, role files, router fleet config and installation marker. |
| [Engine preparation](#3-inspect-each-engine-host) | Remote PCI vendor, observed GPU model and count, declared replica allocation and TP, image identity, model hash, paths and ports. | Device, artifact or listener mismatch: inspect the failing resource, restore the declared artifact or resolve resource ownership before launch. | Engine-host environment and private inventory. |
| [Fabric](#4-prepare-the-transfer-fabric) | Advertised peer addresses, interface, source address and expected KV rate. | Route, bandwidth or transfer failure: inspect the affected directed edge, transport devices, firewall, MTU and NIXL logs. | Private peer inventory, host-network configuration and fabric measurements. |
| [Fleet config and engine launch](#5-configure-the-fleet-and-launch-engines) | Engine IDs, roles, runtime contract, model, TP size and launcher inputs. | Config error or startup exit: correct the named field or inspect engine logs against the declared launch configuration. | Router `config/fleet.local.json`, router environment and private engine launcher. |
| [Attestation](#6-attest-each-engine-process) | Running engine identity, contract and sidecar bind address. | Identity endpoint failure or contract mismatch: verify the engine process and document, then restart its sidecar against that process. | Engine `runs/engine-attestation.production.json` and private inventory. |
| [Profiling](#7-profile-the-idle-engines) | Idle engine reservation, cache policy, workload lengths and concurrency. | Probe failure or fit rejection: inspect the named engine, measured range and sample file; repair the cause and retain a new sweep under a fresh profile path. | Router fleet config and profile/sample files under `runs/`. |
| [Preflight](#8-check-the-engine-and-kv-contract) | Current engine set, profiles and SLO targets. | Failed gate: use its engine, leg and budget to select the corresponding [fleet troubleshooting](Troubleshoot.md) check. | Router environment, fleet config and private preflight output. |
| [Router verification](#9-start-the-router-and-send-a-request) | Listener address, served model, engine count and opening split. | Bind error or failed readiness/completion: check listener ownership, URL address family and the engine or controller error in the router log. | Router environment, ignored fleet config and endpoint captures. |
| [Capacity acceptance](#10-validate-ingress-and-capacity) | Client workload, ingress route, SLO target and scrape targets. | SLO, accounting or scrape failure: reconcile client and router records, repair the identified bottleneck or target, then rerun the affected acceptance checks. | Private load-client and observability configuration, deployment evidence store. |
