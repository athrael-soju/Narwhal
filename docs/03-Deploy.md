# Deploy Narwhal

Start from a fresh checkout on your management workstation, use the supplied private access to reach the inventory's router and engine hosts, and run each command in the host shell named below. Connect one model fleet to a Narwhal router on idle engines, then add public ingress after the validation sequence passes.

## Requirements

- Git and the supplied management access tooling on the management workstation.
- Linux with Python 3.11 or newer, its `venv` module, Git, Make, and curl on the router and engine hosts where the CLI tools are installed.
- A supplied node inventory and authenticated management access to the router and engine hosts.
- One model and KV layout across every engine.
- A transfer fabric reachable by every engine.
- Engines that can produce and consume KV for every eligible peer.
- One active controller for the fleet.

Operators provision one hardware and tensor-parallel shape, launch vLLM with NIXL and effective `kv_both` behaviour across a compatible model and KV layout, then bind that running contract to the fleet config, attestation documents and profiles measured from the deployed image and launch configuration.

## Use the provided fleet access

Read the deployment handoff for the supplied private inventory and access locations before checking host prerequisites. A fresh clone supplies source and documentation; the handoff supplies the deployment values separately. If the named inventory, credential source or management route is unavailable, record fleet access as the first blocked gate and resolve it before installation.

On the management workstation, load the supplied management credentials through the private environment or access tooling named in the deployment handoff. Read the supplied inventory there to identify the router host, engine hosts, management destinations and host roles. Use its connection method, such as an SSH alias or a bastion route, to open the remote shells. The management route opens a shell on a host; each engine's HTTP URL reaches vLLM, its attestation URL reaches the sidecar, and its advertised fabric address carries NIXL traffic. Keep those endpoints distinct in the private inventory.

Open a management shell on the designated router host and one selected engine host. Run `hostname` in each shell and match the result to the supplied inventory before installing Narwhal. Check accelerator availability in the engine-host shell in step 1; hardware detected on the management workstation describes that workstation alone. Resolve credential, route, or host-mapping failures against the supplied access configuration before continuing to the remaining engine hosts.

Keep management credentials in the workstation's private access tooling. Supply engine launch values on each engine host and engine URLs and API credentials in each router shell that runs Narwhal, using the site's environment injection or a mode-600, Git-ignored `.env` in that host's checkout. [Environment variables](07-Configuration.md#environment-variables) describes how to populate and source the file. Use the supplied values; `.env.example` documents names and defaults. Keep private inventory, environment contents and raw host output in the deployment's private record.

## Router host: install Narwhal

Select the full commit SHA approved for this deployment and export it as `NARWHAL_DEPLOYMENT_REVISION` on the router and every engine host. Clone Narwhal on the router host, check out that commit, and install the CLI tools.

```bash
: "${NARWHAL_DEPLOYMENT_REVISION:?set the approved commit SHA}"
git clone https://github.com/athrael-soju/Narwhal
cd Narwhal
git switch --detach "$NARWHAL_DEPLOYMENT_REVISION"
test "$(git rev-parse HEAD)" = "$NARWHAL_DEPLOYMENT_REVISION"
make setup
source .venv/bin/activate
```

Run later router commands from this checkout root with the environment active. Repeat the clone, checkout, revision check, and setup in the selected engine-host shell before step 1. Repeat this setup on the remaining engine hosts after the first passes. If setup fails, use its error to repair that host's Python, `venv`, package access or checkout permissions, then rerun setup there.

In each router and engine shell, load that host's supplied deployment values before using them. A `.env` deployment uses the following commands from the host's checkout root; environment injection supplies the same values directly.

```bash
set -a
. ./.env
set +a
```

## 1. Prepare each engine host

Run read-only checks in the selected engine-host shell. Use `nvidia-smi` for an NVIDIA host or `rocminfo` for an AMD ROCm host, according to the supplied inventory, and compare the visible accelerators and device count with its declared tensor-parallel shape. A driver error or device mismatch requires inspection of that engine host's driver, device exposure and allocation before launch. Inspect the host-local image, model, devices, listeners, and routes next.

Before launching vLLM, identify each engine host, its opening role, the immutable engine image and model checkpoint, the address NIXL will advertise, the peer addresses, and the ports the engine and attestation sidecar will bind. Record the intended accelerator and tensor-parallel shape. Pin one Narwhal revision for the router and sidecars, and record each engine's process generation separately.

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

For every peer address, run `ip -6 route get <peer-address> from <this-node-address>` for IPv6 or `ip -4 route get <peer-address> from <this-node-address>` for IPv4. The result must select the intended fabric interface and this node's advertised source address. An address or route mismatch sends the operator back to inventory or host-network configuration before vLLM starts. The [fabric checks](#7-check-the-fabric) later measure directed bandwidth and exercise real KV transfers.

## 2. Create the fleet config

On the router host, create the working fleet document from the supplied inventory. Preserve an existing `config/fleet.local.json`; create the starter only when that path is available.

