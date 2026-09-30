# Open the dashboards

By default, Grafana and Prometheus listen only on the router host's loopback address. To use them from your workstation, reach them over SSH.

## From your workstation

If the tunnel from [Serve and Measure](../deploy/07-Serve-and-Measure.md#tunnel-router-prometheus-and-grafana-to-the-workstation) is already running with the monitoring ports forwarded, you can use it. Otherwise, open one just for monitoring. Run this from your management checkout with the workstation `.env` loaded:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --forward 13000:3000 --forward 19090:9090
```

Leave it running, then open:

- Grafana: `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator`
- Prometheus: `http://127.0.0.1:19090`

Grafana allows anonymous Viewer access, so you won't need to log in through the tunnel. If you expose Grafana or Prometheus any other way, put it behind the same ingress, authentication, and TLS setup as the rest of the deployment.

The [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) explains how to choose which routers and engines the dashboard shows.

## Run a second monitoring stack on the same host

If another monitoring stack already runs on the router host, give this one its own loopback address:

```bash
NARWHAL_GRAFANA_BIND_ADDRESS=127.0.0.2 \
NARWHAL_PROMETHEUS_LISTEN_ADDRESS=127.0.0.2:19090 \
make observe
```

You don't need to update Grafana's Prometheus datasource by hand; it follows whatever listener you set. If you bind Prometheus to a wildcard address, Grafana and the readiness checks connect to it over loopback.

Then point your tunnel at the new address. Prometheus is on port 19090 in this example, so the second forward changes too:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --remote-address 127.0.0.2 \
  --forward 13000:3000 --forward 19090:19090
```
