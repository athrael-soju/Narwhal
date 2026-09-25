# Gate G: Start the service and validate capacity through the private path

## Start and locally verify the router

On the router host:

```bash
.venv/bin/narwhal-serve \
  --fleet runs/deployment/fleet.json \
  --host 127.0.0.1 \
  --port 8000
```

The SSH tunnel carries workstation trial traffic to this loopback listener. Public ingress needs TLS, authentication, WAF policy, request limits, and model routing ahead of the router.

From another shell on the router host:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/narwhal/state | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/metrics
curl -fsS http://127.0.0.1:8000/v1/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<served-model>","prompt":"Return one sentence about narwhals.","max_tokens":32}'
```

Replace `<served-model>` with the fleet model.

Before load, retain:

- `/health`, including liveness and cached instance counts;
- `/ready`, including admission state;
- `/narwhal/state`, including engine inventory and role split;
- `/metrics`;
- one successful completion that increments `served`.

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

The helper targets `127.0.0.1` on the router and writes a log under `runs/access-<id>/`. If a workstation port is occupied, change its local `--forward` value and the corresponding client URL; keep the remote service port.

If a forwarded request fails after SSH connects, inspect the listener on the router host. Use `--remote-address` for a different bind address; the helper applies one remote address to all forwards in an invocation, so services on different addresses need separate tunnels. Ctrl-C closes that tunnel's forwards.

Record router role assignment, workstation hostname, source revision, and local-to-remote tunnel mappings.

## Run the initial capacity trial

Run the capacity trial and retain its evidence in this order:

1. Create a deployment identifier and assemble the deployment evidence set defined by [Measure a fleet](../Measure.md).
2. Attach Gate F's passing preflight to the deployment evidence.
3. Retain monitoring startup output and successful Prometheus scrape evidence for router and engines.
4. Run the initial synthetic workload from the workstation through `$NARWHAL_TRIAL_URL`: 200-request runs at 0.5 and 1 request/s, 8,192 input tokens, 128 output tokens. Retain workload definition, request-level records, and summaries.

    Treat 2 s TTFT, 33.3 ms TPOT, and 95% attainment as candidate thresholds until measured performance and service requirements define acceptance.

    Capture client CPU, memory, network, and scheduler behaviour so workstation or SSH-path saturation can be separated from serving saturation.

5. Drain resident work. Reconcile every offer against client and router terminal classes. Query engine, request, token, role, and pool-load series through Grafana's provisioned data source. Then run the post-load KV ring.

After drain:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json --ring
```

The trial passes when the router serves the measured workload, client records reconcile with the router journal, Prometheus scrapes router and engines successfully, Grafana contains the required series, and the post-load KV ring passes.

Retain service locations, approved source revision, fleet configuration, profiles, router journal, and monitoring endpoints with the private deployment record.

Once the client records and post-load KV ring are retained, stop the client and close the workstation tunnel when private access ends. Keep the engines, attestation sidecars, router, and monitoring stack running after the trial passes; use [Operate Narwhal](../Operate.md) for a later planned engine drain or shutdown.

Use the [evidence and recovery index](../Deploy.md#evidence-and-recovery-index) when recording the final gate.
