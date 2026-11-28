---
description: Open the Narwhal Grafana dashboard from a workstation and isolate a second monitoring stack.
---

# Accessing dashboards and isolating listeners

## Accessing the dashboard from a workstation

An open [deployment tunnel](../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) already forwards both monitoring ports.

To open a monitoring tunnel:

1. Open a terminal in the management checkout with the workstation `.env` and `config/deployment.env` loaded.
2. Open the tunnel:

    ```bash
    narwhal deploy tunnel --forward 13000:3000 --forward 19090:9090
    ```

3. Keep the tunnel terminal open.

The dashboard opens at `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator`, and Prometheus opens at `http://127.0.0.1:19090`. Grafana grants anonymous Viewer access through the local tunnel. To edit dashboards or settings, sign in as `admin` with the password in `runs/observability/grafana-admin-password`, a random password that the stack writes at mode 0600 on its first start, beside the stack's other state on the host that runs it. Each start resets Grafana's admin password to that file, so a stack from an earlier release no longer keeps the default `admin` password. Grafana reads a copy under the stack's private `runs/observability/mounts/` directory.

To add the fleet control service port to this tunnel, follow [Reaching the console through the tunnel](../operate/fleet-control/02-Open-the-Console.md#reaching-the-console-through-the-tunnel).

The dashboard's **Fleet control** link opens the fleet control console in a new tab. To set it up, follow [Using the console from Grafana](../operate/fleet-control/03-Grafana-Dashboard.md).

## Isolating a second monitoring stack

Grafana listens on `NARWHAL_GRAFANA_BIND_ADDRESS`, default `127.0.0.1`. Prometheus listens on `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`, default `127.0.0.1:9090`.

When another monitoring deployment shares the router host, run `make observe` with distinct loopback listeners:

```bash
NARWHAL_GRAFANA_BIND_ADDRESS=127.0.0.2 \
NARWHAL_PROMETHEUS_LISTEN_ADDRESS=127.0.0.2:19090 \
make observe
```

Grafana binds port `3000` and the image renderer binds port `8081` on the Grafana address. When that address is a wildcard (`0.0.0.0` or `::`), Grafana binds the wildcard and the image renderer binds loopback.

The Grafana `Prometheus` datasource and the readiness probes use the Prometheus address. When that address is a wildcard (`0.0.0.0` or `::`), Prometheus binds the wildcard and the datasource and probes use loopback.

Tunnel to the `127.0.0.2` listeners:

```bash
narwhal deploy tunnel --remote-address 127.0.0.2 \
  --forward 13000:3000 --forward 19090:19090
```

[Read the dashboard](05-Dashboard.md)
