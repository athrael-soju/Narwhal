# Start and verify monitoring

## Prerequisites

- a running Narwhal router
- the fleet configuration for that deployment
- Linux
- Docker Engine with the Compose plugin
- Python 3.11 or newer
- `curl`
- network reachability from the router host to every engine metrics endpoint

Shell setup for every command on this page:

1. Work in the deployed checkout.
2. Open the [installed router-role shell](../deploy/02-Install.md#open-installed-role-shells).
3. Load `runs/deployment/.env.router`.
4. Activate the `.venv` from `make setup`.

## Configure the monitored deployment

| Variable | Value |
| --- | --- |
| `NARWHAL_FLEET` | The fleet configuration file supplying the engine IDs and metrics URLs |
| `NARWHAL_ROUTER_URL` | The router origin Prometheus scrapes, direct or through a stable local tunnel |

Set both in the router-role shell:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

[Environment references](../configuration/05-Engine-Launch.md#14-engine-endpoints-generated-from-node-environments) in the fleet file use the variables loaded in the router-role shell.

## Start Prometheus and Grafana

Run:

```bash
make observe
```

The command returns after Prometheus `3.14.0` and Grafana `13.2.1` pass the [readiness checks](#readiness-contract).

If startup stops on a listener held by a process outside the monitoring Compose project, free the listener or choose another address.

### Readiness contract

Readiness checks:

- the `narwhal-router` job reports exactly one healthy target
- every configured engine target is healthy
- the `narwhal_router_ready` metric is present
- Grafana has selected the expected Prometheus datasource
- the `narwhal-router` dashboard is available with its `router` selector defaulting to All (regex `.*`)
- at least one dashboard query uses `instance=~"$router"`
- every router-scoped query uses that regex form

Startup deadlines:

- Listener checks and Docker inspection commands finish within 10 seconds.
- Initial image pulls and container creation finish within 5 minutes.
- Prometheus and Grafana HTTP checks, including datasource and dashboard, finish within 60 seconds after the containers start.
- Target health and `narwhal_router_ready` pass within 60 seconds after the HTTP checks pass.

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

Continue with the [workstation dashboard route](02-Access.md).
