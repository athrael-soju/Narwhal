# Set up observability

The shipped Compose project runs Prometheus and Grafana on the router host. Prometheus scrapes Narwhal and vLLM, evaluates the Narwhal alert rules, and supplies the provisioned **Narwhal Orchestrator** dashboard.

## Requirements

- A running Narwhal router and its deployed fleet document.
- Linux, Docker Engine with the Compose plugin, Python 3.11 or newer, and curl.
- Network reachability from the router host to every engine metrics endpoint.

Run the collection commands from the deployed checkout in the [installed router-role shell](Deploy.md#open-the-installed-role-shells). That shell loads `.env.router` and activates the installed environment. The Compose file pins Prometheus `3.14.0` and Grafana `13.2.1`.

## 1. Select the deployment

In the router-role shell, select the deployed fleet and local router listener:

```bash
export NARWHAL_FLEET=config/fleet.local.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

`NARWHAL_FLEET` supplies every engine identity and metrics address. Engine URLs may use the [environment references](Configuration.md#node-urls-from-the-environment) loaded in this terminal. `make observe` uses the project's Python environment created by `make setup`. `NARWHAL_ROUTER_URL` supplies the router origin Prometheus reaches from its host network. Deployment automation can route that origin directly or through a stable local tunnel.

## 2. Start and verify collection

```bash
make observe
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

`make observe` writes router and engine discovery files from the selected deployment, binds each listener check to this Compose project, reports an external owner before Compose changes the project, and starts the pinned images. Startup then requires one healthy router target, every fleet engine target, `narwhal_router_ready`, the selected Prometheus datasource and the provisioned dashboard query contract. Docker and owner queries receive a 10-second deadline, while initial image retrieval and container creation receive five minutes. Prometheus 3.14.0 and Grafana 13.2.1 receive 60 seconds to satisfy the complete contract.

After listener ownership checks, startup copies the Prometheus configuration and alert rules, Grafana provisioning and dashboard, and generated targets into `runs/observability/mounts/`. This parent directory uses mode `0700`; each mounted subtree uses directories with mode `0755` and files with mode `0644`, so the containers' service users can read the bind mounts. Docker mounts only the `prometheus`, `grafana-provisioning` and `grafana-dashboards` subdirectories, each read-only. Source checkout permissions and private role environments retain their existing modes. Rerunning `make observe` refreshes these copies and repairs their permissions before Compose updates the monitoring services.

Select isolated loopback listeners when the defaults belong to another deployment:

```bash
NARWHAL_GRAFANA_BIND_ADDRESS=127.0.0.2 \
NARWHAL_PROMETHEUS_LISTEN_ADDRESS=127.0.0.2:19090 \
make observe
```

Grafana provisioning derives its `Prometheus` datasource URL from the selected Prometheus listener, keeping both services inside the isolated deployment.

The Prometheus query returns one router series and one series for each configured engine; value `1` records a successful scrape and value `0` records a failed scrape. Prometheus `/targets` gives the discovery state and scrape error for each endpoint.

## 3. Open the dashboard

From the management checkout with the step 1 workstation `.env` loaded, forward the loopback listeners through the inventory's existing router access entry. The [step 10 tunnel](Deploy.md#open-the-private-trial-route-from-the-workstation) already includes these forwards; reuse that running connection or open this dashboard-only connection:

```bash
python3 tools/deploy_hosts.py tunnel --role router \
  --forward 13000:3000 --forward 19090:9090
```

Open `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator`; Prometheus is available at `http://127.0.0.1:19090`. Keep the forwarding terminal open. For the isolated listener example above, add `--remote-address 127.0.0.2` and use `--forward 19090:19090` for Prometheus. Grafana grants anonymous Viewer access through this local tunnel. Deployments that publish the services directly apply their established ingress, authentication and TLS policy.

Use the [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) when selecting router and engine scopes. Run the deployment's selected AMD or NVIDIA exporter to discover GPUs and collect sensor telemetry, then present those metrics through its hardware dashboard.

## Alert rules

Prometheus loads `tools/prometheus-alerts.yml` and exposes firing rules through its `ALERTS` series. Production monitoring loads the same file and routes `severity="page"` and `severity="warn"` through the deployment's alert manager. The rules depend on `job="narwhal-router"`, `job="engines"` and the engine `iid` labels supplied by the target generator.

Inspect evaluated rules with:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The browser reports a connection error | The SSH tunnel, local port and router-host route. |
| Startup reports an occupied listener | Stop the reported process or socket unit, or select isolated addresses with `NARWHAL_GRAFANA_BIND_ADDRESS` and `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| Startup reports a command deadline | Docker daemon health, registry reachability and `docker compose -f tools/observability/compose.yml ps`. |
| Startup reports a container exit or readiness deadline | `docker compose -f tools/observability/compose.yml logs prometheus grafana` and the pinned image versions. |
| Prometheus reports config permission denied or Grafana returns dashboard 404 | Use the updated monitoring startup helper and Compose file together, then rerun `make observe` from the same deployed checkout. Inspect the staged mount permissions and container logs if readiness still fails; retain the failure output in the private deployment record. |
| Startup reports a router target failure | `NARWHAL_ROUTER_URL`, its route from the Prometheus host, and Prometheus `/targets`. |
| Engine charts lose a replica | Generated targets, engine metrics reachability and its `iid` label. |
| Grafana serves an older dashboard | Rerun `make observe` to refresh the staged dashboard; the directory mount exposes replacements to Grafana's provisioner. Inspect the provisioning log if the dashboard check still fails. |
| A rule fires for the wrong scope | Target relabelling for `job`, `instance` and `iid`. |

Keep deployment addresses and captured responses under `runs/`. [Operate Narwhal](Operate.md#monitor-the-fleet) defines health semantics and operator actions. The [deployment acceptance sequence](Deploy.md#10-validate-private-route-capacity) binds the verified targets and dashboard queries to the retained load evidence.
