# Start and verify monitoring

## Prerequisites

- a running Narwhal router
- the fleet configuration for that deployment
- Linux
- Docker Engine with the Compose plugin
- Python 3.11 or newer
- `curl`
- network reachability from the router host to every engine metrics endpoint

Run every command on this page in the [installed router-role shell](../deploy/02-Install.md#open-installed-role-shells).

| Shell state | Value |
| --- | --- |
| Working directory | Deployed checkout |
| Environment | `runs/deployment/.env.router` |
| Python | `.venv` active |

## Configure the monitored deployment

| Variable | Value |
| --- | --- |
| `NARWHAL_FLEET` | Fleet configuration file with the engine IDs and metrics URLs |
| `NARWHAL_ROUTER_URL` | Router origin Prometheus scrapes, direct or through a stable local tunnel |

Set both in the router-role shell:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

[Environment references](../configuration/05-Engine-Launch.md#14-engine-endpoints-generated-from-node-environments) in the fleet file resolve from the variables loaded in the router-role shell.

## Start Prometheus and Grafana

Run:

```bash
make observe
```

The command returns after Prometheus `3.14.0` and Grafana `13.2.1` pass the [readiness checks](#readiness-contract).

### Readiness contract

| Component | Check |
| --- | --- |
| Prometheus | `/-/ready` answers and the build reports version `3.14.0` |
| Grafana | `/api/health` reports database `ok` and version `13.2.1` |
| Grafana datasource | The `Prometheus` datasource points at the Prometheus listener |
| Dashboard | `narwhal-router` loads with its `router` selector defaulting to All (regex `.*`) |
| Dashboard router queries | Every router-scoped query uses `instance=~"$router"`, with at least one present |
| `narwhal-router` job | Exactly one healthy target at `NARWHAL_ROUTER_URL` |
| `engines` job | One healthy target per fleet engine, labelled with its `iid` |
| Router readiness | `narwhal_router_ready` reports `1` |

| Startup stage | Deadline |
| --- | --- |
| Listener checks and each Docker inspection command | 10 seconds |
| Initial image pulls and container creation | 5 minutes |
| Prometheus and Grafana HTTP checks, including datasource and dashboard | 60 seconds after the containers start |
| Target health and `narwhal_router_ready` | 60 seconds after the HTTP checks pass |

## Verify Prometheus targets

Query the scrape state from the router host:

```bash
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

The response has one series for the router and one for each configured engine:

| Value | Scrape |
| :---: | --- |
| `1` | Succeeded |
| `0` | Failed |

Prometheus `/targets` shows the discovery state and scrape errors for each endpoint.

## Staged monitoring files

`make observe` stages its configuration under `runs/observability/mounts/`:

| Source | Staged path |
| --- | --- |
| `tools/observability/prometheus.yml` | `prometheus/prometheus.yml` |
| `tools/observability/prometheus-alerts.yml` | `prometheus/prometheus-alerts.yml` |
| `NARWHAL_FLEET` and `NARWHAL_ROUTER_URL` | `prometheus/targets/router.json`, `prometheus/targets/engines.json` |
| `tools/observability/grafana/provisioning/` | `grafana-provisioning/` |
| `tools/observability/grafana-narwhal.json` | `grafana-dashboards/narwhal.json` |

| Path | Mode |
| --- | :---: |
| `runs/observability/mounts/` | `0700` |
| Mounted subdirectories | `0755` |
| Mounted files | `0644` |

Compose bind-mounts the `prometheus`, `grafana-provisioning`, and `grafana-dashboards` subdirectories read-only.

Each `make observe` run regenerates the staged files and resets their permissions.

[![Next: Access dashboards and isolate listeners](https://img.shields.io/badge/next-Access%20dashboards%20and%20isolate%20listeners-0f766e)](02-Access.md)
