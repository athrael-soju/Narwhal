# Narwhal telemetry and artifacts

Narwhal writes request outcomes and operational events to a journal, prices engine capacity from measured profiles, exports live router state through Prometheus, and versions machine-readable contracts for release compatibility.

- [Request journal](telemetry/01-Journal.md): reconcile terminal outcomes, retries, placement, and timing.
- [Engine profiles and capacity](telemetry/02-Profiles.md): validate the curves Narwhal uses for pricing.
- [Metrics and controller state](telemetry/03-Metrics-and-Control.md): inspect current and process-lifetime state.
- [Monitoring and engine failure diagnosis](telemetry/04-Failures.md): trace monitor degradation and breaker holds.
- [Interface versions and compatibility](telemetry/05-Compatibility.md): compare contract manifests before an upgrade.
