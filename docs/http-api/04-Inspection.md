---
description: Inspect a Narwhal router through GET /v1/models, /health, /ready, /metrics and /narwhal/targets, and read fleet state from the router that holds the lease.
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
| `instances`           | Engines in the membership snapshot |
| `available_instances` | Engines eligible for placement |

The earliest matching row sets `status`:

| `status`      | Meaning                                                                                                |
| ------------- | ------------------------------------------------------------------------------------------------------ |
| `ok`          | Router admits new client requests                                                                      |
| `fenced`      | Failover block set by a failed lease renewal, a blocked takeover, an invalid active-router lease, or shutdown of the lease holder |
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
| Fencing, lease-storage failure or shutdown of the lease holder | Failover block, such as `lease renewal failed` or `shutting down` |
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

During a [whole-wave hold](../operate/03-Restart-Engines.md#restart-an-engine-wave), completion requests return HTTP `503` with error code `standby`.

## `GET /metrics`

Returns the [Narwhal metrics](../telemetry/03-Metrics-and-Control.md#prometheus-metrics) in Prometheus exposition format `0.0.4`.

## `GET /narwhal/targets`

Returns one Prometheus [HTTP service discovery](https://prometheus.io/docs/prometheus/latest/http_sd/) target group per engine in the [membership snapshot](08-Engine-Membership.md), so the list changes when an engine joins or leaves. The target is the host and port of the engine's `url`. The labels carry the engine's `iid`, the fleet's `engine.backend`, the scheme of the `url`, and its path followed by `/metrics`:

```json
[
  {
    "targets": ["node0:8000"],
    "labels": {
      "iid": "n0",
      "backend": "vllm",
      "__scheme__": "http",
      "__metrics_path__": "/metrics"
    }
  }
]
```

The `engines` job of the [monitoring stack](../observability/01-Start-and-Verify.md) reads this route every 15 seconds.

A router that does not control the fleet returns the [fleet-state refusal](#fleet-state-reads-on-a-router-without-the-lease). A standby holds the fleet file's membership until it takes over, so its list would drop engines that joined and keep engines that left. When a refresh fails, Prometheus keeps using the targets of its last successful read and counts the failure in `prometheus_sd_refresh_failures_total`. A standby in the discovery path therefore leaves the engine targets in place, and the targets follow the fleet again on the next read that reaches the active router.

## Fleet-state reads on a router without the lease

`GET /narwhal/engines`, `GET /narwhal/lifecycle`, `GET /narwhal/state`, `GET /narwhal/handoff` and `GET /narwhal/targets` describe the fleet that the lease holder serves. A router that does not control the fleet answers each of them with HTTP `503` and `Retry-After: 1`:

```json
{
  "error": {
    "message": "this router does not control the fleet",
    "type": "standby",
    "code": "standby",
    "reason": "shadowing"
  }
}
```

`reason` names the cause as `/ready` does: `shadowing` on a standby, the failover block on a fenced router, such as `lease renewal failed`, `shutting down` once the lease holder releases its lease at shutdown, or `lease expired`.

A router that holds the lease answers these routes even while its `/ready` returns HTTP `503` for another cause, such as degraded engine monitoring. `/health`, `/ready`, `/metrics` and `/v1/models` answer on every router.

The [shipped HAProxy configuration](../operate/01-Start-Routers.md#configuring-the-client-path) retries an HTTP `503` while both routers are in rotation, so a read through it reaches the active router or returns this refusal.
