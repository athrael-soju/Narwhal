# Model, health, and metrics inspection

## `GET /v1/models`

Returns the configured served model in OpenAI list format.

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

Reports router process liveness.

Possible `status` values are:

- `ok`
- `standby`
- `fenced`
- `maintenance`
- `degraded`

The response also reports:

- configured fleet size as `instances`
- placement-eligible engine count as `available_instances`

Example:

```json
{
  "status": "ok",
  "instances": 6,
  "available_instances": 6
}
```

All `/health` states return HTTP `200`.

Narwhal counts engines eligible for placement after ejection, drain, and quarantine in `available_instances`.

Use Prometheus scrape targets and breaker state for engine-level liveness.

## `GET /ready`

Narwhal answers each `/ready` probe with HTTP `200` when this router holds fleet control and admits new work, allowing the load balancer to send it client requests. An HTTP `503` keeps the router out of rotation for one of these conditions:

- standby state
- fencing
- lease-storage failure
- lifecycle hold
- loss of eligible backends
- monitoring degradation

After `controller.monitor_failure_limit` consecutive failed monitoring passes, `/ready` reports:

```text
monitoring degraded: <stage> <class>
```

Standbys count the same failures toward takeover.

One completely successful monitoring pass clears degraded state.

When the eligible engine count reaches zero, `/ready` returns HTTP `503` with:

```json
{
  "reason": "no available engines"
}
```

New completion requests receive `backend_unavailable` with `Retry-After: 1`.

Lifecycle and control holds take precedence over backend state.

During a [whole-wave hold](../operate/03-Restart-Engines.md#8-restart-an-engine-wave):

- `/health` reports `maintenance`
- `/ready` reports the lifecycle reason
- completion requests return HTTP `503` with error code `standby`

`control_ready` stays true during backend loss or managed maintenance while lease ownership and monitoring remain healthy, allowing standbys to keep handoffs current.

## `GET /metrics`

Returns Prometheus exposition format `0.0.4`.

See [Metrics](../telemetry/03-Metrics-and-Control.md#read-live-state-from-prometheus) for the operational series.