```bash
(set -o noclobber; .venv/bin/narwhal-check --print-example-config > config/fleet.local.json)
```

Replace the starter's example engines with one entry per deployed engine from the inventory. Set the model, engine IDs, opening roles, engine and attestation URLs, SLOs and a fresh profile path under `runs/`, then add the complete production `engine_contract` defined by the [configuration reference](07-Configuration.md#engine-contract). [Node URL references](07-Configuration.md#node-urls-from-the-environment) resolve endpoints from the router shell's private environment; `engine.engine_api_key_env` selects the supplied engine credential. Set `NARWHAL_FLEET=config/fleet.local.json` in that environment for observability. Keep this ignored config with its profile and deployment load evidence in private storage, and replace site addresses before sharing an extract.

On the engine hosts, use the site's deployment system to provision the declared image, model mounts, accelerator devices, tensor-parallel size and vLLM/NIXL launch configuration with effective `kv_both` behaviour. Establish the [host fabric](#7-check-the-fabric) before launch, then confirm each engine serves the declared model and HTTP port before starting its sidecar. An engine startup failure requires its process logs and the image, device, model or fabric check implicated by the error. Preserve existing processes and resolve listener ownership before starting replacements.

The [configuration reference](07-Configuration.md) defines every field and default.

## 3. Attest the running engines

On each engine host, create the attestation document under ignored `runs/`, preserving any document from an earlier deployment.

```bash
mkdir -p runs
(set -o noclobber; cat config/engine-attestation.example.json > runs/engine-attestation.production.json)
```

Populate `contract` from that host's deployed image, packages, model and launch configuration, and match those values to the router fleet config's `engine_contract`. Then launch the sidecar in that engine-host shell, replacing the placeholders with its engine HTTP URL, control-network bind address and attestation port from the private inventory.

```bash
.venv/bin/narwhal-attest \
  --document runs/engine-attestation.production.json \
  --engine-base <engine-http-url> \
  --host <node-serving-address> \
  --port <attestation-port>
```

Start the sidecar after the engine's `/health`, `/version` and `process_start_time_seconds` metric identify the running process. Point `attestation_url` at its `/v1/attestation` route, expose `/health` and `/v1/attestation` through the trusted control network, capture both responses, and restart the sidecar with the engine process. Validate the input and digested response against the [attestation document contract](07-Configuration.md#attestation-document).

## 4. Profile the fleet

