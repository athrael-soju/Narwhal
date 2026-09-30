---
description: Sources and schemas of every artifact and telemetry stream a Narwhal router writes.
---

# Narwhal telemetry and artifacts

| Artifact | Source | Schema |
| --- | --- | --- |
| Request journal | `narwhal-serve --journal`, `journal.jsonl` beside `profiles.path` by default | `narwhal.journal` |
| Engine profile store | `profiles.path` | `narwhal.profiles` |
| Prometheus metrics | Router `/metrics` | `narwhal.metrics` |
| Live state | Router `/narwhal/state` | `narwhal.state` |
| Contract manifest | `narwhal-check --print-contract-versions` | `narwhal.contract-manifest` |

## Telemetry references

<div class="grid cards" markdown>

-   [Request journal](telemetry/01-Journal.md)

    ---

    Reconcile terminal outcomes, retries, placement, and timing.

-   [Engine profiles and capacity](telemetry/02-Profiles.md)

    ---

    Validate the per-engine cost model used for capacity pricing.

-   [Metrics and role controller state](telemetry/03-Metrics-and-Control.md)

    ---

    Inspect live state and process-lifetime counters.

-   [Engine monitoring and failure diagnosis](telemetry/04-Failures.md)

    ---

    Trace monitoring degradation, breaker streaks, verification probes, and ejection.

-   [Interface versions and compatibility](telemetry/05-Compatibility.md)

    ---

    Compare contract manifests before an upgrade.

</div>
