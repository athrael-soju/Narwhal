# Model, health, and metrics inspection

## Inspection API

### `GET /v1/models`

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

### `GET /health`

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

`available_instances` reflects the router's current:

- ejection state
- drain state
- quarantine state

Use Prometheus scrape targets and breaker state for engine-level liveness.

### `GET /ready`

Use `/ready` for load-balancer routing.

HTTP `200` means the router:

- owns control
- is admitting client requests

HTTP `503` may indicate:

- standby state
- fencing
- lease-storage failure
- lifecycle hold
- loss of eligible backends
- monitoring degradation

After:

```text
controller.monitor_failure_limit
```

consecutive failed monitoring passes, readiness reports:

```text
monitoring degraded: <stage> <class>
```

Standbys count the same failures toward takeover.

One completely successful monitoring pass clears degraded state.

If no engine remains eligible for placement:

```json
{
  "reason": "no available engines"
}
```

is reported with HTTP `503`.

New completion requests then receive:

- `backend_unavailable`
- `Retry-After: 1`

Lifecycle and control holds take precedence over backend state.

During a whole-wave hold:

- `/health` reports `maintenance`
- `/ready` reports the lifecycle reason
- completion requests return HTTP `503` with error code `standby`

`control_ready` may remain true during backend loss or managed maintenance when the router still:

- owns its lease
- has healthy monitoring

Standbys use that condition to keep handoffs current.

Client traffic should be sent only to routers whose `/ready` endpoint returns HTTP `200`.

### `GET /metrics`

Returns Prometheus exposition format `0.0.4`.

See [Metrics](../telemetry/03-Metrics-and-Control.md#read-live-state-from-prometheus) for the operational series.
