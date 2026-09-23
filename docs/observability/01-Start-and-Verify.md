# Start and verify monitoring

## Prerequisites

Before starting observability, confirm that you have:

- a running Narwhal router;
- the fleet document used for that deployment;
- Linux;
- Docker Engine with the Compose plugin;
- Python 3.11 or newer;
- `curl`;
- network reachability from the router host to every engine metrics endpoint.

Run monitoring commands from the deployed checkout inside the [installed router-role shell](../deploy/02-Install.md#open-installed-role-shells). That shell loads `runs/deployment/.env.router` and activates the installed Python environment.

`make observe` uses the project environment created by `make setup`.

## Configure the monitored deployment

Set the fleet document and router origin:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

`NARWHAL_FLEET` supplies the engine identities and metrics addresses used to generate Prometheus discovery targets.

Engine URLs may contain [environment references](../configuration/05-Engine-Launch.md#14-engine-endpoints-generated-from-node-environments). Those references are resolved from variables already loaded in the router-role shell.

`NARWHAL_ROUTER_URL` must identify the router origin that Prometheus can reach from the router host network. Deployment automation may expose that origin directly or through a stable local tunnel.

## Start Prometheus and Grafana

Run:

```bash
make observe
```

The command performs the monitoring deployment as a checked startup sequence. It:

1. Generates Prometheus discovery for the router and configured engines.
2. Checks ownership of the monitoring listeners.
3. Reports listeners owned outside the current deployment instead of replacing them.
4. Stages Prometheus, Grafana, alerting, dashboard, and target files.
5. Starts the pinned Prometheus and Grafana images.
6. Verifies the complete monitoring readiness contract.

### Readiness contract

`make observe` succeeds only when all of these conditions hold:

- exactly one router target is healthy;
- every configured engine target is healthy;
- the `narwhal_router_ready` metric is present;
- Grafana has selected the expected Prometheus datasource;
- the provisioned dashboard satisfies its query contract.

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

The result should contain:

- one series for the Narwhal router;
- one series for every configured engine.

For each series:

- `1` means the scrape succeeded;
- `0` means the scrape failed.

Prometheus `/targets` provides the corresponding discovery state and scrape error for each endpoint.

## Staged monitoring files

Monitoring configuration is staged under:

```text
runs/observability/mounts/
```

The parent directory uses mode `0700`.

Files mounted into the containers are staged with permissions that allow the Prometheus and Grafana service users to read them:

- mounted subdirectories: `0755`;
- mounted files: `0644`.

Docker bind-mounts only these staged subdirectories, all read-only:

```text
prometheus
grafana-provisioning
grafana-dashboards
```

Running `make observe` again regenerates the staged files and repairs their permissions before Compose updates the monitoring services.

Open the [workstation dashboard route](02-Access.md) after the scrape targets pass.
