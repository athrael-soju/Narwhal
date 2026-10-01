---
description: Start the Narwhal router and validate fleet capacity through the private path.
---

# Gate G: Starting the service and validating capacity through the private path

## Starting and locally verifying the router

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

3. Retain these responses before load:

    | Response          | Shows                                                                   |
    | ----------------- | ----------------------------------------------------------------------- |
    | `/health`         | Liveness status and instance counts.                                    |
    | `/ready`          | Admission state.                                                        |
    | `/narwhal/state`  | Engine inventory and role split.                                        |
    | `/metrics`        | Router metrics.                                                         |
    | `/v1/completions` | One successful completion that increments `served` in `/narwhal/state`. |

## Starting the monitoring stack on the router

In the router shell, run:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
make observe
```

Retain the target-discovery and dashboard-verification output.

## Tunnelling router, Prometheus, and Grafana to the workstation

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
7. Record:

    - The router role assignment.
    - The workstation hostname.
    - The source revision.
    - The tunnel mappings.

Tunnel settings:

| Tunnel property | Value                                                 |
| --------------- | ----------------------------------------------------- |
| Local ports     | Workstation loopback.                                 |
| Remote address  | `127.0.0.1` on the router.                            |
| Host key        | Verified against the recorded SSH host key.           |
| Authentication  | The router role's configured password, key, or agent. |
| Log             | `runs/access-<id>/`                                   |
| Latency         | Includes SSH network and encryption overhead.         |

### Troubleshooting the tunnel

| Symptom                                                             | Fix                                                                          |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| A workstation port is already in use                                | 1. Pick another local port in `--forward`.<br>2. Match the client URL to it. |
| A forwarded request fails after SSH connects                        | Inspect the listener on the router host.                                     |
| A service listens on an address other than the router's `127.0.0.1` | Pass one tunnel per remote address, each with `--remote-address`.            |

## Running the initial capacity trial

The trial requires the Gate C [capacity-trial prefix-caching setting](03-Validate-Engines.md#preparing-checking-and-starting-each-engine) on every engine.

The [synthetic load trial](../measure/03-Load-Trial.md#7-running-the-synthetic-deployment-trial) sets the TTFT, TPOT, and attainment thresholds.

Run the trial:

1. Confirm that each engine's `checked.json` record shows `"prefix_caching": false`.
2. [Freeze the deployment evidence](../measure/02-Targets-and-Freeze.md#6-freezing-the-deployment-under-test) under a new deployment identifier.
3. Attach Gate F's passing preflight to the deployment evidence.
4. Retain the monitoring startup output and the router and engine scrape evidence.
5. From the workstation, run the [synthetic load trial](../measure/03-Load-Trial.md) through `$NARWHAL_TRIAL_URL`.
6. Retain the workload definition, the request-level records, and the summaries.
7. Capture the client CPU, memory, network, and scheduler behaviour.
8. Confirm the drained, idle router state in `state-after.json`.
9. [Reconcile](../measure/04-Reconcile-and-Accept.md) every offer against the client and router terminal classes.
10. Query the engine, request, token, role, and pool-load series in Grafana.
11. Run the post-load KV ring on the router:

    ```bash
    .venv/bin/narwhal-check --fleet runs/deployment/fleet.json --ring
    ```

The trial passes when steps 5 to 11 succeed.

Close the trial:

1. Retain with the private deployment record:

    - The service locations.
    - The approved source revision.
    - The fleet configuration.
    - The profiles.
    - The router journal.
    - The monitoring endpoints.

2. Save the client records and the post-load KV ring output.
3. Stop the client.
4. Press Ctrl+C in the tunnel terminal when private access ends.
5. Leave the engines, attestation sidecars, router, and monitoring stack running until a planned drain or shutdown in [Operating Narwhal](../Operate.md).
6. Record the final gate against the [evidence and recovery index](../Deploy.md#evidence-and-recovery-index).
