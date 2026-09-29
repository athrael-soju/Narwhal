# Gate G: Start the service and validate capacity through the private path

Start the router and monitoring stack on the router host and tunnel them to the management workstation. Run the initial capacity trial through that tunnel.

## Start and locally verify the router

Replace `<served-model>` with the fleet model.

1. In the router shell, start the router:

    ```bash
    .venv/bin/narwhal-serve \
      --fleet runs/deployment/fleet.json \
      --host 127.0.0.1 \
      --port 8000
    ```

2. From another router-host shell, check the router:

    ```bash
    curl -fsS http://127.0.0.1:8000/health
    curl -fsS http://127.0.0.1:8000/ready
    curl -fsS http://127.0.0.1:8000/narwhal/state | python3 -m json.tool
    curl -fsS http://127.0.0.1:8000/metrics
    curl -fsS http://127.0.0.1:8000/v1/completions \
      -H 'content-type: application/json' \
      -d '{"model":"<served-model>","prompt":"Return one sentence about narwhals.","max_tokens":32}'
    ```

3. Retain these responses before load.

Keep the responses for `/health` (liveness and cached instance counts), `/ready` (admission state), `/narwhal/state` (engine inventory and role split), `/metrics`, and one successful completion that increments `served`.

The router listens only on loopback, so workstation trial traffic reaches it through the [SSH tunnel](#tunnel-router-prometheus-and-grafana-to-the-workstation).

## Start the monitoring stack on the router

`make observe` reads the fleet file from `NARWHAL_FLEET` and the router URL from `NARWHAL_ROUTER_URL`.

Start Prometheus and Grafana in the router shell:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
make observe
```

Prometheus scrapes the router locally and resolves the engine targets from the fleet configuration. Retain the target-discovery and dashboard-verification output.

## Tunnel router, Prometheus, and Grafana to the workstation

1. On the workstation, open a terminal in the checkout with `.env` loaded.
2. Open the tunnel:

    ```bash
    python3 tools/deployment/deploy_hosts.py tunnel --role router \
      --forward 18000:8000 --forward 19090:9090 --forward 13000:3000
    ```

3. Keep the tunnel terminal open.
4. From another workstation shell, check through the tunnel:

    ```bash
    export NARWHAL_TRIAL_URL=http://127.0.0.1:18000
    curl -fsS "$NARWHAL_TRIAL_URL/health"
    curl -fsS "$NARWHAL_TRIAL_URL/ready"
    curl -fsSG http://127.0.0.1:19090/api/v1/query \
      --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
      | python3 -m json.tool
    ```

5. Open Grafana at:

    ```text
    http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator
    ```

6. Point the workload client at `$NARWHAL_TRIAL_URL`.
7. Record the router role assignment, the workstation hostname, the source revision, and the tunnel mappings.

The helper binds the workstation loopback ports, verifies the recorded SSH host key, and uses the router role's configured password, key, or agent. It targets `127.0.0.1` on the router and writes a log under `runs/access-<id>/`. Client latency includes SSH network and encryption overhead.

### Troubleshoot the tunnel

If a workstation port is already in use, change the local side of `--forward` and update the client URL to match. If a forwarded request fails after SSH connects, inspect the listener on the router host. If a service listens on an address other than the router's `127.0.0.1`, pass it with `--remote-address`. One invocation takes one remote address for all forwards, so use a separate tunnel per address.

## Run the initial capacity trial

Set `--no-enable-prefix-caching` in `runtime.extra_args` before launch on every engine, so that its `checked.json` record shows `"prefix_caching": false`. See [Gate C: Prepare, check, and start each engine](03-Validate-Engines.md#prepare-check-and-start-each-engine).

1. Create a deployment identifier and [freeze the deployment evidence](../measure/02-Targets-and-Freeze.md#6-freeze-the-deployment-under-test).
2. Attach Gate F's passing preflight to the deployment evidence.
3. Retain the monitoring startup output and the router and engine scrape evidence.
4. From the workstation, run the [synthetic load trial](../measure/03-Load-Trial.md) through `$NARWHAL_TRIAL_URL`.
5. Retain the workload definition, the request-level records, and the summaries.
6. Capture the client CPU, memory, network, and scheduler behaviour.
7. Confirm the drained, idle router state in `state-after.json`.
8. [Reconcile](../measure/04-Reconcile-and-Accept.md) every offer against the client and router terminal classes.
9. Query the engine, request, token, role, and pool-load series in Grafana.
10. Run the post-load KV ring on the router:

    ```bash
    .venv/bin/narwhal-check --fleet runs/deployment/fleet.json --ring
    ```

The trial uses candidate thresholds of 2 s time to first token (TTFT), 33.3 ms time per output token (TPOT), and 95% attainment. Acceptance follows measured performance against the service requirements.

The trial passes when steps 4 to 10 succeed.

Retain the service locations, the approved source revision, the fleet configuration, the profiles, the router journal, and the monitoring endpoints with the private deployment record.

Stop the client once its records and the post-load KV ring output are saved. Press Ctrl+C in the tunnel terminal when private access ends. Leave the engines, attestation sidecars, router, and monitoring stack running. See [Operate Narwhal](../Operate.md) for a planned engine drain or shutdown.

Use the [evidence and recovery index](../Deploy.md#evidence-and-recovery-index) when you record the final gate.
