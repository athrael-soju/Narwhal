---
description: Open the Narwhal Grafana dashboard from a workstation and isolate a second monitoring stack.
---

# Access dashboards and isolate listeners

## Access the dashboard from a workstation

An open [Gate G tunnel](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation) already forwards both monitoring ports.

To open a monitoring tunnel:

1. Open a terminal in the management checkout with the workstation `.env` loaded.
2. Open the tunnel:

    ```bash
    python3 tools/deployment/deploy_hosts.py tunnel --role router \
      --forward 13000:3000 --forward 19090:9090
    ```

3. Keep the tunnel terminal open.

| Interface | URL |
| --- | --- |
| Grafana | `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator` |
| Prometheus | `http://127.0.0.1:19090` |

Grafana grants anonymous Viewer access through the local tunnel.

The [dashboard selectors](05-Dashboard.md#selectors) switch between router and engine scope.

## Isolate a second monitoring stack

| Variable | Default |
| --- | --- |
| `NARWHAL_GRAFANA_BIND_ADDRESS` | `127.0.0.1` |
| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | `127.0.0.1:9090` |

When another monitoring deployment shares the router host, run `make observe` with distinct loopback listeners:

```bash
NARWHAL_GRAFANA_BIND_ADDRESS=127.0.0.2 \
NARWHAL_PROMETHEUS_LISTEN_ADDRESS=127.0.0.2:19090 \
make observe
```

| `NARWHAL_PROMETHEUS_LISTEN_ADDRESS` | Prometheus binds | Grafana `Prometheus` datasource and readiness probes use |
| --- | --- | --- |
| Specific address | That address | That address |
| Wildcard (`0.0.0.0` or `::`) | The wildcard | Loopback |

| `NARWHAL_GRAFANA_BIND_ADDRESS` | Grafana binds | Image renderer binds |
| --- | --- | --- |
| Specific address | That address, port `3000` | That address, port `8081` |
| Wildcard (`0.0.0.0` or `::`) | The wildcard, port `3000` | Loopback, port `8081` |

Tunnel to the `127.0.0.2` listeners:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --remote-address 127.0.0.2 \
  --forward 13000:3000 --forward 19090:19090
```

[![Next: Read the dashboard](https://img.shields.io/badge/next-Read%20the%20dashboard-0f766e)](05-Dashboard.md)
