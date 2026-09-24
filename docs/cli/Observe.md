# `narwhal-observe`

Start the provisioned Narwhal Orchestrator monitoring stack from an installed wheel:

```bash
narwhal-observe --fleet runs/deployment/fleet.json --router-url http://127.0.0.1:8000
```

The command stages the packaged Prometheus rules, Grafana provisioning and dashboard under `~/.local/share/narwhal/observability/mounts/` by default. Set `NARWHAL_OBSERVABILITY_DATA_DIR` to another absolute persistent path when needed. It uses the same `narwhal-observability` Compose project and the same listener variables as `make observe`. Docker Engine with Compose is required; the wheel supplies the configuration files.

Startup checks listener ownership, router and engine scrapes, router readiness, Grafana's datasource and dashboard provisioning. After a routed request, the Dev verification flow checks that both `narwhal_served_total` and vLLM prompt-token counters advance, with cache telemetry present for every configured engine.

The repository's `make observe` remains a compatibility entry point to this installed implementation. [Set up observability](../Observability.md) documents listeners, access and recovery.

| Option | Default | Purpose |
| --- | --- | --- |
| `--fleet` | `NARWHAL_FLEET` | Select the deployed fleet document. |
| `--router-url` | `NARWHAL_ROUTER_URL` | Select the router origin scraped by Prometheus. |
| `--ready-timeout` | `60.0` | Seconds allowed for versioned health and scrape readiness. |