On the router host, run the profiler with its private engine endpoints and credentials loaded while the real engines are reserved and idle. Warm the model and disable prefix caching under the [measurement conditions](06-Measure.md#1-calibrate-slos) before collecting the sweep.

```bash
.venv/bin/narwhal-profile \
  --fleet config/fleet.local.json \
  --decode-input-lens 512,4096,8192 \
  --decode-concurrency 1,4,16,48
```

The profiler measures one-token prefill latency across input lengths; for decode, it varies prompt length and concurrency, then fits observed token intervals against active request counts plus estimated resident KV while the complete cohort decodes.

Choose decode input lengths and concurrency values that cover expected traffic under the [measurement conditions](06-Measure.md#1-calibrate-slos), and retain the sample sidecar because the controller holds any role change whose projected decode point falls outside that measured range.

At startup, Narwhal compares the profile rows with the configured engine IDs and halts with the differing IDs when the sets diverge.

Set `slo.ttft_s` and `slo.tpot_s` from light-load measurements on this engine shape, keeping TPOT above the measured per-token floor.

## 5. Prove the contract

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

## 6. Start and verify the router

On the designated router host, launch Narwhal from its checkout with the private environment loaded. Bind the listener to the trusted control network according to the supplied inventory.

```bash
.venv/bin/narwhal-serve \
  --fleet config/fleet.local.json \
  --host 0.0.0.0 \
  --port 8000
```

Open another management shell on the same router host for these checks; `localhost` resolves within that remote shell. Replace `<served-model>` with the model in the fleet config. If the listener uses a different address or port, substitute that URL in every probe.

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready
curl -s http://localhost:8000/narwhal/state | python3 -m json.tool
curl -s http://localhost:8000/metrics | head
curl -s http://localhost:8000/v1/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<served-model>","prompt":"Return one sentence about narwhals.","max_tokens":32}'
```

Use one concrete router URL for these probes, the Prometheus target and the deployment load client, confirming that the process listens on its selected address family because an IPv4 `127.0.0.1` client reaches a different listener from an IPv6-only `::` socket.

Before load, retain `/health` with process liveness and cached instance counts, `/ready` with admission state, `/narwhal/state` with every configured engine and the current split, and `/metrics`; then record one successful completion that increments `served`.

Keep the default anonymous listener on a trusted network until ingress supplies TLS, public authentication, WAF policy, request limits and model routing.

## 7. Check the fabric

On each engine host, inspect the fabric and measure directed bandwidth using its supplied peer inventory before the step 2 launch. NIXL advertises one address from each engine to every peer. Site automation must establish these conditions:

- The kernel route to each advertised peer selects the RDMA-capable interface and its advertised source address.
- Every engine can open the NIXL side channel and register memory through the selected transport.
- Directed bandwidth between eligible peers sustains the deployment's peak KV handoff rate.
- Every node uses uniform firewall, MTU, GID, device and port configuration.

Use Kubernetes, Ansible, Terraform or site tooling to provision hosts, distribute artifacts, configure persistent routes and measure directed fabric edges, recording the route and bandwidth checks with the deployment evidence.

After the engines and sidecars start, run the following command on the router host to exercise one role-permitted transfer per ring edge:

```bash
.venv/bin/narwhal-check --fleet config/fleet.local.json --ring
```

Run the default mesh before first ingress or after changing the topology; each passing transfer confirms the vLLM/NIXL path selected by its producer and consumer processes.

## 8. Validate production capacity

Use the deployment's designated load-client and observability hosts from the private inventory for workload and monitoring commands; keep Narwhal preflight on the router host. Close the deployment in this order:

1. Create the deployment identifier and assemble the [deployment evidence set](06-Measure.md#2-validate-the-deployment-under-load).
2. Run the default preflight mesh against those processes and retain its output.
3. Configure Prometheus with the same router URL used by the deployment load client, then verify the router and every engine target.
4. Run the [deployment workload](06-Measure.md#2-validate-the-deployment-under-load) through its intended ingress and attach its artifacts to the deployment identifier.
5. Drain resident work, reconcile every offer to its client and router terminal classes, query the dashboard's engine, request, token, role and pool-load series through Grafana's provisioned data source, then run the post-load KV ring.

[Measure a fleet](06-Measure.md) defines timing boundaries, rate selection and artifact contents; [Set up observability](10-Observability.md) defines scrape and dashboard checks.

Continue with [Operate Narwhal](04-Operate.md) before placing the router behind ingress.

## Deployment gates and recovery

Record the host, full Narwhal revision, starting state, commands actually executed and their exit status at each gate in the private deployment record. At the first blocked gate, capture the error and relevant artifact paths before attempting the recovery below. A deployment test reports that first blocked gate, or the completed acceptance checks, before cleaning only the processes and files it created. Share sanitised extracts with stable host aliases and replace private addresses, paths and credential values.

| Step and documentation | Required knowledge | Likely failure and recovery | Private value location |
| --- | --- | --- | --- |
| [Management access](#use-the-provided-fleet-access) | Router and engine host roles, login method and destination. | Login failure or hostname mismatch: check the supplied credential, bastion route and inventory mapping. | Workstation access tooling and supplied inventory. |
| [Host installation](#router-host-install-narwhal) | Approved revision and host prerequisites. | Revision lookup or setup failure: check source access, revision availability, Python, `venv` and permissions on the named host. | Deployment revision in the private record; per-host `.env` or injected environment. |
| [Engine preparation](#1-prepare-each-engine-host) | Accelerator shape, image identity, model hash, run path, interfaces and ports. | Device, artifact or listener mismatch: inspect the failing resource, restore the declared artifact or resolve resource ownership before launch. | Engine-host environment and private inventory. |
| [Fleet config and engine launch](#2-create-the-fleet-config) | Engine IDs, roles, runtime contract, model, TP size and launcher inputs. | Config error or startup exit: correct the named field or inspect engine logs against the declared launch configuration. | Router `config/fleet.local.json`, router environment and private engine launcher. |
| [Attestation](#3-attest-the-running-engines) | Running engine identity, contract and sidecar bind address. | Identity endpoint failure or contract mismatch: verify the engine process and document, then restart its sidecar against that process. | Engine `runs/engine-attestation.production.json` and private inventory. |
| [Profiling](#4-profile-the-fleet) | Idle engine reservation, cache policy, workload lengths and concurrency. | Probe failure or fit rejection: inspect the named engine, measured range and sample file; repair the cause and retain a new sweep under a fresh profile path. | Router fleet config and profile/sample files under `runs/`. |
| [Preflight](#5-prove-the-contract) | Current engine set, profiles and SLO targets. | Failed gate: use its engine, leg and budget to select the corresponding [fleet troubleshooting](05-Troubleshoot.md) check. | Router environment, fleet config and private preflight output. |
| [Router verification](#6-start-and-verify-the-router) | Listener address, served model, engine count and opening split. | Bind error or failed readiness/completion: check listener ownership, URL address family and the engine or controller error in the router log. | Router environment, ignored fleet config and endpoint captures. |
| [Fabric](#7-check-the-fabric) | Advertised peer addresses, interface, source address and expected KV rate. | Route, bandwidth or transfer failure: inspect the affected directed edge, transport devices, firewall, MTU and NIXL logs. | Private peer inventory, host-network configuration and fabric measurements. |
| [Capacity acceptance](#8-validate-production-capacity) | Client workload, ingress route, SLO target and scrape targets. | SLO, accounting or scrape failure: reconcile client and router records, repair the identified bottleneck or target, then rerun the affected acceptance checks. | Private load-client and observability configuration, deployment evidence store. |
