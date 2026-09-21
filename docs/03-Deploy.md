# Deploy Narwhal

Connect one model fleet to a Narwhal router on idle engines, then add public ingress after the validation sequence passes.

## Requirements

- Linux with Python 3.11 or newer, its `venv` module, Git, Make, and curl on the router host.
- One model and KV layout across every engine.
- A transfer fabric reachable by every engine.
- Engines that can produce and consume KV for every eligible peer.
- One active controller for the fleet.

Operators provision one hardware and tensor-parallel shape, launch vLLM with NIXL and effective `kv_both` behaviour across a compatible model and KV layout, then bind that running contract to the fleet config, attestation documents and profiles measured from the deployed image and launch configuration.

## 1. Install Narwhal

```bash
git clone https://github.com/athrael-soju/Narwhal
cd Narwhal
make setup
source .venv/bin/activate
```

Run the documentation's commands from the checkout root with this environment active.

## 2. Create the fleet config

Print the starter fleet document into a local file.

```bash
.venv/bin/narwhal-check --print-example-config > config/fleet.local.json
```

The starter document supplies the fleet shape and opening roles. Set the model, engine IDs, engine and attestation URLs, SLOs and profile path, then add the complete production `engine_contract` defined by the [configuration reference](07-Configuration.md#engine-contract). Use a `fleet.` filename because the repository ignores `config/fleet.*.json`. Keep the config with its profile and deployment load evidence, and replace site addresses before sharing it.

Provision the engines through the site's deployment system, establish the [host fabric](#7-check-the-fabric), launch the declared processes and then collect their profiles.

The [configuration reference](07-Configuration.md) defines every field and default.

## 3. Attest the running engines

Copy the tracked attestation document, populate `contract` from the deployed image, packages, model and launch configuration, and match those values to the fleet config's `engine_contract`.

```bash
cp config/engine-attestation.example.json runs/engine-attestation.production.json
.venv/bin/narwhal-attest \
  --document runs/engine-attestation.production.json \
  --engine-base http://127.0.0.1:8002 \
  --host <node-serving-address> \
  --port 8010
```

Start the sidecar after the engine's `/health`, `/version` and `process_start_time_seconds` metric identify the running process. Point `attestation_url` at its `/v1/attestation` route, expose `/health` and `/v1/attestation` through the trusted control network, capture both responses, and restart the sidecar with the engine process. Validate the input and digested response against the [attestation document contract](07-Configuration.md#attestation-document).

## 4. Profile the fleet

Run the profiler while the engines are idle.

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

```bash
.venv/bin/narwhal-serve \
  --fleet config/fleet.local.json \
  --host 0.0.0.0 \
  --port 8000
```

Use another terminal for the checks.

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

NIXL advertises one address from each engine to every peer. Before engine launch, site automation must establish these conditions:

- The kernel route to each advertised peer selects the RDMA-capable interface and its advertised source address.
- Every engine can open the NIXL side channel and register memory through the selected transport.
- Directed bandwidth between eligible peers sustains the deployment's peak KV handoff rate.
- Every node uses uniform firewall, MTU, GID, device and port configuration.

Use Kubernetes, Ansible, Terraform or site tooling to provision hosts, distribute artifacts, configure persistent routes and measure directed fabric edges, recording the route and bandwidth checks with the deployment evidence.

After the engines start, exercise one role-permitted transfer per ring edge:

```bash
.venv/bin/narwhal-check --fleet config/fleet.local.json --ring
```

Run the default mesh before first ingress or after changing the topology; each passing transfer confirms the vLLM/NIXL path selected by its producer and consumer processes.

## 8. Validate production capacity

Close the deployment in this order:

1. Create the deployment identifier and assemble the [deployment evidence set](06-Measure.md#2-validate-the-deployment-under-load).
2. Run the default preflight mesh against those processes and retain its output.
3. Configure Prometheus with the same router URL used by the deployment load client, then verify the router and every engine target.
4. Run the [deployment workload](06-Measure.md#2-validate-the-deployment-under-load) through its intended ingress and attach its artifacts to the deployment identifier.
5. Drain resident work, reconcile every offer to its client and router terminal classes, query the dashboard's engine, request, token, role and pool-load series through Grafana's provisioned data source, then run the post-load KV ring.

[Measure a fleet](06-Measure.md) defines timing boundaries, rate selection and artifact contents; [Set up observability](10-Observability.md) defines scrape and dashboard checks.

Continue with [Operate Narwhal](04-Operate.md) before placing the router behind ingress.
