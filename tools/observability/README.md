# Observability stack

Prometheus and Grafana as compose, provisioned with the standard board
and the shipped alert rules. Before `make observe`, write the engine
scrape targets: `python3 tools/observability/make_targets.py <fleet
config>`. Setup: [docs/Deploy.md](../../docs/Deploy.md)
§7. What the metrics, alerts and panels mean:
[docs/Observability.md](../../docs/Observability.md).
