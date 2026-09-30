# Gate G: Start the service and validate capacity through the private path

This gate starts the router and monitoring, then runs the initial capacity trial from your workstation over an SSH tunnel.

## Start and locally verify the router

On the router host:

```bash
.venv/bin/narwhal-serve \
  --fleet runs/deployment/fleet.json \
  --host 127.0.0.1 \
  --port 8000
```

The router listens only on loopback. Trial traffic reaches it through an SSH tunnel. Add TLS, authentication, a WAF policy, request limits, and model routing before exposing it publicly.

From another shell on the router host (replace `<served-model>` with the fleet's model):

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/narwhal/state | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/metrics
curl -fsS http://127.0.0.1:8000/v1/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<served-model>","prompt":"Return one sentence about narwhals.","max_tokens":32}'
```

Save these responses as the pre-load baseline:

- `/health`: liveness and cached instance counts.
- `/ready`: admission state.
- `/narwhal/state`: engine inventory and role split.
- `/metrics`: Prometheus text output.
- The completion: one success, which increments `narwhal_served_total`.

## Start observability on the router

In the router shell, point monitoring at the fleet document and the router, then start it:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
make observe
```

Prometheus scrapes the router locally and finds the engine targets from the fleet document. Save the `make observe` output (target discovery and dashboard verification).

## Tunnel router, Prometheus and Grafana to the workstation

From a management-checkout terminal with `.env` loaded:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --forward 18000:8000 --forward 19090:9090 --forward 13000:3000
```

The helper binds the local ports on loopback, verifies the router's recorded SSH host key, and authenticates with the method configured for the router role (password, key, or agent). Keep the terminal open; Ctrl-C closes all forwards.

From another workstation shell:

```bash
export NARWHAL_TRIAL_URL=http://127.0.0.1:18000
curl -fsS "$NARWHAL_TRIAL_URL/health"
curl -fsS "$NARWHAL_TRIAL_URL/ready"
curl -fsSG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

The Grafana dashboard is at:

```text
http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator
```

Point your workload client at `$NARWHAL_TRIAL_URL`. Trial numbers include the tunnel's network and encryption overhead.

Notes:

- The tunnel connects to `127.0.0.1` on the router and writes a log under `runs/access-<id>/`.
- If a workstation port is taken, change the local side of that `--forward` and the matching client URL. Keep the remote port the same.
- If SSH connects and forwarded requests fail, check the listener on the router host (for example, `ss -ltnp`). If a service is bound to an address other than loopback, pass `--remote-address <address>`. It applies to every forward in the invocation, so services on different addresses need separate tunnels.

Record the router host, the workstation hostname, the source revision, and each local-to-remote port mapping in the private deployment record.

## Run the initial capacity trial

1. Create a deployment ID and start the evidence set described in [Measure a fleet](../Measure.md).
2. Add Gate F's passing preflight output to it.
3. Add the monitoring startup output and evidence that Prometheus scrapes the router and engines.
4. From the workstation, run the initial synthetic workload through `$NARWHAL_TRIAL_URL`: two runs of 200 requests, at 0.5 and at 1 request/s, each with 8,192 input tokens and 128 output tokens. Keep the workload definition, the per-request records, and the summaries.

    The thresholds are provisional: 2 s time to first token (TTFT), 33.3 ms time per output token (TPOT), and 95% attainment. Keep them provisional until measured performance and the service requirements settle the real acceptance criteria.

    Also record client CPU, memory, network, and scheduler behavior. If the trial falls short, this shows whether the bottleneck was the workstation, the SSH path, or the fleet.

5. Let resident work drain.
6. Reconcile every offered request against the terminal outcomes recorded by the client and by the router.
7. Query the engine, request, token, role, and pool-load series through Grafana's provisioned data source.
8. Run the post-load KV ring:

    ```bash
    .venv/bin/narwhal-check --fleet runs/deployment/fleet.json --ring
    ```

The trial passes when:

- The router served the measured workload.
- The client's records reconcile with the router journal.
- Prometheus scraped the router and engines.
- Grafana has the series from step 7.
- The post-load KV ring passed.

See [Measure a fleet](../Measure.md) for the full acceptance criteria.

Store the service locations, approved source revision, fleet configuration, profiles, router journal, and monitoring endpoints with the private deployment record.

After saving the client records and ring result, stop the client and close the tunnel when you finish with private access. Leave the engines, attestation sidecars, router, and monitoring running. For a later planned engine drain or shutdown, see [Operate Narwhal](../Operate.md).

Record Gate G in the [evidence and recovery index](../Deploy.md#evidence-and-recovery-index).
