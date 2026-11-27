---
description: Start Prometheus and Grafana for a Narwhal router and verify every scrape target.
---

# Starting and verifying monitoring

## Prerequisites

- a running Narwhal router
- Linux
- Docker Engine with the Compose v2 plugin, so that `docker compose version` answers. The start command checks it first
- Python 3.11 or newer
- `curl`
- network reachability from the router host to every engine metrics endpoint

On a deployed fleet, `narwhal deploy run` starts the stack in its `monitoring` stage. To start it by hand, run `python -m narwhal.observability.stack.start.cli` in the [installed router-role shell](../deploy/02-Install.md#opening-installed-role-shells), from the deployment directory with `runs/deployment/.env.router` loaded and `.venv` active. In a repository checkout, `make observe` runs the same command. The pages below write `make observe` for either.

## Configuring the monitored deployment

`NARWHAL_ROUTER_URL` is the router origin Prometheus scrapes, direct or through a stable local tunnel. Write it as `http://host:port` with an explicit port and no path, query or credentials. Prometheus reads the engine targets from the router's [`GET /narwhal/targets`](../http-api/04-Inspection.md#get-narwhaltargets) every 15 seconds, so the targets follow the fleet the router serves. A router that does not hold the lease refuses that read with HTTP 503. Prometheus then keeps the targets of its last successful read, or holds none when no read has succeeded, and `make observe` fails its `engines` check with the router's reason. When the router moves to another origin, set the new value and rerun `make observe`, which recreates Prometheus with the new targets.

Set it in the router-role shell:

```bash
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

### Monitoring a router pair

For a [router pair](../operate/01-Start-Routers.md#starting-a-router-pair), Prometheus scrapes each router at its own origin and reads the engine targets through the load balancer, which reaches the active router. Set both router origins, separated by a comma, and the load balancer origin:

```bash
export NARWHAL_ROUTER_URL=http://router-a:8000,http://router-b:8000
export NARWHAL_TARGETS_URL=http://<load-balancer>:8000
```

Replace `<load-balancer>` with the address of the load balancer in front of the pair. Without `NARWHAL_TARGETS_URL`, or with one of the router origins in it, `make observe` stops with an error for a pair. When the load balancer stops answering, Prometheus keeps the engine targets it read last.

When one router of the pair is down, `make observe` starts once the other router answers and is ready, and prints `warning: Prometheus router target <origin> reports down: <error>` for the down router.

Scrape each router at its own origin, not through the load balancer. Each router counts the requests it answered, and its series carry its origin as the `instance` label. Through the load balancer, one series can switch between the two routers' counts during a takeover, and Prometheus reads each switch as a counter reset.

### Scraping the fleet control service

To mark [fleet control](../operate/fleet-control/03-Grafana-Dashboard.md#dashboard-annotations) actions and load jobs on the **Narwhal Orchestrator** dashboard, Prometheus must scrape the fleet control service. Before `make observe`, set its address and token:

```bash
export NARWHAL_CONTROL_METRICS_URL=http://127.0.0.1:8020
export NARWHAL_CONTROL_TOKEN=<control-token>
```

Replace `<control-token>` with the service's bearer token. The address needs an explicit port. If the address is set without the token, `make observe` stops with an error.

### Scraping Narwhal tools

The [engine table](05-Dashboard.md#engine-states) shows which tool keeps an engine busy when Prometheus scrapes `narwhal-check` and `narwhal-profile`. Set the address they serve [tool metrics](../telemetry/03-Metrics-and-Control.md#tool-metrics) on, in the shell that runs `make observe` and in each shell that runs a tool:

```bash
export NARWHAL_TOOL_METRICS_URL=http://127.0.0.1:18030
```

Give the address an explicit port that Prometheus can reach. Run one tool at a time. A second tool finds the port in use, prints `tool metrics: cannot listen on`, and runs with its engines reading Probing.

## Starting Prometheus and Grafana

Run:

```bash
make observe
```

To pass options, run the start command directly:

```bash
.venv/bin/python -m narwhal.observability.stack.start.cli --router-url http://127.0.0.1:8000 --ready-timeout 120
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--router-url URL` | `NARWHAL_ROUTER_URL` | Router origin Prometheus scrapes, required when the variable is unset. Repeat it for each router of a pair |
| `--targets-url URL` | `NARWHAL_TARGETS_URL`, else the router origin | Origin Prometheus reads the engine targets from, required for a router pair |
| `--sidecar-url URL` | `NARWHAL_SIDECAR_URLS`, comma-separated | Attestation sidecar origin Prometheus scrapes for its restart counts and the `NarwhalHostDown` alert. Repeat it for each engine |
| `--fleet-name NAME` | `NARWHAL_FLEET_NAME`, else `default` | The `fleet` label of every series. Give each fleet its own name when one Prometheus holds several |
| `--alertmanager-url URL` | `NARWHAL_ALERTMANAGER_URL` | Alertmanager that Prometheus sends alerts to, `http(s)://HOST:PORT` with an optional path. Without one, alerts show only on the Prometheus alerts page at `/alerts` |
| `--ready-timeout SECONDS` | `60` | Seconds each readiness wait allows |

### Readiness contract

`make observe` returns when every component passes its check:

| Component                | Check                                                                                                                               |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| Prometheus               | `/-/ready` answers and the build reports version `3.14.0`                                                                           |
| Grafana                  | `/api/health` reports database `ok` and version `13.2.1`                                                                            |
| Grafana datasource       | The `Prometheus` datasource points at the Prometheus listener                                                                       |
| Dashboard                | `narwhal-router` loads from Grafana's `dashboard.grafana.app/v2beta1` API with its `router` selector defaulting to All (regex `.*`) |
| Dashboard router queries | Every router-scoped query uses `instance=~"$router"`, with at least one present                                                     |
| `narwhal-router` job     | One healthy target at each router origin in `NARWHAL_ROUTER_URL`. For a pair, one router can be down while the other answers         |
| `engines` job            | One healthy target per engine in `/narwhal/targets` at the engine targets origin, labelled with its `iid`                           |
| Router readiness         | Every router that answers its scrape reports `narwhal_router_ready`, and exactly one reports `1`                                    |

Each startup stage has a deadline:

| Startup stage                                                          | Deadline                              |
| ---------------------------------------------------------------------- | ------------------------------------- |
| Listener checks and each Docker inspection command                     | 10 seconds                            |
| Initial image pulls and container creation                             | 5 minutes                             |
| Prometheus and Grafana HTTP checks, including datasource and dashboard | 60 seconds after the containers start |
| Target health and `narwhal_router_ready`                               | 60 seconds after the HTTP checks pass |

## Verifying Prometheus targets

Query the scrape state from the router host:

```bash
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

The response has one series for each router and one for each member engine. A value of `1` marks a successful scrape, and `0` marks a failed scrape.

If you set `NARWHAL_CONTROL_METRICS_URL`, check `up{job="fleet-control"}` the same way. If you set `NARWHAL_TOOL_METRICS_URL`, `up{job="narwhal-tools"}` is `1` while a tool runs preflight, first-token calibration or a live sweep.

Prometheus `/targets` shows the discovery state and scrape errors for each endpoint.

## Staged monitoring files

`make observe` stages its configuration under `runs/observability/mounts/`:

| Source                                                                                        | Staged path                                                                       |
| --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `src/narwhal/observability/stack/prometheus.yml` and `NARWHAL_TARGETS_URL`, or `NARWHAL_ROUTER_URL`       | `prometheus/prometheus.yml`                                                       |
| `src/narwhal/observability/stack/prometheus-alerts.yml`                                                   | `prometheus/prometheus-alerts.yml`                                                |
| `NARWHAL_ROUTER_URL`                                                                          | `prometheus/targets/router.json`                                                  |
| `src/narwhal/observability/stack/grafana/provisioning/`                                                   | `grafana-provisioning/`                                                           |
| `src/narwhal/observability/stack/grafana-narwhal.json` and `NARWHAL_CONTROL_CONSOLE_URL`                  | `grafana-dashboards/narwhal.json`                                                 |
| `NARWHAL_CONTROL_METRICS_URL`                                                                 | `prometheus/targets/fleet-control.json`, empty if unset                           |
| `NARWHAL_CONTROL_TOKEN`                                                                       | `prometheus/fleet-control-token`, empty if `NARWHAL_CONTROL_METRICS_URL` is unset |
| `NARWHAL_TOOL_METRICS_URL`                                                                    | `prometheus/targets/tools.json`, empty if unset                                   |

The staged files and the renderer token have these modes:

| Path                                                      |  Mode  |
| --------------------------------------------------------- | :----: |
| `runs/observability/mounts/`                              | `0700` |
| Mounted subdirectories                                    | `0755` |
| Mounted files, including `prometheus/fleet-control-token` | `0644` |
| `runs/observability/renderer-token`                       | `0600` |

Compose bind-mounts the `prometheus`, `grafana-provisioning`, and `grafana-dashboards` subdirectories read-only.

`make observe` writes the Grafana-to-renderer token to `runs/observability/renderer-token` on its first run and reuses that file on later runs.

Each `make observe` run regenerates the staged files and resets their permissions.

[Access dashboards and isolate listeners](02-Access.md)
