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

`/health` tells you whether the router process is alive. It always returns HTTP `200`; the router's condition is in `status`, which is `ok`, `standby`, `fenced`, `maintenance`, or `degraded`.

The response also includes `instances`, the configured fleet size, and `available_instances`, the number of engines currently eligible for placement once ejected, draining, and quarantined engines are left out:

```json
{
  "status": "ok",
  "instances": 6,
  "available_instances": 6
}
```

`/health` says nothing about individual engines. For that, check Prometheus scrape targets and breaker state.

## `GET /ready`

This is the endpoint to give your load balancer. It returns HTTP `200` when this router holds fleet control and is admitting new work. It returns `503`, taking the router out of rotation, when any of these apply:

- it is a standby
- it has been fenced
- lease storage has failed
- a lifecycle hold is in place
- no eligible backends remain
- monitoring is degraded
- engine process identities are still being validated

A `503` also carries `Retry-After: 1`. Either way, the body has `status` (`ready` or `not_ready`), `control_ready`, the lease `epoch` and `holder`, and a `reason` that's empty when the router is ready.

Monitoring counts as degraded after `controller.monitor_failure_limit` consecutive failed monitoring passes. `/ready` then reports `monitoring degraded: <stage> <class>`, and one fully successful pass clears it. Standby routers count the same failures toward takeover.

When no engines are eligible, and nothing else is holding the router back, `/ready` returns `503` with a body like this one from a router without a lease:

```json
{
  "status": "not_ready",
  "control_ready": true,
  "epoch": 0,
  "holder": "",
  "reason": "no available engines"
}
```

and new completion requests get `backend_unavailable` with `Retry-After: 1`.

Lifecycle and control holds take precedence over backend state. During a [whole-wave hold](../operate/03-Restart-Engines.md#8-restart-an-engine-wave), `/health` reports `maintenance`, `/ready` gives the lifecycle reason, and completion requests get HTTP `503` with error code `standby`.

`control_ready` is separate from client readiness. It stays true through backend loss or managed maintenance as long as the router still holds its lease and monitoring is healthy, so standbys keep getting current handoffs.

## `GET /metrics`

Serves Prometheus exposition format `0.0.4`. [Metrics](../telemetry/03-Metrics-and-Control.md#read-live-state-from-prometheus) describes the series worth watching.
