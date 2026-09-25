# Start and verify monitoring

## Prerequisites

Start monitoring with:

- a running Narwhal router;
- the fleet document used for that deployment;
- Linux;
- Docker Engine with the Compose plugin;
- Python 3.11 or newer;
- `curl`;
- network reachability from the router host to every engine metrics endpoint.

Run the commands from the deployed checkout inside the [installed router-role shell](../deploy/02-Install.md#open-installed-role-shells), which loads `runs/deployment/.env.router` and activates the installed Python environment. `make observe` uses the project environment created by `make setup`.

## Configure the monitored deployment

Set the fleet document and router origin:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

`make observe` reads `NARWHAL_FLEET` for engine IDs and metrics URLs, resolving [environment references](../configuration/05-Engine-Launch.md#14-engine-endpoints-generated-from-node-environments) with variables loaded by the router-role shell before it writes discovery targets. Set `NARWHAL_ROUTER_URL` to an origin Prometheus can reach from the router host, directly or through a stable local tunnel.

## Start Prometheus and Grafana

Run:

```bash
make observe
```

`make observe` starts monitoring in this order:

1. Generates Prometheus discovery for the router and configured engines.
2. Checks ownership of the monitoring listeners.
3. Reports the owner of an occupied listener so the deployment can use another address.
4. Stages Prometheus, Grafana, alerting, dashboard, and target files.
5. Starts the pinned Prometheus and Grafana images.
6. Verifies the complete monitoring readiness contract.

### Readiness contract

Startup completes after these checks pass:

- exactly one router target is healthy;
- every configured engine target is healthy;
- the `narwhal_router_ready` metric is present;
- Grafana has selected the expected Prometheus datasource;
- the `narwhal-router` dashboard is available and its `router` selector defaults to **All**, using the regex `.*`;
- at least one dashboard query uses `instance=~"$router"`, and none uses `instance="$router"`.

Listener ownership and Docker inspection commands have a 10-second deadline.

Initial image pulls and container creation may take up to five minutes.

After the containers start, Prometheus `3.14.0` and Grafana `13.2.1` have 60 seconds to satisfy the full readiness contract.

## Verify Prometheus targets

After startup, query the scrape state directly:

```bash
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

Prometheus returns:

- one series for the Narwhal router;
- one series for every configured engine.

For each series:

- `1` means the scrape succeeded;
- `0` means the scrape failed.

Prometheus `/targets` provides the corresponding discovery state and scrape error for each endpoint.

## Staged monitoring files

Narwhal stages monitoring configuration under `runs/observability/mounts/`, whose parent directory uses mode `0700`.

Files mounted into the containers are staged with permissions that allow the Prometheus and Grafana service users to read them:

- mounted subdirectories: `0755`;
- mounted files: `0644`.

Compose bind-mounts these staged subdirectories read-only:

```text
prometheus
grafana-provisioning
grafana-dashboards
```

Running `make observe` again regenerates the staged files and repairs their permissions before Compose updates the monitoring services.

Open the [workstation dashboard route](02-Access.md) after the scrape targets pass.
