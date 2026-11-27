---
description: Start the Narwhal router and validate fleet capacity through the private path.
---

# Serve and measure

`narwhal deploy run` starts a router pair after preflight: the primary router on the router host, its standby on the standby router host, HAProxy in front of both, and the monitoring stack. With `--trial`, it then runs the [capacity trial](#running-the-initial-capacity-trial). The pair's inputs are in [Deployment inputs](../configuration/04-Deployment-Inputs.md#router-pair).

A fleet without `NARWHAL_STANDBY_ROUTER_SSH` runs one router instead, with no router failover. `lease-check` and `load-balancer` do not run. `router-start` starts the router at `NARWHAL_ROUTER_URL` without `--standby-of`, `--lease-path` or `--first-start-primary`, waits for it to control the fleet and measures its overhead as below. Monitoring scrapes that router and reads its targets from it.

## Checking the shared lease path

Both routers hold one lease at `NARWHAL_LEASE_PATH`, so both router hosts must mount that file's directory at the same path, with POSIX `flock`, coherent reads and atomic rename. The `lease-check` stage proves this with the router's own lease code before either router starts. On a probe lease beside the pair's lease:

1. The primary router's host claims the lease.
2. The standby router's host reads that claim, and its own claim is refused.
3. While the primary's host holds the lease lock, `flock` on the standby's host reports it held.
4. Once the lease expires, the standby's host claims it, and the primary's host reads the new claim.
5. The primary's host leaves the lease witness, `.narwhal-lease-witness`, in the lease directory, and the standby's host reads it.
6. The stage reads the lease directory's mount on each host from `/proc/self/mountinfo`, records its file system type and options, and fails on a mount whose options break the lease, as [Mount options for the lease](#mount-options-for-the-lease) lists, or when the two hosts mount different file system types.
7. The stage reads each host's clock several times over SSH and fails when the hosts' clocks may differ by more than 1 second, the `--lease-safety-margin` the routers reserve for skew. A lease's expiry is a wall-clock time that the other host compares with its own clock.

A failed step names the host, what it read and what it expected, and the stage removes the probe files. The witness stays. The stage keeps each step's output, the mounts and the measured skew in `lease-check.json`. Fix the mount or the clocks on both hosts, then rerun `narwhal deploy run`.

### Mount options for the lease

On NFS, mount the lease directory on both router hosts with these options:

- Close-to-open consistency, the default. With `nocto`, an open of the lease can read what this host cached rather than what the other host wrote.
- `local_lock=none`, the default, and not `nolock`. Otherwise `flock` stays on each host and the two hosts' locks do not exclude each other.
- `actimeo=1` or `noac`. The client may cache the lease's attributes for no longer than the 1 second the routers reserve with `--lease-safety-margin`. Without `actimeo`, Linux caches them for up to 60 seconds.

For example, in `/etc/fstab`:

```text
nfs-server:/export/narwhal  /srv/shared  nfs4  rw,hard,actimeo=1  0  0
```

The stage names each offending option and its fix. Other file systems pass this check, and the lease checks above still test them.

## Starting the router pair

`router-start` runs for the primary first, then for the standby. Before either starts, the stage checks on that router's host that the lease directory holds the witness and has the file system type that `lease-check` recorded, so a host that rebooted without the shared mount starts no router. It also checks the mount's options again, since a remount may have changed them. Each router runs as a systemd user unit that restarts it on failure:

- The primary serves `NARWHAL_ROUTER_URL` with `--standby-of` the standby and [`--first-start-primary`](../cli/Serve.md#standby-takeover-and-lease-fencing). It claims the lease when no lease file exists, or when the lease expired more than 30 seconds ago, the standby's `--standby-max-handoff-age`, so no standby can still take it over. Otherwise it starts as the standby of its peer. When the lease expired more recently, as when the pair stopped moments before, the stage waits until no standby can take it over before it starts the primary.
- The standby serves `NARWHAL_STANDBY_ROUTER_URL` with `--standby-of` the primary.

The stage never removes the lease. It reads the lease's holder and expiry on the router's host before the start and keeps them in `router.json`. Both routers use `--lease-path NARWHAL_LEASE_PATH`, `--lease-witness` with the witness `lease-check` left, and the host's ID as `--router-id`. Each router must pass `/health`. The stage then waits up to 180 seconds for one router of the pair to control the fleet, and keeps that router's health, readiness, state, metrics and lifecycle responses and one completion under `router/`. The router's log and journal stay on its host in the attempt directory.

Every `router-start`, including a readmission and a primary that rejoins as the standby, then measures the router overhead under load on the router that controls the fleet. It sends 64 completions, 8 at a time, and reads that router's `/metrics` before and after. Over the completions that reached a first token, the overhead is the router's time to first token less the time in the prefill legs and in the admission and prefill seat queues, per completion:

```text
overhead = (Δ narwhal_ttft_seconds_sum
            − Δ narwhal_upstream_seconds_total{phase="prefill"}
            − Δ narwhal_queue_wait_seconds_sum{stage="admission"}
            − Δ narwhal_queue_wait_seconds_sum{stage="prefill"})
           / Δ narwhal_ttft_seconds_count
```

A window in which a counter went back, or in which the router counted fewer first tokens than the completions that succeeded, measured a restarted router, so the stage measures again, up to three windows. The stage fails when every window saw a reset, when a completion fails or when the overhead exceeds `NARWHAL_ROUTER_OVERHEAD_S`, the `slo.router_overhead_s` that preflight's [`slo` gate](../cli/Check.md#the-slo-gate) budgets. It keeps the terms in `overhead.json`.

When an engine restarts and the routers still run on an unchanged fleet, the router that holds the lease readmits the engine, and its standby follows through state handoffs.

## Starting the load balancer

The `load-balancer` stage renders [`deploy/ha/haproxy.cfg`](https://github.com/athrael-soju/Narwhal/blob/main/deploy/ha/haproxy.cfg), which the wheel carries, with `bind` set to the `NARWHAL_LOAD_BALANCER_URL` address and the two `server` lines set to the routers' origins. On the load balancer's host, it checks the file with `haproxy -c`, then starts `NARWHAL_HAPROXY_IMAGE` as the container `narwhal-<run ID>-load-balancer`, with host networking and Docker's `on-failure` restart policy. HAProxy reads the file from the attempt directory as the deployment user.

The stage passes when `/ready` answers HTTP 200 through HAProxy. It keeps `haproxy.cfg` and the health, readiness, state, metrics, lifecycle and completion responses through HAProxy under `router/`. Clients and the capacity trial use `NARWHAL_LOAD_BALANCER_URL`.

## Starting the monitoring stack

The `monitoring` stage starts the [monitoring stack](../observability/01-Start-and-Verify.md) on the primary router's host from the installed wheel, in the deployment directory:

```bash
.venv/bin/python -m narwhal.observability.stack.start.cli \
  --router-url "$NARWHAL_ROUTER_URL" --router-url "$NARWHAL_STANDBY_ROUTER_URL" \
  --targets-url "$NARWHAL_LOAD_BALANCER_URL" \
  --sidecar-url http://<engine-1 sidecar host>:<port> --sidecar-url http://<engine-2 sidecar host>:<port>
```

The stage runs it with `NARWHAL_FLEET_NAME`, `NARWHAL_ALERTMANAGER_URL` and the listen addresses from the deployment inputs in its environment. Prometheus scrapes both routers and each engine's attestation sidecar, and reads the engine targets through the load balancer. Every series carries the `fleet` label. Prometheus sends alerts to the Alertmanager when `NARWHAL_ALERTMANAGER_URL` is set; without one, they show only on the Prometheus alerts page. Prometheus runs on the primary router's host, so route `NarwhalWatchdog` to a dead man's switch that pages while that host is down, as in [Monitor](../operate/02-Monitor.md). After every run, the command uploads the [deployment status](../telemetry/06-Deployment-Runs.md#deployment-status) to both router hosts, and the dashboard's [Deployment row](../observability/05-Dashboard.md#deployment) shows it. The stage prints the command's `<service> ready at <URL> (<container ID>, <image>)` lines and records each service's container. When a container stops or is replaced, the next run starts the stack again. The stack's state is `runs/observability/` in the deployment directory.

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

6. To run a load client by hand, as the [measurement pages](../Measure.md) do, point it at `$NARWHAL_TRIAL_URL`.
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

`narwhal deploy run --trial <workload>` runs the capacity trial after the monitoring stack, as the `trial` stage. `<workload>` is a trial file that holds the [synthetic load trial](../measure/03-Load-Trial.md)'s workload, its offered rates and the thresholds each rate must meet:

```bash
narwhal deploy run --run runs/deploy/first-deploy --trial config/capacity-trial.json
```

```json
{
  "schema": "narwhal.capacity-trial",
  "schema_version": 1,
  "input_tokens": 8192,
  "output_tokens": 128,
  "seed": 1729,
  "requests": 200,
  "rates_rps": [0.5, 1],
  "ttft_s": 2.0,
  "tpot_s": 0.0333,
  "attainment": 0.95
}
```

| Field | Value |
| --- | --- |
| `schema`, `schema_version` | `narwhal.capacity-trial` and `1` |
| `input_tokens`, `output_tokens` | Prompt and output tokens of every request, integers of at least 1 |
| `seed` | Seed of the synthetic prompts, an integer of at least 0 |
| `requests` | Requests offered at each rate, an integer of at least 1 |
| `rates_rps` | Offered rates in requests per second, in increasing order. The trial stops at the first rate that fails |
| `ttft_s`, `tpot_s` | The time to first token and time per output token that a request must meet, positive seconds |
| `attainment` | The fraction of offered requests that must meet both limits, above 0 and at most 1 |
| `max_inflight`, `max_lag_s`, `timeout_s` | Optional client limits: concurrent requests, default `64`, scheduling lag, default `0.05`, and the per-request and drain timeout, default `120` |

`plan` and `run` name every problem of the file before they reach a host. They also refuse `--trial` while an engine's inputs leave prefix caching on: set `NARWHAL_PREFIX_CACHING=false` before discovery, as in [Preparing, checking, and starting each engine](03-Validate-Engines.md#preparing-checking-and-starting-each-engine), and unset any `NARWHAL_NODE_<n>_PREFIX_CACHING` that turns it on again.

The load client runs on the workstation from the installed wheel. For the length of the stage, SSH forwards on workstation loopback ports reach the load balancer, or the router of a fleet with one router, each router, and Prometheus on the primary router's host. The stage then:

1. Refuses to run when an engine's `checked.json` shows `prefix_caching` other than `false`, and when the workload leaves an engine's profiled domain: a prompt outside the profile's `prefill_min_tokens` to `prefill_max_tokens`, an output outside `decode_min_output_tokens` to `decode_max_output_tokens`, or one request's tokens above `decode_max_kv_tokens`.
2. Prepares the workload from the router's served model with `load_trial prepare`.
3. Offers each rate in order with `load_trial run`, which waits for a drained router first, against the file's thresholds. It reads each router's `/narwhal/state`, as a diagnostic read that a standby answers too, and `/metrics` before and after the rate and [reconciles](../measure/04-Reconcile-and-Accept.md) every offer with the routers' journals and counters. A rate fails when the client missed its schedule, when fewer offers than `attainment` met both limits, or when an offer, a journal row or a counter disagrees.
4. Checks these series in Prometheus, each scoped to `NARWHAL_FLEET_NAME` over a window from 15 seconds before the workload was prepared to the query:

    | Series | Passes when |
    | --- | --- |
    | `engine` | Every engine's scrape target was up at every scrape of the trial |
    | `request` | `narwhal_served_total` grew |
    | `token` | `narwhal_decode_tokens_observed_total` grew, and the engines computed prompt tokens and generated output tokens |
    | `role` | Every engine held a role in a pool, as `narwhal_instance_role` shows |
    | `pool-load` | The prefill and the decode pool each reported `narwhal_pool_load`. A pool whose engines run at their idle pace reports 0, which passes |

5. Runs the post-load KV ring on the primary router's host, with the fleet that `fleet-limits` wrote:

    ```bash
    narwhal-check --fleet <fleet-limits fleet> --ring --format json
    ```

The trial passes when every rate, every series and the KV ring pass. Every other request to the routers during the trial breaks the reconciliation, so keep other clients off the fleet. With `--format json`, the result's `data.trial` holds the trial result.

The stage keeps its evidence in `stages/trial/attempt-<n>/` of the run directory:

| Path | Contents |
| --- | --- |
| `trial.json` | The trial file |
| `prefix-caching.json` | Each engine's `prefix_caching` from its `checked.json` |
| `workload/` | The prepared `workload.json`, `seed-response.json` and `manifest.json` |
| `rate-<rate>/` | The [client's records](../measure/04-Reconcile-and-Accept.md) of the rate, with `samples.json`, the router reads before and after it, `journal-rows.json`, the routers' journal rows of its offers, and `reconcile.json` |
| `series.json` | Each series query, the samples Prometheus returned and its verdict |
| `host/ring.json` | The KV ring's command result |
| `trial-result.json` | Each rate's counts, attainment and verdicts, each series verdict, the ring, and the failure of a failed trial |
| `workload.log`, `rate-<rate>.log`, `logs/` | The load client's output and the SSH logs |

A failed check fails the stage's gate. Fix the cause and run `run --trial` again. A trial that passed is skipped while its file and the inputs that the [rerun table](../Deploy.md#rerunning-after-a-change) names for `trial` are unchanged, so give a changed file, such as another `seed`, to repeat it.

After the trial, archive the run directory with the private deployment record. Leave the engines, attestation sidecars, routers and monitoring stack running until a planned drain or shutdown in [Operating Narwhal](../Operate.md).
