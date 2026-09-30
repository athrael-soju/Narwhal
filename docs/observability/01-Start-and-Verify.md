# Start and verify monitoring

## Before you start

You need a running Narwhal router and the fleet document you deployed it with. The router host also needs:

- Linux
- Docker Engine with the Compose plugin
- Python 3.11 or newer
- `curl`
- Network access to every engine's metrics endpoint

Run every command on this page from the deployed checkout, inside the [router shell you set up during install](../deploy/02-Install.md#open-role-shells). That shell loads `runs/deployment/.env.router` and activates the installed Python environment. `make observe` itself runs in the environment that `make setup` created.

## Point monitoring at your deployment

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

`make observe` reads the engine IDs and metrics URLs from the fleet document. If the document uses [environment references](../configuration/05-Engine-Launch.md#14-engine-endpoints-generated-from-node-environments), they are resolved from the variables your router shell loaded.

Set `NARWHAL_ROUTER_URL` to an `http://host:port` origin with no path. Prometheus must reach it from the router host, directly or through a stable local tunnel.

## Start Prometheus and Grafana

```bash
make observe
```

This command:

- generates scrape targets for the router and each engine
- checks that the monitoring ports are free
- writes the Prometheus, Grafana, alerting, and dashboard configuration
- starts the pinned Prometheus and Grafana containers

If a port is in use, the command reports the owning process. Stop that process or [run this stack on a different address](02-Access.md#run-a-second-monitoring-stack-on-the-same-host).

Timeouts:

- Image pull and container creation: up to 5 minutes on the first run
- Prometheus and Grafana healthy: 60 seconds after the containers start
- Prometheus scrapes every target: a further 60 seconds
- Port and Docker checks: 10 seconds each

### What "ready" means

`make observe` reports success only when all of these hold:

- Prometheus is scraping exactly one router and every engine in the fleet, and all of them are up
- The router reports `narwhal_router_ready` as `1`, meaning it can admit traffic
- Grafana is using the Prometheus datasource that `make observe` set up
- The `narwhal-router` dashboard has loaded
- The dashboard's router filter defaults to **All**, and the panels match it as a regex (`instance=~"$router"`). **All** is the pattern `.*`. An exact match (`instance="$router"`) leaves every panel empty.

Against a standby router, or one whose `/ready` returns HTTP 503, `make observe` fails.

## Check the scrape targets

```bash
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

The query returns one series for the router and one per engine. `1` means the last scrape succeeded; `0` means it failed. The Prometheus `/targets` page shows the last error for each endpoint.

If you moved Prometheus to a different listener, use that address instead of `127.0.0.1:9090`.

## Where the config files live

`make observe` writes its configuration under `runs/observability/mounts/`. That directory is `0700`. The subdirectories mounted into the containers are `0755` with `0644` files, so the Prometheus and Grafana service users can read them. Compose mounts these three subdirectories read-only:

```text
prometheus
grafana-provisioning
grafana-dashboards
```

Every run of `make observe` rewrites these files and resets their permissions before updating the containers. If a container cannot read its configuration, rerun `make observe`.

When all targets are up, [open the dashboard from your workstation](02-Access.md).
