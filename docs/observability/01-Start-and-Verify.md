# Start and verify monitoring

## Before you start

You need a running Narwhal router and the fleet document you deployed it with. The router host also needs:

- Linux
- Docker Engine with the Compose plugin
- Python 3.11 or newer
- `curl`
- network access to every engine's metrics endpoint

Run everything below from the deployed checkout, inside the [router shell you set up during install](../deploy/02-Install.md#open-role-shells). That shell loads `runs/deployment/.env.router` and activates the installed Python environment. `make observe` itself runs in the environment that `make setup` created.

## Point monitoring at your deployment

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

`make observe` gets the engine IDs and metrics URLs from the fleet document. If the document uses [environment references](../configuration/05-Engine-Launch.md#14-engine-endpoints-generated-from-node-environments), they're filled in from the variables your router shell loaded.

Set `NARWHAL_ROUTER_URL` to a plain `http://host:port` origin with no path. Prometheus has to be able to reach it from the router host. A direct address works, and so does a stable local tunnel.

## Start Prometheus and Grafana

```bash
make observe
```

This generates scrape targets for the router and each engine, checks that the monitoring ports are free, writes the Prometheus, Grafana, alerting, and dashboard config, and starts the pinned Prometheus and Grafana containers. If a port is already in use, it tells you which process owns it so you can stop that process or [run this stack on a different address](02-Access.md#run-a-second-monitoring-stack-on-the-same-host).

The first run can take up to five minutes while Docker pulls images and creates the containers. After that, Prometheus and Grafana get 60 seconds to come up healthy, and then Prometheus gets another 60 seconds to scrape every target. The port and Docker checks each time out after 10 seconds.

### What "ready" means

`make observe` only reports success once:

- Prometheus is scraping exactly one router and every engine in the fleet, and all of them are up
- the router reports `narwhal_router_ready` as `1`, meaning it can admit traffic (so `make observe` fails against a standby router, or one whose `/ready` returns HTTP 503)
- Grafana is using the Prometheus datasource that `make observe` set up
- the `narwhal-router` dashboard has loaded

It also checks that the dashboard's router filter defaults to **All** and that the panels match it as a regex (`instance=~"$router"`). **All** is the pattern `.*`, so an exact match (`instance="$router"`) would leave every panel empty.

## Check the scrape targets

```bash
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

You should get one series for the router and one for each engine. A value of `1` means the last scrape worked and `0` means it failed. To see why a scrape failed, open the Prometheus `/targets` page, which lists each endpoint with its last error.

If you moved Prometheus to a different listener, use that address instead of `127.0.0.1:9090`.

## Where the config files live

`make observe` writes its config under `runs/observability/mounts/`. That directory is private (`0700`), but the subdirectories mounted into the containers are `0755` and their files `0644`, so the Prometheus and Grafana service users can read them. Compose mounts these read-only:

```text
prometheus
grafana-provisioning
grafana-dashboards
```

Every run of `make observe` rewrites these files and resets their permissions before updating the containers. If a container can't read its config, rerunning it is the first thing to try.

When the targets look healthy, [open the dashboard from your workstation](02-Access.md).
