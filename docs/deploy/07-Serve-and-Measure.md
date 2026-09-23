# Gate G: Start the service and validate capacity through the private path

## Start and locally verify the router

On the router host:

```bash
.venv/bin/narwhal-serve \
  --fleet runs/deployment/fleet.json \
  --host 0.0.0.0 \
  --port 8000
```

Bind only to the trusted control network selected during discovery.

From another shell on the router host:

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/ready
curl -fsS http://localhost:8000/narwhal/state | python3 -m json.tool
curl -fsS http://localhost:8000/metrics
curl -fsS http://localhost:8000/v1/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<served-model>","prompt":"Return one sentence about narwhals.","max_tokens":32}'
```

Replace `<served-model>` with the fleet model. If the router listens elsewhere, change every URL consistently. Check listener address family: `127.0.0.1` cannot reach a socket bound only to IPv6 `::`.

Before load, retain `/health` including liveness and cached instance counts, `/ready` including admission state, `/narwhal/state` including engine inventory and role split, `/metrics`, and one successful completion that increments `served`.

Anonymous router access belongs only on a trusted network. Public ingress requires TLS, authentication, WAF policy, request limits, and model routing.

## Start observability on the router

Use `runs/deployment/fleet.json` and router URL `http://127.0.0.1:8000`, then:

```bash
make observe
```

Retain target-discovery and dashboard-verification output. Prometheus scrapes the router locally and resolves engine targets from the fleet document.

## Tunnel router, Prometheus, and Grafana to the workstation

From a management-checkout terminal with `.env` loaded:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --forward 18000:8000 --forward 19090:9090 --forward 13000:3000
```

The helper binds workstation loopback ports, verifies the recorded SSH host key, and uses the router role's configured password, key, or agent. Keep the terminal open.

From another workstation shell:

```bash
export NARWHAL_TRIAL_URL=http://127.0.0.1:18000
curl -fsS "$NARWHAL_TRIAL_URL/health"
curl -fsS "$NARWHAL_TRIAL_URL/ready"
curl -fsSG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

Grafana is available at:

```text
http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator
```

Point the workload client at `$NARWHAL_TRIAL_URL`. The trial therefore includes SSH network and encryption overhead in the measured client path.

If a workstation port is occupied, change only the local side and update the client URL. If forwarding succeeds but the service is unreachable, inspect the listener from the router host. Use `--remote-address` when a remote service binds somewhere other than the default; services on different remote addresses require separate tunnel invocations. Keep the tunnel log under the reported `runs/access-<id>/`. Ctrl-C closes only the forwards owned by that tunnel process.

Record router role assignment, workstation hostname, source revision, and local-to-remote tunnel mappings.

## Run the initial capacity trial

Close the trial in this order:

1. Create a deployment identifier and assemble the deployment evidence set defined by [Measure a fleet](../Measure.md).
2. Attach the passing full preflight mesh for the current processes, fleet, profiles, and targets. Repeat preflight whenever any one changes.
3. Retain monitoring startup output and successful Prometheus scrape evidence for router and engines.
4. Run the initial synthetic workload from the workstation through `$NARWHAL_TRIAL_URL`: 200-request runs at 0.5 and 1 request/s, 8,192 input tokens, 128 output tokens. Retain workload definition, request-level records, and summaries. Treat 2 s TTFT, 33.3 ms TPOT, and 95% attainment as candidate thresholds until measured performance and service requirements define acceptance. Capture client CPU, memory, network, and scheduler behaviour so workstation or SSH-path saturation can be separated from serving saturation.
5. Drain resident work. Reconcile every offer against client and router terminal classes. Query engine, request, token, role, and pool-load series through Grafana's provisioned data source. Then run the post-load KV ring.

After drain:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json --ring
```

The trial closes only when the router serves the measured workload, client records reconcile with the router journal, Prometheus scrapes router and engines successfully, Grafana contains the required series, and the post-load KV ring passes.

Retain service locations, approved source revision, fleet configuration, profiles, router journal, and monitoring endpoints with the private deployment record.

After a passing trial, leave the engines, attestation sidecars, router, and monitoring stack running as the deployed service. Stop the workload client after its records are retained. Close the workstation tunnel when private access is no longer needed; that closes the forwards owned by the tunnel process. Use the documented lifecycle procedure in [Operate Narwhal](../Operate.md) for a later planned engine drain or shutdown.

Use the [evidence and recovery index](../Deploy.md#evidence-and-recovery-index) when recording the final gate.
