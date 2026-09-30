# Access dashboards and isolate listeners

## Access the dashboard from a workstation

1. Skip the tunnel command when the [Gate G tunnel](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation) already forwards monitoring.
2. From the management checkout with the workstation `.env` loaded, open a tunnel:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --forward 13000:3000 --forward 19090:9090
```

3. Keep the tunnel terminal open.

| Interface | URL |
| --- | --- |
| Grafana | `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator` |
| Prometheus | `http://127.0.0.1:19090` |

- Grafana permits anonymous Viewer access through the local tunnel.
- The [dashboard selectors](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) switch between router and engine scope.

## Isolate a second monitoring stack

When another monitoring deployment shares the router host, run `make observe` with distinct loopback listeners:

```bash
NARWHAL_GRAFANA_BIND_ADDRESS=127.0.0.2 \
NARWHAL_PROMETHEUS_LISTEN_ADDRESS=127.0.0.2:19090 \
make observe
```

| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | Prometheus binds | Grafana `Prometheus` datasource and readiness probes use |
| --- | --- | --- |
| Specific address | That address | That address |
| Wildcard | The wildcard | Loopback |

Tunnel to the `127.0.0.2` listeners:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --remote-address 127.0.0.2 \
  --forward 13000:3000 --forward 19090:19090
```

Next: [GPU telemetry, alerts, and recovery](03-Telemetry-and-Recovery.md).
