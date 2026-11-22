---
description: Sources and schemas of every artifact and telemetry stream a Narwhal router writes.
---

# Narwhal telemetry and artifacts

Narwhal writes these artifacts and telemetry streams:

| Artifact | Source | Schema | Reference |
| --- | --- | --- | --- |
| Request journal | `narwhal-serve --journal`, `journal.jsonl` beside `profiles.path` by default | `narwhal.journal` | [Request journal](telemetry/01-Journal.md), [Engine monitoring and failure diagnosis](telemetry/04-Failures.md) |
| Engine profile store | `profiles.path` | `narwhal.profiles` | [Engine profiles and capacity](telemetry/02-Profiles.md) |
| Prometheus metrics | Router `/metrics` | `narwhal.metrics` | [Metrics and role controller state](telemetry/03-Metrics-and-Control.md) |
| Live state | Router `/narwhal/state` | `narwhal.state` | [Metrics and role controller state](telemetry/03-Metrics-and-Control.md) |
| Contract manifest | `narwhal-check --print-contract-versions` | `narwhal.contract-manifest` | [Interface versions and compatibility](telemetry/05-Compatibility.md) |
