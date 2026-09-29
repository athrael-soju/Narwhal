# Start and verify monitoring

Start Prometheus and Grafana on the router host with `make observe`, then check the router and engine scrape targets.

## Prerequisites

- a running Narwhal router;
- the fleet configuration for that deployment;
- Linux;
- Docker Engine with the Compose plugin;
- Python 3.11 or newer;
- `curl`;
- network reachability from the router host to every engine metrics endpoint.

Run the commands from the deployed checkout, inside the [installed router-role shell](../deploy/02-Install.md#open-installed-role-shells). That shell loads `runs/deployment/.env.router` and activates the `.venv` that `make setup` creates.

## Configure the monitored deployment

Two variables control what the monitoring stack discovers. `NARWHAL_FLEET` points at the fleet configuration file, which supplies the engine IDs and metrics URLs. `NARWHAL_ROUTER_URL` names the router origin that Prometheus reaches from the router host, either directly or through a stable local tunnel.

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

The command starts Prometheus `3.14.0` and Grafana `13.2.1`. It returns once the [readiness checks](#readiness-contract) pass.

Startup stops and names any process outside the monitoring Compose project that holds a monitoring listener. Free the listener or choose another address.

### Readiness contract

Startup finishes when these checks pass:

- the `narwhal-router` job reports exactly one healthy target;
- every configured engine target is healthy;
- the `narwhal_router_ready` metric is present;
- Grafana has selected the expected Prometheus datasource;
- the `narwhal-router` dashboard is available with its `router` selector defaulting to **All** (regex `.*`).

At least one dashboard query must use `instance=~"$router"`, and every router-scoped query must use that regex form.

Each stage of the startup sequence runs under a deadline:

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

The response returns one series for the router and one for each configured engine. A value of `1` means the scrape succeeded and `0` means it failed. Prometheus `/targets` shows the discovery state and any scrape error for each endpoint.

## Staged monitoring files

`make observe` stages its configuration in subdirectories of `runs/observability/mounts/` with permissions the Prometheus and Grafana service users can read.

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

Running `make observe` again regenerates the staged files and repairs their permissions. Next, open the [workstation dashboard route](02-Access.md).
