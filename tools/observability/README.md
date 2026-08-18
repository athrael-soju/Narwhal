# Observability stack

Prometheus and Grafana as compose, for the router node. Grafana is provisioned with the Prometheus datasource and the board from `tools/grafana-narwhal.json`. Prometheus scrapes the router on `localhost:8011` and the engines listed in `runs/observability/targets/engines.json`, and evaluates the alert rules in `tools/prometheus-alerts.yml`.

Write the engine targets first:

    python3 tools/observability/make_targets.py config/fleet.local.json

Point the script at the fleet config with real engine addresses. A preset's `fleet.json` contains placeholder URLs, so a target generated from one scrapes a host that never answers. `make observe` from the repository root starts the stack. [docs/Deploy.md](../../docs/Deploy.md) §7 covers setup, and [docs/Observability.md](../../docs/Observability.md) is the reference for the metrics, alerts, and panels.
