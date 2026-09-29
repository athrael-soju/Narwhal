# Model, health, and metrics inspection

## `GET /v1/models`

Returns the configured model in OpenAI list format:

```json
{
  "object": "list",
  "data": [
    {
      "id": "example/model",
      "object": "model",
      "owned_by": "narwhal"
    }
  ]
}
```

## `GET /health`

Reports router process liveness. It returns HTTP `200` in every state.

| Field                 | Meaning                                                                          |
| --------------------- | -------------------------------------------------------------------------------- |
| `status`              | Router state                                                                     |
| `instances`           | Configured fleet size                                                            |
| `available_instances` | Engines eligible for placement; excludes ejected, draining, and quarantined engines |

`status` takes the first value that matches, checked in order:

| `status`      | Meaning                                                                                                   |
| ------------- | --------------------------------------------------------------------------------------------------------- |
| `ok`          | Router admits new client requests                                                                         |
| `fenced`      | Failover block set by a failed lease renewal, a blocked takeover, or an invalid active-router lease       |
| `maintenance` | Lifecycle hold, such as a whole-wave drain                                                                |
| `standby`     | Standby router                                                                                            |
| `degraded`    | Degraded engine monitoring, pending engine identity validation, or zero engines eligible for placement   |

Example:

```json
{
  "status": "ok",
  "instances": 6,
  "available_instances": 6
}
```

Engine-level liveness comes from Prometheus scrape targets and breaker state.

## `GET /ready`

Returns HTTP `200` when this router controls the fleet and admits new work.

It returns HTTP `503` with `Retry-After: 1` in any of these cases:

- standby state
- fencing
- lease-storage failure
- lifecycle hold
- pending engine identity validation
- zero engines eligible for placement
- engine-monitoring degradation

| Field           | Meaning                                                                                             |
| --------------- | --------------------------------------------------------------------------------------------------- |
| `status`        | `ready` or `not_ready`                                                                              |
| `control_ready` | `true` when this router controls the fleet and `/narwhal/state` reports `monitoring.degraded` as `false` |
| `epoch`         | Lease epoch                                                                                         |
| `holder`        | Lease-holder token                                                                                  |
| `reason`        | Cause of `not_ready`; empty when `status` is `ready`                                                |

Engine monitoring degrades after `controller.monitor_failure_limit` consecutive failed passes. One fully successful pass clears it.

A degraded monitoring state reports this `reason`:

```text
monitoring degraded: <stage> <class>
```

A standby router polls the active router's `/ready`. Each poll that returns `control_ready: false` counts as a missed takeover probe.

When no engine is eligible, the response is:

```json
{
  "status": "not_ready",
  "control_ready": true,
  "epoch": 0,
  "holder": "",
  "reason": "no available engines"
}
```

`reason` shows an active lifecycle or control hold ahead of `no available engines`.

During a [whole-wave hold](../operate/03-Restart-Engines.md#8-restart-an-engine-wave):

- `/health` reports `maintenance`
- `/ready` reports the lifecycle reason
- completion requests return HTTP `503` with error code `standby`

`control_ready` stays `true` through backend loss or managed maintenance as long as the router holds the lease and monitoring is healthy.

## `GET /metrics`

Returns Prometheus exposition format `0.0.4`.

Metric names and labels are listed in [Metrics](../telemetry/03-Metrics-and-Control.md#read-live-state-from-prometheus).
