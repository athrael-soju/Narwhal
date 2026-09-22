# Set up observability

The shipped Compose project runs Prometheus and Grafana on the router host. Prometheus scrapes Narwhal and vLLM, evaluates the Narwhal alert rules, and provides data for the provisioned **Narwhal Orchestrator** dashboard. :chatgpt-content-reference{index="0"}

## Requirements

- A running Narwhal router and its deployed fleet document.
- Linux, Docker Engine with the Compose plugin, Python 3.11 or newer, and curl.
- Network access from the router host to every engine metrics endpoint.

Run the collection commands from the deployed checkout in the [installed router-role shell](Deploy.md#open-installed-shells). The shell loads `.env.router` and activates the installed environment.

The Compose file pins Prometheus `3.14.0` and Grafana `13.2.1`.

## 1. Select the deployment

From the router-role shell:

```bash
export NARWHAL_FLEET=config/fleet.local.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
```

`NARWHAL_FLEET` defines the engine identities and metrics addresses. Engine URLs may contain [environment references](Configuration.md#node-urls-from-the-environment) resolved from variables already loaded in the shell.

`make observe` runs with the project Python environment created by `make setup`.

`NARWHAL_ROUTER_URL` is the router origin that Prometheus reaches from the router host network. Deployment automation may expose that origin directly or through a stable local tunnel.

## 2. Start collection and check the targets

```bash
make observe

curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

`make observe` generates router and engine discovery files from the selected deployment. Before changing the Compose project, it checks listener ownership and reports any listener owned outside this deployment. It then starts the pinned Prometheus and Grafana images.

Startup succeeds only after all of the following are true:

- one router target is healthy;
- every configured engine target is healthy;
- `narwhal_router_ready` is present;
- the expected Prometheus datasource is selected;
- the provisioned dashboard satisfies its query contract.

Docker and listener-owner queries have a 10-second deadline. Initial image pulls and container creation may run for up to five minutes. Prometheus `3.14.0` and Grafana `13.2.1` have 60 seconds to satisfy the full readiness contract.

After the listener checks, startup stages the Prometheus configuration and alert rules, Grafana provisioning files and dashboard, and generated targets under `runs/observability/mounts/`.

The parent directory has mode `0700`. Mounted subdirectories use `0755`; mounted files use `0644`, allowing the container service users to read them. Docker bind-mounts only these subdirectories, all read-only:

- `prometheus`
- `grafana-provisioning`
- `grafana-dashboards`

Permissions on the source checkout and private role environments are left unchanged.

Running `make observe` again regenerates the staged files and repairs their permissions before Compose updates the monitoring services.

If the default listeners belong to another deployment, assign isolated loopback addresses:

```bash
NARWHAL_GRAFANA_BIND_ADDRESS=127.0.0.2 \
NARWHAL_PROMETHEUS_LISTEN_ADDRESS=127.0.0.2:19090 \
make observe
```

Grafana derives its `Prometheus` datasource URL from the selected Prometheus listener. A wildcard bind resolves to loopback for the datasource and readiness checks; the listener retains the configured wildcard address.

The Prometheus query should return one router series plus one series for each configured engine. A value of `1` means the scrape succeeded; `0` means it failed. Prometheus `/targets` shows discovery state and the scrape error for each endpoint.

## 3. Open the dashboard

From the management checkout, with the workstation `.env` from step 1 loaded, forward the router's loopback listeners through the existing inventory access entry.

The [step 10 tunnel](Deploy.md#open-the-ssh-forwards) already contains these forwards. Reuse it if it is running. Otherwise, open a dashboard-only tunnel:

```bash
python3 tools/deploy_hosts.py tunnel --role router \
  --forward 13000:3000 --forward 19090:9090
```

Open:

- Grafana: `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator`
- Prometheus: `http://127.0.0.1:19090`

Keep the forwarding terminal open.

For the isolated listener configuration shown above, add `--remote-address 127.0.0.2` and forward Prometheus with `--forward 19090:19090`.

Grafana allows anonymous Viewer access through the local tunnel. Deployments that expose these services directly should use their existing ingress, authentication, and TLS policy.

The [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) documents router and engine scope selection.

For GPU telemetry, run the deployment's selected AMD or NVIDIA exporter to discover devices and collect sensor metrics, then expose those metrics through its hardware dashboard.

## Alert rules

Prometheus loads `tools/prometheus-alerts.yml`. Evaluated and firing alerts are available through the `ALERTS` series.

Production monitoring uses the same rules file and routes `severity="page"` and `severity="warn"` through the deployment's alert manager.

The rules expect these target labels:

- `job="narwhal-router"`
- `job="engines"`
- engine `iid`

The target generator supplies them.

Inspect the evaluated rules with:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```

## Troubleshooting

| Symptom                                                                         | Check                                                                                                                                                                                                                                                                          |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Browser connection error                                                        | Check the SSH tunnel, local port, and route to the router host.                                                                                                                                                                                                                |
| Startup reports an occupied listener                                            | Stop the reported process or socket unit, or choose isolated addresses with `NARWHAL_GRAFANA_BIND_ADDRESS` and `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`.                                                                                                                            |
| Startup reports a command deadline                                              | Check Docker daemon health, registry reachability, and `docker compose -f tools/observability/compose.yml ps`.                                                                                                                                                                 |
| Startup reports a container exit or readiness deadline                          | Check `docker compose -f tools/observability/compose.yml logs prometheus grafana` and confirm the pinned image versions.                                                                                                                                                       |
| Prometheus reports `config permission denied`, or Grafana returns dashboard 404 | Use the updated monitoring startup helper and Compose file together, then rerun `make observe` from the same deployed checkout. If readiness still fails, inspect the staged mount permissions and container logs. Retain the failure output in the private deployment record. |
| Startup reports a router target failure                                         | Check `NARWHAL_ROUTER_URL`, its route from the Prometheus host, and Prometheus `/targets`.                                                                                                                                                                                     |
| Engine charts lose a replica                                                    | Check generated targets, reachability of the engine metrics endpoint, and its `iid` label.                                                                                                                                                                                     |
| Grafana serves an older dashboard                                               | Rerun `make observe` to refresh the staged dashboard. The directory mount exposes the replacement to Grafana's provisioner. If the dashboard check still fails, inspect the provisioning log.                                                                                  |
| A rule fires for the wrong scope                                                | Check target relabelling for `job`, `instance`, and `iid`.                                                                                                                                                                                                                     |

Store deployment addresses and captured responses under `runs/`.

[Operate Narwhal](Operate.md#monitor-the-fleet) defines fleet health semantics and operator actions. The [deployment acceptance sequence](Deploy.md#10-validate-capacity-through-the-private-route) associates the verified targets and dashboard queries with the retained load evidence.
