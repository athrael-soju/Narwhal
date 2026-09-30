# Open the dashboards

Grafana and Prometheus listen on the router host's loopback address by default. Reach them from your workstation through an SSH tunnel.

## From your workstation

If the tunnel from [Serve and Measure](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation) already forwards the monitoring ports, reuse it. Otherwise open one from your management checkout with the workstation `.env` loaded:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --forward 13000:3000 --forward 19090:9090
```

With the tunnel running, open:

- Grafana: `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator`
- Prometheus: `http://127.0.0.1:19090`

Grafana allows anonymous Viewer access, so the tunnel requires no login. Prometheus has no authentication. Binding either service to a non-loopback address exposes it to anyone who can reach that address. Put any other exposure behind ingress with authentication and TLS.

To select the routers and engines the dashboard shows, see the [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard).

## Run a second monitoring stack on the same host

If another monitoring stack already runs on the router host, give this one its own loopback address:

```bash
NARWHAL_GRAFANA_BIND_ADDRESS=127.0.0.2 \
NARWHAL_PROMETHEUS_LISTEN_ADDRESS=127.0.0.2:19090 \
make observe
```

Grafana's Prometheus datasource follows the configured listener. If you bind Prometheus to a wildcard address such as `0.0.0.0`, Grafana and the readiness checks connect to it over loopback.

Point the tunnel at the new address. Prometheus now listens on 19090, so the remote side of its forward changes from 9090 to 19090:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --remote-address 127.0.0.2 \
  --forward 13000:3000 --forward 19090:19090
```
