---
description: Inspect a Narwhal router through GET /v1/models, /health, /ready and /metrics.
---

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

Returns HTTP `200` in every router state.

| Field                 | Meaning                        |
| --------------------- | ------------------------------ |
| `status`              | Router state                   |
| `instances`           | Configured fleet size          |
| `available_instances` | Engines eligible for placement |

The earliest matching row sets `status`:

| `status`      | Meaning                                                                                                |
| ------------- | ------------------------------------------------------------------------------------------------------ |
| `ok`          | Router admits new client requests                                                                      |
| `fenced`      | Failover block set by a failed lease renewal, a blocked takeover, or an invalid active-router lease    |
| `maintenance` | Whole-wave drain, recovery, or restart hold                                                            |
| `standby`     | Standby router                                                                                         |
| `degraded`    | Degraded engine monitoring, pending engine identity validation, or zero engines eligible for placement |

```json
{
  "status": "ok",
  "instances": 6,
  "available_instances": 6
}
```

## `GET /ready`

Returns HTTP `200` when this router controls the fleet and admits new work.

Returns HTTP `503` with `Retry-After: 1` otherwise.

| Field           | Meaning                                                                                                  |
| --------------- | -------------------------------------------------------------------------------------------------------- |
| `status`        | `ready` or `not_ready`                                                                                   |
| `control_ready` | `true` when this router controls the fleet and `/narwhal/state` reports `monitoring.degraded` as `false` |
| `epoch`         | Lease epoch                                                                                              |
| `holder`        | Lease-holder token                                                                                       |
| `reason`        | Cause of `not_ready`, empty when `status` is `ready`                                                     |

The earliest matching row sets `reason`:

| Cause                               | `reason`                                                                                       |
| ----------------------------------- | ---------------------------------------------------------------------------------------------- |
| Fencing or lease-storage failure    | Failover block, such as `lease renewal failed`                                                 |
| Whole-wave hold                     | `whole-wave drain <id>`, `whole-wave recovery <id>`, or `whole-wave restart required: <cause>` |
| Engine-monitoring degradation       | `monitoring degraded: <stage> <class>`                                                         |
| Standby state                       | `shadowing`                                                                                    |
| Expired lease                       | `lease expired`                                                                                |
| Pending engine identity validation  | `engine identity validation pending`                                                           |
| Zero engines eligible for placement | `no available engines`                                                                         |

`monitoring.degraded` turns `true` after `controller.monitor_failure_limit` consecutive failed passes. One fully successful pass sets it back to `false`.

A standby router counts each `control_ready: false` response from the active router's `/ready` as a missed takeover probe.

With zero eligible engines, the response is:

```json
{
  "status": "not_ready",
  "control_ready": true,
  "epoch": 0,
  "holder": "",
  "reason": "no available engines"
}
```

During a [whole-wave hold](../operate/03-Restart-Engines.md#restart-an-engine-wave):

- `/health` reports `maintenance`
- `/ready` reports the lifecycle reason
- completion requests return HTTP `503` with error code `standby`

## `GET /metrics`

Returns the [Narwhal metrics](../telemetry/03-Metrics-and-Control.md#prometheus-metrics) in Prometheus exposition format `0.0.4`.
