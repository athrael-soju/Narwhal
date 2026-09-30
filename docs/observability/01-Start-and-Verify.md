# Start and verify monitoring

## Prerequisites

- a running Narwhal router;
- the fleet configuration for that deployment;
- Linux;
- Docker Engine with the Compose plugin;
- Python 3.11 or newer;
- `curl`;
- network reachability from the router host to every engine metrics endpoint.

Run the commands from the deployed checkout, inside the [installed router-role shell](../deploy/02-Install.md#open-installed-role-shells), with `runs/deployment/.env.router` loaded and the `.venv` from `make setup` active.

## Configure the monitored deployment

| Variable | Value |
| --- | --- |
| `NARWHAL_FLEET` | The fleet configuration file, the source of the engine IDs and metrics URLs |
| `NARWHAL_ROUTER_URL` | The router origin that Prometheus reaches from the router host, directly or through a stable local tunnel |

Set both in the router-role shell:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

[Environment references](../configuration/05-Engine-Launch.md#14-engine-endpoints-generated-from-node-environments) inside the fleet file resolve against the variables the router-role shell has loaded.

## Start Prometheus and Grafana

Run:

```bash
make observe
```

The command starts Prometheus `3.14.0` and Grafana `13.2.1` and returns when the [readiness checks](#readiness-contract) pass.

When startup stops and names a process outside the monitoring Compose project that holds a monitoring listener, free the listener or choose another address.

### Readiness contract

Startup finishes when these checks pass:

- the `narwhal-router` job reports exactly one healthy target;
- every configured engine target is healthy;
- the `narwhal_router_ready` metric is present;
- Grafana has selected the expected Prometheus datasource;
- the `narwhal-router` dashboard is available with its `router` selector defaulting to All (regex `.*`);
- at least one dashboard query uses `instance=~"$router"`;
- every router-scoped query uses that regex form.

Startup deadlines:

| Stage | Deadline |
| --- | --- |
| Listener checks and Docker inspection commands | 10 seconds |
| Initial image pulls and container creation | 5 minutes |
| Prometheus and Grafana HTTP checks, including datasource and dashboard, after the containers start | 60 seconds |
| Target health and `narwhal_router_ready`, after the HTTP checks pass | 60 seconds |

## Verify Prometheus targets

Query the scrape state from the router host:

```bash
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

The response has one series for the router and one for each configured engine:

| Value | Scrape |
| --- | --- |
| `1` | Succeeded |
| `0` | Failed |

Prometheus `/targets` shows the discovery state and scrape errors for each endpoint.

## Staged monitoring files

`make observe` stages its configuration in subdirectories of `runs/observability/mounts/`:

| Path | Mode |
| --- | --- |
| `runs/observability/mounts/` | `0700` |
| Mounted subdirectories | `0755` |
| Mounted files | `0644` |

Compose bind-mounts these staged subdirectories read-only:

```text
prometheus
grafana-provisioning
grafana-dashboards
```

Running `make observe` again regenerates the staged files and repairs their permissions.

Next: [workstation dashboard route](02-Access.md).
