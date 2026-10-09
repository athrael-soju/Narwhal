---
description: Open the Narwhal Grafana dashboard from a workstation and isolate a second monitoring stack.
---

# Accessing dashboards and isolating listeners

## Accessing the dashboard from a workstation

An open [Gate G tunnel](../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) already forwards both monitoring ports.

To open a monitoring tunnel:

1. Open a terminal in the management checkout with the workstation `.env` loaded.
2. Open the tunnel:

    ```bash
    python3 tools/deployment/deploy_hosts.py tunnel --role router \
      --forward 13000:3000 --forward 19090:9090
    ```

3. Keep the tunnel terminal open.

The dashboard opens at `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator`, and Prometheus opens at `http://127.0.0.1:19090`. Grafana grants anonymous Viewer access through the local tunnel.

Grafana also allows its panels to be framed by other pages. The fleet control console uses this to show dashboard panels beside its controls. [Reaching the console through the tunnel](../operate/06-Controlling-the-Fleet.md#reaching-the-console-through-the-tunnel) adds the control service port to this tunnel.

The **Fleet control** dashboard opens at `http://127.0.0.1:13000/d/narwhal-fleet-control/fleet-control` and shows each fleet control console view in its own panel beside dashboard charts. Grafana renders Text panel HTML without sanitizing it, so the panels can hold the frames that load the console. [Using the console from Grafana](../operate/06-Controlling-the-Fleet.md#using-the-console-from-grafana) describes the setup and the effect of that setting.

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
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --remote-address 127.0.0.2 \
  --forward 13000:3000 --forward 19090:19090
```

[![Next: Reading the dashboard](https://img.shields.io/badge/next-Reading%20the%20dashboard-0f766e)](05-Dashboard.md)
