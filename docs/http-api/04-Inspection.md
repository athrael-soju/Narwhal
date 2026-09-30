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

| Field                 | Meaning                                                                          |
| --------------------- | -------------------------------------------------------------------------------- |
| `status`              | Router state                                                                     |
| `instances`           | Configured fleet size                                                            |
| `available_instances` | Engines eligible for placement, excluding ejected, draining, and quarantined engines |

First matching `status` value, top to bottom:

| `status`      | Meaning                                                                                                   |
| ------------- | --------------------------------------------------------------------------------------------------------- |
| `ok`          | Router admits new client requests                                                                         |
| `fenced`      | Failover block set by a failed lease renewal, a blocked takeover, or an invalid active-router lease       |
| `maintenance` | Lifecycle hold, such as a whole-wave drain                                                                |
| `standby`     | Standby router                                                                                            |
| `degraded`    | Degraded engine monitoring, pending engine identity validation, or zero engines eligible for placement   |

```json
{
  "status": "ok",
  "instances": 6,
  "available_instances": 6
}
```

## `GET /ready`

Returns HTTP `200` when this router controls the fleet and admits new work.

Returns HTTP `503` with `Retry-After: 1` in these cases:

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
| `reason`        | Cause of `not_ready`, empty when `status` is `ready`                                                |

`monitoring.degraded` transitions:

| Transition | Condition                                                    |
| ---------- | ------------------------------------------------------------ |
| To `true`  | `controller.monitor_failure_limit` consecutive failed passes |
| To `false` | One fully successful pass                                    |

Degraded monitoring reports this `reason`:

```text
monitoring degraded: <stage> <class>
```

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

`reason` reports an active lifecycle or control hold before `no available engines`.

During a [whole-wave hold](../operate/03-Restart-Engines.md#8-restart-an-engine-wave):

- `/health` reports `maintenance`
- `/ready` reports the lifecycle reason
- completion requests return HTTP `503` with error code `standby`

## `GET /metrics`

Returns the [Narwhal metrics](../telemetry/03-Metrics-and-Control.md#read-live-state-from-prometheus) in Prometheus exposition format `0.0.4`.
