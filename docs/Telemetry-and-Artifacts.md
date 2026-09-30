# Narwhal telemetry and artifacts

These pages cover what Narwhal records and exports, and how to use that data to diagnose failures.

- [Request journal](telemetry/01-Journal.md): one row per request, with its outcome, retries, placement, and timing, plus router events.
- [Engine profiles and capacity](telemetry/02-Profiles.md): the measured cost curves behind capacity pricing, and how they are validated.
- [Metrics and controller state](telemetry/03-Metrics-and-Control.md): the Prometheus metrics, and which of them survive a router restart.
- [Monitoring and engine failure diagnosis](telemetry/04-Failures.md): why monitoring degrades, and how engine breakers trip and clear.
- [Interface versions and compatibility](telemetry/05-Compatibility.md): schema versions, and what to compare before an upgrade.
