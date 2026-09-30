# Inspection endpoints

## `GET /v1/models`

Returns the configured model in OpenAI's list format:

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

`/health` reports whether the router process is alive. It always returns HTTP `200`. `status` gives the router's condition and is `ok`, `standby`, `fenced`, `maintenance`, or `degraded`.

The response also includes:

- `instances`: the configured fleet size.
- `available_instances`: the number of engines eligible for placement. Ejected, draining, and quarantined engines are excluded.

```json
{
  "status": "ok",
  "instances": 6,
  "available_instances": 6
}
```

`/health` omits per-engine state. Read it from Prometheus scrape targets and breaker state.

## `GET /ready`

Point load balancer health checks at `/ready`. It returns HTTP `200` when this router holds fleet control and is admitting new work. It returns `503`, which removes the router from rotation, when any of these hold:

- the router is a standby
- the router is fenced
- lease storage has failed
- a lifecycle hold is in place
- no eligible engines remain
- monitoring is degraded
- engine process identities are still being validated

A `503` also carries `Retry-After: 1`. The body, for both `200` and `503`, contains:

- `status`: `ready` or `not_ready`
- `control_ready`
- `epoch` and `holder`: the lease epoch and holder
- `reason`: empty when the router is ready

Monitoring counts as degraded after `controller.monitor_failure_limit` consecutive failed monitoring passes. `/ready` then reports `monitoring degraded: <stage> <class>`. One fully successful pass clears it. Standby routers count the same failures when deciding whether to take over.

If no engines are eligible and no other condition applies, `/ready` returns `503`. This example is from a router without a lease:

```json
{
  "status": "not_ready",
  "control_ready": true,
  "epoch": 0,
  "holder": "",
  "reason": "no available engines"
}
```

New completion requests then fail with error code `backend_unavailable` and `Retry-After: 1`.

During a [whole-wave hold](../operate/03-Restart-Engines.md#8-restart-an-engine-wave), lifecycle and control holds take precedence over engine state. `/health` reports `maintenance`, `/ready` gives the lifecycle reason, and completion requests get HTTP `503` with error code `standby`.

`control_ready` is independent of client admission. It stays true through engine loss or managed maintenance while the router holds its lease and monitoring is healthy, so standbys keep receiving current handoffs.

## `GET /metrics`

Serves Prometheus exposition format `0.0.4`. See [Metrics](../telemetry/03-Metrics-and-Control.md#read-live-state-from-prometheus) for the key series.
