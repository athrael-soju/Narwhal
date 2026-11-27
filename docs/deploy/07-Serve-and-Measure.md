---
description: Start the Narwhal router and validate fleet capacity through the private path.
---

# Serve and measure

`narwhal deploy run` starts a router pair after preflight: the primary router on the router host, its standby on the standby router host, HAProxy in front of both, and the monitoring stack. The pair's inputs are in [Deployment inputs](../configuration/04-Deployment-Inputs.md#router-pair).

## Checking the shared lease path

Both routers hold one lease at `NARWHAL_LEASE_PATH`, so both router hosts must mount that file's directory at the same path, with POSIX `flock`, coherent reads and atomic rename. The `lease-check` stage proves this with the router's own lease code before either router starts. On a probe lease beside the pair's lease:

1. The primary router's host claims the lease.
2. The standby router's host reads that claim, and its own claim is refused.
3. While the primary's host holds the lease lock, `flock` on the standby's host reports it held.
4. Once the lease expires, the standby's host claims it, and the primary's host reads the new claim.

A failed step names the host, what it read and what it expected, and the stage removes the probe files. The stage keeps each step's output in `lease-check.json`. Fix the mount on both hosts, then rerun `narwhal deploy run`.

## Starting the router pair

`router-start` runs for the primary first, then for the standby. Each router runs as a systemd user unit that restarts it on failure:

- The primary serves `NARWHAL_ROUTER_URL` with `--standby-of` the standby and [`--first-start-primary`](../cli/Serve.md#standby-takeover-and-lease-fencing). Unless the standby holds the lease on the current fleet, the stage removes the lease file first, so the primary claims the lease and must pass `/ready`. A later restart of the primary finds the lease file and rejoins as the standby.
- The standby serves `NARWHAL_STANDBY_ROUTER_URL` with `--standby-of` the primary and must pass `/health`.

Both use `--lease-path NARWHAL_LEASE_PATH` and the host's ID as `--router-id`. The stage keeps the primary's health, readiness, state, metrics and lifecycle responses and one completion under `router/`, and the router's log and journal on its host in the attempt directory.

When the primary starts the pair, the stage then measures the router overhead under load. It sends 64 completions, 8 at a time, to the primary and reads its `/metrics` before and after. Over the completions that reached a first token, the overhead is the router's time to first token less the time in the prefill legs and in the admission and prefill seat queues, per completion:

```text
overhead = (Δ narwhal_ttft_seconds_sum
            − Δ narwhal_upstream_seconds_total{phase="prefill"}
            − Δ narwhal_queue_wait_seconds_sum{stage="admission"}
            − Δ narwhal_queue_wait_seconds_sum{stage="prefill"})
           / Δ narwhal_ttft_seconds_count
```

The stage fails when a completion fails or when the overhead exceeds `NARWHAL_ROUTER_OVERHEAD_S`, the `slo.router_overhead_s` that preflight's [`slo` gate](../cli/Check.md#the-slo-gate) budgets. It keeps the terms in `overhead.json`.

When an engine restarts and the routers still run on an unchanged fleet, the router that holds the lease readmits the engine, and its standby follows through state handoffs.

## Starting the load balancer

The `load-balancer` stage renders [`deploy/ha/haproxy.cfg`](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg), which the wheel carries, with `bind` set to the `NARWHAL_LOAD_BALANCER_URL` address and the two `server` lines set to the routers' origins. On the load balancer's host, it checks the file with `haproxy -c`, then starts `NARWHAL_HAPROXY_IMAGE` as the container `narwhal-<run ID>-load-balancer`, with host networking and Docker's `on-failure` restart policy. HAProxy reads the file from the attempt directory as the deployment user.

The stage passes when `/ready` answers HTTP 200 through HAProxy. It keeps `haproxy.cfg` and the health, readiness, state, metrics, lifecycle and completion responses through HAProxy under `router/`. Clients and the capacity trial use `NARWHAL_LOAD_BALANCER_URL`.

## Starting the monitoring stack

The `monitoring` stage starts the [monitoring stack](../observability/01-Start-and-Verify.md) on the primary router's host from the installed wheel, in the deployment directory:

```bash
.venv/bin/python -m narwhal.observability.stack.start.cli \
  --router-url "$NARWHAL_ROUTER_URL" --router-url "$NARWHAL_STANDBY_ROUTER_URL" \
  --targets-url "$NARWHAL_LOAD_BALANCER_URL"
```

Prometheus scrapes both routers and reads the engine targets through the load balancer. The stage prints the command's `<service> ready at <URL> (<container ID>, <image>)` lines and records each service's container. When a container stops or is replaced, the next run starts the stack again. The stack's state is `runs/observability/` in the deployment directory.

## Tunnelling router, Prometheus, and Grafana to the workstation

1. On the workstation, open a terminal in the management checkout with `.env` and `config/deployment.env` loaded.
2. Open the tunnel:

    ```bash
    narwhal deploy tunnel --forward 18000:8000 --forward 19090:9090 --forward 13000:3000
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
    - The wheel's SHA-256.
    - The tunnel mappings.

The tunnel listens on workstation loopback ports and forwards them to `127.0.0.1` on the router. It verifies the router against its key in `NARWHAL_SSH_KNOWN_HOSTS`, authenticates with the router role's configured password, key, or agent, and logs to `runs/access-<id>/`. Latency through the tunnel includes SSH network and encryption overhead.

### Troubleshooting the tunnel

If a workstation port is already in use:

1. Pick another local port in `--forward`.
2. Match the client URL to it.

If a forwarded request fails after SSH connects, inspect the listener on the router host.

If a service listens on an address other than the router's `127.0.0.1`, pass one tunnel per remote address, each with `--remote-address`.

## Running the initial capacity trial

The trial requires the [capacity-trial prefix-caching setting](03-Validate-Engines.md#preparing-checking-and-starting-each-engine) on every engine.

Run the trial:

1. Confirm that each engine's `checked.json` record shows `"prefix_caching": false`.
2. [Freeze the deployment evidence](../measure/02-Targets-and-Freeze.md#freezing-the-deployment-under-test) under a new deployment identifier.
3. Attach the passing preflight result to the deployment evidence.
4. Retain the monitoring startup output and the router and engine scrape evidence.
5. From the workstation, run the [synthetic load trial](../measure/03-Load-Trial.md#running-the-synthetic-deployment-trial) through `$NARWHAL_TRIAL_URL`, against its TTFT, TPOT, and attainment thresholds.
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
    - The SHA-256 of the installed wheel.
    - The fleet configuration.
    - The profiles.
    - The router journal.
    - The monitoring endpoints.

2. Save the client records and the post-load KV ring output.
3. Stop the client.
4. Press Ctrl+C in the tunnel terminal when private access ends.
5. Leave the engines, attestation sidecars, router, and monitoring stack running until a planned drain or shutdown in [Operating Narwhal](../Operate.md).
6. Record the final gate against the [evidence and recovery index](../Deploy.md#evidence-and-recovery-index).
