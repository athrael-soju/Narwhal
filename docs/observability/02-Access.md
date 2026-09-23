# Access dashboards and isolate listeners

## Access the dashboard from a workstation

Grafana and Prometheus normally remain bound to the router side of the deployment. Access them through the deployment SSH path.

Run the tunnel command from the management checkout with the workstation `.env` loaded.

If the [Gate G tunnel](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation) is already running, reuse it. That tunnel already includes the monitoring forwards.

Otherwise, open a monitoring-only tunnel:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --forward 13000:3000 --forward 19090:9090
```

Keep that terminal running while using the monitoring interfaces.

Open:

- Grafana: `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator`
- Prometheus: `http://127.0.0.1:19090`

Grafana permits anonymous Viewer access through the local tunnel.

If a deployment exposes Prometheus or Grafana through another route, apply that deployment's existing ingress, authentication, and TLS policy.

The [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) documents router and engine scope selection.

## Isolate a second monitoring deployment

When another monitoring deployment shares the router host, assign distinct loopback listeners:

```bash
NARWHAL_GRAFANA_BIND_ADDRESS=127.0.0.2 \
NARWHAL_PROMETHEUS_LISTEN_ADDRESS=127.0.0.2:19090 \
make observe
```

Grafana derives its `Prometheus` datasource URL from the selected Prometheus listener.

When Prometheus is configured with a wildcard bind, Grafana datasource generation and readiness checks resolve that listener through loopback. The actual Prometheus listener keeps the configured wildcard address.

For the isolated listener example using `127.0.0.2`, add:

```text
--remote-address 127.0.0.2
```

and forward Prometheus as:

```text
--forward 19090:19090
```

Review [GPU telemetry, alerts, and recovery](03-Telemetry-and-Recovery.md) after confirming dashboard access.
